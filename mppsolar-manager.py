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
    LEGACY_CONFIG_PATH,
    MANIFEST_PATH,
    clamp_poll_interval,
    load_manifest,
    protocol_id_from_response,
    save_manifest,
    serial_from_response,
    service_suffix,
    settings_prefix,
)


VERSION = "0.3"
SCAN_INTERVAL = 2
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

    def _ordered_records(self):
        return sorted(self.records.values(), key=lambda record: record.serial)

    def _slot_writer(self, index, setting_name):
        def write(path, value):
            records = self._ordered_records()
            if index >= len(records):
                return False
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
            elif setting_name == "CustomName":
                value = str(value)[:32]
            serial = records[index].serial
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

    def _legacy_defaults(self, path, serial):
        if serial in self.manifest or not LEGACY_CONFIG_PATH.exists():
            return
        try:
            with LEGACY_CONFIG_PATH.open(encoding="utf-8") as handle:
                legacy = json.load(handle)
            item = legacy.get(path, {})
            if not isinstance(item, dict):
                return
            self.manifest[serial] = {
                "custom_name": str(item.get("productname") or f"MPP Solar {serial[-6:]}")[:32],
                "device_instance": int(item.get("deviceinstance") or 0),
                "poll_interval": clamp_poll_interval(
                    int(item.get("updateInterval", 10000)) // 1000
                ),
            }
            save_manifest(self.manifest)
            logging.info("Migrated legacy defaults for serial %s", serial)
        except Exception:
            logging.exception("Unable to migrate legacy defaults for %s", path)

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
            self._legacy_defaults(path, serial)
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
            self.failed.pop(path, None)
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

    def _stop_device(self, path, reason):
        record = self.records.pop(path, None)
        if record is None:
            return
        logging.info("Stopping serial %s on %s: %s", record.serial, path, reason)
        self._terminate_process(record.driver)
        self._terminate_process(record.backend)
        record.log_handle.close()

    def _read_service_value(self, service, path, default):
        try:
            value = VeDbusItemImport(self.bus, service, path, createsignal=False).get_value()
            return default if value is None else value
        except Exception:
            return default

    def _update_service(self):
        records = self._ordered_records()
        self.service["/DeviceCount"] = len(records)
        for index in range(MAX_DEVICES):
            prefix = f"/Devices/{index}"
            if index >= len(records):
                for key, value in (
                    ("Connected", 0), ("Serial", ""), ("Hidraw", ""),
                    ("InverterService", ""), ("ChargerService", ""),
                    ("CustomName", ""), ("DeviceInstance", 0), ("PollInterval", 10),
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
            self.service[f"{prefix}/CustomName"] = self._read_service_value(
                inverter_service, "/CustomName", saved.get("custom_name", "")
            )
            self.service[f"{prefix}/DeviceInstance"] = self._read_service_value(
                inverter_service, "/Settings/DeviceInstance", saved.get("device_instance", 0)
            )
            self.service[f"{prefix}/PollInterval"] = self._read_service_value(
                inverter_service, "/Settings/PollInterval", saved.get("poll_interval", 10)
            )

    def _notify_topology_change(self):
        topology = frozenset(record.serial for record in self.records.values())
        if topology == self._last_topology:
            return
        if not topology:
            self._last_topology = topology
            return
        marker = int(time.time())
        complete = True
        for record in self.records.values():
            service = (
                "com.victronenergy.inverter.mppsolar-inverter."
                + service_suffix(record.serial)
            )
            try:
                item = VeDbusItemImport(
                    self.bus, service, '/Diagnostics/P18/RefreshTopology',
                    createsignal=False,
                )
                if item.exists:
                    item.set_value(marker)
                else:
                    complete = False
            except Exception:
                complete = False
                logging.debug("Topology refresh deferred for %s", record.serial)
        if complete:
            self._last_topology = topology

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
        self._notify_topology_change()
        return True

    def shutdown(self):
        for path in list(self.records):
            self._stop_device(path, "manager shutdown")


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
