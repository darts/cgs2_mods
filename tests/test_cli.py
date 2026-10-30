"""Exercise the user workflow without permitting any real device operation."""
import argparse
import contextlib
import io
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qingping


class HumanCLITests(unittest.TestCase):
    def setUp(self):
        # Saved folders go to <ROOT>/.private/workflow.json; never touch the real one.
        temporary = tempfile.TemporaryDirectory(prefix="cgs2-cli-test-")
        self.addCleanup(temporary.cleanup)
        context = patch.object(qingping, "ROOT", Path(temporary.name))
        context.start(); self.addCleanup(context.stop)

    def test_setup_passes_explicit_adb_without_device_access(self):
        args = qingping.parser().parse_args(["setup", "--adb", "custom/adb"])
        with patch.object(qingping, "install_adb") as setup, patch.object(qingping, "doctor", return_value=0), patch.object(qingping, "adb_for", side_effect=AssertionError("no device")):
            self.assertEqual(qingping.dispatch(args), 0)
        setup.assert_called_once_with(qingping.ROOT, "custom/adb")

    def test_connection_options_work_before_and_after_command(self):
        for argv in (("--adb", "C:/Tool Folder/adb.exe", "doctor"), ("doctor", "--adb", "C:/Tool Folder/adb.exe")):
            args = qingping.parser().parse_args(argv)
            self.assertEqual(args.adb, "C:/Tool Folder/adb.exe")
            self.assertIsNone(args.serial)

    def test_prepare_never_accesses_device_and_preserves_space_paths(self):
        args = qingping.parser().parse_args(["prepare", "--backup", "backups/my device", "--output", "private/release one"])
        with patch.object(qingping, "adb_for", side_effect=AssertionError("no device")), patch.object(qingping, "run_script") as run, patch.object(qingping, "remember"):
            qingping.dispatch(args)
        self.assertEqual(run.call_args.args, ("prepare.py", "--backup", Path("backups/my device"), "--output", Path("private/release one")))

    def test_noninteractive_install_refuses_without_explicit_yes(self):
        with patch.object(sys.stdin, "isatty", return_value=False), self.assertRaisesRegex(ValueError, "Examine the prepared update"):
            qingping.confirm_install(argparse.Namespace(yes=False), {"changes": [{"path": "etc/init.d/rcS"}]}, Path("bundle"))

    def test_cancelled_stock_install_cannot_mutate_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "manifest.json").write_text(json.dumps({"changes": []}), encoding="utf-8")
            args = qingping.parser().parse_args(["install", "--backup", "backup", "--bundle", str(folder)])
            with patch.object(qingping, "adb_for", return_value=Mock()), patch.object(qingping.device, "load_bundle"), patch.object(qingping, "confirm_install", side_effect=ValueError("cancelled")), patch.object(qingping.device, "install") as install:
                with self.assertRaisesRegex(ValueError, "cancelled"):
                    qingping.dispatch(args)
                install.assert_not_called()

    def test_stock_install_initializes_only_after_success_and_never_reboots(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "manifest.json").write_text(json.dumps({"changes": []}), encoding="utf-8")
            args = qingping.parser().parse_args(["install", "--backup", "backup", "--bundle", str(folder), "--yes"])
            adb = Mock(serial="fixture")
            events = []
            adb.shell.side_effect = lambda *items: events.append(items)
            with patch.object(qingping, "adb_for", return_value=adb), patch.object(qingping.device, "load_bundle"), patch.object(qingping.device, "verify"), patch.object(qingping.device, "install", side_effect=lambda *a: events.append(("installed",))):
                qingping.dispatch(args)
            self.assertEqual(events, [("installed",), ("/usr/local/sbin/cgs2-mqtt-config", "initialize"), ("/usr/local/sbin/cgs2-services", "initialize")])

    def test_update_installs_only_a_changed_app_and_refuses_unknown_helpers(self):
        import install_services as updater
        current = updater.current_helpers()
        cases = {"changed": (current, False), "current": (current, True), "unknown": ({name: "f" * 64 for name in current}, False)}
        for case, (live, current_app) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                adb = Mock()
                adb.shell.side_effect = lambda *command: live["/".join(command[1].split("/")[1:])] + "  " + command[1]
                args = qingping.parser().parse_args(["update", "--backup", "backup", "--installed", "installed", "--yes"])
                with patch.object(qingping, "adb_for", return_value=adb), patch.object(qingping, "run_script") as build, \
                     patch.object(qingping, "private_output", return_value=Path(temporary) / "runtime"), \
                     patch.object(qingping, "remember") as remember, \
                     patch.object(qingping.recovery, "present", return_value=False), \
                     patch.object(updater, "prepare_app_update", side_effect=ValueError("identical; no update is needed") if current_app else None,
                                  return_value={"kind": "runtime-update"}) as app, \
                     patch.object(updater, "install_app_update") as install_app, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    if case == "unknown":
                        with self.assertRaisesRegex(ValueError, "not from a known release"):
                            qingping.dispatch(args)
                        build.assert_not_called(); app.assert_not_called(); install_app.assert_not_called()
                        continue
                    qingping.dispatch(args)
                build.assert_called_once_with("build.py", "services")
                app.assert_called_once_with(Path("backup"), Path(temporary) / "runtime", qingping.ROOT, Path("installed/manifest.json"),
                                            controller=qingping.ROOT / "build/cgs2-services")
                if current_app:
                    self.assertIn("up to date", output.getvalue())
                    install_app.assert_not_called(); remember.assert_not_called()
                else:
                    install_app.assert_called_once()
                    remember.assert_called_once_with("installed", Path(temporary) / "runtime", adb.serial)

    def test_no_input_displays_help_without_device_access(self):
        with patch.object(sys.stdin, "isatty", return_value=False), patch.object(qingping, "adb_for", side_effect=AssertionError("no device")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(qingping.menu(argparse.Namespace()), 0)
            self.assertIn("usage:", out.getvalue())


class MenuTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cgs2-menu-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = patch.object(qingping, "ROOT", self.root)
        context.start(); self.addCleanup(context.stop)
        self.args = argparse.Namespace(adb="adb", serial=None)

    def folder(self, name, serial, record="backup.json"):
        path = self.root / ".private" / name
        path.mkdir(parents=True)
        (path / record).write_text(json.dumps({"serial": serial}), encoding="utf-8")
        return path

    def status(self, **values):
        base = {"adb": "adb", "connection": "ready", "serial": "MON-1", "firmware": qingping.SUPPORTED_FIRMWARE,
                "installed": False, "backup": None, "bundle": None, "update": None}
        return {**base, **values}

    def test_next_step_follows_the_owner_journey(self):
        expected = [
            (self.status(adb=None), "Select 6"),
            (self.status(connection="none"), "Adbd Debugging is on"),
            (self.status(connection="unauthorized"), "allow USB debugging"),
            (self.status(connection="several"), "Disconnect all other"),
            (self.status(connection="no root"), "no root access"),
            (self.status(firmware="4.3.9_0137"), "needs 4.5.6_0181"),
            (self.status(), "Select 1 to install"),
            (self.status(backup=self.root), "Select 1 to install"),
            (self.status(backup=self.root, installed=True), "Select 2 to examine"),
        ]
        for status, text in expected:
            with self.subTest(text=text):
                self.assertIn(text, qingping.next_step(status))

    def test_status_reads_the_connection_and_only_this_monitors_records(self):
        mine = self.folder("backup-mine", "MON-1")
        other = self.folder("backup-other", "MON-2")
        bundle = self.folder("release/bundle", "MON-2", "manifest.json")
        qingping.remember("backup", mine, "MON-1")
        qingping.remember("bundle", bundle)
        qingping.remember("backup", other, "MON-2")
        listings = {"unauthorized": "List of devices attached\nMON-1\tunauthorized\n",
                    "several": "List of devices attached\nMON-1\tdevice\nMON-2\tdevice\n",
                    "ready": "List of devices attached\nMON-1\tdevice usb:1\n", "none": "List of devices attached\n"}
        for connection, listing in listings.items():
            with self.subTest(connection=connection):
                adb = Mock(serial="MON-1")
                adb.shell.side_effect = lambda *command: ("CLEARGRASS_VERSION=4.5.6_0181\n" if "os-release" in command[-1] else "yes\n")
                with patch.object(qingping, "find_adb", return_value="adb"), \
                     patch.object(qingping.subprocess, "run", return_value=Mock(stdout=listing)), \
                     patch.object(qingping.device, "ADB", return_value=adb):
                    status = qingping.monitor_status(self.args)
                self.assertEqual(status["connection"], connection)
                if connection == "ready":
                    # The other monitor's global bundle record is not used for this monitor.
                    self.assertEqual((status["firmware"], status["installed"], status["backup"], status["bundle"]),
                                     (qingping.SUPPORTED_FIRMWARE, True, mine.resolve(), None))
        with patch.object(qingping, "find_adb", return_value="adb"), \
             patch.object(qingping.subprocess, "run", return_value=Mock(stdout=listings["ready"])), \
             patch.object(qingping.device, "ADB", side_effect=ValueError("CGS2 root USB ADB is required")):
            self.assertEqual(qingping.monitor_status(self.args)["connection"], "no root")

    def test_saved_folders_follow_the_private_folder_to_a_new_release(self):
        backup = self.folder("backup-mine", "MON-1")
        qingping.remember("backup", backup, "MON-1")
        self.assertEqual(qingping.load_state()["monitors"]["MON-1"]["backup"], ".private/backup-mine")
        with tempfile.TemporaryDirectory(prefix="cgs2-new-release-") as directory:
            new_root = Path(directory)
            shutil.copytree(self.root / ".private", new_root / ".private")
            with patch.object(qingping, "ROOT", new_root):
                self.assertEqual(qingping.saved("backup", "MON-1"), (new_root / ".private/backup-mine").resolve())

    def test_install_refuses_unsafe_states_before_any_change(self):
        backup = self.folder("backup", "MON-1")
        cases = {"installed": (self.status(backup=backup, installed=True), "already installed"),
                 "firmware": (self.status(backup=backup, firmware="4.3.9_0137"), "needs 4.5.6_0181"),
                 "monitor": (self.status(connection="none"), "Connect")}
        for name, (status, message) in cases.items():
            with self.subTest(name=name), patch.object(qingping, "run_script") as prepare:
                with self.assertRaisesRegex(ValueError, message):
                    qingping.menu_install(self.args, status)
                prepare.assert_not_called()

    def test_install_needs_the_typed_word_then_restarts_and_examines(self):
        backup = self.folder("backup", "MON-1")
        for word in ("", "INSTALL"):
            with self.subTest(word=word):
                adb = Mock(serial="MON-1")
                manifest = {"changes": [{"path": "usr/local/sbin/cgs2-filter"}]}
                with patch.object(qingping, "run_script") as prepare, patch.object(qingping, "adb_for", return_value=adb), \
                     patch.object(qingping.device, "load_bundle", return_value=(manifest, None)), \
                     patch.object(qingping.device, "install") as install, patch.object(qingping.device, "verify"), \
                     patch.object(qingping, "wait_for_monitor", return_value=adb), \
                     patch.object(qingping, "examine", return_value=True) as examine, \
                     patch("builtins.input", side_effect=[word, ""]), contextlib.redirect_stdout(io.StringIO()) as output:
                    qingping.menu_install(self.args, self.status(backup=backup))
                prepare.assert_called_once()
                self.assertEqual(install.called, word == "INSTALL")
                self.assertEqual(examine.called, word == "INSTALL")
                if word == "INSTALL":
                    self.assertIn("Stop the monitor with its power control", output.getvalue())
                    self.assertIsNotNone(qingping.load_state()["monitors"]["MON-1"].get("installed"))
                else:
                    self.assertIn("did not change", output.getvalue())

    def test_remove_defaults_to_monitor_copy_and_has_one_confirmation(self):
        backup, bundle = self.folder("backup", "MON-1"), self.folder("bundle", "MON-1", "manifest.json")
        for stock in (False, True):
            for word in ("", "REMOVE"):
                with self.subTest(stock=stock,word=word), patch.object(qingping, "adb_for", return_value=Mock(serial="MON-1")), \
                     patch.object(qingping.device, "rollback") as rollback, patch.object(qingping.recovery, "start") as restore, \
                     patch("builtins.input", return_value=word), contextlib.redirect_stdout(io.StringIO()):
                    qingping.menu_remove(self.args, self.status(backup=backup, bundle=bundle, installed=True,stock_copy=stock))
                self.assertEqual(restore.called,word=="REMOVE" and stock)
                self.assertEqual(rollback.called,word=="REMOVE" and not stock)

    def test_examine_reports_owner_choices_as_information(self):
        import validate
        checks = {key: True for key in qingping.CHECKS}
        for servers in (False, True):
            with self.subTest(servers=servers):
                values = {**checks, "empty_allowlist": True if not servers else "has entries", "wifi_disabled": True,
                          "kernel_policy_default_deny": True}
                with patch.object(validate, "run_checks", return_value={"checks": values}), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertTrue(qingping.examine(Mock()))
                self.assertIn("Approved servers: " + ("yes" if servers else "none"), output.getvalue())
                self.assertNotIn("PROBLEM", output.getvalue())

    def test_examine_never_skips_failed_firewall_with_approved_servers(self):
        import validate
        checks={key: True for key in qingping.CHECKS}
        checks.update(empty_allowlist=False, wifi_disabled=False,kernel_policy_default_deny=False)
        with patch.object(validate,"run_checks",return_value={"checks":checks}), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(qingping.examine(Mock()))
