"""Portable build planning and target-bundle metadata checks; no device access."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import build
import device
import prepare


def elf(machine=183, interpreter=False):
    content=bytearray(120)
    content[:6]=b"\x7fELF\x02\x01"
    struct.pack_into("<H",content,18,machine)
    struct.pack_into("<Q",content,32,64)
    struct.pack_into("<HH",content,54,56,1)
    struct.pack_into("<I",content,64,3 if interpreter else 1)
    return bytes(content)


class PortableBuildTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(prefix="cgs2 build & test ")
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)

    def test_both_builds_are_static_arm64_without_shell(self):
        commands=[]
        def fake_run(command,repo,env,**kwargs):
            commands.append((command,dict(env)))
            Path(command[command.index("-o")+1]).write_bytes(elf())
            return subprocess.CompletedProcess(command,0)
        with patch.object(build,"executable",side_effect=lambda name,explicit=None:explicit or name),patch.object(build,"run",side_effect=fake_run):
            result=build.build_all(self.root,zig=r"C:\Program Files\Zig\zig.exe",go=r"C:\Program Files\Go\go.exe")
        self.assertEqual(set(result),{"filter","services"})
        for output in result.values():build.verify_target(output)
        c,go=commands
        self.assertEqual(c[0][0],r"C:\Program Files\Zig\zig.exe")
        self.assertIn("aarch64-linux-musl",c[0]);self.assertIn("-static",c[0])
        self.assertEqual(go[0][0],r"C:\Program Files\Go\go.exe")
        self.assertEqual(go[1]["GOOS"],"linux");self.assertEqual(go[1]["GOARCH"],"arm64")
        self.assertEqual(go[1]["CGO_ENABLED"],"0");self.assertEqual(go[1]["GOTOOLCHAIN"],"local")
        self.assertTrue(str(self.root) in go[1]["GOCACHE"])

    def test_invalid_or_failed_build_keeps_previous_artifact(self):
        destination=self.root/"build/cgs2-filter";destination.parent.mkdir();destination.write_bytes(b"previous verified output")
        def invalid(command,*args,**kwargs):
            Path(command[command.index("-o")+1]).write_bytes(elf(machine=62))
        with patch.object(build,"executable",return_value="zig"),patch.object(build,"run",side_effect=invalid):
            with self.assertRaises(build.BuildError):build.build_filter(self.root)
        self.assertEqual(destination.read_bytes(),b"previous verified output")
        self.assertEqual(list(destination.parent.iterdir()),[destination])

    def test_target_verifier_rejects_dynamic_and_wrong_host_images(self):
        p=self.root/"candidate"
        for contents in (b"MZ Windows",elf(machine=62),elf(interpreter=True),elf()[:80]):
            with self.subTest(contents=contents[:8]):
                p.write_bytes(contents)
                with self.assertRaises(build.BuildError):build.verify_target(p)
        p.write_bytes(elf());build.verify_target(p)

    def test_missing_tools_gives_setup_hint(self):
        with patch.object(build.shutil,"which",return_value=None):
            with self.assertRaisesRegex(build.BuildError,"mise install"):build.executable("zig")

    def test_zig_environment_accepts_json_and_current_zon(self):
        self.assertEqual(build.zig_lib_directory('{"lib_dir":"/opt/zig/lib"}'),Path("/opt/zig/lib"))
        self.assertEqual(build.zig_lib_directory('.{\n    .lib_dir = "C:\\\\Tools\\\\Zig\\\\lib",\n}'),Path(r"C:\Tools\Zig\lib"))
        with self.assertRaises(build.BuildError):build.zig_lib_directory("no directory")

    def test_windows_skips_posix_execution_but_checks_target_build(self):
        with patch.object(build.platform,"system",return_value="Windows"),patch.object(build,"build_services") as cross,patch.object(build,"run",side_effect=AssertionError("native test ran")):
            self.assertFalse(build.test_filter(self.root))
            self.assertFalse(build.test_services(self.root))
            cross.assert_called_once_with(self.root,go=None)

    def test_python_calls_tools_with_argument_lists_not_a_shell(self):
        with patch.object(build.subprocess,"run",return_value=subprocess.CompletedProcess([],0)) as execute:
            build.run([r"C:\Compiler & Tools\go.exe","version"],self.root,{})
        self.assertFalse(execute.call_args.kwargs["shell"])
        self.assertEqual(execute.call_args.args[0][0],r"C:\Compiler & Tools\go.exe")

    def backup_and_payload(self):
        backup=self.root/"backup";backup.mkdir()
        payload=self.root/"payload";payload.mkdir()
        with tarfile.open(backup/"original.tar","w") as archive:
            for name in device.REQUIRED_FILES:
                info=tarfile.TarInfo(name);raw=b"original";info.size=len(raw);info.mode=0o644
                archive.addfile(info,io.BytesIO(raw))
                p=payload/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b"replacement");p.chmod(0o666)
        (backup/"backup.json").write_text(json.dumps({"format":1,"serial":"fixture","sha256":device.digest(backup/"original.tar")}))
        return backup,payload

    def test_target_modes_do_not_depend_on_host_permissions(self):
        backup,payload=self.backup_and_payload()
        modes={name:0o600 if name.endswith(".conf") else 0o755 for name in device.REQUIRED_FILES}
        output=self.root/"bundle"
        device.prepare(backup,payload,output,[],mode_overrides=modes)
        manifest=json.loads((output/"manifest.json").read_text())
        self.assertEqual({v["path"]:v["mode"] for v in manifest["changes"]},modes)
        with tarfile.open(output/"payload.tar") as archive:
            for member in archive:
                self.assertEqual(member.mode,modes[member.name]);self.assertEqual(member.uid,0);self.assertEqual(member.uname,"")

    def test_explicit_mode_map_must_cover_every_payload_file(self):
        backup,payload=self.backup_and_payload()
        with self.assertRaisesRegex(ValueError,"Missing explicit target"):
            device.prepare(backup,payload,self.root/"bundle",[],mode_overrides={})
        with self.assertRaisesRegex(ValueError,"Invalid target permission"):
            device.prepare(backup,payload,self.root/"other",[],mode_overrides={"usr/local/sbin/cgs2-filter":0o4755})

    def test_remote_shell_script_bytes_have_unix_newlines(self):
        adb=object.__new__(device.ADB)
        captured=[]
        adb.run=lambda *args:captured.append(Path(args[1]).read_bytes())
        adb.shell=lambda *args:"fixture completion"
        self.assertEqual(adb.script("echo one\necho two\n","/fixture","test.sh"),"fixture completion")
        self.assertEqual(captured,[b"#!/bin/sh\nset -eu\numask 077\necho one\necho two\n"])

    def test_mise_uses_pinned_core_tools_and_portable_tasks(self):
        repo=Path(__file__).resolve().parents[1]
        config=tomllib.loads((repo/"mise.toml").read_text())
        self.assertEqual(config["tools"],{"core:python":"3.15.0","core:go":"1.27.2","core:zig":"0.17.0"})
        for name in ("setup","start","build","test"):
            self.assertEqual(config["tasks"][name]["run"],"python qingping.py "+name)
            self.assertEqual(config["tasks"][name]["extends"],"pinned-tools")
        task_path=config["task_templates"]["pinned-tools"]["env"]["PATH"]
        self.assertIn('os() == "windows"',task_path)
        for tool in ("python","go","zig"):
            self.assertIn("{{ tools."+tool+".path }}",task_path)
        self.assertIn(";{{ env.PATH }}",task_path)
        self.assertIn(":{{ env.PATH }}",task_path)
        self.assertEqual(prepare.TARGET_MODES["data/etc/cgs2-mqtt.ini"],0o600)
        self.assertEqual(prepare.TARGET_MODES["qingping/bin/QingSnow2App"],0o755)


if __name__=="__main__":unittest.main()
