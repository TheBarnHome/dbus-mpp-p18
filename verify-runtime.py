#!/usr/bin/env python3
"""Validate a running serial-indexed dbus-mppsolar installation."""

from __future__ import annotations

import argparse
import json
import os
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
values = {}
for path in sys.argv[2:]:
    value = bus.get_object(sys.argv[1], path).GetValue(
        dbus_interface="com.victronenergy.BusItem"
    )
    if not isinstance(value, (str, int, float, bool, list, dict)):
        value = str(value)
    values[path] = value
print(json.dumps(values))
'''


def read_values(service, paths, timeout=20):
    try:
        result = subprocess.run(
            [sys.executable, "-c", DBUS_READ_SCRIPT, service, *paths],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ReadTimeout(f"D-Bus read timed out: {service}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"D-Bus read failed: {service}: {exc.stderr.strip()}"
        ) from exc


def read_value(bus, service, path, timeout=20):
    return read_values(service, [path], timeout)[path]


def verify(require_migrated=False, expected_serials=(), require_legacy_backup=False):
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
    manager_values = {}
    active_serials = []
    if manager in names:
        try:
            count = int(read_value(bus, manager, "/DeviceCount"))
            manager_paths = [
                path
                for index in range(count)
                for path in (
                    f"/Devices/{index}/Serial",
                    f"/Devices/{index}/NumberOfChargers",
                )
            ]
            manager_values = read_values(manager, manager_paths) if manager_paths else {}
            active_serials = [
                str(manager_values[f"/Devices/{index}/Serial"])
                for index in range(count)
            ]
            report.append(f"manager devices={count}")
            check(len(set(active_serials)) == count, "manager contains duplicate serial slots")
            for serial in active_serials:
                check(serial in manifest, f"active serial {serial} is missing from manifest")
            missing_expected = set(expected_serials) - set(active_serials)
            check(
                not missing_expected,
                "expected serials are not active: " + ", ".join(sorted(missing_expected)),
            )
        except Exception as exc:
            failures.append(f"manager /DeviceCount: {exc}")

    for index, serial in enumerate(active_serials):
        suffix = service_suffix(serial)
        inverter = f"com.victronenergy.inverter.mppsolar-inverter.{suffix}"
        charger = f"com.victronenergy.solarcharger.mppsolar-charger.{suffix}"
        check(inverter in names, f"missing service {inverter}")
        check(charger in names, f"missing service {charger}")
        if inverter not in names or charger not in names:
            continue
        try:
            setting_base = settings_prefix(serial)
            inv_values = read_values(inverter, [
                "/Serial", "/CustomName", "/DeviceInstance", "/Settings/PollInterval",
                "/Mode", "/Diagnostics/P18/NumberOfChargers", "/Diagnostics/P18/CurrentHidraw",
            ])
            chg_values = read_values(charger, [
                "/Serial", "/CustomName", "/DeviceInstance", "/Settings/PollInterval", "/Mode",
            ])
            setting_values = read_values("com.victronenergy.settings", [
                f"{setting_base}/CustomName",
                f"{setting_base}/DeviceInstance",
                f"{setting_base}/PollInterval",
            ])
            inv_serial = str(inv_values["/Serial"])
            chg_serial = str(chg_values["/Serial"])
            inv_name = str(inv_values["/CustomName"])
            chg_name = str(chg_values["/CustomName"])
            inv_instance = int(inv_values["/DeviceInstance"])
            chg_instance = int(chg_values["/DeviceInstance"])
            inv_poll = int(inv_values["/Settings/PollInterval"])
            chg_poll = int(chg_values["/Settings/PollInterval"])
            inverter_mode = int(inv_values["/Mode"])
            charger_mode = int(chg_values["/Mode"])
            topology = int(inv_values["/Diagnostics/P18/NumberOfChargers"])
            hidraw = str(inv_values["/Diagnostics/P18/CurrentHidraw"])
            saved_name = str(setting_values[f"{setting_base}/CustomName"])
            saved_instance = int(setting_values[f"{setting_base}/DeviceInstance"])
            saved_poll = int(setting_values[f"{setting_base}/PollInterval"])
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
            manager_serial = str(manager_values.get(f"/Devices/{index}/Serial", ""))
            manager_topology = int(
                manager_values.get(f"/Devices/{index}/NumberOfChargers", 0)
            )
            check(manager_serial == serial, f"manager slot {index} serial mismatch")
            check(
                manager_topology == topology,
                f"topology mismatch for {serial}: manager={manager_topology}, driver={topology}",
            )
            check(HistoryStore(serial).path.exists(), f"history file missing for {serial}")
            report.append(
                f"{serial} name={inv_name!r} instance={inv_instance} poll={inv_poll}s "
                f"hidraw={hidraw} chargers={topology} modes={inverter_mode}/{charger_mode}"
            )
        except Exception as exc:
            failures.append(f"runtime values for {serial}: {exc}")

    if require_migrated:
        check(not LEGACY_CONFIG_PATH.exists(), "config.json is still present")
    if require_legacy_backup:
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
    parser.add_argument("--require-legacy-backup", action="store_true")
    parser.add_argument(
        "--expected-serials",
        default=os.environ.get("DBUS_MPP_EXPECTED_SERIALS", ""),
        help="space-separated serials which must still be connected",
    )
    args = parser.parse_args()
    expected = tuple(filter(None, args.expected_serials.split()))
    return 0 if verify(
        args.require_migrated, expected, args.require_legacy_backup
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
