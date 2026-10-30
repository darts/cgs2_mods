"""One stock copy and restore worker, shared by Reset and USB removal.

This is not a flash image or personal-data backup. No downloads are made.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import tempfile
import time
import device
import patch_app
import patch_services

STOCK = "/data/cgs2-stock"
STOCK_PINS = {**patch_services.ORIGINALS, "qingping/bin/QingSnow2App": patch_app.STOCK_SHA256}
MAX_ARCHIVE = 64 * 1024 * 1024
RUNTIME_FILES = (
    "etc/cgs2-privacy/services.json", "etc/cgs2-privacy/allowlist.conf",
    *("data/etc/cgs2-" + name for name in (
        "homeassistant.ini", "homeassistant-state.json", "screensaver.ini",
        "services-draft.ini", "location-search.ini", "location-search-last-id.json",
        "mqtt.ini", "mqtt-applied.ini", "release.ini")),
)
PERSONAL_FILES = (
    "data/etc/setting.ini", "data/etc/alarmx.ini", "data/etc/Snow2.db", "data/etc/Snow2.db-journal",
    "data/etc/Snow2.db-wal", "data/etc/Snow2.db-shm", "data/etc/timezone", "data/etc/localtime",
    "data/etc/miio/device.country", "data/etc/miio/device.token", "data/etc/wifi/wifi.flag",
    "data/cfg/wpa_supplicant.conf",
)

PERSONAL_DIRECTORIES = ("data/etc/log", "data/etc/debug", "data/etc/debugFile")


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o600)


def stock_path(name: str, pins: dict) -> str:
    device.safe_path(name)
    if name not in pins and not any(name == root or name.startswith(root + "/") for root in device.DEFAULT_REMOVALS):
        raise ValueError("Unexpected file in stock copy")
    return name


def inventory(archive: Path, pins: dict) -> dict:
    if not 0 < archive.stat().st_size <= MAX_ARCHIVE:
        raise ValueError("Stock archive is empty or too large")
    entries = {}
    with tarfile.open(archive) as packed:
        index = device.tar_index(packed)
        if len(index) > 1000 or sum(item.size for item in index.values()) > MAX_ARCHIVE:
            raise ValueError("Stock inventory is too large")
        for name, member in index.items():
            stock_path(name, pins)
            if member.isdir():
                entries[name] = {"type": "directory", "mode": member.mode & 0o777}
            elif member.isfile():
                entries[name] = {"type": "file", **device.original_info(packed, member)}
            else:
                raise ValueError("Stock copy must contain only regular files and directories")
    for name, expected in pins.items():
        if entries.get(name, {}).get("sha256") != expected:
            raise ValueError(f"Stock copy does not match audited firmware: {name}")
    return entries


def validate(folder: Path, serial: str, *, pins: dict | None = None) -> dict:
    pins = STOCK_PINS if pins is None else pins
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if metadata.get("format") != 1 or metadata.get("serial") != serial:
        raise ValueError("Stock copy belongs to a different monitor or format")
    if device.digest(folder / "stock.tar") != metadata.get("sha256"):
        raise ValueError("Stock archive checksum mismatch")
    if inventory(folder / "stock.tar", pins) != metadata.get("files"):
        raise ValueError("Stock inventory differs from its archive")
    return metadata


def create(backup: Path, manifest: dict, destination: Path, *, pins: dict | None = None) -> dict:
    """Canonical archive; private migrations may supply their own audited pins."""
    pins = STOCK_PINS if pins is None else pins
    metadata, archive = device.load_backup(backup)
    if metadata["serial"] != manifest["serial"]:
        raise ValueError("Stock source and installation identities differ")
    device.private_directory(destination)
    with tarfile.open(archive) as source, tarfile.open(destination / "stock.tar", "w") as output:
        index = device.tar_index(source)
        for name, member in sorted(index.items()):
            if name not in pins and not any(name == root or name.startswith(root + "/") for root in device.DEFAULT_REMOVALS):
                continue
            stock_path(name, pins)
            if not (member.isfile() or member.isdir()):
                raise ValueError("Links and special files cannot enter the stock copy")
            info = copy.copy(member)
            info.name = name
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.pax_headers = {}
            output.addfile(info, source.extractfile(member) if member.isfile() else None)
    (destination / "stock.tar").chmod(0o600)
    saved = {"format": 1, "serial": metadata["serial"], "sha256": device.digest(destination / "stock.tar"),
             "files": inventory(destination / "stock.tar", pins)}
    write_json(destination / "manifest.json", saved)
    receipt = copy.deepcopy(manifest)
    receipt["backup_sha256"] = saved["sha256"]
    write_json(destination / "installed.json", receipt)
    return saved


def no_links(path: str) -> str:
    checks = []
    for parent in (PurePosixPath(path), *PurePosixPath(path).parents):
        if str(parent) == "/":
            continue
        if str(parent) == "/data":
            # Both audited stock images use this relative link to their data
            # partition. Refuse every other link, including a linked target.
            checks.append('{ test ! -L /data || { test "$(readlink /data)" = userdata && test ! -L /userdata && test -d /userdata; }; }\n')
        else:
            checks.append(f"test ! -L {shlex.quote(str(parent))}\n")
    return "".join(checks)


def worker(metadata: dict, installed: dict, manifest_hash: str, filter_hash: str, view_hash: str) -> str:
    """Fixed paths/hashes protect the validated tar; no runtime manifest execution."""
    for value in (metadata["sha256"], manifest_hash, filter_hash, view_hash):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid recovery checksum")
    files = metadata["files"]
    added = [change["path"] for change in installed["changes"] if change["original"] is None]
    for name in added:
        device.safe_path(name)
    preflight = no_links(STOCK) + no_links("/data/.cgs2-restore")
    for name, checksum in (("stock.tar", metadata["sha256"]), ("manifest.json", manifest_hash),
                           ("filter", filter_hash), ("ResetStatus.qml", view_hash)):
        preflight += no_links(STOCK + "/" + name) + device.check_line(STOCK + "/" + name, checksum)
    for name in sorted(set(files) | set(added) | set(RUNTIME_FILES) | set(PERSONAL_FILES) | set(PERSONAL_DIRECTORIES)):
        target = str(PurePosixPath("/" + name).parent) if name == "data/etc/localtime" else "/" + name
        preflight += no_links(target)
        if name in files or name in ("data/etc/setting.ini", "data/cfg/wpa_supplicant.conf"):
            preflight += no_links("/" + name + ".cgs2-new")
    # A function called in an if/! context does not inherit reliable set -e
    # behavior in POSIX shells. Every preflight assertion must return explicitly.
    preflight = "".join(line + " || return 1\n" for line in preflight.splitlines())
    directories = sorted((name for name, item in files.items() if item["type"] == "directory"),
                         key=lambda n: (n.count("/"), n))
    restores = "".join(f"mkdir -p /{name}\nchmod {files[name]['mode']:o} /{name}\n" for name in directories)
    normal, boot = "", ""
    for name, item in sorted(files.items()):
        if item["type"] != "file":
            continue
        device.safe_path(name)
        text = (f"mkdir -p {shlex.quote('/' + str(PurePosixPath(name).parent))}\n"
                f"cp -p {shlex.quote('/data/.cgs2-restore/' + name)} {shlex.quote('/' + name + '.cgs2-new')}\n"
                f"chmod {item['mode']:o} {shlex.quote('/' + name + '.cgs2-new')}\nsync\n"
                f"mv -f {shlex.quote('/' + name + '.cgs2-new')} {shlex.quote('/' + name)}\n"
                + device.check_line('/' + name, item['sha256']))
        if name == "etc/init.d/rcS":
            boot += text
        else:
            normal += text
    removes = "".join("rm -f " + shlex.quote("/" + name) + "\n"
                      for name in sorted(set(added) | set(RUNTIME_FILES) | set(PERSONAL_FILES)))
    removes += "".join("rm -rf " + shlex.quote("/" + name) + "\n" for name in PERSONAL_DIRECTORIES)
    extracted = "".join(device.check_line("/data/.cgs2-restore/" + name, item["sha256"])
                        for name, item in files.items() if item["type"] == "file")
    return '''#!/bin/sh
set -eu
umask 077
status=/tmp/cgs2-reset-status.json
runtime=/run/cgs2-reset
lock=/run/cgs2-mqtt-config.lock
message() {
    printf '{"ok":%s,"message":"%s"}\\n' "$1" "$2" > "$status.new"
    mv -f "$status.new" "$status"
}
preflight() {
''' + preflight + '''}
case "$1" in
    check) preflight; exit 0 ;;
    start)
        mkdir "$lock" 2>/dev/null || { message false 'An update is running. Try Reset after it finishes.'; exit 1; }
        trap 'message false "Restore could not start. The stock copy is kept. Use USB recovery."; rmdir "$lock" 2>/dev/null || :' EXIT
        message null 'Checking the stock copy. Keep the power connected.'
        if ! (set -e; preflight); then
            message false 'The stock copy failed verification. No files were restored. Use a verified computer backup.'
            exit 1
        fi
        test ! -L "$runtime"
        mkdir -p "$runtime"
        cp /data/cgs2-stock/restore.sh "$runtime/restore.sh"
        cp /data/cgs2-stock/ResetStatus.qml "$runtime/ResetStatus.qml"
        cp /data/cgs2-stock/filter "$runtime/filter"
        chmod 700 "$runtime/filter"
        : > "$runtime/empty.conf"
        trap '' HUP
        setsid sh "$runtime/restore.sh" run > "$runtime/restore.log" 2>&1 < /dev/null &
        trap - EXIT
        exit 0 ;;
    run) test -d "$lock" ;;
    *) exit 2 ;;
esac
finished=0
cleanup() {
    if [ "$finished" != 1 ]; then
        message false 'Restore stopped. Keep USB connected. The stock copy is kept for recovery.'
    fi
    rmdir "$lock" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 1' INT TERM
preflight
rm -rf /data/.cgs2-restore
required=$(du -k /data/cgs2-stock/stock.tar | awk '{print $1}')
available=$(df -Pk /data | awk 'END {print $4}')
test "$available" -gt "$((required * 2 + 8192))"
mkdir -m 700 /data/.cgs2-restore
tar -xf /data/cgs2-stock/stock.tar -C /data/.cgs2-restore
''' + extracted + device.STOP_PROCESSES + '''
if [ -x /usr/local/sbin/cgs2-services ]; then
''' + device.STOP_INTERNET_SERVICES + '''
else
    ! pgrep -x cgs2-services >/dev/null
fi
"$runtime/filter" apply --config "$runtime/empty.conf"
killall wpa_supplicant dhcpcd udhcpc 2>/dev/null || :
ifconfig wlan0 down
set +u
. /etc/profile
set -u
export QT_QPA_FONTDIR=/usr/share/fonts QT_QPA_EGLFS_ALWAYS_SET_MODE=1 QT_QPA_EGLFS_KMS_ATOMIC=1
export QT_QPA_EGLFS_WIDTH=720 QT_QPA_EGLFS_HEIGHT=720 QT_QPA_EGLFS_PHYSICAL_WIDTH=720 QT_QPA_EGLFS_PHYSICAL_HEIGHT=720
export QML_DISABLE_DISK_CACHE=1
qmlscene -platform eglfs "$runtime/ResetStatus.qml" > "$runtime/display.log" 2>&1 < /dev/null &
view_pid=$!
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$view_pid"
    grep -q CGS2_RESET_DISPLAY_READY "$runtime/display.log" && break
    sleep 1
done
grep -q CGS2_RESET_DISPLAY_READY "$runtime/display.log"
message null 'Restoring vendor software. Keep the power connected.'
''' + "".join("rm -rf " + shlex.quote("/" + name) + "\n" for name in device.DEFAULT_REMOVALS) + restores + normal + removes + '''
mkdir -p /data/etc /data/cfg
printf '[application]\\nwifi_disabled=true\\n' > /data/etc/setting.ini.cgs2-new
chmod 600 /data/etc/setting.ini.cgs2-new
sync
mv -f /data/etc/setting.ini.cgs2-new /data/etc/setting.ini
# The stock reconnect script has unquoted tests that treat an empty file as
# configured. Leave it absent so its outer existence check stops reconnection.
test ! -e /data/cfg/wpa_supplicant.conf
grep -qx wifi_disabled=true /data/etc/setting.ini
''' + boot + '''sync
rm -f /run/cgs2-privacy.ready /run/cgs2-readings.ini
rmdir /etc/cgs2-privacy /usr/local/share/cgs2-privacy 2>/dev/null || :
rm -rf /run/cgs2-services /data/.cgs2-restore
rm -rf /data/cgs2-stock
sync
finished=1
message true 'Restore complete. Switch the monitor off, then on with its power control. Wi-Fi will stay off.'
'''


def finish_copy(folder: Path, installed: dict, filter_path: Path, view: Path) -> None:
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for source, name in ((filter_path, "filter"), (view, "ResetStatus.qml")):
        (folder / name).write_bytes(source.read_bytes())
        (folder / name).chmod(0o700 if name == "filter" else 0o600)
    script = worker(metadata, installed, device.digest(folder / "manifest.json"), device.digest(filter_path), device.digest(view))
    (folder / "restore.sh").write_text(script, encoding="utf-8", newline="\n")
    (folder / "restore.sh").chmod(0o700)


def upload_script(folder: Path, stage: str) -> str:
    """Commit verified originals before any stock-file mutation."""
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    body = no_links(STOCK)
    for item in sorted(folder.iterdir()):
        body += device.check_line(stage + "/stock/" + item.name, device.digest(item))
    for name, item in metadata["files"].items():
        body += no_links("/" + name)
        if item["type"] == "file":
            body += device.check_line("/" + name, item["sha256"])
    body += f"if [ -e {STOCK} ]; then\n"
    for item in sorted(folder.iterdir()):
        body += no_links(STOCK + "/" + item.name) + device.check_line(STOCK + "/" + item.name, device.digest(item))
    body += f"else\nchmod 700 {shlex.quote(stage + '/stock')}\nsync\nmv {shlex.quote(stage + '/stock')} {STOCK}\nsync\nfi\n"
    return body


def present(adb: device.ADB) -> bool:
    return adb.shell("sh", "-c", f"test -f {STOCK}/manifest.json && echo yes || echo no").strip() == "yes"


def fetch(adb: device.ADB, destination: Path) -> tuple[Path, Path]:
    """Recreate build inputs on another computer without changing the monitor."""
    device.private_directory(destination)
    stock = destination / "stock"
    device.private_directory(stock)
    for name in ("stock.tar", "manifest.json", "installed.json"):
        adb.run("pull", STOCK + "/" + name, str(stock / name))
        (stock / name).chmod(0o600)
    metadata = validate(stock, adb.serial)
    backup, installed = destination / "backup", destination / "installed"
    device.private_directory(backup)
    device.private_directory(installed)
    (stock / "stock.tar").replace(backup / "original.tar")
    write_json(backup / "backup.json", {"format": 1, "serial": adb.serial, "sha256": metadata["sha256"], "minimal": True})
    receipt = json.loads((stock / "installed.json").read_text(encoding="utf-8"))
    import install_services
    install_services.checked_bundle_source({"serial": adb.serial, "sha256": metadata["sha256"]}, receipt)
    for change in receipt["changes"]:
        device.safe_path(change["path"])
        if change["path"] not in device.MUTABLE_FILES:
            adb.shell("sh", "-ec", device.check_line("/" + change["path"], change["sha256"]))
    write_json(installed / "manifest.json", receipt)
    return backup, installed


def record_update(adb: device.ADB, output: Path, manifest: dict) -> None:
    if not present(adb):
        return  # Older owner installations have a separate private migration.
    target = output / "installed-receipt.json"
    adb.run("pull", STOCK + "/installed.json", str(target))
    receipt = json.loads(target.read_text(encoding="utf-8"))
    if receipt.get("serial") != adb.serial or receipt.get("format") != 1:
        raise ValueError("Recovery installation receipt does not match this monitor")
    updates = {"qingping/bin/QingSnow2App": manifest["app_sha256"], "usr/local/sbin/cgs2-services": manifest["controller_sha256"]}
    for change in receipt["changes"]:
        if change["path"] in updates:
            change["sha256"] = updates.pop(change["path"])
    if updates:
        raise ValueError("Recovery installation receipt is incomplete")
    write_json(target, receipt)
    adb.run("push", str(target), STOCK + "/installed.json.new")
    adb.shell("sh", "-ec", device.check_line(STOCK + "/installed.json.new", device.digest(target))
              + f"chmod 600 {STOCK}/installed.json.new\nsync\nmv {STOCK}/installed.json.new {STOCK}/installed.json\nsync\n")


def start(adb: device.ADB) -> None:
    adb.shell("sh", STOCK + "/restore.sh", "check")
    adb.shell("sh", STOCK + "/restore.sh", "start")
    print("Restoration started. Follow the instructions on the monitor. Keep power connected.")
    for attempt in range(180):
        result = json.loads(adb.shell("cat", "/tmp/cgs2-reset-status.json"))
        if result.get("ok") is True:
            print(result["message"])
            return
        if result.get("ok") is False:
            raise RuntimeError(result["message"])
        time.sleep(1)
    raise RuntimeError("Restore is still running. Keep power and USB connected; read the monitor's status.")


def from_backup(adb: device.ADB, bundle: Path | None, backup: Path) -> None:
    """Explicit PC fallback, using the same restore worker and erase behavior."""
    with tempfile.TemporaryDirectory(prefix="cgs2-restore-") as temporary:
        folder = Path(temporary) / "stock"
        saved, archive = device.load_backup(backup)
        if saved["serial"] != adb.serial:
            raise ValueError("Computer backup belongs to another monitor")
        device.private_directory(folder)
        with tarfile.open(archive) as packed:
            index = device.tar_index(packed)
            embedded = "data/cgs2-stock/stock.tar" in index
            if embedded:
                for name in ("stock.tar", "manifest.json", "installed.json", "filter"):
                    member = index.get("data/cgs2-stock/" + name)
                    if member is None or not member.isfile() or member.size > MAX_ARCHIVE:
                        raise ValueError("Computer backup has an incomplete stock copy")
                    (folder / name).write_bytes(packed.extractfile(member).read())
        if embedded:
            metadata = validate(folder, adb.serial)
            manifest = json.loads((folder / "installed.json").read_text(encoding="utf-8"))
            if manifest.get("serial") != adb.serial or manifest.get("backup_sha256") != metadata["sha256"]:
                raise ValueError("Stock copy receipt does not match the computer backup")
            filter_path = folder / "filter"
        else:
            if bundle is None:
                raise ValueError("This backup predates installation. Prepare an install bundle, then use Remove with that bundle.")
            manifest, _ = device.load_bundle(bundle, backup, adb)
            create(backup, manifest, folder)
            with tarfile.open(bundle / "payload.tar") as packed:
                member = device.tar_index(packed)["usr/local/sbin/cgs2-filter"]
                filter_path = Path(temporary) / "filter"
                filter_path.write_bytes(packed.extractfile(member).read())
        import install_services
        if device.digest(filter_path) not in install_services.AUDITED_FILTER_HASHES:
            raise ValueError("Backup fallback needs the audited filter")
        finish_copy(folder, manifest, filter_path, Path(__file__).resolve().parents[1] / "device/ResetStatus.qml")
        stage = "/data/.cgs2-restore-source"
        adb.shell("sh", "-ec", no_links(stage) + f"test ! -e {stage}\nmkdir -m 700 {stage}\n")
        adb.run("push", str(folder), stage + "/stock")
        body = no_links(STOCK) + no_links(STOCK + ".previous") + f"test ! -e {STOCK}.previous\n"
        for item in folder.iterdir():
            body += device.check_line(stage + "/stock/" + item.name, device.digest(item))
        body += (f"sync\nif [ -e {STOCK} ]; then mv {STOCK} {STOCK}.previous; fi\n"
                 f"mv {stage}/stock {STOCK}\nsync\nrm -rf {STOCK}.previous {stage}\n")
        adb.script(body, stage, "save-stock.sh")
    start(adb)

