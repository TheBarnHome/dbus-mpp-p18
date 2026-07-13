import importlib.util
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


sys.modules.setdefault("dbus", types.ModuleType("dbus"))
SPEC = importlib.util.spec_from_file_location(
    "verify_runtime", Path(__file__).parents[1] / "verify-runtime.py"
)
verify_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_runtime)


class RuntimeReadTests(unittest.TestCase):
    def test_subprocess_value_is_decoded(self):
        completed = subprocess.CompletedProcess([], 0, stdout='"Master"\n', stderr="")
        with mock.patch.object(verify_runtime.subprocess, "run", return_value=completed):
            self.assertEqual(
                verify_runtime.read_value(None, "com.example", "/CustomName"),
                "Master",
            )

    def test_subprocess_timeout_is_bounded(self):
        with mock.patch.object(
            verify_runtime.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["python3"], 12),
        ):
            with self.assertRaises(verify_runtime.ReadTimeout):
                verify_runtime.read_value(None, "com.example", "/Value")


if __name__ == "__main__":
    unittest.main()
