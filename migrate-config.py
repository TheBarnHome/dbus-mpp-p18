#!/usr/bin/env python3
"""One-shot migration from hidraw-indexed config to serial-indexed state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from inverterd import Client, Format

sys.path.insert(1, os.path.join(os.path.dirname(__file__), "velib_python"))

from mppsolar_common import (
    HISTORY_PATHS,
    HistoryStore,
    LEGACY_CONFIG_PATH,
    MANIFEST_PATH,
    clamp_poll_interval,
    save_manifest,
    serial_from_response,
)


def command(port, name):
    client = Client(port, "127.0.0.1")
    client.sock.settimeout(10)
    client.connect()
    client.format(Format.JSON)
    return json.loads(client.exec(name))


def dbus_history(hidraw):
    try:
        import dbus
        from vedbus import VeDbusItemImport

        bus = dbus.SystemBus()
        service = f"com.victronenergy.solarcharger.mppsolar-charger.{Path(hidraw).name}"
        if service not in bus.list_names():
            return {}
        result = {}
        for path in HISTORY_PATHS:
            value = VeDbusItemImport(bus, service, path, createsignal=False).get_value()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[path] = value
        return result
    except Exception as exc:
        print(f"Warning: unable to capture D-Bus history for {hidraw}: {exc}", file=sys.stderr)
        return {}


def merge_history(old, current):
    result = dict(old)
    for path, value in current.items():
        previous = result.get(path)
        if previous is None:
            result[path] = value
        elif path.endswith("/MinBatteryVoltage"):
            positives = [item for item in (previous, value) if item and item > 0]
            result[path] = min(positives) if positives else 0
        elif path.startswith("/History/") or path.startswith("/Yield/"):
            result[path] = max(previous, value)
        else:
            result[path] = value
    return result


def migrate(config_path, dry_run=False, keep_config=False):
    config_path = Path(config_path)
    if not config_path.exists():
        print("No legacy config.json to migrate.")
        return {}
    with config_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict) or not config:
        raise RuntimeError("legacy config is empty or invalid")

    manifest = {}
    captured = {}
    used_instances = set()
    for hidraw, item in config.items():
        if not hidraw.startswith("/dev/hidraw") or not isinstance(item, dict):
            raise RuntimeError(f"invalid legacy entry: {hidraw}")
        instance = int(item.get("deviceinstance", 0))
        if instance < 1 or instance in used_instances:
            raise RuntimeError(f"invalid or duplicate Device Instance {instance}")
        used_instances.add(instance)
        serial = serial_from_response(command(8305 + instance, "get-serial-number"))
        if serial in manifest:
            raise RuntimeError(f"duplicate P18 serial {serial}")
        manifest[serial] = {
            "custom_name": str(item.get("productname") or f"MPP Solar {serial[-6:]}")[:32],
            "device_instance": instance,
            "poll_interval": clamp_poll_interval(
                int(item.get("updateInterval", 10000)) // 1000
            ),
        }
        captured[serial] = dbus_history(hidraw)

    print(json.dumps({"devices": manifest}, indent=2, sort_keys=True))
    if dry_run:
        return manifest

    save_manifest(manifest, MANIFEST_PATH)
    for serial, values in captured.items():
        store = HistoryStore(serial)
        store.save(merge_history(store.load(), values))

    if not keep_config:
        legacy_path = config_path.with_name(config_path.name + ".legacy")
        if legacy_path.exists():
            shutil.copy2(config_path, legacy_path)
            config_path.unlink()
        else:
            os.replace(config_path, legacy_path)
        print(f"Legacy configuration retained at {legacy_path}")
    print(f"Migration completed: {len(manifest)} device(s), manifest {MANIFEST_PATH}")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(LEGACY_CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-config", action="store_true")
    args = parser.parse_args()
    migrate(args.config, args.dry_run, args.keep_config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
