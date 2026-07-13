#!/usr/bin/env python3
"""Validate a running serial-indexed dbus-mppsolar installation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import dbus

from mppsolar_common import (
    LEGACY_CONFIG_PATH,
    HistoryStore,
    load_manifest,
    service_suffix,
    settings_prefix,
)


class ReadTimeout(RuntimeError):
    pass


DBUS_READ_SCRIPT = r'''
import dbus
import json
import sys

bus = dbus.SystemBus()
value = bus.get_object(sys.argv[1], sys.argv[2]).GetValue(
    dbus_interface="com.victronenergy.BusItem"
)
if not isinstance(value, (str, int, float, bool, list, dict)):
    value = str(value)
print(json.dumps(value))
'''


def read_value(bus, service, path, timeout=12):
    try:
        result = subprocess.run(
            [sys.executable, "-c", DBUS_READ_SCRIPT, service, path],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ReadTimeout(f"D-Bus read timed out: {service} {path}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"D-Bus read failed: {service} {path}: {exc.stderr.strip()}"
        ) from exc


def verify(require_migrated=False):
    bus = dbus.SystemBus()
    names = set(bus.list_names())
    manifest = load_manifest()
    failures = []
    report = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    manager = "com.victronenergy.mppsolar.manager"
    check(manager in names, "manager D-Bus service is missing")
    if manager in names:
        try:
            count = int(read_value(bus, manager, "/DeviceCount"))
            report.append(f"manager devices={count}")
            check(count == len(manifest), f"manager reports {count}, manifest has {len(manifest)}")
        except Exception as exc:
            failures.append(f"manager /DeviceCount: {exc}")

    for index, serial in enumerate(sorted(manifest)):
        suffix = service_suffix(serial)
        inverter = f"com.victronenergy.inverter.mppsolar-inverter.{suffix}"
        charger = f"com.victronenergy.solarcharger.mppsolar-charger.{suffix}"
        check(inverter in names, f"missing service {inverter}")
        check(charger in names, f"missing service {charger}")
        if inverter not in names or charger not in names:
            continue
        try:
            inv_serial = str(read_value(bus, inverter, "/Serial"))
            chg_serial = str(read_value(bus, charger, "/Serial"))
            inv_name = str(read_value(bus, inverter, "/CustomName"))
            chg_name = str(read_value(bus, charger, "/CustomName"))
            inv_instance = int(read_value(bus, inverter, "/DeviceInstance"))
            chg_instance = int(read_value(bus, charger, "/DeviceInstance"))
            inv_poll = int(read_value(bus, inverter, "/Settings/PollInterval"))
            chg_poll = int(read_value(bus, charger, "/Settings/PollInterval"))
            inverter_mode = int(read_value(bus, inverter, "/Mode"))
            charger_mode = int(read_value(bus, charger, "/Mode"))
            topology = int(read_value(bus, inverter, "/Diagnostics/P18/NumberOfChargers"))
            hidraw = str(read_value(bus, inverter, "/Diagnostics/P18/CurrentHidraw"))
            setting_base = settings_prefix(serial)
            saved_name = str(read_value(bus, "com.victronenergy.settings", f"{setting_base}/CustomName"))
            saved_instance = int(read_value(bus, "com.victronenergy.settings", f"{setting_base}/DeviceInstance"))
            saved_poll = int(read_value(bus, "com.victronenergy.settings", f"{setting_base}/PollInterval"))
            check(inv_serial == serial and chg_serial == serial, f"serial mismatch for {serial}")
            check(inv_name == chg_name, f"name mismatch for {serial}")
            check(inv_instance == chg_instance, f"Device Instance mismatch for {serial}")
            check(inv_poll == chg_poll, f"poll interval mismatch for {serial}")
            check(inverter_mode == 2, f"invalid inverter mode {inverter_mode} for {serial}")
            check(charger_mode == 1, f"invalid charger mode {charger_mode} for {serial}")
            check(topology >= 1, f"invalid numberOfChargers {topology} for {serial}")
            check(hidraw.startswith("/dev/hidraw"), f"invalid HID path for {serial}: {hidraw}")
            check(inv_name == saved_name, f"name not persisted for {serial}")
            check(inv_instance == saved_instance, f"instance not persisted for {serial}")
            check(inv_poll == saved_poll, f"poll interval not persisted for {serial}")
            manager_serial = str(read_value(bus, manager, f"/Devices/{index}/Serial"))
            check(manager_serial == serial, f"manager slot {index} serial mismatch")
            check(HistoryStore(serial).path.exists(), f"history file missing for {serial}")
            report.append(
                f"{serial} name={inv_name!r} instance={inv_instance} poll={inv_poll}s "
                f"hidraw={hidraw} chargers={topology} modes={inverter_mode}/{charger_mode}"
            )
        except Exception as exc:
            failures.append(f"runtime values for {serial}: {exc}")

    if require_migrated:
        check(not LEGACY_CONFIG_PATH.exists(), "config.json is still present")
        check(
            LEGACY_CONFIG_PATH.with_name("config.json.legacy").exists(),
            "config.json.legacy is missing",
        )

    for line in report:
        print(line)
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return not failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-migrated", action="store_true")
    args = parser.parse_args()
    return 0 if verify(args.require_migrated) else 1


if __name__ == "__main__":
    sys.exit(main())
