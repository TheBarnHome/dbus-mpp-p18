import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock


class FakeBusConnection:
    TYPE_SYSTEM = 1
    TYPE_SESSION = 2

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)


fake_dbus = types.ModuleType("dbus")
fake_dbus.bus = types.SimpleNamespace(BusConnection=FakeBusConnection)
sys.modules.setdefault("dbus", fake_dbus)

fake_gi = types.ModuleType("gi")
fake_repository = types.ModuleType("gi.repository")
fake_repository.GLib = types.SimpleNamespace()
fake_gi.repository = fake_repository
sys.modules.setdefault("gi", fake_gi)
sys.modules.setdefault("gi.repository", fake_repository)

fake_inverterd = types.ModuleType("inverterd")
fake_inverterd.Client = object
fake_inverterd.Format = types.SimpleNamespace(JSON="json")
sys.modules.setdefault("inverterd", fake_inverterd)

fake_vedbus = types.ModuleType("vedbus")
fake_vedbus.VeDbusItemImport = object
fake_vedbus.VeDbusService = object
sys.modules.setdefault("vedbus", fake_vedbus)

SPEC = importlib.util.spec_from_file_location(
    "mppsolar_manager", Path(__file__).parents[1] / "mppsolar-manager.py"
)
mppsolar_manager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mppsolar_manager
SPEC.loader.exec_module(mppsolar_manager)


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakeLog:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def bare_manager():
    manager = object.__new__(mppsolar_manager.MppSolarManager)
    manager.records = {}
    manager.failed = {}
    manager._last_topology = frozenset()
    manager._topology_counts = {}
    manager._topology_running = False
    manager._next_topology_refresh = 0.0
    manager._update_service = mock.Mock()
    manager._schedule_topology_refresh = mock.Mock()
    return manager


class ManagerScanTests(unittest.TestCase):
    def test_new_serial_gets_smallest_free_persistent_instance(self):
        manager = object.__new__(mppsolar_manager.MppSolarManager)
        manager.manifest = {
            "EXISTING1": {"device_instance": 1},
            "EXISTING3": {"device_instance": 3},
        }
        manager._used_device_instances = mock.Mock(return_value={1, 3})
        manager._read_setting = mock.Mock(side_effect=lambda serial, name, default: default)
        with mock.patch.object(mppsolar_manager, "save_manifest") as save:
            manager._ensure_manifest_entry("NEWSERIAL")
        self.assertEqual(manager.manifest["NEWSERIAL"], {
            "custom_name": "MPP Solar SERIAL",
            "device_instance": 2,
            "poll_interval": 10,
        })
        save.assert_called_once_with(manager.manifest)

    def test_new_hidraw_is_started_and_backoff_is_honoured(self):
        manager = bare_manager()
        manager._start_device = mock.Mock()
        with mock.patch.object(mppsolar_manager.glob, "glob", return_value=["/dev/hidraw2"]):
            manager.scan()
        manager._start_device.assert_called_once_with("/dev/hidraw2")

        manager._start_device.reset_mock()
        manager.failed["/dev/hidraw2"] = (time.monotonic() + 30, 2)
        with mock.patch.object(mppsolar_manager.glob, "glob", return_value=["/dev/hidraw2"]):
            manager.scan()
        manager._start_device.assert_not_called()

    def test_removal_stops_both_children(self):
        manager = bare_manager()
        backend = FakeProcess()
        driver = FakeProcess()
        log = FakeLog()
        manager.records["/dev/hidraw0"] = mppsolar_manager.DeviceRecord(
            "/dev/hidraw0", "SERIAL1", 8400, backend, driver, log, time.monotonic()
        )
        manager._start_device = mock.Mock()
        with mock.patch.object(mppsolar_manager.glob, "glob", return_value=[]):
            manager.scan()
        self.assertTrue(backend.terminated)
        self.assertTrue(driver.terminated)
        self.assertTrue(log.closed)
        self.assertEqual(manager.records, {})

    def test_hidraw_permutation_removes_old_and_starts_new_in_same_scan(self):
        manager = bare_manager()
        manager.records["/dev/hidraw0"] = mppsolar_manager.DeviceRecord(
            "/dev/hidraw0", "SERIAL1", 8400,
            FakeProcess(), FakeProcess(), FakeLog(), time.monotonic(),
        )
        manager._start_device = mock.Mock()
        with mock.patch.object(mppsolar_manager.glob, "glob", return_value=["/dev/hidraw3"]):
            manager.scan()
        self.assertNotIn("/dev/hidraw0", manager.records)
        manager._start_device.assert_called_once_with("/dev/hidraw3")

    def test_shutdown_stops_all_drivers_before_backends(self):
        manager = bare_manager()
        records = []
        for index in range(2):
            record = mppsolar_manager.DeviceRecord(
                f"/dev/hidraw{index}", f"SERIAL{index}", 8400 + index,
                FakeProcess(), FakeProcess(), FakeLog(), time.monotonic(),
            )
            manager.records[record.path] = record
            records.append(record)
        manager._terminate_processes = mock.Mock()

        manager.shutdown()

        self.assertEqual(manager.records, {})
        first_processes = list(manager._terminate_processes.call_args_list[0].args[0])
        second_processes = list(manager._terminate_processes.call_args_list[1].args[0])
        self.assertEqual(first_processes, [record.driver for record in records])
        self.assertEqual(second_processes, [record.backend for record in records])
        self.assertTrue(all(record.log_handle.closed for record in records))


class ManagerTopologyTests(unittest.TestCase):
    def test_each_backend_gets_its_parallel_group_count(self):
        responses = {
            (8400, 0): {
                "result": "ok", "data": {
                    "parallel_connection_status": "Existent", "serial_number": "A1"
                },
            },
            (8400, 1): {
                "result": "ok", "data": {
                    "parallel_connection_status": "Existent", "serial_number": "A2"
                },
            },
            (8401, 0): {
                "result": "ok", "data": {
                    "parallel_connection_status": "Existent", "serial_number": "B1"
                },
            },
        }

        def command(port, name, params=()):
            self.assertEqual(name, "get-p-rated")
            try:
                return responses[(port, params[0])]
            except KeyError as exc:
                raise RuntimeError("missing parallel member") from exc

        with mock.patch.object(mppsolar_manager, "exec_command", side_effect=command):
            counts = mppsolar_manager.MppSolarManager._calculate_topology(
                (("A1", 8400), ("B1", 8401))
            )

        self.assertEqual(counts, {"A1": 2, "B1": 1})

    def test_failed_queries_fall_back_to_active_device_count(self):
        with mock.patch.object(
            mppsolar_manager, "exec_command", side_effect=RuntimeError("timeout")
        ):
            counts = mppsolar_manager.MppSolarManager._calculate_topology(
                (("A1", 8400), ("A2", 8401))
            )
        self.assertEqual(counts, {"A1": 2, "A2": 2})


if __name__ == "__main__":
    unittest.main()
