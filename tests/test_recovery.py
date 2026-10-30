"""Exercise restoration in an isolated filesystem; never use ADB or real signals."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import device
import recovery


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StockFixture:
    def __init__(self, base):
        self.base = base
        self.root = base / "monitor"
        self.root.mkdir()
        (self.root / "userdata").mkdir()
        (self.root / "data").symlink_to("userdata", target_is_directory=True)
        self.backup = base / "backup"
        self.backup.mkdir()
        self.stock = self.root / "data/cgs2-stock"
        self.originals = {name: ("original:" + name).encode() for name in recovery.STOCK_PINS}
        self.pins = {name: sha(data) for name, data in self.originals.items()}
        self.originals["qingping/bin/miio/client"] = b"original vendor client"
        with tarfile.open(self.backup / "original.tar", "w") as packed:
            directory = tarfile.TarInfo("qingping/bin/miio")
            directory.type, directory.mode = tarfile.DIRTYPE, 0o755
            packed.addfile(directory)
            for name, data in {**self.originals, "data/etc/setting.ini": b"private settings"}.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o755
                packed.addfile(item, io.BytesIO(data))
        recovery.write_json(self.backup / "backup.json", {"format": 1, "serial": "fixture",
                            "sha256": device.digest(self.backup / "original.tar")})
        self.added = {"usr/local/sbin/cgs2-services", "usr/local/sbin/cgs2-reset", "etc/init.d/S02privacy"}
        self.manifest = {"format": 1, "serial": "fixture", "changes": [
            {"path": name, "original": None if name in self.added else {"sha256": self.pins[name], "mode": 0o755}}
            for name in [*self.pins, *sorted(self.added)]], "removals": ["qingping/bin/miio"]}
        recovery.create(self.backup, self.manifest, self.stock, pins=self.pins)
        for name in self.pins:
            self.write(name, b"modified")
        for name in self.added:
            self.write(name, b"#!/bin/sh\nexit 0\n", 0o755)
        for name in (*recovery.RUNTIME_FILES, *recovery.PERSONAL_FILES):
            self.write(name, b"private fixture")
        self.write("data/etc/alarmx.ini", b"[volume]\nalarm_clock=75\nover_limit=50\n")
        self.write("qingping/etc/temp_fix.ini", b"calibration must stay")
        self.write("qingping/etc/miio/device.conf", b"identity must stay")
        self.write("data/etc/unknown-owner-file", b"unrelated data")
        self.write("etc/profile", b":\n")
        self.write("dev/watchdog", b"")
        self.write("run/cgs2-readings.ini", b"private current readings")
        (self.root / "run/cgs2-mqtt-config.lock").mkdir(parents=True)
        (self.root / "tmp").mkdir()
        view = base / "view.qml"
        view.write_text("fixture view")
        filter_path = base / "filter"
        filter_path.write_text("#!/bin/sh\nexit 0\n")
        recovery.finish_copy(self.stock, self.manifest, filter_path, view)
        self.runtime = self.root / "run/cgs2-reset"
        self.runtime.mkdir()
        for name in ("restore.sh", "ResetStatus.qml", "filter"):
            shutil.copy2(self.stock / name, self.runtime / name)
        (self.runtime / "empty.conf").touch()
        self.script = (self.stock / "restore.sh").read_text()
        self.script = re.sub(r"(?<![A-Za-z0-9_/$.-])/(?:etc|usr|qingping|data|userdata|run|tmp|dev)(?=/|\s|$|[\"');])",
                             lambda m: str(self.root) + m[0], self.script)
        self.worker = base / "worker.sh"
        self.worker.write_text(self.script)
        self.mocks = base / "bin"
        self.mocks.mkdir()
        self.log = base / "events"
        mock = '''import hashlib,json,os,pathlib,sys,time
name=pathlib.Path(sys.argv[0]).name; a=sys.argv[1:]
root=pathlib.Path(os.environ["MOCK_ROOT"]); mode=os.environ["MOCK_MODE"]
with open(os.environ["MOCK_EVENTS"],"a") as f: f.write(json.dumps([name,*a])+"\\n")
if name=="pgrep": sys.exit(0 if mode=="stop-fails" and a[-1]=="QingSnow2App" else 1)
if name=="sha256sum":
    print(hashlib.sha256(pathlib.Path(a[-1]).read_bytes()).hexdigest()+"  "+a[-1]); sys.exit()
if name=="df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print("fixture 1000000 0 "+("1" if mode=="no-space" else "1000000")+" 1% /data"); sys.exit()
if name=="du": print("64 "+a[-1]); sys.exit()
if name=="timeout": os.execv(a[1],a[1:])
if name=="sleep": time.sleep(0.01); sys.exit()
if name=="qmlscene":
    if mode=="view-fails": sys.exit(1)
    (root/"view.pid").write_text(str(os.getpid()))
    print("CGS2_RESET_DISPLAY_READY",flush=True); time.sleep(60); sys.exit()
if name=="mv":
    if mode=="write-fails" and a[-1].endswith("/qingping/bin/start.sh"): sys.exit(1)
    os.execv("/bin/mv",["mv",*a])
'''
        for name in ("pgrep", "pkill", "killall", "sleep", "sync", "sha256sum", "timeout", "df", "du", "qmlscene", "ifconfig", "mv"):
            path = self.mocks / name
            path.write_text("#!" + sys.executable + "\n" + mock)
            path.chmod(0o755)

    def write(self, name, data, mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def run(self, mode="normal", command="run"):
        env = dict(os.environ, PATH=str(self.mocks)+":/usr/bin:/bin", MOCK_ROOT=str(self.root),
                   MOCK_MODE=mode, MOCK_EVENTS=str(self.log))
        try:
            return subprocess.run(["/bin/sh", str(self.worker), command], text=True, capture_output=True, env=env, timeout=20)
        finally:
            pid = self.root / "view.pid"
            if pid.exists():
                try: os.kill(int(pid.read_text()), signal.SIGTERM)
                except ProcessLookupError: pass
                pid.unlink()

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@unittest.skipIf(os.name == "nt", "POSIX restoration shell fixtures; covered on Linux and macOS")
class StockCopyTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix="cgs2-recovery-")
        self.addCleanup(temporary.cleanup)
        return StockFixture(Path(temporary.name))

    def test_stock_copy_excludes_personal_data_and_checks_identity_and_pins(self):
        f = self.fixture()
        data = recovery.validate(f.stock, "fixture", pins=f.pins)
        self.assertNotIn("data/etc/setting.ini", data["files"])
        with self.assertRaisesRegex(ValueError, "different monitor"):
            recovery.validate(f.stock, "other", pins=f.pins)
        with self.assertRaisesRegex(ValueError, "audited firmware"):
            recovery.validate(f.stock, "fixture", pins={**f.pins, "etc/init.d/rcS": "f"*64})
        data["files"]["etc/init.d/rcS"]["sha256"] = "e"*64
        recovery.write_json(f.stock / "manifest.json", data)
        with self.assertRaisesRegex(ValueError, "inventory"):
            recovery.validate(f.stock, "fixture", pins=f.pins)

    def test_only_the_stock_data_alias_is_accepted(self):
        f = self.fixture()
        self.assertEqual(f.run(command="check").returncode, 0)
        alias = f.root / "data"
        alias.unlink()
        alias.symlink_to("./userdata", target_is_directory=True)
        self.assertNotEqual(f.run(command="check").returncode, 0)
        alias.unlink()
        alias.symlink_to("userdata", target_is_directory=True)
        (f.root / "userdata").rename(f.root / "other-data")
        (f.root / "userdata").symlink_to("other-data", target_is_directory=True)
        self.assertNotEqual(f.run(command="check").returncode, 0)

    def test_backup_after_installation_restores_without_a_separate_bundle(self):
        import install_services
        f = self.fixture()
        backup = f.base / "later-backup"
        backup.mkdir()
        with tarfile.open(backup / "original.tar", "w") as packed:
            for file in f.stock.iterdir():
                packed.add(file, arcname="data/cgs2-stock/" + file.name)
            # The live executable is modified; it is not a valid stock source.
            packed.add(f.root / "qingping/bin/QingSnow2App", arcname="qingping/bin/QingSnow2App")
        recovery.write_json(backup / "backup.json", {"format":1,"serial":"fixture","sha256":device.digest(backup/"original.tar")})
        adb = Mock(serial="fixture")
        transferred = []
        def transfer(*args):
            if args[0] == "push":
                transferred.append(recovery.validate(Path(args[1]), "fixture", pins=f.pins))
        adb.run.side_effect = transfer
        with patch.object(recovery,"STOCK_PINS",f.pins), \
             patch.object(install_services,"AUDITED_FILTER_HASHES",{device.digest(f.stock/"filter")}), \
             patch.object(recovery,"start") as start:
            recovery.from_backup(adb,None,backup)
            start.assert_called_once_with(adb)
        self.assertEqual(len(transferred),1)
        self.assertEqual(transferred[0]["files"]["qingping/bin/QingSnow2App"]["sha256"],f.pins["qingping/bin/QingSnow2App"])

    def test_update_receipt_preserves_the_stock_archive(self):
        f=self.fixture()
        before=device.digest(f.stock/"stock.tar")
        output=f.base/"update";output.mkdir()
        adb=Mock(serial="fixture")
        def transfer(*args):
            if args[0]=="pull":shutil.copyfile(f.stock/"installed.json",args[2])
        adb.run.side_effect=transfer
        with patch.object(recovery,"present",return_value=True):
            recovery.record_update(adb,output,{"app_sha256":"a"*64,"controller_sha256":"b"*64})
        receipt=json.loads((output/"installed-receipt.json").read_text())
        changes={item["path"]:item for item in receipt["changes"]}
        self.assertEqual(changes["qingping/bin/QingSnow2App"]["sha256"],"a"*64)
        self.assertEqual(changes["usr/local/sbin/cgs2-services"]["sha256"],"b"*64)
        self.assertEqual(changes["qingping/bin/QingSnow2App"]["original"]["sha256"],f.pins["qingping/bin/QingSnow2App"])
        self.assertEqual(device.digest(f.stock/"stock.tar"),before)

    def test_unsafe_archive_entries_are_rejected_even_with_matching_archive_hash(self):
        for name, kind in (("../escape", "file"), ("etc/passwd", "file"), ("qingping/bin/miio/link", "symlink"),
                           ("qingping/bin/miio/link", "hardlink"), ("qingping/bin/miio/device", "fifo")):
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "stock.tar"
                with tarfile.open(archive, "w") as packed:
                    item = tarfile.TarInfo(name)
                    if kind == "symlink": item.type, item.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    if kind == "hardlink": item.type, item.linkname = tarfile.LNKTYPE, "etc/passwd"
                    if kind == "fifo": item.type = tarfile.FIFOTYPE
                    packed.addfile(item)
                with self.assertRaises(ValueError):
                    recovery.inventory(archive, {})

    def test_install_commits_verified_stock_before_the_first_original_change(self):
        f = self.fixture()
        payload=f.base / "payload"
        names=set(f.pins) | device.REQUIRED_FILES | {"usr/local/sbin/cgs2-services", "usr/local/sbin/cgs2-reset"}
        for name in names:
            target=payload/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((f.stock/"filter").read_bytes() if name=="usr/local/sbin/cgs2-filter" else b"replacement")
        bundle=f.base/"bundle"
        device.prepare(f.backup,payload,bundle,list(device.DEFAULT_REMOVALS))
        manifest=json.loads((bundle/"manifest.json").read_text())
        recovery.create(f.backup,manifest,bundle/"stock",pins=f.pins)
        recovery.finish_copy(bundle/"stock",manifest,f.stock/"filter",ROOT/"device/ResetStatus.qml")
        adb=Mock(serial="fixture");adb.script.return_value=""
        with patch.object(recovery,"STOCK_PINS",f.pins):
            device.install(adb,bundle,f.backup)
        body=adb.script.call_args.args[0]
        stock_commit=body.index(" /data/cgs2-stock\n")
        original_change=body.index("mv -f /etc/init.d/rcS.cgs2-new")
        self.assertLess(stock_commit,body.index("pkill"))
        self.assertLess(stock_commit,original_change)
        self.assertIn("test ! -L /etc/init.d/rcS.cgs2-new",body[:stock_commit])
        self.assertGreater(body.rindex("/data/etc/cgs2-release.ini"),body.rindex("S02privacy start"))
        # A mismatched progress view cannot be installed as a trusted restore helper.
        (bundle/"stock/ResetStatus.qml").write_text("unreviewed")
        with patch.object(recovery,"STOCK_PINS",f.pins), self.assertRaises(ValueError):
            device.install(adb,bundle,f.backup)

    @unittest.skipUnless(os.name == "posix", "POSIX shell fixture")
    def test_reset_refuses_a_concurrent_configuration_or_update(self):
        f=self.fixture()
        result=f.run(command="start")
        self.assertNotEqual(result.returncode,0)
        self.assertTrue((f.root/"run/cgs2-mqtt-config.lock").is_dir())
        self.assertIn("An update is running",(f.root/"tmp/cgs2-reset-status.json").read_text())
        self.assertEqual((f.root/"etc/init.d/rcS").read_bytes(),b"modified")

    @unittest.skipUnless(os.name == "posix", "POSIX shell fixture")
    def test_restore_erases_only_owned_data_and_restores_boot_last(self):
        f = self.fixture()
        result = f.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, data in f.originals.items():
            self.assertEqual((f.root / name).read_bytes(), data)
            self.assertEqual((f.root / name).stat().st_mode & 0o777, 0o755)
        self.assertEqual((f.root / "data/etc/setting.ini").read_text(), "[application]\nwifi_disabled=true\n")
        self.assertEqual((f.root / "data/etc/setting.ini").stat().st_mode & 0o777, 0o600)
        self.assertFalse((f.root / "data/cfg/wpa_supplicant.conf").exists())
        for name in (*f.added, *recovery.RUNTIME_FILES, "data/etc/alarmx.ini", "data/etc/Snow2.db", "run/cgs2-readings.ini"):
            self.assertFalse((f.root / name).exists(), name)
        for name in ("qingping/etc/temp_fix.ini", "qingping/etc/miio/device.conf", "data/etc/unknown-owner-file"):
            self.assertTrue((f.root / name).exists(), name)
        self.assertFalse(f.stock.exists())
        self.assertTrue(json.loads((f.root / "tmp/cgs2-reset-status.json").read_text())["ok"])
        moves = [event for event in f.events() if event[0] == "mv" and ".cgs2-new" in event[-2]]
        self.assertTrue(moves[-1][-1].endswith("/etc/init.d/rcS"))
        events = f.events()
        for event in moves:
            # chmod/copy are followed by a sync before the first file's rename.
            self.assertTrue(any(e[0] == "sync" for e in events[:events.index(event)]))

    @unittest.skipUnless(os.name == "posix", "POSIX shell fixture")
    def test_failure_preserves_recovery_and_never_restores_stock_boot_early(self):
        for mode in ("corrupt", "missing", "no-space", "stop-fails", "view-fails", "write-fails", "symlink", "temporary-symlink"):
            with self.subTest(mode=mode):
                f = self.fixture()
                if mode == "corrupt": (f.stock / "stock.tar").write_bytes(b"broken")
                if mode == "missing": (f.stock / "stock.tar").unlink()
                if mode == "symlink":
                    target = f.root / "data/etc/Snow2.db"
                    target.unlink(); target.symlink_to(f.root / "qingping/etc/temp_fix.ini")
                if mode == "temporary-symlink":
                    (f.root / "qingping/bin/start.sh.cgs2-new").symlink_to(f.root / "qingping/etc/temp_fix.ini")
                result = f.run(mode)
                self.assertNotEqual(result.returncode, 0, mode)
                self.assertTrue(f.stock.exists())
                self.assertEqual((f.root / "etc/init.d/rcS").read_bytes(), b"modified")
                if mode != "write-fails":
                    self.assertEqual((f.root / "data/etc/setting.ini").read_bytes(), b"private fixture")

    @unittest.skipUnless(os.name == "posix", "POSIX shell fixture")
    def test_preflight_failure_cannot_be_hidden_by_shell_if_context(self):
        f = self.fixture()
        (f.stock / "stock.tar").write_bytes(b"broken")
        # The start branch calls preflight from an if/! context.
        (f.root / "run/cgs2-mqtt-config.lock").rmdir()
        result = f.run(command="start")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((f.root / "run/cgs2-mqtt-config.lock").exists())
        self.assertEqual((f.root / "etc/init.d/rcS").read_bytes(), b"modified")

