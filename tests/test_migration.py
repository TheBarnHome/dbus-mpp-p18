import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


fake_inverterd = types.ModuleType("inverterd")
fake_inverterd.Client = object
fake_inverterd.Format = types.SimpleNamespace(JSON="json")
sys.modules.setdefault("inverterd", fake_inverterd)

SPEC = importlib.util.spec_from_file_location(
    "migrate_config", Path(__file__).parents[1] / "migrate-config.py"
)
migrate_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrate_config)


class MigrationTests(unittest.TestCase):
    def test_exact_current_installation_mapping(self):
        legacy = {
            "/dev/hidraw1": {
                "productname": "Master",
                "deviceinstance": 1,
                "numberOfChargers": 2,
                "updateInterval": 10000,
            },
            "/dev/hidraw0": {
                "productname": "Slave_1",
                "deviceinstance": 2,
                "numberOfChargers": 2,
                "updateInterval": 10000,
            },
        }
        serial_by_port = {8306: "96132301100087", 8307: "96132301100091"}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(legacy), encoding="utf-8")

            def response(port, command):
                self.assertEqual(command, "get-serial-number")
                return {"result": "ok", "data": {"sn": serial_by_port[port]}}

            with mock.patch.object(migrate_config, "command", side_effect=response):
                result = migrate_config.migrate(config, dry_run=True)

        self.assertEqual(result, {
            "96132301100087": {
                "custom_name": "Master",
                "device_instance": 1,
                "poll_interval": 10,
            },
            "96132301100091": {
                "custom_name": "Slave_1",
                "device_instance": 2,
                "poll_interval": 10,
            },
        })

    def test_duplicate_instance_is_rejected_before_writes(self):
        legacy = {
            "/dev/hidraw0": {"deviceinstance": 1},
            "/dev/hidraw1": {"deviceinstance": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps(legacy), encoding="utf-8")
            with mock.patch.object(
                migrate_config,
                "command",
                return_value={"result": "ok", "data": {"sn": "SERIAL1"}},
            ):
                with self.assertRaisesRegex(RuntimeError, "duplicate Device Instance"):
                    migrate_config.migrate(config, dry_run=True)


if __name__ == "__main__":
    unittest.main()
