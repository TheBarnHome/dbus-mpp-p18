#!/usr/bin/env python3
"""Shared, dependency-free helpers for dbus-mppsolar."""

from __future__ import annotations

import json
import math
import os
import re
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path


INSTALL_DIR = Path(os.environ.get("DBUS_MPP_INSTALL_DIR", "/data/etc/dbus-mppsolar"))
STATE_DIR = Path(os.environ.get("DBUS_MPP_STATE_DIR", "/data/etc/dbus-mppsolar-state"))
MANIFEST_PATH = Path(
    os.environ.get("DBUS_MPP_MANIFEST", str(STATE_DIR / "devices.json"))
)
LEGACY_CONFIG_PATH = INSTALL_DIR / "config.json"
POLL_MIN = 5
POLL_MAX = 60
POLL_DEFAULT = 10
INSTANCE_MIN = 1
INSTANCE_MAX = 255
P18_WARNING_FIELDS = (
    "line_fail",
    "output_circuit_short",
    "inverter_over_temperature",
    "fan_lock",
    "battery_voltage_high",
    "battery_low",
    "battery_under",
    "over_load",
    "eeprom_fail",
    "power_limit",
    "pv1_voltage_high",
    "pv2_voltage_high",
    "mppt1_overload_warning",
    "mppt2_overload_warning",
    "battery_too_low_to_charge_for_scc1",
    "battery_too_low_to_charge_for_scc2",
)
P18_FAULT_TEXT = {
    0: "No fault",
    1: "Fan is locked",
    2: "Over temperature",
    3: "Battery voltage is too high",
    4: "Battery voltage is too low",
    5: "Output short circuited or over temperature",
    6: "Output voltage is too high",
    7: "Overload timed out",
    8: "Bus voltage is too high",
    9: "Bus soft start failed",
    11: "Main relay failed",
    51: "Inverter over current",
    52: "Bus soft start failed",
    53: "Inverter soft start failed",
    54: "Self-test failed",
    55: "DC voltage detected on inverter output",
    56: "Battery connection is open",
    57: "Current sensor failed",
    58: "Output voltage is too low",
    60: "Inverter negative power",
    71: "Parallel version differs",
    72: "Output circuit failed",
    80: "CAN communication failed",
    81: "Parallel host line lost",
    82: "Parallel synchronization signal lost",
    83: "Parallel battery voltage differs",
    84: "Parallel line voltage or frequency differs",
    85: "Parallel line input current is unbalanced",
    86: "Parallel output setting differs",
}
BACKEND_LOCK_DIR = Path(
    os.environ.get("DBUS_MPP_LOCK_DIR", str(STATE_DIR / "locks"))
)

HISTORY_PATHS = (
    "/Yield/User",
    "/Yield/System",
    "/History/Overall/Yield",
    "/History/Overall/Consumption",
    "/History/Overall/MaxPower",
    "/History/Overall/MaxPvVoltage",
    "/History/Overall/MinBatteryVoltage",
    "/History/Overall/MaxBatteryVoltage",
    "/History/Overall/MaxBatteryCurrent",
    "/History/Overall/TimeInBulk",
    "/History/Overall/TimeInAbsorption",
    "/History/Overall/TimeInFloat",
    "/History/Overall/LastError1",
    "/History/Overall/LastError2",
    "/History/Overall/LastError3",
    "/History/Overall/LastError4",
)


@contextmanager
def backend_lock(port: int):
    """Serialize all clients talking to the same inverterd backend."""
    BACKEND_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = BACKEND_LOCK_DIR / f"backend-{int(port)}.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def call_with_retries(call, attempts: int = 2, retry_delay: float = 0.5, on_retry=None):
    """Run a read operation again after a transient failure."""
    attempts = max(1, int(attempts))
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            if attempt >= attempts:
                raise
            if on_retry is not None:
                on_retry(exc, attempt, attempts)
            if retry_delay > 0:
                time.sleep(retry_delay)


def normalize_charge_voltage(value: object) -> float | None:
    """Return the P18 one-decimal voltage, rejecting missing/non-finite values."""
    try:
        voltage = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(voltage):
        return None
    return round(voltage, 1)


def changed_charge_voltage(previous: float | None, requested: object) -> float | None:
    """Return a new P18 voltage only when its encoded value has changed."""
    voltage = normalize_charge_voltage(requested)
    if voltage is None or voltage == previous:
        return None
    return voltage


def validate_serial(serial: object) -> str:
    """Return a P18 serial safe for D-Bus/settings paths, or raise ValueError."""
    value = str(serial or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError("invalid or missing P18 serial number")
    return value


def service_suffix(serial: str) -> str:
    return "sn_" + validate_serial(serial).replace("-", "_")


def settings_prefix(serial: str) -> str:
    return f"/Settings/Devices/mppsolar_{validate_serial(serial)}"


def allocate_instance(used: set[int], preferred: int | None = None) -> int:
    if preferred is not None:
        preferred = int(preferred)
        if INSTANCE_MIN <= preferred <= INSTANCE_MAX and preferred not in used:
            return preferred
    for candidate in range(INSTANCE_MIN, INSTANCE_MAX + 1):
        if candidate not in used:
            return candidate
    raise RuntimeError("no free Device Instance remains")


def clamp_poll_interval(value: object) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = POLL_DEFAULT
    return min(POLL_MAX, max(POLL_MIN, interval))


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp.exists():
            temp.unlink()


def load_json_quarantined(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        quarantine_file(path)
        return default


def quarantine_file(path: Path) -> None:
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 1
    while quarantine.exists():
        quarantine = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        suffix += 1
    try:
        os.replace(path, quarantine)
    except OSError:
        pass


class HistoryStore:
    def __init__(self, serial: str, state_dir: Path = STATE_DIR):
        self.serial = validate_serial(serial)
        self.path = Path(state_dir) / f"{self.serial}.json"

    def load(self) -> dict[str, float | int]:
        payload = load_json_quarantined(self.path, {})
        if not isinstance(payload, dict) or (
            payload and (
                payload.get("schema") != 1
                or payload.get("serial") != self.serial
                or not isinstance(payload.get("history"), dict)
            )
        ):
            quarantine_file(self.path)
            return {}
        history = payload.get("history", {})
        if not isinstance(history, dict):
            history = {}
        clean = {}
        for path in HISTORY_PATHS:
            value = history.get(path)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[path] = value
        return clean

    def save(self, history: dict[str, float | int]) -> None:
        clean = {
            path: history[path]
            for path in HISTORY_PATHS
            if path in history
            and isinstance(history[path], (int, float))
            and not isinstance(history[path], bool)
        }
        atomic_write_json(
            self.path,
            {
                "schema": 1,
                "serial": self.serial,
                "saved_at": int(time.time()),
                "history": clean,
            },
        )


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict]:
    payload = load_json_quarantined(Path(path), {})
    if not isinstance(payload, dict):
        return {}
    devices = payload.get("devices", payload)
    if not isinstance(devices, dict):
        return {}
    result = {}
    for serial, settings in devices.items():
        try:
            serial = validate_serial(serial)
        except ValueError:
            continue
        if isinstance(settings, dict):
            result[serial] = dict(settings)
    return result


def save_manifest(devices: dict[str, dict], path: Path = MANIFEST_PATH) -> None:
    normalized = {validate_serial(serial): dict(value) for serial, value in devices.items()}
    atomic_write_json(
        Path(path),
        {"schema": 1, "devices": normalized, "saved_at": int(time.time())},
    )


def parse_p18_result(response: object) -> dict:
    if not isinstance(response, dict):
        raise ValueError("P18 response is not an object")
    if response.get("result") == "error":
        raise ValueError(str(response.get("message") or "P18 command failed"))
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("P18 response has no data")
    return data


def normalize_p18_alerts(response: object) -> dict[str, int | bool]:
    """Validate and normalize the complete P18 FWS response."""
    data = parse_p18_result(response)
    if "fault_code" not in data:
        raise ValueError("P18 alert response has no fault_code")
    raw_fault_code = data["fault_code"]
    if isinstance(raw_fault_code, bool):
        raise ValueError("P18 fault_code is not an integer")
    if isinstance(raw_fault_code, int):
        fault_code = raw_fault_code
    elif isinstance(raw_fault_code, str) and re.fullmatch(
        r"[0-9]+", raw_fault_code.strip()
    ):
        fault_code = int(raw_fault_code)
    else:
        raise ValueError("P18 fault_code is not an integer")
    if fault_code < 0:
        raise ValueError("P18 fault_code cannot be negative")

    normalized: dict[str, int | bool] = {"fault_code": fault_code}
    for name in P18_WARNING_FIELDS:
        if name not in data:
            raise ValueError(f"P18 alert response has no {name}")
        value = data[name]
        if isinstance(value, bool):
            normalized[name] = value
        elif (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value in (0, 1)
        ):
            normalized[name] = bool(value)
        elif isinstance(value, str) and value.strip().lower() in (
            "0",
            "1",
            "false",
            "true",
        ):
            normalized[name] = value.strip().lower() in ("1", "true")
        else:
            raise ValueError(f"P18 alert {name} is not boolean")
    return normalized


def p18_fault_text(fault_code: object) -> str:
    try:
        code = int(fault_code)
    except (TypeError, ValueError):
        return "Invalid fault code"
    return P18_FAULT_TEXT.get(code, f"Unknown fault code {code}")


def serial_from_response(response: object) -> str:
    data = parse_p18_result(response)
    for key in ("serial_number", "serial", "sn"):
        if key in data:
            value = data[key]
            if isinstance(value, dict):
                value = value.get("value")
            return validate_serial(value)
    raise ValueError("P18 response has no serial number")


def protocol_id_from_response(response: object) -> int:
    data = parse_p18_result(response)
    value = data.get("protocol_id", data.get("id"))
    if isinstance(value, dict):
        value = value.get("value")
    return int(value)


def count_parallel_members(responses: list[object], fallback: int) -> int:
    serials = set()
    for response in responses:
        try:
            data = parse_p18_result(response)
        except ValueError:
            continue
        status = data.get("parallel_connection_status")
        if isinstance(status, dict):
            status = status.get("value")
        if str(status).strip().lower() != "existent":
            continue
        serial = data.get("serial_number")
        if isinstance(serial, dict):
            serial = serial.get("value")
        try:
            serials.add(validate_serial(serial))
        except ValueError:
            continue
    return max(1, len(serials) or int(fallback or 1))
