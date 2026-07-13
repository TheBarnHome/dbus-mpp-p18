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
    manager._update_service = mock.Mock()
    manager._notify_topology_change = mock.Mock()
    return manager


class ManagerScanTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
