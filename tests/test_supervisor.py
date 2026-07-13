import importlib.util
import sys
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "mppsolar_supervisor", Path(__file__).parents[1] / "mppsolar-supervisor.py"
)
mppsolar_supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mppsolar_supervisor
SPEC.loader.exec_module(mppsolar_supervisor)


class SupervisorTests(unittest.TestCase):
    def test_restart_backoff_is_bounded(self):
        self.assertEqual(mppsolar_supervisor.restart_delay(1), 2)
        self.assertEqual(mppsolar_supervisor.restart_delay(2), 4)
        self.assertEqual(mppsolar_supervisor.restart_delay(20), 60)


if __name__ == "__main__":
    unittest.main()
