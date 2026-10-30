"""Host checks for the About page rows and the installation record, with synthetic stock text only.

The device smoke test (scripts/build_screensaver_ui_smoke.py, AboutMain.qml) renders the real page.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import device
import patch_app

ABOUT = '''import QtQuick 2.0
import QtQuick.Controls 2.0
import QtQuick.Layouts 1.3
import Qing.Controls 1.0
import Qing.Frames 1.0
import "../Developer"

QPage {
    id: control
    title: qsTr("About")
    property int count: 0
    property int clicedIndex: -1
    initialItem: QChildPage {
        ListView {
            id: listView
            model: global.aboutList
        }
    }
}
'''
TIME = re.compile(r"'\d{4}-\d{2}-\d{2} \d{2}:\d{2}'")


def patched(text=ABOUT):
    path = "/Modules/About/AboutPage.qml"
    return patch_app.about_rows({path: {"payload": text.encode()}})[path].decode()


class AboutRowsTests(unittest.TestCase):
    def test_rows_follow_the_stock_rows(self):
        text = patched()
        self.assertIn("import QtQuick.Layouts 1.3\nimport Qt.labs.settings 1.0\n", text)
        self.assertIn("model: control.cgs2AboutList()", text)
        self.assertNotIn("model: global.aboutList", text)
        self.assertIn(f'fileName: "{patch_app.RELEASE_RECORD}"', text)
        # The hidden developer and test-mode taps use the indexes of the stock rows.
        self.assertLess(text.index("rows.push(stock[i])"), text.index(f'{{key: "Mod Version", value: "{patch_app.MOD_VERSION}"}}'))
        self.assertLess(text.index('"Mod Version"'), text.index('"Mod Installed"'))
        self.assertEqual(patch_app.closing_brace(text, text.index("QPage {") + 6), len(text.rstrip()))

    def test_an_unexpected_stock_page_is_refused(self):
        for text in (ABOUT.replace("model: global.aboutList", "model: aboutModel"),
                     ABOUT.replace("import QtQuick.Layouts 1.3\n", "")):
            with self.subTest(), self.assertRaisesRegex(ValueError, "expected one QML anchor"):
                patched(text)

    def test_settings_object_declares_no_properties(self):
        # A declared property would write its default back to the record.
        block = patch_app.ABOUT_ROWS[:patch_app.ABOUT_ROWS.index("}")]
        self.assertNotIn("property", block)


class ReleaseRecordTests(unittest.TestCase):
    def test_record_is_a_qt_settings_file_written_through_a_synced_rename(self):
        if os.name != "posix":
            self.skipTest("The record script requires a POSIX shell")
        script = device.release_record_script("2026-10-23 14:05")
        lines = script.splitlines()
        self.assertEqual(lines[lines.index(next(line for line in lines if line.startswith("mv -f"))) - 1], "sync")
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "cgs2-release.ini"
            subprocess.run(["/bin/sh", "-eu", "-c", script.replace(patch_app.RELEASE_RECORD, str(record))], check=True)
            self.assertEqual(record.read_text(), '[release]\ninstalled="2026-10-23 14:05"\n')
            self.assertEqual(record.stat().st_mode & 0o777, 0o644)
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["cgs2-release.ini"])

    def test_the_time_comes_from_this_computer_and_is_checked(self):
        self.assertRegex(device.release_record_script(), TIME)
        for value in ("2026-10-23 14:05'; reboot; '", "23.10.2026 14:05", "2026-10-23T14:05"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid installation time"):
                device.release_record_script(value)



if __name__ == "__main__":
    unittest.main()
