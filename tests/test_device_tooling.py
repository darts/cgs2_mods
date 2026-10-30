"""Offline safety checks: never execute ADB or touch the connected device."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


device = module("device")
services = module("patch_services")


class RemoteResultTests(unittest.TestCase):
    def stub(self, output):
        adb = object.__new__(device.ADB)
        adb.run = lambda *arguments: output
        return adb

    def test_old_adbd_exit_status_is_checked(self):
        self.assertEqual(self.stub("ok\n\n__CGS2_RC_0__\n").shell("true"), "ok\n")
        with self.assertRaises(RuntimeError):
            self.stub("write failed\n__CGS2_RC_1__\n").shell("false")
        with self.assertRaises(RuntimeError):
            self.stub("disconnected midway").shell("true")

    def test_shell_arguments_are_quoted(self):
        adb = object.__new__(device.ADB)
        calls = []
        adb.run = lambda *arguments: calls.append(arguments) or "\n__CGS2_RC_0__\n"
        adb.shell("printf", "%s", "a; touch /tmp/should-not-exist")
        self.assertIn("'a; touch /tmp/should-not-exist'", calls[0][1])


class PathTests(unittest.TestCase):
    def test_rejects_injection_escape_and_usb_modification(self):
        for name in ("/etc/passwd", "etc/init.d/../../passwd", "etc/init.d/S50usbdevice",
                     "etc/init.d/rcS;reboot", "etc/cgs2-privacy/x\nreboot", "usr/local/sbin/../bin/x"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                device.safe_path(name)
        self.assertEqual(device.safe_path("data/etc/cgs2-mqtt.ini"), "data/etc/cgs2-mqtt.ini")

    def test_version_metadata_exception_does_not_admit_identity_files(self):
        self.assertEqual(device.safe_path("qingping/etc/os-release"), "qingping/etc/os-release")
        for name in ("qingping/etc/temp_fix.ini", "qingping/etc/miio/device.conf",
                     "qingping/etc/os-release.bak", "oem/.usb_config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                device.safe_path(name)

    def test_rejects_archive_traversal_and_duplicate_paths(self):
        for names in (("../../escape",), ("/etc/passwd",), ("etc/a", "./etc/a")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "bad.tar"
                with tarfile.open(path, "w") as archive:
                    for name in names:
                        info = tarfile.TarInfo(name)
                        archive.addfile(info, io.BytesIO())
                with tarfile.open(path) as archive, self.assertRaises(ValueError):
                    device.tar_index(archive)


class BootTests(unittest.TestCase):
    def test_wifi_reconnect_honors_disabled_setting(self):
        for disabled in (True, False):
            with self.subTest(disabled=disabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                setting = root / "setting.ini"
                setting.write_text("wifi_disabled=" + str(disabled).lower() + "\n")
                marker = root / "network-started"
                original = '#!/bin/sh\ncase "$1" in\n  start)\n    touch ' + str(marker) + '\n;;\nesac\n'
                patched = services.patch_wifi_reconnect(original).replace("/data/etc/setting.ini", str(setting))
                script = root / "wifi.sh"
                script.write_text(patched)
                subprocess.run(["/bin/sh", str(script), "start"], check=True)
                self.assertEqual(marker.exists(), not disabled)

    def run_boot(self, filter_success):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            init = root / "init.d"
            init.mkdir()
            log = root / "boot.log"
            network_log = root / "network.log"
            mocks = root / "mocks"
            mocks.mkdir()
            ifconfig = mocks / "ifconfig"
            ifconfig.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + str(network_log) + '\n[ "$1" = lo ]\n')
            ifconfig.chmod(0o755)
            names = ("S01logging", "S02privacy", "S10async_commit.sh", "S10udev", "S20urandom",
                     "S21mountall.sh", "S36load_wifi_modules", "S40network", "S41dhcpcd", "S49ntp",
                     "S50launcher", "S50usbdevice", "S51n4", "S80wifireconnect")
            for name in names:
                script = init / name
                result = 1 if name == "S02privacy" and not filter_success else 0
                script.write_text(f"#!/bin/sh\necho {name} >> '{log}'\nexit {result}\n")
                script.chmod(0o755)
            original = "#!/bin/sh\nfor i in /etc/init.d/S??* ;do\n[ ! -f \"$i\" ] && continue\n\"$i\" start\ndone\n"
            patched = services.patch_rcs(original).replace("/etc/init.d", str(init))
            script = root / "rcS"
            script.write_text(patched)
            environment = dict(os.environ, PATH=str(mocks) + os.pathsep + os.environ["PATH"])
            subprocess.run(["/bin/sh", str(script)], check=True, capture_output=True, env=environment)
            self.network_commands = network_log.read_text().splitlines() if network_log.exists() else []
            return log.read_text().splitlines()

    def test_failure_keeps_usb_and_skips_every_network_and_app_script(self):
        self.assertEqual(self.run_boot(False), ["S02privacy", "S01logging", "S10async_commit.sh", "S10udev",
                                               "S20urandom", "S21mountall.sh", "S50usbdevice"])

    def test_rescue_initializes_only_loopback_without_normal_network_startup(self):
        results = self.run_boot(False)
        self.assertEqual(self.network_commands, ["lo 127.0.0.1 netmask 255.0.0.0 up"])
        self.assertNotIn("S40network", results)
        self.assertNotIn("S41dhcpcd", results)
        self.assertIn("S50usbdevice", results)

    def test_filter_runs_once_before_network_when_successful(self):
        results = self.run_boot(True)
        self.assertEqual(results[0], "S02privacy")
        self.assertEqual(results.count("S02privacy"), 1)
        self.assertIn("S50usbdevice", results)
        self.assertIn("S50launcher", results)
        self.assertIn("S40network", results)
        self.assertEqual(self.network_commands, [])


class RollbackShutdownTests(unittest.TestCase):
    def run_shutdown(self, mode, *, helper=True, runtime=True, processes=None):
        """Execute the real shell guard with no host process/network access."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mocks = root / "mocks"
            mocks.mkdir()
            state = {"mode": mode, "processes": ["supervise", "daemon"] if processes is None else processes}
            (root / "state.json").write_text(json.dumps(state))
            mock = root / "mock.py"
            mock.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["MOCK_ROOT"])
state = json.loads((root / "state.json").read_text())
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "pgrep":
    assert args == ["-x", "cgs2-services"]
    sys.exit(0 if state["processes"] else 1)
elif name == "sleep":
    assert args == ["1"]
elif name == "killall":
    assert args == ["cgs2-services"]
    if state["mode"] != "stubborn": state["processes"] = []
elif name == "timeout":
    assert args[0] in ["25", "30"]
    assert args[1] == str(root / "mocks" / "cgs2-services")
    sys.exit(subprocess.run(args[1:]).returncode)
elif name == "cgs2-services":
    assert args in [["stop"], ["runtime-reset"]]
    if args == ["stop"]:
        if state["mode"] in ["graceful", "reset-fails"]:
            state["processes"] = []
        else: sys.exit(1)
    else:
        assert not state["processes"], "permissions reset before worker/supervisor exit"
        if state["mode"] == "reset-fails": sys.exit(1)
        (root / "policy-reset").touch()
else:
    raise AssertionError("Unexpected command: " + name)
(root / "state.json").write_text(json.dumps(state))
''')
            mock.chmod(0o755)
            for name in ("pgrep", "killall", "timeout", "sleep") + (("cgs2-services",) if helper else ()):
                (mocks / name).symlink_to(mock)
            if runtime:
                (root / "runtime").mkdir()
            script = device.STOP_INTERNET_SERVICES.replace("/run/cgs2-services", str(root / "runtime"))
            script = script.replace("/usr/local/sbin/cgs2-services", str(mocks / "cgs2-services"))
            script += "printf restored > " + shlex.quote(str(root / "files-restored")) + "\n"
            result = subprocess.run(["/bin/sh", "-eu", "-c", script], capture_output=True, text=True,
                                    env=dict(os.environ, PATH=str(mocks), MOCK_ROOT=str(root)))
            commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
            return result, commands, (root / "files-restored").exists(), (root / "policy-reset").exists()

    def test_graceful_stop_resets_permissions_before_restore(self):
        result, commands, restored, reset = self.run_shutdown("graceful")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(restored and reset)
        self.assertIn(["cgs2-services", "stop"], commands)
        self.assertNotIn(["killall", "cgs2-services"], commands)
        self.assertLess(commands.index(["cgs2-services", "stop"]),
                        commands.index(["cgs2-services", "runtime-reset"]))

    def test_stale_supervisor_is_terminated_before_restore(self):
        result, commands, restored, reset = self.run_shutdown("control-unavailable", processes=["supervise"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(restored and reset)
        self.assertLess(commands.index(["killall", "cgs2-services"]),
                        commands.index(["cgs2-services", "runtime-reset"]))

    def test_surviving_service_blocks_file_restore(self):
        result, commands, restored, reset = self.run_shutdown("stubborn")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not stop", result.stderr)
        self.assertFalse(restored or reset)
        self.assertEqual(commands.count(["sleep", "1"]), 10)

    def test_permission_reset_failure_blocks_file_restore(self):
        result, _, restored, reset = self.run_shutdown("reset-fails")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not clear transient", result.stderr)
        self.assertFalse(restored or reset)

    def test_older_install_without_services_can_be_restored(self):
        result, commands, restored, reset = self.run_shutdown("unused", helper=False, runtime=False, processes=[])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(restored)
        self.assertFalse(reset)
        self.assertTrue(all(command[0] == "pgrep" for command in commands))

    def test_missing_helper_with_runtime_state_blocks_restore(self):
        result, _, restored, reset = self.run_shutdown("unused", helper=False, processes=[])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helper is missing", result.stderr)
        self.assertFalse(restored or reset)


class BundleTests(unittest.TestCase):
    def test_verify_keeps_mutable_settings_and_checks_allowlist_syntax(self):
        old_load = device.load_bundle
        calls = []
        expected_hash = "1" * 64
        manifest = {"changes": [{"path": path, "sha256": expected_hash} for path in
                                ("etc/init.d/rcS", "data/etc/cgs2-mqtt.ini", "etc/cgs2-privacy/allowlist.conf")],
                    "removals": []}
        adb = object.__new__(device.ADB)
        def shell(*arguments):
            calls.append(arguments)
            return expected_hash + " /etc/init.d/rcS\n" if arguments[0] == "sha256sum" else ""
        adb.shell = shell
        try:
            device.load_bundle = lambda *arguments: (manifest, Path("unused"))
            device.verify(adb, Path("unused"), Path("unused"))
        finally:
            device.load_bundle = old_load
        self.assertEqual([call for call in calls if call[0] == "sha256sum"], [("sha256sum", "/etc/init.d/rcS")])
        self.assertIn(("/usr/local/sbin/cgs2-filter", "dry-run", "--config", "/etc/cgs2-privacy/allowlist.conf"), calls)

    def test_tampered_preimage_rejected_before_device_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup, payload, bundle = root / "backup", root / "payload", root / "bundle"
            backup.mkdir()
            archive = backup / "original.tar"
            with tarfile.open(archive, "w") as packed:
                original = b"stock boot"
                info = tarfile.TarInfo("etc/init.d/rcS")
                info.size = len(original)
                packed.addfile(info, io.BytesIO(original))
            (backup / "backup.json").write_text(json.dumps({"format": 1, "serial": "test", "sha256": device.digest(archive)}))
            for name in device.REQUIRED_FILES:
                path = payload / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"replacement")
            device.prepare(backup, payload, bundle, [])
            adb = object.__new__(device.ADB)
            adb.serial = "test"
            device.load_bundle(bundle, backup, adb)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            change = next(item for item in manifest["changes"] if item["path"] == "etc/init.d/rcS")
            change["original"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "preimage"):
                device.load_bundle(bundle, backup, adb)


if __name__ == "__main__":
    unittest.main()
