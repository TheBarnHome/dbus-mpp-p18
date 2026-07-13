import importlib.util
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "gui_extension", Path(__file__).parents[1] / "install-gui-extension.py"
)
gui_extension = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gui_extension)


FIXTURE = """import QtQuick 2
import com.victron.velib 1.0
MbPage {
\tmodel: VisibleItemModel {
\t\tMbItemValue { description: \"VRM instance\" }
\t}
}
"""


class ClassicGuiPatchTests(unittest.TestCase):
    def test_patch_is_idempotent_and_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "PageDeviceInfo.qml"
            target.write_text(FIXTURE, encoding="utf-8")
            self.assertTrue(gui_extension.patch_target(target))
            first = target.read_text(encoding="utf-8")
            self.assertIn(gui_extension.BEGIN, first)
            self.assertIn("/Settings/DeviceInstance", first)
            self.assertIn("/Settings/PollInterval", first)
            self.assertFalse(gui_extension.patch_target(target))
            self.assertEqual(target.read_text(encoding="utf-8"), first)
            backup = target.with_name(target.name + ".dbus-mpp-p18.orig")
            self.assertEqual(backup.read_text(encoding="utf-8"), FIXTURE)

    def test_structural_validator_rejects_unbalanced_qml(self):
        self.assertFalse(gui_extension.balanced_qml("Item { Item { }"))

    def test_rc_local_block_is_idempotent_and_precedes_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_local = Path(directory) / "rc.local"
            rc_local.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"DBUS_MPP_RC_LOCAL": str(rc_local)}):
                gui_extension.install_rc_local(Path("/data/test/install-gui-extension.py"))
                gui_extension.install_rc_local(Path("/data/test/install-gui-extension.py"))
            text = rc_local.read_text(encoding="utf-8")
            self.assertEqual(text.count("DBUS_MPP_P18_GUI_BEGIN"), 1)
            self.assertIn("--restart-gui", text)
            self.assertLess(text.index("DBUS_MPP_P18_GUI_BEGIN"), text.index("exit 0"))


if __name__ == "__main__":
    unittest.main()
