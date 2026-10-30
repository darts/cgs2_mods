#!/usr/bin/env python3
"""Build a private, device-specific install bundle from a verified backup."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tarfile

import device
import install_services
import patch_services
import build
import recovery


TARGET_MODES = {
    "qingping/bin/start.sh": 0o755,
    "qingping/bin/watchdog.sh": 0o755,
    "qingping/bin/QingSnow2App": 0o755,
    "etc/init.d/rcS": 0o755,
    "etc/init.d/S49ntp": 0o755,
    "etc/init.d/S80wifireconnect": 0o755,
    "etc/init.d/S02privacy": 0o755,
    "etc/cgs2-privacy/allowlist.conf": 0o600,
    "usr/local/sbin/cgs2-filter": 0o755,
    "usr/local/sbin/cgs2-services": 0o755,
    "usr/local/sbin/cgs2-mqtt-config": 0o755,
    "usr/local/sbin/cgs2-reset": 0o755,
    "usr/local/share/cgs2-privacy/MqttConfig.qml": 0o644,
    "data/etc/cgs2-mqtt.ini": 0o600,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--zig", help="Path to the Zig executable")
    parser.add_argument("--go", help="Path to the Go executable")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    _, archive = device.load_backup(args.backup)
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("Output directory must be empty")
    device.private_directory(args.output)
    original = args.output / "original"
    payload = args.output / "payload"
    required = set(patch_services.ORIGINALS) | {"qingping/bin/QingSnow2App"}
    with tarfile.open(archive) as stock:
        index = device.tar_index(stock)
        for name in sorted(required):
            member = index.get(name)
            if member is None or not member.isfile():
                raise ValueError(f"Missing regular firmware file: {name}")
            dest = original / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = stock.extractfile(member)
            assert source is not None
            dest.write_bytes(source.read())
    patch_services.build(original / "qingping", original / "etc", payload)
    build.build_all(repo, zig=args.zig, go=args.go)
    # Updates check the installed filter, so refuse a build that they would refuse.
    if device.digest(repo / "build/cgs2-filter") not in install_services.AUDITED_FILTER_HASHES:
        raise ValueError("The built firewall program is not the audited build. Delete the folders "
                         ".tools/zig-cache and .tools/zig-local-cache. Then prepare again with the Zig "
                         "version that mise.toml pins.")
    import sys
    subprocess.run([sys.executable, str(repo / "scripts/patch_app.py"),
                    str(original / "qingping/bin/QingSnow2App"),
                    str(payload / "qingping/bin/QingSnow2App"),
                    "--report", str(args.output / "app-patch.json")], check=True)
    (payload / "qingping/bin/QingSnow2App").chmod(0o755)
    for source, target, mode in (
        ("build/cgs2-filter", "usr/local/sbin/cgs2-filter", 0o755),
        ("build/cgs2-services", "usr/local/sbin/cgs2-services", 0o755),
        ("device/cgs2-mqtt-config", "usr/local/sbin/cgs2-mqtt-config", 0o755),
        ("device/cgs2-reset", "usr/local/sbin/cgs2-reset", 0o755),
        ("device/MqttConfig.qml", "usr/local/share/cgs2-privacy/MqttConfig.qml", 0o644),
        ("device/cgs2-mqtt.ini", "data/etc/cgs2-mqtt.ini", 0o600),
    ):
        dest = payload / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / source, dest)
        dest.chmod(mode)
    device.prepare(args.backup, payload, args.output / "bundle", list(device.DEFAULT_REMOVALS),
                   mode_overrides=TARGET_MODES)
    import json
    manifest = json.loads((args.output / "bundle/manifest.json").read_text(encoding="utf-8"))
    stock = args.output / "bundle/stock"
    recovery.create(args.backup, manifest, stock)
    recovery.finish_copy(stock, manifest, repo / "build/cgs2-filter", repo / "device/ResetStatus.qml")
    print(f"Install bundle ready: {args.output / 'bundle'}")


if __name__ == "__main__":
    main()
