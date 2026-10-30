#!/usr/bin/env python3
"""Prepare/install guarded updates of an installed monitor without boot/USB edits.

Preparation includes the complete install.sh and performs no device operations.
App-only updates preserve existing services and permissions. Runtime updates
also restart the controller while retaining saved configuration and broker
permissions; transient service leases are cleaned by their owning helper.
Both start from the installed release bundle or a verified earlier update.
No updater reboots the device.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shlex
import tarfile
import time
import uuid

from device import ADB, STOP_PROCESSES, check_line, digest, load_backup, private_directory, release_record_script
import patch_app

PROTECTED = {
    "etc/init.d/rcS": "655266d868d3b09d49174314a96684e1915bfc5a5b69596703d662a0a0fd9d0f",
    "etc/init.d/S50usbdevice": "1b28d4e696f050751c796e3b43929c8deb3939effa0611ea17a615964ad63f06",
    "etc/init.d/.usb_config": "07ba2e2560d62ab7fc990a031f40c43156ec01608050d9c9fe64365678c41e50",
    "etc/init.d/S02privacy": "7c2e9207f328d663455b25d50bbe29b32d63f96415932fefd320a1ed832540ea",
    "usr/local/sbin/cgs2-filter": "fe464daa3ad9e3c17adaf358cae582d0f241a4244a1db7b4a666d0e5313d30b0",
}
FILTER_PATH = "usr/local/sbin/cgs2-filter"
# The filter that the Zig version pinned in mise.toml builds with a fresh cache.
# Another Zig build, or a cache that another Zig wrote, changes nonallocated
# metadata and thus the hash. Preparation refuses such a build, so that later
# updates accept every installed monitor. A Zig change needs a new audit entry.
AUDITED_FILTER_HASHES = frozenset({
    "fe464daa3ad9e3c17adaf358cae582d0f241a4244a1db7b4a666d0e5313d30b0",
})
RESTART = '''set +u
. /etc/profile || return 1
export QT_QPA_FONTDIR=/usr/share/fonts QT_QPA_EGLFS_ALWAYS_SET_MODE=1 QT_QPA_EGLFS_KMS_ATOMIC=1
export QT_QPA_EGLFS_WIDTH=720 QT_QPA_EGLFS_HEIGHT=720 QT_QPA_EGLFS_PHYSICAL_WIDTH=720 QT_QPA_EGLFS_PHYSICAL_HEIGHT=720
cd /qingping/bin || return 1
# The app already keeps bounded diagnostic logs. Do not create an unlimited
# second copy of its console output in RAM during a runtime update.
setsid /qingping/bin/watchdog.sh > /dev/null 2>&1 < /dev/null &
'''


def checked_hash(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid SHA256")
    return value


def audited_boot_file(name: str, value: str | None) -> bool:
    return value in AUDITED_FILTER_HASHES if name == FILTER_PATH else value == PROTECTED[name]


def boot_file_checks() -> str:
    """Shell checks of the live boot files used by every later update."""
    text = ""
    for name, value in PROTECTED.items():
        text += f"test -f /{name}\ntest ! -L /{name}\n"
        if name == FILTER_PATH:
            text += f"case \"$(sha256sum /{name} | cut -d ' ' -f 1)\" in {'|'.join(sorted(AUDITED_FILTER_HASHES))}) ;; *) false;; esac\n"
        else:
            text += check_line("/"+name, value)
    return text


APP_PATH = "qingping/bin/QingSnow2App"
CONTROLLER_PATH = "usr/local/sbin/cgs2-services"
APP_SNAPSHOT_FILES = tuple(PROTECTED) + (
    CONTROLLER_PATH, "usr/local/sbin/cgs2-mqtt-config",
    "usr/local/share/cgs2-privacy/MqttConfig.qml", "qingping/bin/start.sh",
    "qingping/bin/watchdog.sh", "etc/profile", "tmp/.usb_config",
    "etc/cgs2-privacy/services.json", "etc/cgs2-privacy/allowlist.conf",
    "data/etc/cgs2-mqtt.ini", "data/etc/cgs2-mqtt-applied.ini", "usr/local/sbin/cgs2-reset",
)
APP_DRAFT = "data/etc/cgs2-services-draft.ini"
LOCATION_DRAFT = "data/etc/cgs2-location-search.ini"
LOCATION_LAST_ID = "data/etc/cgs2-location-search-last-id.json"
RUNTIME_OPTIONAL_FILES = (LOCATION_DRAFT, LOCATION_LAST_ID)


def update_verification_name(kind: str) -> str:
    return "runtime-update-verified.json" if kind == "runtime-update" else "app-update-verified.json"


def app_source_manifest(backup: Path, installed_manifest: Path) -> tuple[dict, Path, str, str]:
    """Accept the installed release bundle or a verified earlier update."""
    metadata, archive = load_backup(backup)
    app_hash, controller_hash = checked_app_source(metadata, installed_manifest)
    return metadata, archive, app_hash, controller_hash


def checked_bundle_source(metadata: dict, source: dict) -> tuple[str, str]:
    """Read the app/controller pair from a release bundle installed on a stock 0181 device."""
    if (source.get("format") != 1 or source.get("serial") != metadata["serial"]
            or source.get("backup_sha256") != metadata["sha256"]):
        raise ValueError("Installed bundle and verified stock backup must match")
    changes = {change["path"]: change for change in source["changes"]}
    installed = {name: change["sha256"] for name, change in changes.items()}
    # The installer never replaces USB init files; preflight checks them live.
    for name in ("etc/init.d/rcS", "etc/init.d/S02privacy", FILTER_PATH):
        if not audited_boot_file(name, installed.get(name)):
            raise ValueError("Installed boot files differ from the audited release")
    if ((changes.get(APP_PATH) or {}).get("original") or {}).get("sha256") != patch_app.STOCK_SHA256:
        raise ValueError("The bundle must replace the audited stock 4.5.6_0181 application")
    # Byte-copied helper files identify the release. Its app and controller were
    # compiled by each owner's toolchain, so their exact bytes are not pinned.
    # A helper change needs an explicit migration; only this revision is accepted.
    helpers = {name: installed.get(name) for name in HELPER_FILES}
    if CONTROLLER_PATH not in installed or helpers != current_helpers():
        raise ValueError("Unknown installed release; a fresh audit is required")
    return checked_hash(installed[APP_PATH]), checked_hash(installed[CONTROLLER_PATH])


def checked_app_source(metadata: dict, installed_manifest: Path) -> tuple[str, str]:
    source = json.loads(installed_manifest.read_text(encoding="utf-8"))
    if source.get("kind") is None and "changes" in source:
        return checked_bundle_source(metadata, source)
    if source.get("kind") not in {"app-only", "runtime-update"}:
        raise ValueError("Expected the installed release bundle or a verified update manifest")
    if (source.get("format") != 1 or source.get("serial") != metadata["serial"]
            or source.get("backup_sha256") != metadata["sha256"] or source.get("protected") != PROTECTED):
        raise ValueError("Installed manifest and verified stock backup must match the audited device/boot files")
    app_hash, controller_hash = source["app_sha256"], source["controller_sha256"]
    verified = json.loads((installed_manifest.parent / update_verification_name(source["kind"])).read_text(encoding="utf-8"))
    if (verified.get("ok") is not True or verified.get("manifest_sha256") != digest(installed_manifest)
            or verified.get("serial") != metadata["serial"] or verified.get("app_sha256") != app_hash
            or verified.get("controller_sha256") != controller_hash):
        raise ValueError("A previous app update must have a matching successful verification record")
    return checked_hash(app_hash), checked_hash(controller_hash)


def app_preflight(manifest: dict) -> str:
    text = "set -eu\n" + boot_file_checks()
    for name, expected in {APP_PATH:manifest["preimage_sha256"], CONTROLLER_PATH:manifest["controller_sha256"]}.items():
        text += f"test -f /{name}\ntest ! -L /{name}\n" + check_line("/"+name, expected)
    for name in APP_SNAPSHOT_FILES:
        text += f"test -f /{name}\ntest ! -L /{name}\n"
    # The draft appears once the Internet services page has been opened.
    text += f"test ! -L /{APP_DRAFT}\n[ ! -e /{APP_DRAFT} ] || test -f /{APP_DRAFT}\n"
    text += "test -f /run/cgs2-privacy.ready\npgrep -x QingSnow2App >/dev/null\npgrep -x adbd >/dev/null\n"
    text += "/usr/local/sbin/cgs2-services ping >/dev/null\n"
    return text


def build_app_update_script(app_hash: str, preimage_hash: str, controller_hash: str,
                            replacement_controller_hash: str | None = None) -> str:
    for value in (app_hash, preimage_hash, controller_hash):
        checked_hash(value)
    runtime = replacement_controller_hash is not None
    if runtime:
        checked_hash(replacement_controller_hash)
    body = '''#!/bin/sh
set -eu
umask 077
stage=$1
baseline_sha=$2
case "$stage" in /data/.cgs2-app-update-*) ;; *) exit 2;; esac
[ "${#baseline_sha}" = 64 ] || exit 2
case "$baseline_sha" in *[!0-9a-f]*) exit 2;; esac
trap '' HUP
lock=/run/cgs2-mqtt-config.lock
app=/qingping/bin/QingSnow2App
new=$app.cgs2-app-new
restore=$app.cgs2-app-rollback
locked=0
maintenance=0
changed=0
new_owned=0
restore_owned=0
draft_captured=0
assert_hash() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(sha256sum "$1" | cut -d ' ' -f 1)" = "$2" ]
}
controller_pids() { pgrep -x cgs2-services | sort -n; }
verify_snapshot() {
    [ "$(cat /proc/sys/kernel/random/boot_id)" = "$boot_id" ] || return 1
    [ "$(controller_pids)" = "$controller_ids" ] || return 1
'''
    for name in APP_SNAPSHOT_FILES:
        if runtime and name == CONTROLLER_PATH:
            body += '''    assert_hash "$controller" "$active_controller_hash" || return 1
    [ "$(stat -c '%a' "$controller")" = "$active_controller_mode" ] || return 1
'''
            continue
        body += f'''    expected=$(jq -er --arg name {shlex.quote(name)} '.files[$name].sha256' "$stage/baseline.json") || return 1
    mode=$(jq -er --arg name {shlex.quote(name)} '.files[$name].mode' "$stage/baseline.json") || return 1
    assert_hash /{name} "$expected" || return 1
    [ "$(stat -c '%a' /{name})" = "$mode" ] || return 1
'''
    if runtime:
        for index, name in enumerate(RUNTIME_OPTIONAL_FILES):
            body += f'''    if [ "$optional_{index}_captured" = 1 ]; then
        assert_hash /{name} "$optional_{index}_sha" || return 1
        [ "$(stat -c '%a' /{name})" = "$optional_{index}_mode" ] || return 1
    fi
'''
    body += f'''    if [ "$draft_captured" = 1 ]; then
        assert_hash /{APP_DRAFT} "$draft_sha" || return 1
        [ "$(stat -c '%a' /{APP_DRAFT})" = "$draft_mode" ] || return 1
    fi
    return 0
}}
stop_display() (
    trap - EXIT
    set -e
'''+STOP_PROCESSES+''')
restart_display() {
'''+RESTART+'''}
wait_display() {
    ready=0
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        if pgrep -x QingSnow2App >/dev/null && pgrep -x adbd >/dev/null; then ready=1; break; fi
        sleep 1
    done
    [ "$ready" = 1 ] || return 1
    # Observe multiple five-second app watchdog feed intervals, not just exec.
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        sleep 1
        pgrep -x QingSnow2App >/dev/null || return 1
        pgrep -x adbd >/dev/null || return 1
    done
    /usr/local/sbin/cgs2-services ping >/dev/null || return 1
    verify_snapshot
}
restore_app() {
'''+f'''    assert_hash "$stage/rollback-app" {preimage_hash} || return 1
    [ ! -e "$restore" ] && [ ! -L "$restore" ] || return 1
    restore_owned=1
    cp "$stage/rollback-app" "$restore" || return 1
    chmod "$old_mode" "$restore" || return 1
    assert_hash "$restore" {preimage_hash} || return 1
    mv "$restore" "$app" || return 1
    assert_hash "$app" {preimage_hash} || return 1
'''+'''    [ "$(stat -c '%a' "$app")" = "$old_mode" ] || return 1
    sync
}
finish() {
    result=$?
    trap - EXIT INT TERM
    set +e
    set +u
    outcome=not-needed
    if [ "$result" != 0 ] && [ "$maintenance" = 1 ]; then
        outcome=incomplete
        # Only the display is stopped/restored. The services daemon and its
        # current permissions remain running throughout success and rollback.
        if stop_display; then
            if [ "$changed" = 0 ] || restore_app; then
                if restart_display && wait_display; then outcome=restored; fi
            fi
        fi
        [ "$outcome" = restored ] || echo 'App rollback incomplete; keep USB connected and inspect the private log.' >&2
    fi
    [ "$new_owned" = 0 ] || rm -f "$new"
    [ "$restore_owned" = 0 ] || rm -f "$restore"
    [ "$locked" = 0 ] || rmdir "$lock"
    echo "$outcome" > "$stage/rollback-result"
    echo "$result" > "$stage/result"
}
trap finish EXIT
trap 'exit 1' INT TERM
mkdir "$lock"
locked=1
assert_hash "$stage/baseline.json" "$baseline_sha"
boot_id=$(jq -er '.boot_id' "$stage/baseline.json")
controller_ids=$(jq -er '.controller_pids' "$stage/baseline.json")
old_mode=$(jq -er '.app_mode' "$stage/baseline.json")
case "$old_mode" in ''|*[!0-7]*) exit 2;; esac
'''
    body += app_preflight({"preimage_sha256":preimage_hash,"controller_sha256":controller_hash}).removeprefix("set -eu\n")
    body += f'''assert_hash "$stage/app" {app_hash}
assert_hash "$stage/rollback-app" {preimage_hash}
verify_snapshot
test ! -e "$new"
test ! -L "$new"
test ! -e "$restore"
test ! -L "$restore"
new_owned=1
cp "$stage/app" "$new"
chmod 755 "$new"
assert_hash "$new" {app_hash}
maintenance=1
stop_display
# QSettings can flush while exiting; capture the draft only once it has exited.
if [ -e /{APP_DRAFT} ] || [ -L /{APP_DRAFT} ]; then
    test -f /{APP_DRAFT}
    test ! -L /{APP_DRAFT}
    draft_sha=$(sha256sum /{APP_DRAFT} | cut -d ' ' -f 1)
    draft_mode=$(stat -c '%a' /{APP_DRAFT})
    draft_captured=1
    printf '%s\\n' "$draft_sha" > "$stage/draft.sha256"
    printf '%s\\n' "$draft_mode" > "$stage/draft.mode"
fi
verify_snapshot
changed=1
mv "$new" "$app"
assert_hash "$app" {app_hash}
test "$(stat -c '%a' "$app")" = 755
sync
restart_display
wait_display
assert_hash "$app" {app_hash}
maintenance=0
changed=0
'''
    if runtime:
        # Extend the shared reviewed worker at uniquely guarded anchors. App-only
        # callers retain exactly their existing script and process behavior.
        optional_initializers = "".join(f"optional_{index}_captured=0\n" for index in range(len(RUNTIME_OPTIONAL_FILES)))
        optional_capture = "# Snapshot private optional state only after both of its writers have exited.\n"
        for index, name in enumerate(RUNTIME_OPTIONAL_FILES):
            optional_capture += f'''if [ -e /{name} ] || [ -L /{name} ]; then
    test -f /{name}
    test ! -L /{name}
    optional_{index}_sha=$(sha256sum /{name} | cut -d ' ' -f 1)
    optional_{index}_mode=$(stat -c '%a' /{name})
    optional_{index}_captured=1
fi
'''
        replacements = {
            'case "$stage" in /data/.cgs2-app-update-*)': 'case "$stage" in /data/.cgs2-runtime-update-*)',
            '    [ "$(controller_pids)" = "$controller_ids" ] || return 1\n':
                '    # Runtime updates restart services; transient city-search CLI PIDs may also change.\n',
            'draft_captured=0\n': f'''draft_captured=0
{optional_initializers}controller=/{CONTROLLER_PATH}
controller_new=$controller.cgs2-runtime-new
controller_restore=$controller.cgs2-runtime-rollback
controller_new_owned=0
controller_restore_owned=0
active_controller_hash={controller_hash}
active_controller_mode=
''',
            'restore_app() {\n': '''stop_services() {
    # This executable must remain in place until all its processes, including
    # standalone city-search commands, exit and its own lease parser resets them.
    timeout 25 "$controller" stop >/dev/null 2>&1 || :
    for attempt in 1 2 3 4 5; do
        pgrep -x cgs2-services >/dev/null || break
        sleep 1
    done
    if pgrep -x cgs2-services >/dev/null; then
        killall cgs2-services 2>/dev/null || :
        for attempt in 1 2 3 4 5; do
            pgrep -x cgs2-services >/dev/null || break
            sleep 1
        done
    fi
    pgrep -x cgs2-services >/dev/null && return 1
    # Never run the older helper over leases written by a newer helper.
    timeout 30 "$controller" runtime-reset || return 1
    controller_ids=
    return 0
}
start_services() {
    "$controller" start || return 1
    ready=0
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        if "$controller" ping >/dev/null 2>&1; then ready=1; break; fi
        sleep 1
    done
    [ "$ready" = 1 ] || return 1
    controller_ids=$(controller_pids)
    [ -n "$controller_ids" ] || return 1
    verify_snapshot
}
restore_controller() {
'''+f'''    assert_hash "$stage/rollback-controller" {controller_hash} || return 1
    [ ! -e "$controller_restore" ] && [ ! -L "$controller_restore" ] || return 1
    controller_restore_owned=1
    cp "$stage/rollback-controller" "$controller_restore" || return 1
    chmod "$old_controller_mode" "$controller_restore" || return 1
    assert_hash "$controller_restore" {controller_hash} || return 1
    mv "$controller_restore" "$controller" || return 1
    active_controller_hash={controller_hash}
    active_controller_mode=$old_controller_mode
    assert_hash "$controller" "$active_controller_hash" || return 1
    [ "$(stat -c '%a' "$controller")" = "$active_controller_mode" ] || return 1
}}
restore_app() {{
''',
            '''        # Only the display is stopped/restored. The services daemon and its
        # current permissions remain running throughout success and rollback.
        if stop_display; then
            if [ "$changed" = 0 ] || restore_app; then
                if restart_display && wait_display; then outcome=restored; fi
''': '''        # Clean leases with whichever helper is currently installed before
        # restoring either executable. Failed cleanup keeps that helper available.
        if stop_display && stop_services; then
            if [ "$changed" = 0 ] || { restore_controller && restore_app; }; then
                if start_services && restart_display && wait_display; then outcome=restored; fi
''',
            '    [ "$locked" = 0 ] || rmdir "$lock"\n': '''    [ "$controller_new_owned" = 0 ] || rm -f "$controller_new"
    [ "$controller_restore_owned" = 0 ] || rm -f "$controller_restore"
    [ "$locked" = 0 ] || rmdir "$lock"
''',
            '''case "$old_mode" in ''|*[!0-7]*) exit 2;; esac
''': f'''case "$old_mode" in ''|*[!0-7]*) exit 2;; esac
old_controller_mode=$(jq -er --arg name {shlex.quote(CONTROLLER_PATH)} '.files[$name].mode' "$stage/baseline.json")
case "$old_controller_mode" in ''|*[!0-7]*) exit 2;; esac
active_controller_mode=$old_controller_mode
assert_hash "$stage/controller" {replacement_controller_hash}
assert_hash "$stage/rollback-controller" {controller_hash}
test ! -e "$controller_new"
test ! -L "$controller_new"
test ! -e "$controller_restore"
test ! -L "$controller_restore"
controller_new_owned=1
cp "$stage/controller" "$controller_new"
chmod 755 "$controller_new"
assert_hash "$controller_new" {replacement_controller_hash}
''',
            '''verify_snapshot
changed=1
mv "$new" "$app"
''': f'''verify_snapshot
stop_services
{optional_capture}verify_snapshot
changed=1
mv "$controller_new" "$controller"
active_controller_hash={replacement_controller_hash}
active_controller_mode=755
assert_hash "$controller" "$active_controller_hash"
test "$(stat -c '%a' "$controller")" = 755
mv "$new" "$app"
''',
            '''sync
restart_display
wait_display
''': '''sync
start_services
restart_display
wait_display
''',
        }
        for before, after in replacements.items():
            if body.count(before) != 1:
                raise ValueError("Runtime worker anchor changed; review the app-only worker before extending it")
            body = body.replace(before, after)
    return body


def prepare_app_update(backup: Path, output: Path, repo: Path, installed_manifest: Path, *,
                       controller: Path | None = None) -> dict:
    metadata, archive, old_hash, controller_hash = app_source_manifest(backup, installed_manifest)
    source = json.loads(installed_manifest.read_text(encoding="utf-8"))
    source_kind = source.get("kind")
    replacement_controller_hash = None
    if controller is not None:
        from build import verify_target
        verify_target(controller)
        replacement_controller_hash = digest(controller)
    if output.exists() and any(output.iterdir()):
        raise ValueError("App-update output must be empty")
    private_directory(output)
    private_directory(output / "preimage")
    (output / "preimage/manifest.json").write_bytes(installed_manifest.read_bytes())
    if source_kind in {"app-only", "runtime-update"}:
        marker = update_verification_name(source_kind)
        (output / "preimage" / marker).write_bytes((installed_manifest.parent / marker).read_bytes())
    with tarfile.open(archive) as packed:
        stock = packed.extractfile(APP_PATH)
        if stock is None:
            raise ValueError("Stock application missing")
        app, report = patch_app.patch(stock.read())
    (output / "app").write_bytes(app)
    (output / "app").chmod(0o600)
    app_hash = digest(output / "app")
    if app_hash == old_hash and (controller is None or replacement_controller_hash == controller_hash):
        raise ValueError("The rebuilt executable(s) are identical; no update is needed")
    manifest = {"format":1, "kind":"app-only", "serial":metadata["serial"], "backup_sha256":metadata["sha256"],
                "protected":PROTECTED, "preimage_sha256":old_hash, "controller_sha256":controller_hash,
                "app_sha256":app_hash, "source_manifest_sha256":digest(installed_manifest)}
    if controller is not None:
        (output / "controller").write_bytes(controller.read_bytes())
        (output / "controller").chmod(0o600)
        if digest(output / "controller") != replacement_controller_hash:
            raise ValueError("Services helper changed while preparing the update")
        manifest.update(kind="runtime-update", controller_preimage_sha256=controller_hash,
                        controller_sha256=replacement_controller_hash)
    (output / "install.sh").write_text(build_app_update_script(app_hash, old_hash, controller_hash, replacement_controller_hash), encoding="utf-8", newline="\n")
    manifest["install_script_sha256"] = digest(output / "install.sh")
    (output / "app-patch.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    return manifest


def app_snapshot(adb: ADB) -> dict:
    boot_id = adb.shell("cat", "/proc/sys/kernel/random/boot_id").strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id):
        raise ValueError("Invalid boot identifier")
    pids = adb.shell("sh", "-ec", "pgrep -x cgs2-services | sort -n").strip()
    if not pids or any(not re.fullmatch(r"[1-9][0-9]*", value) for value in pids.splitlines()):
        raise ValueError("Cannot identify running services processes")
    files = {}
    for name in APP_SNAPSHOT_FILES:
        checksum = checked_hash(adb.shell("sha256sum", "/"+name).split()[0])
        mode = adb.shell("stat", "-c", "%a", "/"+name).strip()
        if not re.fullmatch(r"[0-7]{3,4}", mode):
            raise ValueError("Invalid saved file mode")
        files[name] = {"sha256":checksum,"mode":mode}
    app_mode = adb.shell("stat", "-c", "%a", "/"+APP_PATH).strip()
    if not re.fullmatch(r"[0-7]{3,4}", app_mode):
        raise ValueError("Invalid app mode")
    return {"boot_id":boot_id,"controller_pids":pids,"files":files,"app_mode":app_mode}


def install_app_update(adb: ADB, output: Path, manifest: dict) -> None:
    runtime = manifest.get("kind") == "runtime-update"
    if (manifest.get("kind") not in {"app-only", "runtime-update"} or manifest.get("format") != 1 or manifest.get("serial") != adb.serial
            or manifest.get("protected") != PROTECTED):
        raise ValueError("App update does not match the selected device/audited boot files")
    source = output / "preimage/manifest.json"
    if digest(source) != manifest["source_manifest_sha256"]:
        raise ValueError("Installed source manifest changed before app update")
    preimage = checked_app_source({"serial":manifest["serial"],"sha256":manifest["backup_sha256"]}, source)
    old_controller_hash = manifest["controller_preimage_sha256"] if runtime else manifest["controller_sha256"]
    if preimage != (manifest["preimage_sha256"], old_controller_hash):
        raise ValueError("App update differs from its audited/verified predecessor")
    if digest(output / "app") != manifest["app_sha256"] or digest(output / "install.sh") != manifest["install_script_sha256"]:
        raise ValueError("Prepared app update changed before installation")
    if runtime and digest(output / "controller") != manifest["controller_sha256"]:
        raise ValueError("Prepared controller changed before installation")
    expected_script = build_app_update_script(manifest["app_sha256"], manifest["preimage_sha256"], old_controller_hash,
                                              manifest["controller_sha256"] if runtime else None)
    if (output / "install.sh").read_bytes() != expected_script.encode("utf-8"):
        raise ValueError("App update worker differs from the reviewed implementation")
    adb.shell("sh", "-ec", app_preflight({**manifest,"controller_sha256":old_controller_hash}))
    baseline = app_snapshot(adb)
    (output / "baseline.json").write_text(json.dumps(baseline, indent=2)+"\n", encoding="utf-8")
    stage = ("/data/.cgs2-runtime-update-" if runtime else "/data/.cgs2-app-update-")+uuid.uuid4().hex
    adb.shell("mkdir", "-m", "700", stage)
    rollback_files = {"app":(APP_PATH,manifest["preimage_sha256"])}
    if runtime:
        rollback_files["controller"] = (CONTROLLER_PATH,old_controller_hash)
    for label, (name, expected) in rollback_files.items():
        remote, local = stage+"/rollback-"+label, output/("rollback-"+label)
        adb.shell("cp", "-p", "/"+name, remote)
        adb.shell("sh", "-ec", check_line(remote, expected))
        adb.run("pull", remote, str(local))
        local.chmod(0o600)
        if digest(local) != expected:
            raise ValueError(f"{label.title()} rollback copy verification failed; display was not stopped")
    for name in ("app", "install.sh", "baseline.json", *(("controller",) if runtime else ())):
        adb.run("push", str(output/name), stage+"/"+name)
    adb.shell("sh", "-ec", check_line(stage+"/install.sh", manifest["install_script_sha256"]))
    (output / "remote-stage.txt").write_text(stage+"\n", encoding="utf-8")
    launch = 'trap "" HUP; nohup setsid sh "$1/install.sh" "$1" "$2" </dev/null >"$1/install.log" 2>&1 & sleep 1'
    adb.shell("sh", "-c", launch, "sh", stage, digest(output / "baseline.json"))
    for attempt in range(240 if runtime else 120):
        result = adb.shell("sh", "-c", 'if test -f "$1/result"; then cat "$1/result"; else echo pending; fi', "sh", stage).strip()
        if result == "pending":
            time.sleep(1)
            continue
        for name in ("install.log", "rollback-result"):
            adb.run("pull", stage+"/"+name, str(output/name))
        if result != "0":
            outcome = (output / "rollback-result").read_text(encoding="utf-8").strip()
            raise ValueError(f"App update failed; rollback: {outcome}. Retain {output} and keep USB connected.")
        after = app_snapshot(adb)
        expected = {**baseline,"app_mode":"755"}
        if runtime:
            expected["controller_pids"] = after["controller_pids"]
            expected["files"] = {**baseline["files"], CONTROLLER_PATH:{"sha256":manifest["controller_sha256"],"mode":"755"}}
        if after != expected:
            raise ValueError("App worker finished but protected state changed; keep USB connected and inspect baseline.json")
        adb.shell("sh", "-ec", check_line("/"+APP_PATH, manifest["app_sha256"]))
        adb.shell("/usr/local/sbin/cgs2-services", "ping")
        adb.shell("pgrep", "-x", "QingSnow2App")
        adb.shell("pgrep", "-x", "adbd")
        verified = {"ok":True,"manifest_sha256":digest(output / "manifest.json"),"serial":adb.serial,
                    "app_sha256":manifest["app_sha256"],"controller_sha256":manifest["controller_sha256"],
                    "boot_id":baseline["boot_id"]}
        (output / update_verification_name(manifest["kind"])).write_text(json.dumps(verified, indent=2)+"\n", encoding="utf-8")
        adb.shell("sh", "-ec", release_record_script())
        import recovery
        recovery.record_update(adb, output, manifest)
        # All receipts and diagnostics now exist on the computer. Do not leave
        # one persistent copy of both binaries behind after every update.
        adb.shell("rm", "-rf", stage)
        if runtime:
            print("Runtime update verified after 15 seconds of display health. Saved configurations/permissions and boot/USB files preserved; no reboot.")
        else:
            print("App-only update verified after 15 seconds of display health. Services, permissions, boot and USB files preserved; no reboot.")
        return
    raise ValueError(f"App worker is still pending; do not retry or power off. Inspect {stage}/install.log over USB.")


HELPER_FILES = {
    "usr/local/sbin/cgs2-reset": ("device/cgs2-reset", "755"),
    "usr/local/sbin/cgs2-mqtt-config": ("device/cgs2-mqtt-config", "755"),
    "usr/local/share/cgs2-privacy/MqttConfig.qml": ("device/MqttConfig.qml", "644"),
}


def current_helpers() -> dict[str, str]:
    """The helper revision in this repository, which a new bundle installs."""
    root = Path(__file__).resolve().parents[1]
    return {name: digest(root / origin) for name, (origin, _) in HELPER_FILES.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    updates = parser.add_mutually_exclusive_group(required=True)
    updates.add_argument("--app-only-from-manifest", type=Path, help="Update only the app from an audited installed update manifest")
    updates.add_argument("--runtime-from-manifest", type=Path, help="Update app and services helper, preserving existing saved settings and permissions")
    parser.add_argument("--controller", type=Path, help="Reviewed static ARM64 helper for --runtime-from-manifest (default: build/cgs2-services)")
    parser.add_argument("--install", action="store_true", help="Install the guarded update; otherwise prepare only")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.controller and not args.runtime_from_manifest:
        parser.error("--controller requires --runtime-from-manifest")
    source_manifest = args.app_only_from_manifest or args.runtime_from_manifest
    manifest = prepare_app_update(args.backup, args.output, repo, source_manifest,
                                  controller=(args.controller or repo/"build/cgs2-services") if args.runtime_from_manifest else None)
    if not args.install:
        print(f"Prepared {args.output}, including install.sh; no device operations performed")
        return
    install_app_update(ADB(args.adb,args.serial), args.output, manifest)


if __name__ == "__main__":
    main()
