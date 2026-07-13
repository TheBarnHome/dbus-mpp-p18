#!/usr/bin/env python3
"""Hotplug manager for P18 MPP Solar hidraw devices."""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import dbus
from gi.repository import GLib
from inverterd import Client, Format

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "velib_python"))
from vedbus import VeDbusItemImport, VeDbusService

from mppsolar_common import (
    INSTALL_DIR,
    POLL_DEFAULT,
    allocate_instance,
    backend_lock,
    clamp_poll_interval,
    count_parallel_members,
    load_manifest,
    protocol_id_from_response,
    save_manifest,
    serial_from_response,
    service_suffix,
    settings_prefix,
)


VERSION = "0.4"
SCAN_INTERVAL = 2
TOPOLOGY_INTERVAL = 60
MAX_DEVICES = 7
PORT_BASE = 8400
LOG_DIR = Path(os.environ.get("DBUS_MPP_LOG_DIR", "/var/log/dbus-mppsolar"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)


def dbusconnection():
    return SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else SystemBus()


def exec_command(port: int, command: str, params: tuple = ()) -> dict:
    with backend_lock(port):
        client = Client(port, "127.0.0.1")
        client.sock.settimeout(4)
        client.connect()
        client.format(Format.JSON)
        output = client.exec(command, params) if params else client.exec(command)
    return json.loads(output)


@dataclass
class DeviceRecord:
    path: str
    serial: str
    port: int
    backend: subprocess.Popen
    driver: subprocess.Popen
    log_handle: object
    started_at: float


class MppSolarManager:
    def __init__(self, scan_interval=SCAN_INTERVAL):
        self.scan_interval = int(scan_interval)
        self.records: dict[str, DeviceRecord] = {}
        self.failed: dict[str, tuple[float, int]] = {}
        self._last_topology = frozenset()
        self._topology_counts: dict[str, int] = {}
        self._topology_running = False
        self._next_topology_refresh = 0.0
        self.manifest = load_manifest()
        self.bus = dbusconnection()
        self.service = VeDbusService(
            "com.victronenergy.mppsolar.manager", bus=self.bus, register=False
        )
        self._setup_service()
        self.service.register()

    def _setup_service(self):
        service = self.service
        service.add_path("/Mgmt/ProcessName", __file__)
        service.add_path("/Mgmt/ProcessVersion", VERSION)
        service.add_path("/Mgmt/Connection", "MPP Solar P18 hotplug manager")
        service.add_path("/Connected", 1)
        service.add_path("/DeviceInstance", 0)
        service.add_path("/ProductId", 0)
        service.add_path("/ProductName", "MPP Solar Manager")
        service.add_path("/DeviceCount", 0)
        service.add_path("/ScanInterval", self.scan_interval)
        for index in range(MAX_DEVICES):
            prefix = f"/Devices/{index}"
            service.add_path(f"{prefix}/Connected", 0)
            service.add_path(f"{prefix}/Serial", "")
            service.add_path(f"{prefix}/Hidraw", "")
            service.add_path(f"{prefix}/InverterService", "")
            service.add_path(f"{prefix}/ChargerService", "")
            service.add_path(
                f"{prefix}/CustomName", "", writeable=True,
                onchangecallback=self._slot_writer(index, "CustomName"),
            )
            service.add_path(
                f"{prefix}/DeviceInstance", 0, writeable=True,
                onchangecallback=self._slot_writer(index, "DeviceInstance"),
            )
            service.add_path(
                f"{prefix}/PollInterval", 10, writeable=True,
                onchangecallback=self._slot_writer(index, "PollInterval"),
            )
            service.add_path(f"{prefix}/NumberOfChargers", 1)

    def _ordered_records(self):
        return sorted(self.records.values(), key=lambda record: record.serial)

    def _slot_writer(self, index, setting_name):
        def write(path, value):
            records = self._ordered_records()
            if index >= len(records):
                return False
            serial = records[index].serial
            if setting_name == "PollInterval":
                try:
                    if not 5 <= int(value) <= 60:
                        return False
                    value = int(value)
                except (TypeError, ValueError):
                    return False
            elif setting_name == "DeviceInstance":
                try:
                    if not 1 <= int(value) <= 255:
                        return False
                    value = int(value)
                except (TypeError, ValueError):
                    return False
                current = int(self._read_setting(serial, "DeviceInstance", 0))
                if value != current and value in self._used_device_instances(serial):
                    return False
            elif setting_name == "CustomName":
                value = str(value)[:32]
            item = VeDbusItemImport(
                self.bus,
                "com.victronenergy.settings",
                f"{settings_prefix(serial)}/{setting_name}",
                createsignal=False,
            )
            return item.exists and item.set_value(value) == 0
        return write

    def _free_port(self):
        used = {record.port for record in self.records.values()}
        for port in range(PORT_BASE, PORT_BASE + MAX_DEVICES * 2):
            if port not in used:
                return port
        raise RuntimeError("no free inverterd port")

    def _open_log(self, label):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return (LOG_DIR / f"{label}.log").open("ab", buffering=0)

    def _start_backend(self, path, port):
        log_handle = self._open_log(Path(path).name)
        backend = subprocess.Popen(
            [
                str(INSTALL_DIR / "inverterd"),
                "--usb-path", path,
                "--port", str(port),
                "--delay", "1000",
                "--device-error-limit", "3",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        return backend, log_handle

    def _probe(self, port):
        last_error = None
        for _ in range(6):
            try:
                protocol = protocol_id_from_response(exec_command(port, "get-protocol-id"))
                if protocol != 18:
                    raise ValueError(f"unsupported protocol {protocol}")
                serial = serial_from_response(exec_command(port, "get-serial-number"))
                return serial
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"P18 validation failed: {last_error}")

    def _used_device_instances(self, exclude_serial=None):
        used = set()
        for serial, item in self.manifest.items():
            if serial == exclude_serial:
                continue
            try:
                instance = int(item.get("device_instance", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if instance > 0:
                used.add(instance)
        try:
            for name in self.bus.list_names():
                if not (
                    name.startswith("com.victronenergy.inverter.")
                    or name.startswith("com.victronenergy.solarcharger.")
                ):
                    continue
                if exclude_serial and name.endswith(service_suffix(exclude_serial)):
                    continue
                value = VeDbusItemImport(
                    self.bus, name, "/DeviceInstance", createsignal=False
                ).get_value()
                if value is not None:
                    used.add(int(value))
        except Exception:
            logging.warning("Unable to enumerate active Device Instances")
        return used

    def _ensure_manifest_entry(self, serial):
        if serial in self.manifest:
            return
        default_name = f"MPP Solar {serial[-6:]}"
        preferred_instance = self._read_setting(serial, "DeviceInstance", 0)
        try:
            preferred_instance = int(preferred_instance)
        except (TypeError, ValueError):
            preferred_instance = 0
        instance = allocate_instance(
            self._used_device_instances(), preferred_instance or None
        )
        self.manifest[serial] = {
            "custom_name": str(
                self._read_setting(serial, "CustomName", default_name)
            )[:32],
            "device_instance": instance,
            "poll_interval": clamp_poll_interval(
                self._read_setting(serial, "PollInterval", POLL_DEFAULT)
            ),
        }
        save_manifest(self.manifest)
        logging.info("Registered new serial %s as Device Instance %s", serial, instance)

    def _start_device(self, path):
        port = self._free_port()
        backend = driver = None
        log_handle = None
        try:
            backend, log_handle = self._start_backend(path, port)
            serial = self._probe(port)
            duplicate = next(
                (record for record in self.records.values() if record.serial == serial), None
            )
            if duplicate is not None:
                raise RuntimeError(
                    f"duplicate serial {serial} already active on {duplicate.path}"
                )
            self._ensure_manifest_entry(serial)
            driver = subprocess.Popen(
                [
                    sys.executable,
                    str(INSTALL_DIR / "dbus-mppsolar.py"),
                    "--serial", path,
                    "--serial-number", serial,
                    "--port", str(port),
                ],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            self.records[path] = DeviceRecord(
                path, serial, port, backend, driver, log_handle, time.monotonic()
            )
            logging.info("Started serial %s on %s (port %s)", serial, path, port)
        except Exception as exc:
            logging.warning("Ignoring %s: %s", path, exc)
            self._terminate_process(driver)
            self._terminate_process(backend)
            if log_handle:
                log_handle.close()
            _, attempts = self.failed.get(path, (0, 0))
            attempts += 1
            self.failed[path] = (
                time.monotonic() + min(60, 2 ** min(attempts, 6)), attempts
            )

    @staticmethod
    def _terminate_process(process):
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _terminate_processes(processes, timeout):
        processes = [
            process for process in processes
            if process is not None and process.poll() is None
        ]
        for process in processes:
            process.terminate()
        deadline = time.monotonic() + timeout
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        remaining = [process for process in processes if process.poll() is None]
        for process in remaining:
            process.kill()
        for process in remaining:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logging.error("Process %s did not stop after SIGKILL", process.pid)

    def _stop_device(self, path, reason):
        record = self.records.pop(path, None)
        if record is None:
            return
        logging.info("Stopping serial %s on %s: %s", record.serial, path, reason)
        self._terminate_process(record.driver)
        self._terminate_process(record.backend)
        record.log_handle.close()

    def _read_setting(self, serial, setting_name, default):
        try:
            value = VeDbusItemImport(
                self.bus,
                "com.victronenergy.settings",
                f"{settings_prefix(serial)}/{setting_name}",
                createsignal=False,
            ).get_value()
            return default if value is None else value
        except Exception:
            return default

    def _update_service(self):
        records = self._ordered_records()
        manifest_changed = False
        self.service["/DeviceCount"] = len(records)
        for index in range(MAX_DEVICES):
            prefix = f"/Devices/{index}"
            if index >= len(records):
                for key, value in (
                    ("Connected", 0), ("Serial", ""), ("Hidraw", ""),
                    ("InverterService", ""), ("ChargerService", ""),
                    ("CustomName", ""), ("DeviceInstance", 0), ("PollInterval", 10),
                    ("NumberOfChargers", 1),
                ):
                    self.service[f"{prefix}/{key}"] = value
                continue
            record = records[index]
            suffix = service_suffix(record.serial)
            inverter_service = f"com.victronenergy.inverter.mppsolar-inverter.{suffix}"
            charger_service = f"com.victronenergy.solarcharger.mppsolar-charger.{suffix}"
            saved = self.manifest.get(record.serial, {})
            self.service[f"{prefix}/Connected"] = int(record.driver.poll() is None)
            self.service[f"{prefix}/Serial"] = record.serial
            self.service[f"{prefix}/Hidraw"] = record.path
            self.service[f"{prefix}/InverterService"] = inverter_service
            self.service[f"{prefix}/ChargerService"] = charger_service
            custom_name = str(self._read_setting(
                record.serial, "CustomName", saved.get("custom_name", "")
            ))[:32]
            device_instance = int(self._read_setting(
                record.serial, "DeviceInstance", saved.get("device_instance", 0)
            ))
            poll_interval = clamp_poll_interval(self._read_setting(
                record.serial, "PollInterval", saved.get("poll_interval", 10)
            ))
            self.service[f"{prefix}/CustomName"] = custom_name
            self.service[f"{prefix}/DeviceInstance"] = device_instance
            self.service[f"{prefix}/PollInterval"] = poll_interval
            self.service[f"{prefix}/NumberOfChargers"] = self._topology_counts.get(
                record.serial, max(1, len(records))
            )
            current_manifest = {
                "custom_name": custom_name,
                "device_instance": device_instance,
                "poll_interval": poll_interval,
            }
            if self.manifest.get(record.serial) != current_manifest:
                self.manifest[record.serial] = current_manifest
                manifest_changed = True
        if manifest_changed:
            save_manifest(self.manifest)

    @staticmethod
    def _calculate_topology(snapshot):
        fallback = max(1, len(snapshot))
        counts = {}
        for serial, port in snapshot:
            responses = []
            for parallel_id in range(7):
                try:
                    responses.append(exec_command(port, "get-p-rated", (parallel_id,)))
                except Exception:
                    continue
            counts[serial] = count_parallel_members(responses, fallback)
        return counts

    def _publish_topology_count(self, serial, count):
        service = (
            "com.victronenergy.inverter.mppsolar-inverter."
            + service_suffix(serial)
        )
        try:
            item = self.bus.get_object(
                service, "/Diagnostics/P18/NumberOfChargers"
            )
            item.SetValue(
                dbus.Int32(count),
                dbus_interface="com.victronenergy.BusItem",
                timeout=5,
                reply_handler=lambda *args: None,
                error_handler=lambda error: self._topology_publish_failed(
                    serial, count, error
                ),
            )
        except Exception as exc:
            self._topology_publish_failed(serial, count, exc)

    def _topology_publish_failed(self, serial, count, error):
        logging.warning("Unable to publish topology for %s: %s", serial, error)
        GLib.timeout_add_seconds(5, self._retry_topology_publish, serial, count)

    def _retry_topology_publish(self, serial, count):
        if any(record.serial == serial for record in self.records.values()):
            self._publish_topology_count(serial, count)
        return False

    def _topology_worker(self, snapshot):
        try:
            counts = self._calculate_topology(snapshot)
            GLib.idle_add(self._apply_topology, snapshot, counts)
        except Exception:
            logging.exception("Unable to calculate parallel topology")
            GLib.idle_add(self._apply_topology, snapshot, {})

    def _apply_topology(self, snapshot, counts):
        self._topology_running = False
        current = frozenset(record.serial for record in self.records.values())
        snapshot_serials = frozenset(serial for serial, _ in snapshot)
        if current == snapshot_serials:
            fallback = max(1, len(current))
            self._topology_counts = {
                serial: max(1, int(counts.get(serial, fallback)))
                for serial in current
            }
            self._last_topology = current
            self._next_topology_refresh = time.monotonic() + TOPOLOGY_INTERVAL
            self._update_service()
            for serial, count in self._topology_counts.items():
                self._publish_topology_count(serial, count)
        else:
            self._next_topology_refresh = 0.0
        return False

    def _schedule_topology_refresh(self):
        topology = frozenset(record.serial for record in self.records.values())
        if not topology:
            self._topology_counts = {}
            self._last_topology = topology
            self._next_topology_refresh = time.monotonic() + TOPOLOGY_INTERVAL
            return
        if self._topology_running:
            return
        if topology == self._last_topology and time.monotonic() < self._next_topology_refresh:
            return
        snapshot = tuple(
            (record.serial, record.port) for record in self._ordered_records()
        )
        self._topology_running = True
        threading.Thread(
            target=self._topology_worker,
            args=(snapshot,),
            name="mppsolar-topology",
            daemon=True,
        ).start()

    def scan(self):
        paths = set(glob.glob("/dev/hidraw*"))
        for path in list(self.records):
            record = self.records[path]
            if path not in paths:
                self._stop_device(path, "device removed")
                self.failed.pop(path, None)
            elif record.backend.poll() is not None or record.driver.poll() is not None:
                lived = time.monotonic() - record.started_at
                self._stop_device(path, "child exited")
                _, attempts = self.failed.get(path, (0, 0))
                attempts = 0 if lived >= 300 else attempts + 1
                self.failed[path] = (
                    time.monotonic() + min(60, 2 ** min(attempts, 6)), attempts
                )
        now = time.monotonic()
        for path in sorted(paths):
            if path in self.records:
                continue
            retry_at, _ = self.failed.get(path, (0, 0))
            if now >= retry_at:
                self._start_device(path)
        for path in list(self.failed):
            if path not in paths:
                self.failed.pop(path, None)
        self._update_service()
        self._schedule_topology_refresh()
        return True

    def shutdown(self):
        records = list(self.records.values())
        self.records.clear()
        for record in records:
            logging.info(
                "Stopping serial %s on %s: manager shutdown",
                record.serial, record.path,
            )
        # Signal every driver first so their 10-second socket timeout and
        # atexit history save run in parallel, then stop their backends.
        self._terminate_processes((record.driver for record in records), 15)
        self._terminate_processes((record.backend for record in records), 5)
        for record in records:
            record.log_handle.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-interval", type=int, default=SCAN_INTERVAL)
    args = parser.parse_args()

    from dbus.mainloop.glib import DBusGMainLoop
    DBusGMainLoop(set_as_default=True)
    manager = MppSolarManager(args.scan_interval)
    loop = GLib.MainLoop()

    def stop_handler(signum, frame):
        logging.info("Received signal %s", signum)
        loop.quit()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    manager.scan()
    GLib.timeout_add_seconds(manager.scan_interval, manager.scan)
    loop.run()
    manager.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
