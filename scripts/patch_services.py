#!/usr/bin/env python3
"""Produce service patches only for the audited CGS2 firmware revision."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


ORIGINALS = {
    "qingping/bin/start.sh": "0ead46c804a9e3ed7515cfd7853e241bcc7c6b750fec7a8b74ff0d44e70cc9e2",
    "qingping/bin/watchdog.sh": "f2753d6de46d9ae8b1f8b119ae7afab3f60e944a6f723baee3224b237eb606ec",
    "etc/init.d/rcS": "dc1615df9f2012b20b81ffad8e07e16293039ba7fd897854ca3646d6cfea0c0f",
    "etc/init.d/S49ntp": "fc12cb399e086034b374e07bab0465dad18abb7133335641ed9fd3569a011681",
    "etc/init.d/S80wifireconnect": "aa2497d3ec9eb445e73a168485e2e30b075d20fda4412377e01a7ea15f7c6a56",
}


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Patch anchor must occur exactly once: {old[:70]!r}")
    return text.replace(old, new, 1)


def patch_start(text: str) -> str:
    text = replace_once(text, "source /etc/profile", ". /etc/profile\n"
                        "[ -f /run/cgs2-privacy.ready ] || {\n"
                        "  echo 'CGS2 privacy filter is not ready; refusing application startup' >&2\n"
                        "  exit 1\n}\n")
    begin = text.index("# 检查米家配置文件\n")
    end = text.index("# 检查 product 配置文件\n", begin)
    text = text[:begin] + text[end:]
    text = replace_once(text, "Miio_File=/qingping/etc/miio/device.conf\n", "")
    text = replace_once(text, "# 检查米家配置\ncheck_miio_config\n", "")
    text = replace_once(text, "chmod 666 /data/* -R\n", "# Keep private settings and backup permissions intact.\n")
    text = replace_once(text, "mv /etc/init.d/S80dnsmasq  /qingping/etc", "[ ! -f /etc/init.d/S80dnsmasq ] || mv /etc/init.d/S80dnsmasq /qingping/etc")
    text = replace_once(text, "# 启动看门狗\n", '''# Noise readings are transient and rewritten twice per measurement.
# Keep those tiny files in RAM rather than writing flash continuously.
mkdir -p /data/etc/tmp
if ! awk '$2 == "/data/etc/tmp" {found=1} END {exit !found}' /proc/mounts; then
  mount -t tmpfs -o size=256k,mode=0700,nosuid,nodev,noexec cgs2-noise /data/etc/tmp ||
    echo 'Could not mount temporary noise storage; continuing with existing storage' >&2
fi

# 启动看门狗
''')
    begin = text.index("if [ -f $Miio_File ]; then\n")
    text = text[:begin] + "# Xiaomi miio services are intentionally removed.\n"
    return text


def patch_watchdog(text: str) -> str:
    begin = text.index("# 定义计数器\n")
    end = text.index("while [ 0 -lt 1 ]\n", begin)
    text = text[:begin] + '''# Check genuine memory pressure once per minute. Linux file cache is reclaimable.
checkMemCount=0
checkMem(){
    checkMemCount=$((checkMemCount + 1))
    [ "$checkMemCount" -ge 12 ] || return 0
    checkMemCount=0
    available_kb=$(awk '$1 == "MemAvailable:" {print $2}' /proc/meminfo)
    case "$available_kb" in ''|*[!0-9]*) return 0 ;; esac
    if [ "$available_kb" -lt 46080 ]; then
        echo "[$now][watchdog.sh] MemAvailable below 45 MiB; restarting QingSnow2App" >> "$snowLog"
        pkill "$SNOW_OS"
    fi
}

''' + text[end:]
    text = "\n".join(line for line in text.split("\n")
                     if not line.strip().startswith(("miioHelperCount=", "miioCount=")))
    begin = text.index("\t# miio_client_helper_nomqtt.sh\n")
    end = text.index("\t# analyze_noise.sh\n", begin)
    text = text[:begin] + text[end:]
    return text


def patch_rcs(text: str) -> str:
    text = replace_once(text, "for i in /etc/init.d/S??* ;do", '''# Enforce privacy before any network or application init script can run.
# A filter failure still boots USB ADB and its ordinary prerequisites.
privacy_ready=false
if /etc/init.d/S02privacy start; then
    privacy_ready=true
else
    echo 'CGS2 privacy preflight failed: starting only USB rescue services' >&2
    # Normal boot raises loopback through S40network. Rescue skips that script
    # and must still provide localhost for the USB debug daemon and helpers.
    ifconfig lo 127.0.0.1 netmask 255.0.0.0 up ||
        echo 'Could not initialize rescue loopback' >&2
fi

for i in /etc/init.d/S??* ;do
     [ "$i" = /etc/init.d/S02privacy ] && continue
     if [ "$privacy_ready" != true ]; then
         case "${i##*/}" in
             S01logging|S10async_commit.sh|S10udev|S20urandom|S21mountall.sh|S50usbdevice) ;;
             *) continue ;;
         esac
     fi''')
    return text


def patch_wifi_reconnect(text: str) -> str:
    return replace_once(text, "  start)\n", "  start)\n"
                        "      # Honor the same user Wi-Fi switch as the launcher/watchdog.\n"
                        "      if grep -q '^wifi_disabled=true$' /data/etc/setting.ini 2>/dev/null; then\n"
                        "          exit 0\n"
                        "      fi\n")


def build(qingping: Path, etc: Path, output: Path) -> None:
    sources = {}
    for name, expected in ORIGINALS.items():
        prefix, relative = name.split("/", 1)
        raw = ((qingping if prefix == "qingping" else etc) / relative).read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise ValueError(f"Unsupported original {name}: SHA256 {actual}, expected {expected}")
        sources[name] = raw.decode("utf-8")
    changed = {
        "qingping/bin/start.sh": patch_start(sources["qingping/bin/start.sh"]),
        "qingping/bin/watchdog.sh": patch_watchdog(sources["qingping/bin/watchdog.sh"]),
        "etc/init.d/rcS": patch_rcs(sources["etc/init.d/rcS"]),
        "etc/init.d/S49ntp": "#!/bin/sh\n# No NTP server has been authorized. RTC time is retained.\nexit 0\n",
        "etc/init.d/S80wifireconnect": patch_wifi_reconnect(sources["etc/init.d/S80wifireconnect"]),
    }
    for name, content in changed.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        destination.chmod(0o755)
    repository = Path(__file__).resolve().parent.parent
    for source, name, mode in (
        ("device/S02privacy", "etc/init.d/S02privacy", 0o755),
        ("device/allowlist.conf", "etc/cgs2-privacy/allowlist.conf", 0o600),
    ):
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((repository / source).read_bytes())
        destination.chmod(mode)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qingping-dir", type=Path, required=True)
    parser.add_argument("--etc-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.qingping_dir, args.etc_dir, args.output)
    print(f"Wrote audited service patches to {args.output}")


if __name__ == "__main__":
    main()
