"""Run the upgrade shell against an isolated fake filesystem and fake processes.

No ADB, actual process signals, network calls, hardware watchdog or clock writes.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_services as installer


def sha(data):
    return hashlib.sha256(data).hexdigest()


def release_bundle(serial, backup_sha256, app, controller):
    """A prepare.py bundle manifest as installed on a stock 0181 monitor."""
    files = {"etc/init.d/rcS": installer.PROTECTED["etc/init.d/rcS"],
             "etc/init.d/S02privacy": installer.PROTECTED["etc/init.d/S02privacy"],
             installer.FILTER_PATH: installer.PROTECTED[installer.FILTER_PATH],
             installer.APP_PATH: app, installer.CONTROLLER_PATH: controller, **installer.current_helpers()}
    return {"format": 1, "serial": serial, "backup_sha256": backup_sha256, "payload_sha256": "d" * 64, "removals": [],
            "changes": [{"path": name, "sha256": value, "mode": 0o755,
                         "original": {"sha256": installer.patch_app.STOCK_SHA256, "mode": 0o755} if name == installer.APP_PATH else None}
                        for name, value in files.items()]}


APP_MOCK = r'''
import hashlib,json,os,pathlib,sys,time
root=pathlib.Path(os.environ["FIXTURE_ROOT"]);state=root/"state"
mode=os.environ.get("FIXTURE_MODE","normal");a=sys.argv[1:]
runtime=os.environ.get("FIXTURE_RUNTIME")=="1"
with (state/"events").open("a") as stream:stream.write(json.dumps([COMMAND,*a])+"\n")
def end(code=0):raise SystemExit(code)
if COMMAND=="pgrep":
    name=a[-1]
    if runtime and name=="cgs2-services":
        pids=[]
        if (state/name).exists():
            revision=(state/"revision").read_text() if (state/"revision").exists() else "old"
            pids += ["401","402"] if revision=="new" else ["201","202"]
        if (state/"geocode").exists():pids.append("303")
        if not pids:end(1)
        print("\n".join(pids));end()
    if not (state/name).exists():end(1)
    print("201\n202" if name=="cgs2-services" else "300");end()
if COMMAND in ("pkill","killall"):
    if "cgs2-services" in a:
        if not runtime:raise AssertionError("controller must stay running")
        if mode!="cleanup-stuck":
            (state/"cgs2-services").unlink(missing_ok=True)
            (state/"geocode").unlink(missing_ok=True)
    if "QingSnow2App" in a and mode!="stop-fails":
        (state/"QingSnow2App").unlink(missing_ok=True)
        if mode=="draft-flush":(root/"data/etc/cgs2-services-draft.ini").write_text("flushed draft\n")
    end()
if COMMAND=="sleep":time.sleep(0.002);end()
if COMMAND=="sync":end()
if COMMAND=="setsid":os.execv(a[0],a)
if COMMAND=="timeout":os.execv(a[1],a[1:])
if COMMAND=="sha256sum":print(hashlib.sha256(pathlib.Path(a[-1]).read_bytes()).hexdigest()+"  "+a[-1]);end()
if COMMAND=="stat":print(oct(pathlib.Path(a[-1]).stat().st_mode&0o777)[2:]);end()
if COMMAND=="jq":
    value=json.loads(pathlib.Path(a[-1]).read_text());query=a[-2]
    if "--arg" in a: value=value["files"][a[a.index("--arg")+2]][query.rsplit(".",1)[1]]
    else:value=value[query[1:]]
    print(value);end()
if COMMAND=="mv":
    if mode=="rename-fails" and a[0].endswith(".cgs2-app-new") and not (state/"failed").exists():
        (state/"failed").touch();end(1)
    if ((mode=="controller-rename-fails" and a[0].endswith(".cgs2-runtime-new")) or
        (mode=="app-rename-fails" and a[0].endswith(".cgs2-app-new"))) and not (state/"failed").exists():
        (state/"failed").touch();end(1)
    os.execv("/bin/mv",["mv",*a])
if COMMAND=="watchdog":
    if mode in ("start-fails","app-start-fails") and (root/"qingping/bin/QingSnow2App").read_bytes()==b"new application":end()
    if runtime and mode=="geocode-starts":(state/"geocode").touch()
    (state/"QingSnow2App").touch();end()
if COMMAND=="service":
    if runtime:
        with (state/"events").open("a") as stream:stream.write(json.dumps(["service-version",REVISION,*a])+"\n")
        command=a[0]
        if command=="ping":
            if mode=="geocode-finishes":(state/"geocode").unlink(missing_ok=True)
            end(0 if (state/"cgs2-services").exists() else 1)
        if command=="stop":
            if mode=="lookup-marker-flush":
                marker=root/"data/etc/cgs2-location-search-last-id.json"
                marker.write_text('{"last_request_id":"consumed-fixture"}\n');marker.chmod(0o600)
            if mode!="cleanup-stuck":(state/"cgs2-services").unlink(missing_ok=True)
            end()
        if command=="runtime-reset":
            if mode=="reset-fails" or mode=="new-reset-fails" and REVISION=="new":end(1)
            if (state/"leases").exists() and (state/"leases").read_text()=="new" and REVISION!="new":
                raise AssertionError("old helper cannot clean newer lease types")
            (state/"leases").unlink(missing_ok=True);end()
        if command=="start":
            (state/"cgs2-services").touch();(state/"revision").write_text(REVISION)
            (state/"leases").write_text(REVISION)
            if mode in ("controller-start-fails","new-reset-fails") and REVISION=="new":end(1)
            end()
        raise AssertionError("updater must not initialize or change saved settings")
    if a!=["ping"]:raise AssertionError("only readonly controller ping is allowed")
    end()
end(2)
'''


class AppShellFixture:
    def __init__(self, root, mode="normal", runtime=False, filter_text=None):
        self.root=root;self.mode=mode;self.runtime=runtime
        prefix=".cgs2-runtime-update-test" if runtime else ".cgs2-app-update-test"
        self.stage=root/"data"/prefix;self.stage.mkdir(parents=True)
        self.state=root/"state";self.state.mkdir()
        for name in ("QingSnow2App","adbd","cgs2-services"):(self.state/name).touch()
        self.bin=root/"mock-bin";self.bin.mkdir()
        for name in ("pgrep","pkill","killall","sleep","sync","setsid","timeout","sha256sum","stat","jq","mv"):
            self.write("mock-bin/"+name,self.mock(name),0o755)
        self.boot="12345678-1234-1234-1234-123456789abc"
        self.write("proc/sys/kernel/random/boot_id",self.boot+"\n")
        self.write("run/cgs2-privacy.ready","ready")
        self.write("dev/watchdog","")
        for name in installer.APP_SNAPSHOT_FILES:self.write(name,"fixture:"+name)
        self.write("etc/profile",': "${OPTIONAL_UNSET_VARIABLE}"\n')
        self.write("qingping/bin/watchdog.sh",self.mock("watchdog"),0o755)
        self.write(installer.CONTROLLER_PATH,self.mock("service"),0o755)
        self.write(installer.APP_DRAFT,"original draft\n",0o600)
        self.write(installer.APP_PATH,"old application",0o755)
        self.protected={name:installer.digest(root/name) for name in installer.PROTECTED}
        # Each owner's install may hold a different audited filter build.
        self.filters=frozenset({self.protected[installer.FILTER_PATH],sha(b"alternative audited filter")})
        if filter_text is not None:self.write(installer.FILTER_PATH,filter_text,0o755)
        self.old_hash=installer.digest(root/installer.APP_PATH)
        self.controller_hash=installer.digest(root/installer.CONTROLLER_PATH)
        (self.stage/"app").write_text("new application")
        (self.stage/"rollback-app").write_text("old application")
        if runtime:
            self.write("etc/cgs2-privacy/services.json",json.dumps(dict(time_enabled=True,weather_enabled=True,dns_enabled=True)),0o600)
            self.write("etc/cgs2-privacy/allowlist.conf","tcp:192.0.2.8:1883\n",0o600)
            (self.stage/"rollback-controller").write_bytes((root/installer.CONTROLLER_PATH).read_bytes())
            (self.stage/"controller").write_text(self.mock("service",revision="new"))
            self.new_controller_hash=installer.digest(self.stage/"controller")
            (self.state/"leases").write_text("old")
            if mode in ("geocode-running","geocode-finishes"):(self.state/"geocode").touch()
        self.app_hash=installer.digest(self.stage/"app")
        self.baseline={"boot_id":self.boot,"controller_pids":"201\n202","app_mode":"755","files":{
            name:{"sha256":installer.digest(root/name),"mode":oct((root/name).stat().st_mode&0o777)[2:]}
            for name in installer.APP_SNAPSHOT_FILES}}
        if runtime and mode in ("geocode-running","geocode-finishes"):self.baseline["controller_pids"]+="\n303"
        (self.stage/"baseline.json").write_text(json.dumps(self.baseline))
        self.baseline_hash=installer.digest(self.stage/"baseline.json")
        with patch.object(installer,"PROTECTED",self.protected),patch.object(installer,"AUDITED_FILTER_HASHES",self.filters):
            self.script=installer.build_app_update_script(self.app_hash,self.old_hash,self.controller_hash,
                                                          self.new_controller_hash if runtime else None)
        self.script=re.sub(r"(?<![A-Za-z0-9_/$.-])/(?:etc|usr|qingping|data|run|proc|tmp)(?=/)",lambda m:str(root)+m[0],self.script)
        self.script=self.script.replace("/dev/watchdog",str(root/"dev/watchdog"))
        # This fixture represents processes with files, and compresses each
        # one-second sleep to two milliseconds. Start its finite fake watchdog
        # synchronously so host process scheduling cannot exhaust that clock.
        # The real daemon stays asynchronous and is checked on the monitor.
        self.script=self.script.replace(' > /dev/null 2>&1 < /dev/null &',
                                        ' > "$stage/watchdog.log" 2>&1 < /dev/null')
        (self.stage/"install.sh").write_text(self.script)
        (root/"tmp").mkdir(exist_ok=True)

    def write(self,name,text,mode=0o644):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text);path.chmod(mode)

    def mock(self,name,revision="old"):return f"#!{sys.executable}\nCOMMAND={name!r}\nREVISION={revision!r}\n"+APP_MOCK

    def run(self):
        env=dict(os.environ,PATH=str(self.bin)+":/usr/bin:/bin",FIXTURE_ROOT=str(self.root),FIXTURE_MODE=self.mode,
                 FIXTURE_RUNTIME="1" if self.runtime else "0")
        result = subprocess.run(["/bin/sh","-x",str(self.stage/"install.sh"),str(self.stage),self.baseline_hash],
                                env=env,text=True,capture_output=True,timeout=30)
        log = self.stage/"watchdog.log"
        if log.exists():
            result.stderr += "\nFixture watchdog output:\n" + log.read_text()
        return result

    def assert_preserved(self,case,draft="original draft\n",controller_changed=False,controller_running=True):
        for name,expected in self.baseline["files"].items():
            if name==installer.CONTROLLER_PATH and controller_changed:continue
            case.assertEqual(installer.digest(self.root/name),expected["sha256"],name)
            case.assertEqual(oct((self.root/name).stat().st_mode&0o777)[2:],expected["mode"],name)
        case.assertEqual((self.root/installer.APP_DRAFT).read_text(),draft)
        case.assertEqual((self.state/"cgs2-services").exists(),controller_running)
        case.assertTrue((self.state/"adbd").exists())
        case.assertFalse((self.root/"run/cgs2-mqtt-config.lock").exists())
        events=[json.loads(line) for line in (self.state/"events").read_text().splitlines()]
        for event in events:
            if not self.runtime:
                if event[0]=="service":case.assertEqual(event,["service","ping"])
                if event[0] in ("killall","pkill"):case.assertNotIn("cgs2-services",event)
            elif event[0]=="service":case.assertIn(event[1],("ping","stop","runtime-reset","start"))


class AppUpdateTests(unittest.TestCase):
    def fixture(self,mode="normal",filter_text=None):
        if os.name!="posix":self.skipTest("App worker fixture requires POSIX shell semantics")
        temporary=tempfile.TemporaryDirectory(prefix="cgs2-app-test-");self.addCleanup(temporary.cleanup)
        return AppShellFixture(Path(temporary.name),mode,filter_text=filter_text)

    def test_success_only_replaces_app_and_observes_health(self):
        f=self.fixture();result=f.run()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((f.stage/"result").read_text().strip(),"0")
        self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"new application")
        self.assertEqual((f.root/"dev/watchdog").read_text(),"V\n")
        self.assertTrue((f.state/"QingSnow2App").exists())
        events=[json.loads(line) for line in (f.state/"events").read_text().splitlines()]
        self.assertGreaterEqual(sum(event==["sleep","1"] for event in events),15)
        f.assert_preserved(self)

    def test_rename_and_restart_failure_restore_only_app(self):
        for mode in ("rename-fails","start-fails"):
            with self.subTest(mode=mode):
                f=self.fixture(mode);result=f.run()
                self.assertNotEqual(result.returncode,0)
                self.assertEqual((f.stage/"rollback-result").read_text().strip(),"restored",result.stderr)
                self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"old application")
                self.assertEqual((f.root/installer.APP_PATH).stat().st_mode&0o777,0o755)
                self.assertTrue((f.state/"QingSnow2App").exists())
                f.assert_preserved(self)

    def test_draft_is_captured_after_exit_flush(self):
        f=self.fixture("draft-flush");result=f.run()
        self.assertEqual(result.returncode,0,result.stderr)
        f.assert_preserved(self,"flushed draft\n")

    def test_corrupt_rollback_or_changed_config_aborts_before_stop(self):
        for changed in ("rollback-app","config"):
            with self.subTest(changed=changed):
                f=self.fixture()
                target=f.stage/"rollback-app" if changed=="rollback-app" else f.root/"etc/cgs2-privacy/services.json"
                target.write_text("changed")
                result=f.run();self.assertNotEqual(result.returncode,0)
                self.assertEqual((f.stage/"rollback-result").read_text().strip(),"not-needed")
                self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"old application")
                self.assertEqual((f.root/"dev/watchdog").read_text(),"")
                self.assertTrue((f.state/"QingSnow2App").exists())

    def test_stuck_app_is_not_replaced_or_magic_closed(self):
        f=self.fixture("stop-fails");result=f.run()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"old application")
        self.assertEqual((f.root/"dev/watchdog").read_text(),"")
        f.assert_preserved(self)

    def test_any_audited_filter_build_is_accepted_and_others_refused_before_stop(self):
        for text,accepted in (("alternative audited filter",True),("unaudited filter",False)):
            with self.subTest(filter=text):
                f=self.fixture(filter_text=text);result=f.run()
                self.assertEqual(result.returncode==0,accepted,result.stderr)
                self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"new application" if accepted else b"old application")
                self.assertEqual((f.root/"dev/watchdog").read_text(),"V\n" if accepted else "")
                self.assertEqual((f.stage/"rollback-result").read_text().strip(),"not-needed")
                f.assert_preserved(self)

    def test_device_without_services_draft_updates_and_gains_none(self):
        f=self.fixture();(f.root/installer.APP_DRAFT).unlink()
        result=f.run();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((f.root/installer.APP_PATH).read_bytes(),b"new application")
        self.assertFalse((f.root/installer.APP_DRAFT).exists())
        self.assertFalse((f.stage/"draft.sha256").exists())

    def test_prepare_and_install_checks_are_offline_until_preimages_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);backup=root/"backup";backup.mkdir();output=root/"prepared"
            with tarfile.open(backup/"original.tar","w") as archive:
                info=tarfile.TarInfo(installer.APP_PATH);data=b"stock fixture";info.size=len(data);archive.addfile(info,io.BytesIO(data))
            metadata={"format":1,"serial":"fixture-serial","sha256":installer.digest(backup/"original.tar")}
            (backup/"backup.json").write_text(json.dumps(metadata))
            old,controller="a"*64,"c"*64
            source=release_bundle(metadata["serial"],metadata["sha256"],old,controller)
            installed=root/"installed.json";installed.write_text(json.dumps(source))
            with patch.object(installer,"ADB",side_effect=AssertionError("no device")),patch.object(installer.patch_app,"patch",return_value=(b"new fixture",{})):
                manifest=installer.prepare_app_update(backup,output,root,installed)
            self.assertEqual(manifest["kind"],"app-only")
            self.assertEqual((output/"preimage/manifest.json").read_bytes(),installed.read_bytes())
            self.assertNotIn(b"\r",(output/"install.sh").read_bytes())
            adb=type("NoDevice",(),{"serial":"fixture-serial","shell":lambda *a:(_ for _ in ()).throw(AssertionError("no device"))})()
            (output/"app").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError,"changed before"):
                installer.install_app_update(adb,output,manifest)
            source["changes"][-1]["sha256"]="f"*64;installed.write_text(json.dumps(source))
            with self.assertRaisesRegex(ValueError,"Unknown installed"):
                installer.app_source_manifest(backup,installed)

    def test_unverified_successor_is_not_accepted_as_a_preimage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=root/"manifest.json"
            controller="c"*64
            source={"format":1,"kind":"app-only","serial":"fixture","backup_sha256":"a"*64,
                    "protected":installer.PROTECTED,"app_sha256":"b"*64,"controller_sha256":controller}
            manifest.write_text(json.dumps(source))
            with self.assertRaises(FileNotFoundError):
                installer.checked_app_source({"serial":"fixture","sha256":"a"*64},manifest)
            verified={"ok":True,"serial":"fixture","manifest_sha256":installer.digest(manifest),
                      "app_sha256":"b"*64,"controller_sha256":controller}
            (root/"app-update-verified.json").write_text(json.dumps(verified))
            self.assertEqual(installer.checked_app_source({"serial":"fixture","sha256":"a"*64},manifest),("b"*64,controller))

    def test_host_install_records_verified_success_only_after_worker_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/"update";output.mkdir();(output/"preimage").mkdir()
            old,controller=sha(b"old fixture"),sha(b"controller fixture")
            source=release_bundle("fixture","a"*64,old,controller)
            (output/"preimage/manifest.json").write_text(json.dumps(source))
            (output/"app").write_bytes(b"new fixture")
            app_hash=installer.digest(output/"app")
            (output/"install.sh").write_bytes(installer.build_app_update_script(app_hash,old,controller).encode())
            manifest={"format":1,"kind":"app-only","serial":"fixture","backup_sha256":"a"*64,
                      "protected":installer.PROTECTED,"preimage_sha256":old,"controller_sha256":controller,
                      "app_sha256":app_hash,"install_script_sha256":installer.digest(output/"install.sh"),
                      "source_manifest_sha256":installer.digest(output/"preimage/manifest.json")}
            (output/"manifest.json").write_text(json.dumps(manifest))
            baseline={"boot_id":"fixture-boot","controller_pids":"201\n202","files":{},"app_mode":"755"}
            class FakeADB:
                serial="fixture"
                def __init__(self):self.calls=[]
                def shell(self,*args):
                    self.calls.append(args)
                    return "0" if args[:2]==("sh","-c") and "if test -f" in args[2] else ""
                def run(self,*args):
                    self.calls.append(args)
                    if args[0]=="pull":
                        data=b"old fixture" if args[1].endswith("/rollback-app") else b"not-needed\n"
                        Path(args[2]).write_bytes(data)
            adb=FakeADB()
            with patch.object(installer,"app_snapshot",return_value=baseline):
                installer.install_app_update(adb,output,manifest)
                verified=json.loads((output/"app-update-verified.json").read_text())
                self.assertTrue(verified["ok"])
                self.assertEqual(verified["manifest_sha256"],installer.digest(output/"manifest.json"))
                self.assertEqual(installer.checked_app_source({"serial":"fixture","sha256":"a"*64},output/"manifest.json"),(app_hash,controller))
            for call in adb.calls:
                if call and call[0]=="/usr/local/sbin/cgs2-services":self.assertEqual(call,(call[0],"ping"))
            # The About page time is recorded only after the update is verified.
            record=[index for index,call in enumerate(adb.calls) if call[:2]==("sh","-ec") and "cgs2-release.ini" in call[2]]
            self.assertEqual(len(record),1)
            self.assertEqual(adb.calls[record[0]-1],("pgrep","-x","adbd"))
            cleaned = [call for call in adb.calls if call[:2] == ("rm", "-rf")]
            self.assertEqual(len(cleaned), 1)
            self.assertTrue(cleaned[0][2].startswith("/data/.cgs2-app-update-"))
            self.assertGreater(adb.calls.index(cleaned[0]), record[0])



class RuntimeUpdateTests(unittest.TestCase):
    def fixture(self,mode="normal"):
        if os.name!="posix":self.skipTest("Runtime worker fixture requires POSIX shell semantics")
        temporary=tempfile.TemporaryDirectory(prefix="cgs2-runtime-test-");self.addCleanup(temporary.cleanup)
        return AppShellFixture(Path(temporary.name),mode,runtime=True)

    def events(self,f):return [json.loads(line) for line in (f.state/"events").read_text().splitlines()]

    def test_success_preserves_enabled_config_and_broker_permission(self):
        f=self.fixture();result=f.run()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(installer.digest(f.root/installer.APP_PATH),f.app_hash)
        self.assertEqual(installer.digest(f.root/installer.CONTROLLER_PATH),f.new_controller_hash)
        self.assertEqual((f.stage/"rollback-result").read_text().strip(),"not-needed")
        self.assertTrue((f.state/"QingSnow2App").exists())
        self.assertEqual((f.state/"leases").read_text(),"new")
        self.assertGreaterEqual(sum(event==["sleep","1"] for event in self.events(f)),15)
        f.assert_preserved(self,controller_changed=True)
        commands=self.events(f)
        reset=commands.index(["service-version","old","runtime-reset"])
        first_rename=next(i for i,event in enumerate(commands) if event[0]=="mv")
        self.assertLess(reset,first_rename)

    def test_both_rename_failures_and_both_start_failures_restore_pair(self):
        for mode in ("controller-rename-fails","app-rename-fails","controller-start-fails","app-start-fails"):
            with self.subTest(mode=mode):
                f=self.fixture(mode)
                for name in installer.RUNTIME_OPTIONAL_FILES:f.write(name,"private fixture\n",0o600)
                result=f.run()
                self.assertNotEqual(result.returncode,0)
                self.assertEqual((f.stage/"rollback-result").read_text().strip(),"restored",result.stderr)
                self.assertEqual(installer.digest(f.root/installer.APP_PATH),f.old_hash)
                self.assertEqual(installer.digest(f.root/installer.CONTROLLER_PATH),f.controller_hash)
                self.assertTrue((f.state/"QingSnow2App").exists())
                f.assert_preserved(self)
                for name in installer.RUNTIME_OPTIONAL_FILES:
                    self.assertEqual((f.root/name).read_text(),"private fixture\n")
                    self.assertEqual((f.root/name).stat().st_mode&0o777,0o600)
                if mode in ("controller-start-fails","app-start-fails"):
                    commands=self.events(f)
                    new_cleanup=commands.index(["service-version","new","runtime-reset"])
                    old_restart=commands.index(["service-version","old","start"])
                    self.assertLess(new_cleanup,old_restart)

    def test_cleanup_failure_keeps_required_parser_and_refuses_replacement(self):
        for mode in ("reset-fails","cleanup-stuck","new-reset-fails"):
            with self.subTest(mode=mode):
                f=self.fixture(mode);result=f.run()
                self.assertNotEqual(result.returncode,0)
                self.assertEqual((f.stage/"rollback-result").read_text().strip(),"incomplete",result.stderr)
                changed=mode=="new-reset-fails"
                self.assertEqual(installer.digest(f.root/installer.APP_PATH),f.app_hash if changed else f.old_hash)
                self.assertEqual(installer.digest(f.root/installer.CONTROLLER_PATH),f.new_controller_hash if changed else f.controller_hash)
                self.assertFalse((f.state/"QingSnow2App").exists())
                f.assert_preserved(self,controller_changed=changed,controller_running=mode=="cleanup-stuck")
                if changed:self.assertNotIn(["service-version","old","start"],self.events(f))

    def test_standalone_city_search_process_is_stopped_before_replacement(self):
        f=self.fixture("geocode-running");result=f.run()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((f.state/"geocode").exists())
        self.assertIn(["killall","cgs2-services"],self.events(f))
        f.assert_preserved(self,controller_changed=True)

    def test_normal_city_process_lifecycle_does_not_require_frozen_pids(self):
        for mode in ("geocode-finishes","geocode-starts"):
            with self.subTest(mode=mode):
                f=self.fixture(mode);result=f.run()
                self.assertEqual(result.returncode,0,result.stderr)
                f.assert_preserved(self,controller_changed=True)

    def test_existing_city_draft_and_marker_are_preserved_and_absence_is_allowed(self):
        for exists in (True,False):
            with self.subTest(exists=exists):
                f=self.fixture()
                if exists:
                    for name in installer.RUNTIME_OPTIONAL_FILES:f.write(name,"city fixture\n",0o600)
                result=f.run();self.assertEqual(result.returncode,0,result.stderr)
                for name in installer.RUNTIME_OPTIONAL_FILES:
                    path=f.root/name
                    self.assertEqual(path.exists(),exists)
                    if exists:
                        self.assertEqual(path.read_text(),"city fixture\n")
                        self.assertEqual(path.stat().st_mode&0o777,0o600)
                f.assert_preserved(self,controller_changed=True)

    def test_consumed_lookup_marker_is_captured_after_controller_exits(self):
        f=self.fixture("lookup-marker-flush")
        f.write(installer.LOCATION_LAST_ID,'{"last_request_id":"previous-fixture"}\n',0o600)
        result=f.run();self.assertEqual(result.returncode,0,result.stderr)
        marker=f.root/installer.LOCATION_LAST_ID
        self.assertEqual(json.loads(marker.read_text()),{"last_request_id":"consumed-fixture"})
        self.assertEqual(marker.stat().st_mode&0o777,0o600)
        commands=self.events(f)
        stop=commands.index(["service-version","old","stop"])
        capture=next(i for i,event in enumerate(commands) if event==["sha256sum",str(marker)])
        self.assertLess(stop,capture)
        f.assert_preserved(self,controller_changed=True)

    def test_device_without_services_draft_updates_and_gains_none(self):
        f=self.fixture();(f.root/installer.APP_DRAFT).unlink()
        result=f.run();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(installer.digest(f.root/installer.CONTROLLER_PATH),f.new_controller_hash)
        self.assertFalse((f.root/installer.APP_DRAFT).exists())

    def test_corrupt_controller_rollback_refuses_before_display_stop(self):
        f=self.fixture();(f.stage/"rollback-controller").write_bytes(b"corruption")
        result=f.run();self.assertNotEqual(result.returncode,0)
        self.assertEqual((f.stage/"rollback-result").read_text().strip(),"not-needed")
        self.assertEqual((f.root/"dev/watchdog").read_text(),"")
        self.assertTrue((f.state/"QingSnow2App").exists())
        f.assert_preserved(self)

    def test_runtime_prepare_is_offline_and_requires_reviewed_target(self):
        import build
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);backup=root/"backup";backup.mkdir();output=root/"prepared"
            with tarfile.open(backup/"original.tar","w") as archive:
                info=tarfile.TarInfo(installer.APP_PATH);data=b"stock fixture";info.size=len(data);archive.addfile(info,io.BytesIO(data))
            metadata={"format":1,"serial":"fixture","sha256":installer.digest(backup/"original.tar")}
            (backup/"backup.json").write_text(json.dumps(metadata))
            old,controller="a"*64,"c"*64
            source=release_bundle("fixture",metadata["sha256"],old,controller)
            installed=root/"installed.json";installed.write_text(json.dumps(source))
            new_controller=root/"controller";new_controller.write_bytes(b"mock validated executable")
            with patch.object(build,"verify_target") as verify,patch.object(installer,"ADB",side_effect=AssertionError("no device")),patch.object(installer.patch_app,"patch",return_value=(b"new fixture",{})):
                manifest=installer.prepare_app_update(backup,output,root,installed,controller=new_controller)
            verify.assert_called_once_with(new_controller)
            self.assertEqual(manifest["kind"],"runtime-update")
            self.assertEqual(manifest["controller_preimage_sha256"],controller)
            self.assertEqual(manifest["controller_sha256"],installer.digest(new_controller))
            self.assertEqual((output/"controller").read_bytes(),new_controller.read_bytes())
            script=(output/"install.sh").read_text()
            self.assertIn('timeout 30 "$controller" runtime-reset',script)
            self.assertNotIn('"$controller" initialize',script)
            self.assertNotIn("services_disabled",script)
            marker=output/"runtime-update-verified.json"
            with self.assertRaises(FileNotFoundError):installer.checked_app_source(metadata,output/"manifest.json")
            marker.write_text(json.dumps({"ok":True,"serial":"fixture","manifest_sha256":installer.digest(output/"manifest.json"),
                                         "app_sha256":manifest["app_sha256"],"controller_sha256":manifest["controller_sha256"]}))
            self.assertEqual(installer.checked_app_source(metadata,output/"manifest.json"),(manifest["app_sha256"],manifest["controller_sha256"]))

    def test_host_runtime_install_verifies_both_backups_and_records_new_helper(self):
        for corrupt_backup in (False,True):
            with self.subTest(corrupt_backup=corrupt_backup),tempfile.TemporaryDirectory() as directory:
                output=Path(directory);(output/"preimage").mkdir()
                old_app,old_controller=sha(b"old app"),sha(b"old controller")
                source=release_bundle("fixture","a"*64,old_app,old_controller)
                (output/"preimage/manifest.json").write_text(json.dumps(source))
                (output/"app").write_bytes(b"new app");(output/"controller").write_bytes(b"new controller")
                app_hash,controller_hash=installer.digest(output/"app"),installer.digest(output/"controller")
                (output/"install.sh").write_bytes(installer.build_app_update_script(app_hash,old_app,old_controller,controller_hash).encode())
                manifest={"format":1,"kind":"runtime-update","serial":"fixture","backup_sha256":"a"*64,
                          "protected":installer.PROTECTED,"preimage_sha256":old_app,"controller_preimage_sha256":old_controller,
                          "controller_sha256":controller_hash,"app_sha256":app_hash,
                          "install_script_sha256":installer.digest(output/"install.sh"),
                          "source_manifest_sha256":installer.digest(output/"preimage/manifest.json")}
                (output/"manifest.json").write_text(json.dumps(manifest))
                baseline={"boot_id":"fixture-boot","controller_pids":"201\n202","app_mode":"755",
                          "files":{installer.CONTROLLER_PATH:{"sha256":old_controller,"mode":"755"}}}
                after={**baseline,"controller_pids":"401\n402",
                       "files":{installer.CONTROLLER_PATH:{"sha256":controller_hash,"mode":"755"}}}
                class FakeADB:
                    serial="fixture"
                    def __init__(self):self.launched=False
                    def shell(self,*args):
                        if args[:2]==("sh","-c"):
                            if "nohup setsid" in args[2]:self.launched=True
                            if "if test -f" in args[2]:return "0"
                        return ""
                    def run(self,*args):
                        if args[0]=="pull":
                            data=b"not-needed\n"
                            if args[1].endswith("/rollback-app"):data=b"old app"
                            if args[1].endswith("/rollback-controller"):data=b"bad copy" if corrupt_backup else b"old controller"
                            Path(args[2]).write_bytes(data)
                adb=FakeADB()
                with patch.object(installer,"app_snapshot",side_effect=[baseline,after]):
                    if corrupt_backup:
                        with self.assertRaisesRegex(ValueError,"Controller rollback copy"):
                            installer.install_app_update(adb,output,manifest)
                        self.assertFalse(adb.launched)
                    else:
                        installer.install_app_update(adb,output,manifest)
                        self.assertTrue(adb.launched)
                        verified=json.loads((output/"runtime-update-verified.json").read_text())
                        self.assertEqual(verified["controller_sha256"],controller_hash)
                        self.assertEqual(installer.checked_app_source({"serial":"fixture","sha256":"a"*64},output/"manifest.json"),(app_hash,controller_hash))


def write_backup(directory,serial,files):
    directory.mkdir()
    with tarfile.open(directory/"original.tar","w") as archive:
        for name,data in files.items():
            info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
    metadata={"format":1,"serial":serial,"sha256":installer.digest(directory/"original.tar")}
    (directory/"backup.json").write_text(json.dumps(metadata))
    return metadata


class AuditedFilterTests(unittest.TestCase):
    def test_only_the_independently_compared_artifacts_are_accepted(self):
        self.assertEqual(installer.AUDITED_FILTER_HASHES, {
            "fe464daa3ad9e3c17adaf358cae582d0f241a4244a1db7b4a666d0e5313d30b0",
        })
        self.assertIn(installer.PROTECTED[installer.FILTER_PATH], installer.AUDITED_FILTER_HASHES)

    def test_protected_startup_script_is_the_one_in_this_release(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(installer.PROTECTED["etc/init.d/S02privacy"], installer.digest(root / "device/S02privacy"))

    @unittest.skipUnless(shutil.which("zig"), "needs the pinned Zig")
    def test_pinned_zig_builds_an_audited_filter(self):
        # Updates refuse a monitor whose filter is not audited, so a Zig change
        # must come with a fresh runtime comparison and allowlist entry.
        import build
        root = Path(__file__).resolve().parents[1]
        pinned = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))["tools"]["core:zig"]
        if subprocess.run(["zig", "version"], capture_output=True, text=True).stdout.strip() != pinned:
            self.skipTest("only the Zig version pinned in mise.toml is audited")
        with tempfile.TemporaryDirectory() as directory:
            built = build.build_filter(root, Path(directory))
            self.assertIn(installer.digest(built), installer.AUDITED_FILTER_HASHES)


class BundleSourceTests(unittest.TestCase):
    def bundle(self,metadata,original=installer.patch_app.STOCK_SHA256,**changes):
        files={"etc/init.d/rcS":installer.PROTECTED["etc/init.d/rcS"],
               "etc/init.d/S02privacy":installer.PROTECTED["etc/init.d/S02privacy"],
               installer.FILTER_PATH:"fe464daa3ad9e3c17adaf358cae582d0f241a4244a1db7b4a666d0e5313d30b0",
               installer.APP_PATH:"a"*64,installer.CONTROLLER_PATH:"c"*64,**installer.current_helpers(),**changes}
        return {"format":1,"serial":metadata["serial"],"backup_sha256":metadata["sha256"],"payload_sha256":"d"*64,
                "changes":[{"path":name,"sha256":value,"mode":0o755,
                            "original":{"sha256":original,"mode":0o755} if name==installer.APP_PATH else None}
                           for name,value in files.items() if value is not None],"removals":[]}

    def test_public_release_bundle_on_stock_firmware_is_an_update_source(self):
        metadata={"serial":"fixture","sha256":"b"*64}
        cases={"valid":{},"recipient-app":{"original":"9171f9c04f533d875554f8f1f2ab8af1b7e1166526e3dbde502dca49cf0ce12b"},
               "foreign-helper":{"usr/local/sbin/cgs2-mqtt-config":"e"*64},"unaudited-filter":{installer.FILTER_PATH:"e"*64},
               "no-controller":{installer.CONTROLLER_PATH:None},"other-device":{}}
        errors={"recipient-app":"stock 4.5.6_0181","foreign-helper":"Unknown installed release","unaudited-filter":"boot files",
                "no-controller":"Unknown installed release","other-device":"must match"}
        for case,changes in cases.items():
            with self.subTest(case=case),tempfile.TemporaryDirectory() as directory:
                installed=Path(directory)/"manifest.json"
                installed.write_text(json.dumps(self.bundle(metadata,**changes)))
                checked={**metadata,"serial":"other"} if case=="other-device" else metadata
                if case=="valid":
                    self.assertEqual(installer.checked_app_source(checked,installed),("a"*64,"c"*64));continue
                with self.assertRaisesRegex(ValueError,errors[case]):installer.checked_app_source(checked,installed)

    def test_stock_bundle_update_patches_its_own_backup_and_chains(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            metadata=write_backup(root/"backup","fixture",{installer.APP_PATH:b"stock 0181 app"})
            installed=root/"bundle/manifest.json";installed.parent.mkdir()
            installed.write_text(json.dumps(self.bundle(metadata)))
            with patch.object(installer,"ADB",side_effect=AssertionError("no device")), \
                 patch.object(installer.patch_app,"patch",return_value=(b"new fixture",{})) as patcher:
                manifest=installer.prepare_app_update(root/"backup",root/"update",root,installed)
            patcher.assert_called_once_with(b"stock 0181 app")
            self.assertEqual((manifest["preimage_sha256"],manifest["controller_sha256"],manifest["protected"]),
                             ("a"*64,"c"*64,installer.PROTECTED))
            self.assertEqual(installer.checked_app_source(metadata,root/"update/preimage/manifest.json"),("a"*64,"c"*64))


if __name__ == "__main__": unittest.main()
