#!/usr/bin/env python3
"""Idempotently extend Venus Classic UI's PageDeviceInfo.qml."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_TARGET = Path("/opt/victronenergy/gui/qml/PageDeviceInfo.qml")
BEGIN = "\t\t/* DBUS_MPP_P18_SETTINGS_BEGIN */"
END = "\t\t/* DBUS_MPP_P18_SETTINGS_END */"
SNIPPET = r'''
		/* DBUS_MPP_P18_SETTINGS_BEGIN */
		MbSpinBox {
			description: qsTr("MPP Solar VRM instance (restarts device)")
			item {
				bind: Utils.path(root.bindPrefix, "/Settings/DeviceInstance")
				decimals: 0
				step: 1
				invalidate: false
			}
			show: item.valid
		}

		MbSpinBox {
			description: qsTr("MPP Solar polling interval")
			item {
				bind: Utils.path(root.bindPrefix, "/Settings/PollInterval")
				decimals: 0
				step: 1
				unit: "s"
				invalidate: false
			}
			show: item.valid
		}
		/* DBUS_MPP_P18_SETTINGS_END */
'''.rstrip()


def balanced_qml(text):
    depth = 0
    quote = None
    escaped = False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and quote is None


def validate(path):
    text = path.read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1 or not balanced_qml(text):
        raise RuntimeError("patched QML failed structural validation")
    qmllint = shutil.which("qmllint")
    if qmllint:
        subprocess.run([qmllint, str(path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def patch_target(target):
    target = Path(target)
    backup = target.with_name(target.name + ".dbus-mpp-p18.orig")
    if not target.exists():
        raise FileNotFoundError(target)
    original = target.read_text(encoding="utf-8")
    if BEGIN in original or END in original:
        if original.count(BEGIN) == 1 and original.count(END) == 1:
            validate(target)
            print(f"Classic UI extension already applied: {target}")
            return False
        raise RuntimeError("partial dbus-mpp-p18 QML marker detected")
    anchor = "\t}\n}"
    position = original.rfind(anchor)
    if position < 0:
        raise RuntimeError("PageDeviceInfo.qml model closing anchor not found")
    patched = original[:position] + SNIPPET + "\n\n" + original[position:]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.write_text(patched, encoding="utf-8")
        shutil.copymode(target, temp)
        validate(temp)
        os.replace(temp, target)
    except Exception:
        if temp.exists():
            temp.unlink()
        if not balanced_qml(target.read_text(encoding="utf-8")):
            shutil.copy2(backup, target)
        raise
    print(f"Classic UI extension applied: {target} (backup: {backup})")
    return True


def install_rc_local(script_path):
    rc_local = Path(os.environ.get("DBUS_MPP_RC_LOCAL", "/data/rc.local"))
    begin = "# DBUS_MPP_P18_GUI_BEGIN"
    end = "# DBUS_MPP_P18_GUI_END"
    block = (
        f"{begin}\n"
        f"python3 '{script_path}' >/var/log/dbus-mppsolar-gui-patch.log 2>&1 || true\n"
        f"{end}\n"
    )
    text = rc_local.read_text(encoding="utf-8") if rc_local.exists() else "#!/bin/sh\n"
    if begin in text or end in text:
        if text.count(begin) != 1 or text.count(end) != 1:
            raise RuntimeError("partial dbus-mpp-p18 rc.local marker detected")
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        text = text[:start] + block.rstrip() + text[finish:]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        exit_marker = "exit 0"
        exit_position = text.rfind(exit_marker)
        if exit_position >= 0:
            text = text[:exit_position] + block + text[exit_position:]
        else:
            text += block
    rc_local.parent.mkdir(parents=True, exist_ok=True)
    rc_local.write_text(text, encoding="utf-8")
    rc_local.chmod(rc_local.stat().st_mode | 0o111)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=str(DEFAULT_TARGET))
    parser.add_argument("--no-rc-local", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    target = Path(args.target)
    backup = target.with_name(target.name + ".dbus-mpp-p18.orig")
    if args.restore:
        if not backup.exists():
            raise FileNotFoundError(backup)
        shutil.copy2(backup, target)
        print(f"Restored {target} from {backup}")
        return 0
    patched = patch_target(target)
    try:
        if not args.no_rc_local:
            install_rc_local(Path(__file__).resolve())
    except Exception:
        if patched and backup.exists():
            shutil.copy2(backup, target)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
