#!/usr/bin/env python3
"""Read the installation health and current owner choices over USB."""
import argparse
import json
import re
from pathlib import Path

from device import ADB


def run_checks(adb: ADB) -> dict:
    """Read-only checks; allowlist and Wi-Fi state are informational."""
    checks = {
        "boot_filter_ready": "test -f /run/cgs2-privacy.ready",
        "empty_allowlist": "test -r /etc/cgs2-privacy/allowlist.conf; test -z \"$(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /etc/cgs2-privacy/allowlist.conf)\"",
        "ipv6_disabled": "for f in /proc/sys/net/ipv6/conf/*/disable_ipv6; do test \"$(cat \"$f\")\" = 1 || exit 1; done; test -z \"$(cat /proc/net/if_inet6)\"",
        "forwarding_disabled": "test \"$(cat /proc/sys/net/ipv4/ip_forward)\" = 0",
        "display_running": "pgrep -x QingSnow2App >/dev/null",
        "noise_sensor_running": "pgrep -f '^/bin/bash /qingping/bin/analyze_noise.sh' >/dev/null",
        "cloud_processes_removed": "for name in miio_client miio_recv_line ntpd; do if pgrep -x \"$name\"; then exit 1; fi; done; test ! -e /qingping/bin/miio; test ! -e /qingping/bin/miio_bk",
        "usb_debugging_persistent": "grep -qx usb_adb_en /etc/init.d/.usb_config; pgrep -x adbd >/dev/null",
        "private_settings": "test \"$(stat -c %a /data/etc/setting.ini)\" = 600; test \"$(stat -c %a /data/etc/cgs2-mqtt.ini)\" = 600; "
                            "if [ -e /data/etc/cgs2-mqtt-applied.ini ]; then test \"$(stat -c %a /data/etc/cgs2-mqtt-applied.ini)\" = 600; fi",
        "noise_uses_ram": "grep -q '^cgs2-noise .* tmpfs ' /proc/mounts",
        "history_database_integrity": "test \"$(sqlite3 /data/etc/Snow2.db 'pragma quick_check;')\" = ok",
        "wifi_disabled": "grep -qx wifi_disabled=true /data/etc/setting.ini; if pgrep -x wpa_supplicant; then exit 1; fi",
    }
    report = {"checks": {}, "filter": "", "sensor_history": ""}
    for name, command in checks.items():
        try:
            adb.shell("sh", "-ec", command)
            report["checks"][name] = True
        except RuntimeError as error:
            report["checks"][name] = str(error)
    try:
        report["filter"] = adb.shell("/usr/local/sbin/cgs2-filter", "status").strip()
    except RuntimeError as error:
        report["filter"] = str(error)
    lines = report["filter"].splitlines()
    report["checks"]["kernel_policy_default_deny"] = (
        bool(lines) and re.fullmatch(r"table=mangle owner=recognized entries=[0-9]+", lines[0]) is not None
        and sum("verdict=DROP" in line for line in lines) == 3
        and sum("out=lo" in line for line in lines) == 1
        and any("verdict=DROP in!=lo protocol=6 port=5555" in line for line in lines)
    )
    query = " UNION ALL ".join(
        f"SELECT '{label}', max(timeStamp), value FROM {table}"
        for label, table in (
            ("temperature", "table_temp_data"), ("humidity", "table_humi_data"),
            ("co2", "table_co2e_data"), ("pm25", "table_pm25_data"),
            ("pm10", "table_pm10_data"), ("noise", "table_noise_data"),
        )
    ) + ";"
    report["sensor_history"] = adb.shell("sqlite3", "/data/etc/Snow2.db", query).strip()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_checks(ADB(args.adb, args.serial))
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    if any(value is not True for value in report["checks"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
