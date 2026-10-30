#!/usr/bin/env python3
"""Back up, install, verify, and roll back a CGS2 over USB ADB.

No third-party Python packages. No implicit reboot, TCP ADB, or server permission.
Backups contain credentials and belong under the ignored .private directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import uuid

from patch_app import RELEASE_RECORD


PATH_RE =re.compile(r"[A-Za-z0-9_.+/-]+\Z")
PREFIXES = ("etc/init.d/", "etc/cgs2-privacy/", "qingping/bin/", "usr/local/sbin/", "usr/local/share/cgs2-privacy/")
DEFAULT_REMOVALS = ("qingping/bin/miio", "qingping/bin/miio_bk", "qingping/bin/QingSnow2App.bak")
REQUIRED_FILES = {"etc/init.d/S02privacy", "etc/init.d/rcS", "etc/cgs2-privacy/allowlist.conf", "usr/local/sbin/cgs2-filter"}
MUTABLE_FILES = {"data/etc/cgs2-mqtt.ini", "etc/cgs2-privacy/allowlist.conf"}
STOP_PROCESSES = """pkill -f '^(/bin/)?sh /qingping/bin/watchdog.sh' 2>/dev/null || :
killall watchdog.sh 2>/dev/null || :
pkill -f '^(/bin/)?sh /qingping/bin/miio/' 2>/dev/null || :
killall miio_client QingSnow2App ntpd 2>/dev/null || :
for attempt in 1 2 3 4 5; do
    pgrep -x QingSnow2App >/dev/null || break
    sleep 1
done
if pgrep -x QingSnow2App >/dev/null; then
    echo 'Display did not stop; refusing maintenance while it can re-arm the hardware watchdog' >&2
    exit 1
fi
# The app feeds the hardware watchdog every five seconds. Magic-close only
# after it has exited, so a final application feed cannot re-arm the timer.
echo V > /dev/watchdog
"""

# Run only after STOP_PROCESSES: the display can otherwise restart the helper.
# Keep this separate because the services extension installer imports the older
# display/vendor shutdown sequence and has its own service rollback handling.
STOP_INTERNET_SERVICES = """cgs2_services_seen=0
if [ -d /run/cgs2-services ] || pgrep -x cgs2-services >/dev/null; then
    cgs2_services_seen=1
fi
if [ -x /usr/local/sbin/cgs2-services ]; then
    timeout 25 /usr/local/sbin/cgs2-services stop >/dev/null 2>&1 || :
fi
for attempt in 1 2 3 4 5; do
    pgrep -x cgs2-services >/dev/null || break
    sleep 1
done
if pgrep -x cgs2-services >/dev/null; then
    # Both the supervisor and worker share this process name.
    killall cgs2-services 2>/dev/null || :
    for attempt in 1 2 3 4 5; do
        pgrep -x cgs2-services >/dev/null || break
        sleep 1
    done
fi
if pgrep -x cgs2-services >/dev/null; then
    echo 'Internet services did not stop; refusing to restore or remove their files' >&2
    exit 1
fi
if [ -x /usr/local/sbin/cgs2-services ]; then
    # Clear transient leases before deleting/replacing their owner. This keeps
    # default deny and the existing persistent MQTT permissions in the kernel.
    if ! timeout 30 /usr/local/sbin/cgs2-services runtime-reset; then
        echo 'Could not clear transient server permissions; refusing rollback' >&2
        exit 1
    fi
elif [ "$cgs2_services_seen" = 1 ]; then
    echo 'Services helper is missing; cannot verify removal of transient server permissions' >&2
    exit 1
fi
"""


def digest(path: Path) -> str:
    with path.open("rb") as source:
        result = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not PATH_RE.fullmatch(value) or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"Unsafe relative device path: {value!r}")
    if not value.startswith(PREFIXES) and value not in {"data/etc/cgs2-mqtt.ini", "qingping/etc/os-release"}:
        raise ValueError(f"Outside supported installation paths: {value!r}")
    if value.startswith("etc/init.d/") and value not in {"etc/init.d/S02privacy", "etc/init.d/rcS", "etc/init.d/S49ntp", "etc/init.d/S80wifireconnect"}:
        raise ValueError("Only the audited privacy/rcS/NTP init scripts may be modified; USB init is protected")
    return value


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


class ADB:
    def __init__(self, executable: str, serial: str | None):
        self.executable = executable
        listing = subprocess.run([executable, "devices"], check=True, capture_output=True, text=True).stdout
        connected = [line.split()[0] for line in listing.splitlines() if len(line.split()) == 2 and line.split()[1] == "device"]
        if serial is None:
            if len(connected) != 1:
                raise ValueError("Exactly one authorized device is required; otherwise specify --serial")
            serial = connected[0]
        if serial not in connected:
            raise ValueError("Selected serial is not an authorized, connected ADB device")
        self.serial = serial
        if self.shell("id", "-u").strip() != "0":
            raise ValueError("CGS2 root USB ADB is required")

    def run(self, *arguments: str) -> str:
        result = subprocess.run([self.executable, "-s", self.serial, *arguments], check=True, capture_output=True, text=True)
        return result.stdout.replace("\r\n", "\n")

    def shell(self, *arguments: str) -> str:
        # Old Linux adbd does not propagate remote exit codes. An explicit final
        # marker also detects truncated responses and disconnects during writes.
        command = shlex.join(arguments) + '; _cgs2_rc=$?; printf "\\n__CGS2_RC_%s__\\n" "$_cgs2_rc"'
        output = self.run("shell", command)
        match = re.search(r"\n__CGS2_RC_(\d+)__\s*$", output)
        if match is None:
            raise RuntimeError("ADB command ended without its remote completion marker")
        if int(match.group(1)) != 0:
            raise RuntimeError(f"Remote command failed ({match.group(1)}): {output[:match.start()].strip()}")
        return output[:match.start()]

    def script(self, body: str, stage: str, name: str) -> str:
        with tempfile.TemporaryDirectory(prefix="cgs2-script-") as directory:
            path = Path(directory) / name
            path.write_text("#!/bin/sh\nset -eu\numask 077\n" + body, encoding="utf-8", newline="\n")
            self.run("push", str(path), stage + "/" + name)
        return self.shell("sh", stage + "/" + name, stage)


def backup(adb: ADB, destination: Path, *, minimal: bool = False) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Backup destination must be empty")
    private_directory(destination)
    stage = "/data/.cgs2-backup-" + uuid.uuid4().hex
    adb.shell("mkdir", "-m", "700", stage)
    try:
        body = '''stage=$1
cd /
set -- etc qingping data/etc
for optional in data/cfg data/bin/tools usr/local data/cgs2-stock; do
    [ ! -e "$optional" ] || set -- "$@" "$optional"
done
tar -cf "$stage/original.tar" "$@"
chmod 600 "$stage/original.tar"
sha256sum "$stage/original.tar"
'''
        if minimal:
            import recovery
            body = body.replace("set -- etc qingping data/etc", "set -- " + shlex.join(sorted(recovery.STOCK_PINS)))
            body = body.replace("data/cfg data/bin/tools usr/local data/cgs2-stock", shlex.join(DEFAULT_REMOVALS))
        adb.script(body, stage, "backup.sh")
        expected = adb.shell("sha256sum", stage + "/original.tar").split()[0]
        archive = destination / "original.tar"
        adb.run("pull", stage + "/original.tar", str(archive))
        archive.chmod(0o600)
        if digest(archive) != expected:
            raise RuntimeError("Backup transfer checksum mismatch")
        manifest = {"format": 1, "serial": adb.serial, "sha256": expected, "uname": adb.shell("uname", "-a").strip()}
        if minimal:
            manifest["minimal"] = True
        path = destination / "backup.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        path.chmod(0o600)
    finally:
        adb.shell("rm", "-rf", stage)
    print(f"Verified private backup: {destination}")


def load_backup(directory: Path) -> tuple[dict, Path]:
    data = json.loads((directory / "backup.json").read_text())
    archive = directory / "original.tar"
    if data.get("format") != 1 or digest(archive) != data["sha256"]:
        raise ValueError("Backup format or SHA256 verification failed")
    return data, archive


def tar_index(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    result = {}
    for member in archive.getmembers():
        name = member.name.rstrip("/")
        while name.startswith("./"):
            name = name[2:]
        if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise ValueError("Unsafe path in backup archive")
        if name in result:
            raise ValueError(f"Duplicate path in backup archive: {name}")
        result[name] = member
    return result


def original_info(archive: tarfile.TarFile, member: tarfile.TarInfo | None) -> dict | None:
    if member is None:
        return None
    if not member.isfile():
        raise ValueError(f"Refusing replacement of non-regular original: {member.name}")
    source = archive.extractfile(member)
    assert source is not None
    return {"sha256": hashlib.sha256(source.read()).hexdigest(), "mode": member.mode & 0o777}


def prepare(backup_dir: Path, payload: Path, output: Path, removals: list[str], *,
            mode_overrides: dict[str, int] | None = None) -> None:
    # Target permissions are part of the bundle, not the host filesystem.
    # NTFS cannot represent Linux executable/private mode bits reliably.
    if mode_overrides is not None:
        for name, mode in mode_overrides.items():
            safe_path(name)
            if type(mode) is not int or not 0 <= mode <= 0o777:
                raise ValueError("Invalid target permission mode")
    metadata, archive_path = load_backup(backup_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Bundle destination must be empty")
    private_directory(output)
    changes = []
    with tarfile.open(archive_path) as stock, tarfile.open(output / "payload.tar", "w") as packed:
        originals = tar_index(stock)
        for source in sorted(payload.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"Payload symlinks are not supported: {source}")
            if not source.is_file():
                continue
            name = safe_path(source.relative_to(payload).as_posix())
            if mode_overrides is not None and name not in mode_overrides:
                raise ValueError(f"Missing explicit target permission mode: {name}")
            mode = mode_overrides[name] if mode_overrides is not None else source.stat().st_mode & 0o777
            changes.append({"path": name, "sha256": digest(source), "mode": mode,
                            "original": original_info(stock, originals.get(name))})
            info = packed.gettarinfo(str(source), arcname=name)
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with source.open("rb") as content:
                packed.addfile(info, content)
        removed = []
        for requested in removals:
            name = safe_path(requested)
            if any(change["path"] == name or change["path"].startswith(name + "/") for change in changes):
                raise ValueError("A removal overlaps a payload file")
            if name in originals:
                removed.append(name)
    if not REQUIRED_FILES <= {item["path"] for item in changes}:
        raise ValueError("Payload must include filter, empty/approved allowlist, privacy init, and patched rcS")
    if mode_overrides is not None and set(mode_overrides) != {item["path"] for item in changes}:
        raise ValueError("Target permission map includes files absent from the payload")
    manifest = {"format": 1, "serial": metadata["serial"], "backup_sha256": metadata["sha256"],
                "payload_sha256": digest(output / "payload.tar"), "changes": changes, "removals": removed}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "manifest.json").chmod(0o600)
    (output / "payload.tar").chmod(0o600)
    print(f"Prepared {len(changes)} files and {len(removed)} removals in {output}")


def load_bundle(bundle: Path, backup_dir: Path, adb: ADB) -> tuple[dict, Path]:
    metadata, archive = load_backup(backup_dir)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("format") != 1 or metadata["serial"] != adb.serial or manifest["serial"] != adb.serial:
        raise ValueError("Backup, bundle, and connected device identities must match")
    if manifest["backup_sha256"] != metadata["sha256"] or manifest["payload_sha256"] != digest(bundle / "payload.tar"):
        raise ValueError("Bundle or backup checksum mismatch")
    paths = set()
    for change in manifest["changes"]:
        name = safe_path(change["path"])
        if name in paths or not re.fullmatch(r"[0-9a-f]{64}", change["sha256"]):
            raise ValueError("Invalid bundle file manifest")
        if not isinstance(change["mode"], int) or not 0 <= change["mode"] <= 0o777:
            raise ValueError("Invalid file permissions")
        paths.add(name)
    for name in manifest["removals"]:
        safe_path(name)
        if any(path == name or path.startswith(name + "/") for path in paths):
            raise ValueError("A removal overlaps a payload file")
    if not REQUIRED_FILES <= paths:
        raise ValueError("The bundle is missing required boot privacy files")
    with tarfile.open(archive) as stock:
        index = tar_index(stock)
        for change in manifest["changes"]:
            if change["original"] != original_info(stock, index.get(change["path"])):
                raise ValueError("Bundle preimage differs from the verified backup")
        for name in manifest["removals"]:
            if name not in index:
                raise ValueError("A removal has no copy in the verified backup")
    with tarfile.open(bundle / "payload.tar") as payload:
        contents = tar_index(payload)
        if set(contents) != paths:
            raise ValueError("Payload contents differ from manifest")
        for change in manifest["changes"]:
            info = original_info(payload, contents[change["path"]])
            if info["sha256"] != change["sha256"]:
                raise ValueError("Payload file checksum mismatch")
    return manifest, archive


def check_line(path: str, sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Invalid SHA256")
    return f"test \"$(sha256sum {shlex.quote(path)} | cut -d ' ' -f 1)\" = {sha256}\n"


def release_record_script(installed: str | None = None) -> str:
    """Record when the modification was installed, for the About page.

    The computer's clock is used: the monitor's clock can be unset.
    """
    installed = installed or datetime.now().strftime("%Y-%m-%d %H:%M")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", installed):
        raise ValueError("Invalid installation time")
    temporary = shlex.quote(RELEASE_RECORD + ".cgs2-new")
    return (f"printf '[release]\\ninstalled=\"%s\"\\n' {shlex.quote(installed)} > {temporary}\n"
            f"chmod 644 {temporary}\nsync\nmv -f {temporary} {shlex.quote(RELEASE_RECORD)}\nsync\n")


def install(adb: ADB, bundle: Path, backup_dir: Path) -> None:
    import recovery
    manifest, _ = load_bundle(bundle, backup_dir, adb)
    stock = bundle / "stock"
    metadata = recovery.validate(stock, adb.serial)
    expected_worker = recovery.worker(metadata, manifest, digest(stock / "manifest.json"),
                                      digest(stock / "filter"), digest(stock / "ResetStatus.qml"))
    if (stock / "restore.sh").read_text(encoding="utf-8") != expected_worker:
        raise ValueError("Restore worker differs from the reviewed implementation")
    changes = {item["path"]: item for item in manifest["changes"]}
    if digest(stock / "filter") != changes["usr/local/sbin/cgs2-filter"]["sha256"]:
        raise ValueError("Stock recovery filter differs from the installation payload")
    if digest(stock / "ResetStatus.qml") != digest(Path(__file__).resolve().parents[1] / "device/ResetStatus.qml"):
        raise ValueError("Stock recovery view differs from the reviewed implementation")
    receipt = {**manifest, "backup_sha256": metadata["sha256"]}
    if json.loads((stock / "installed.json").read_text(encoding="utf-8")) != receipt:
        raise ValueError("Stock recovery receipt differs from the installation bundle")
    needed = (sum(p.stat().st_size for p in stock.iterdir()) + 2 * (bundle / "payload.tar").stat().st_size) // 1024 + 8192
    adb.shell("sh", "-ec", f'test "$(df -Pk /data | awk \'END {{print $4}}\')" -gt {needed}')
    stage = "/data/.cgs2-install-" + uuid.uuid4().hex
    adb.shell("mkdir", "-m", "700", stage)
    try:
        adb.run("push", str(bundle / "payload.tar"), stage + "/payload.tar")
        adb.run("push", str(stock), stage + "/stock")
        body = 'stage=$1\n' + check_line(stage + "/payload.tar", manifest["payload_sha256"])
        body += 'mkdir "$stage/payload"\ntar -xf "$stage/payload.tar" -C "$stage/payload"\n'
        # Refuse unknown firmware or edits made since the backup, before mutation.
        for change in manifest["changes"]:
            path = "/" + change["path"]
            body += recovery.no_links(path) + recovery.no_links(path + ".cgs2-new")
            original = change["original"]
            if original is None:
                body += f"test ! -e {shlex.quote(path)}\ntest ! -L {shlex.quote(path)}\n"
            else:
                body += f"test ! -L {shlex.quote(path)}\n" + check_line(path, original["sha256"])
            body += check_line(stage + "/payload" + path, change["sha256"])
        # Commit recovery before stopping the app or replacing any original.
        body += recovery.upload_script(stock, stage)
        body += "sh /data/cgs2-stock/restore.sh check\n"
        # Live enforcement precedes stopping services and changing persistent files.
        body += '''for path in /proc/sys/net/ipv6/conf/*/disable_ipv6; do
    [ ! -e "$path" ] || echo 1 > "$path"
done
echo 0 > /proc/sys/net/ipv4/ip_forward
for path in /proc/sys/net/ipv6/conf/*/forwarding; do
    [ ! -e "$path" ] || echo 0 > "$path"
done
"$stage/payload/usr/local/sbin/cgs2-filter" apply --config "$stage/payload/etc/cgs2-privacy/allowlist.conf"
'''
        body += STOP_PROCESSES
        # Install the boot guard first. A power interruption before its remaining
        # dependencies arrive then boots USB rescue instead of an open network.
        # Sync each copy before its rename: with delayed allocation, a power loss
        # could otherwise keep the rename with an empty file, and the shell runs
        # an empty filter or S02privacy as a script that exits 0.
        ordered = sorted(manifest["changes"], key=lambda item: item["path"] != "etc/init.d/rcS")
        for change in ordered:
            path = "/" + change["path"]
            quoted = shlex.quote(path)
            temporary = shlex.quote(path + ".cgs2-new")
            body += f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}\n"
            body += f"cp {shlex.quote(stage + '/payload' + path)} {temporary}\nchmod {change['mode']:o} {temporary}\nsync\nmv -f {temporary} {quoted}\n"
            body += check_line(path, change["sha256"])
            if change["path"] == "etc/init.d/rcS":
                body += "sync\n"
        for name in manifest["removals"]:
            body += "rm -rf " + shlex.quote("/" + name) + "\n"
        body += "chmod 700 /etc/cgs2-privacy\n/etc/init.d/S02privacy start\nsync\n"
        body += release_record_script()
        print(adb.script(body, stage, "install.sh").strip())
    finally:
        adb.shell("rm", "-rf", stage)
    print("Installed and verified. The stock copy is on the monitor. Power-cycle it with its physical controls.")


def verify(adb: ADB, bundle: Path, backup_dir: Path) -> None:
    manifest, _ = load_bundle(bundle, backup_dir, adb)
    for change in manifest["changes"]:
        if change["path"] in MUTABLE_FILES:
            adb.shell("test", "-f", "/" + change["path"])
            continue
        actual = adb.shell("sha256sum", "/" + change["path"]).split()[0]
        if actual != change["sha256"]:
            raise RuntimeError(f"Installed file differs: {change['path']}")
    for name in manifest["removals"]:
        adb.shell("test", "!", "-e", "/" + name)
    adb.shell("/usr/local/sbin/cgs2-filter", "dry-run", "--config", "/etc/cgs2-privacy/allowlist.conf")
    print(adb.shell("/usr/local/sbin/cgs2-filter", "status").strip())
    adb.shell("sh", "/data/cgs2-stock/restore.sh", "check")
    print("Immutable installed files and removals verified. Mutable MQTT settings preserved; current allowlist syntax validated.")


def rollback(adb: ADB, bundle: Path, backup_dir: Path) -> None:
    """Restore vendor software and clear user data from a verified PC source."""
    import recovery
    recovery.from_backup(adb, bundle, backup_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb", help="ADB executable path")
    parser.add_argument("--serial", help="Required when multiple USB devices are connected")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backup", help="Create a checksum-verified private backup")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("prepare", help="Build an installation manifest and payload; no device access")
    p.add_argument("--backup", type=Path, required=True)
    p.add_argument("--payload", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--remove", action="append", default=list(DEFAULT_REMOVALS))
    for name in ("install", "verify", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--bundle", type=Path, required=True)
        p.add_argument("--backup", type=Path, required=True)
    sub.add_parser("status", help="Read filter counters, boot state, and process names")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.backup, args.payload, args.output, args.remove)
        return
    adb = ADB(args.adb, args.serial)
    if args.command == "backup":
        backup(adb, args.output)
    elif args.command == "install":
        install(adb, args.bundle, args.backup)
    elif args.command == "verify":
        verify(adb, args.bundle, args.backup)
    elif args.command == "rollback":
        rollback(adb, args.bundle, args.backup)
    else:
        print(adb.shell("/usr/local/sbin/cgs2-filter", "status").strip())
        print(adb.shell("test", "-f", "/run/cgs2-privacy.ready"))
        print(adb.shell("ps").strip())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
