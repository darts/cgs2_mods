import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_tools


class SetupToolsTests(unittest.TestCase):
    def archive(self, folder, name="platform-tools/adb", mode=0o100755):
        archive = folder / "tools.zip"
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(info, b"fixture")
        return archive

    def test_rejects_escaping_entries_and_symlinks(self):
        for name, mode in (("../escape", 0o100644), ("platform-tools/../escape", 0o100644), ("platform-tools\\escape", 0o100644), ("platform-tools/adb", 0o120777), ("other/adb", 0o100755)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    setup_tools.extract_tools(self.archive(folder, name, mode), folder / "extracted")

    def test_extracts_only_below_expected_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            setup_tools.extract_tools(self.archive(folder), folder / "extracted")
            self.assertEqual((folder / "extracted/platform-tools/adb").read_bytes(), b"fixture")

    def test_checksum_failure_installs_nothing_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "config").mkdir()
            config = {"platform_tools": {"version": "fixture", "license": "https://example.test/terms", "archives": {"linux": {"url": "https://dl.google.com/android/repository/fixture.zip", "size": 4, "sha256": "0"*64}}}}
            (folder / "config/tool-downloads.json").write_text(json.dumps(config), encoding="utf-8")
            with patch.object(setup_tools, "find_adb", return_value=None), patch.object(setup_tools.platform, "system", return_value="Linux"), patch.object(setup_tools.platform, "machine", return_value="x86_64"), patch.object(setup_tools.urllib.request, "urlopen", return_value=io.BytesIO(b"data")), patch.object(setup_tools.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    setup_tools.install_adb(folder)
                run.assert_not_called()
            self.assertFalse((folder / ".tools/platform-tools").exists())

    def test_existing_adb_is_reused_without_network_or_device_command(self):
        with patch.object(setup_tools, "find_adb", return_value="adb"), patch.object(setup_tools.urllib.request, "urlopen", side_effect=AssertionError("no download")), patch.object(setup_tools.subprocess, "run") as run:
            self.assertEqual(setup_tools.install_adb(Path("unused")), "adb")
        run.assert_called_once_with(["adb", "version"], check=True)
