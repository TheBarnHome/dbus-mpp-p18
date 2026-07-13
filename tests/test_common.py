import json
import math
import tempfile
import unittest
from pathlib import Path

from mppsolar_common import (
    HistoryStore,
    allocate_instance,
    call_with_retries,
    changed_charge_voltage,
    clamp_poll_interval,
    count_parallel_members,
    load_manifest,
    normalize_charge_voltage,
    normalize_p18_alerts,
    p18_fault_text,
    save_manifest,
    serial_from_response,
    service_suffix,
    validate_serial,
)


class RetryTests(unittest.TestCase):
    def test_transient_failure_is_retried(self):
        calls = []
        retries = []

        def operation():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise RuntimeError("transient failure")
            return "ok"

        result = call_with_retries(
            operation,
            attempts=2,
            retry_delay=0,
            on_retry=lambda exc, attempt, attempts: retries.append(
                (str(exc), attempt, attempts)
            ),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [1, 2])
        self.assertEqual(retries, [("transient failure", 1, 2)])

    def test_last_failure_is_raised_after_all_attempts(self):
        calls = []

        def operation():
            calls.append(len(calls) + 1)
            raise RuntimeError(f"failure {len(calls)}")

        with self.assertRaisesRegex(RuntimeError, "failure 2"):
            call_with_retries(operation, attempts=2, retry_delay=0)

        self.assertEqual(calls, [1, 2])


class ChargeVoltageTests(unittest.TestCase):
    def test_voltage_is_normalized_to_p18_precision(self):
        self.assertEqual(normalize_charge_voltage(55.20000076293945), 55.2)
        self.assertEqual(normalize_charge_voltage("54.84"), 54.8)

    def test_missing_or_non_finite_voltage_is_rejected(self):
        for value in (None, "bad", math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assertIsNone(normalize_charge_voltage(value))

    def test_same_encoded_voltage_is_not_reapplied(self):
        self.assertIsNone(changed_charge_voltage(55.2, 55.20000076293945))
        self.assertEqual(changed_charge_voltage(55.2, 55.31), 55.3)


class IdentityTests(unittest.TestCase):
    def test_serial_response_variants(self):
        self.assertEqual(
            serial_from_response({"result": "ok", "data": {"sn": "96132301100087"}}),
            "96132301100087",
        )
        self.assertEqual(service_suffix("96132301100087"), "sn_96132301100087")

    def test_invalid_serial_is_rejected(self):
        for value in (None, "", "serial with spaces", "../../bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_serial(value)

    def test_allocation_and_poll_bounds(self):
        self.assertEqual(allocate_instance({1, 3}, 2), 2)
        self.assertEqual(allocate_instance({1, 2}, 2), 3)
        self.assertEqual(clamp_poll_interval(1), 5)
        self.assertEqual(clamp_poll_interval(90), 60)
        self.assertEqual(clamp_poll_interval("bad"), 10)


class ParallelTopologyTests(unittest.TestCase):
    def test_distinct_existent_serials_are_counted(self):
        responses = [
            {"result": "ok", "data": {"parallel_connection_status": "Existent", "serial_number": "A1"}},
            {"result": "ok", "data": {"parallel_connection_status": "Existent", "serial_number": "A2"}},
            {"result": "ok", "data": {"parallel_connection_status": "Existent", "serial_number": "A1"}},
            {"result": "ok", "data": {"parallel_connection_status": "Non-existent", "serial_number": ""}},
        ]
        self.assertEqual(count_parallel_members(responses, fallback=7), 2)

    def test_active_device_fallback_has_minimum_one(self):
        self.assertEqual(count_parallel_members([], fallback=3), 3)
        self.assertEqual(count_parallel_members([], fallback=0), 1)


class P18AlertTests(unittest.TestCase):
    def complete_response(self, **updates):
        data = {
            "fault_code": 0,
            "line_fail": False,
            "output_circuit_short": False,
            "inverter_over_temperature": False,
            "fan_lock": False,
            "battery_voltage_high": False,
            "battery_low": False,
            "battery_under": False,
            "over_load": False,
            "eeprom_fail": False,
            "power_limit": False,
            "pv1_voltage_high": False,
            "pv2_voltage_high": False,
            "mppt1_overload_warning": False,
            "mppt2_overload_warning": False,
            "battery_too_low_to_charge_for_scc1": False,
            "battery_too_low_to_charge_for_scc2": False,
        }
        data.update(updates)
        return {"result": "ok", "data": data}

    def test_complete_alert_response_is_normalized(self):
        alerts = normalize_p18_alerts(
            self.complete_response(line_fail=True, eeprom_fail="1")
        )
        self.assertTrue(alerts["line_fail"])
        self.assertTrue(alerts["eeprom_fail"])
        self.assertFalse(alerts["fan_lock"])

    def test_incomplete_or_ambiguous_alert_response_is_rejected(self):
        response = self.complete_response()
        del response["data"]["eeprom_fail"]
        with self.assertRaisesRegex(ValueError, "eeprom_fail"):
            normalize_p18_alerts(response)
        with self.assertRaisesRegex(ValueError, "not boolean"):
            normalize_p18_alerts(self.complete_response(line_fail="yes"))

    def test_fault_text_is_explicit_for_known_and_unknown_codes(self):
        self.assertEqual(p18_fault_text(0), "No fault")
        self.assertEqual(p18_fault_text(80), "CAN communication failed")
        self.assertEqual(p18_fault_text(99), "Unknown fault code 99")


class PersistentStateTests(unittest.TestCase):
    def test_history_round_trip_and_corruption_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            store = HistoryStore("ABC123", state_dir)
            store.save({
                "/Yield/User": 42.5,
                "/History/Overall/MaxPower": 3210,
                "/not/persisted": 99,
            })
            self.assertEqual(store.load()["/Yield/User"], 42.5)
            self.assertNotIn("/not/persisted", store.load())
            store.path.write_text("not-json", encoding="utf-8")
            self.assertEqual(store.load(), {})
            self.assertEqual(len(list(state_dir.glob("ABC123.json.corrupt-*"))), 1)
            store.path.write_text('{"schema": 99, "history": {}}', encoding="utf-8")
            self.assertEqual(store.load(), {})
            self.assertEqual(len(list(state_dir.glob("ABC123.json.corrupt-*"))), 2)

    def test_manifest_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            expected = {
                "96132301100087": {
                    "custom_name": "Master",
                    "device_instance": 1,
                    "poll_interval": 10,
                }
            }
            save_manifest(expected, path)
            self.assertEqual(load_manifest(path), expected)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)


if __name__ == "__main__":
    unittest.main()
