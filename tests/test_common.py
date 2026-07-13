import json
import tempfile
import unittest
from pathlib import Path

from mppsolar_common import (
    HistoryStore,
    allocate_instance,
    clamp_poll_interval,
    count_parallel_members,
    load_manifest,
    save_manifest,
    serial_from_response,
    service_suffix,
    validate_serial,
)


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
