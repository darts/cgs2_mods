#!/usr/bin/env python3
"""A small, dependency-free menu and CLI for the Qingping CGS2 tools."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import device
import recovery
from setup_tools import find_adb, install_adb

SUPPORTED_FIRMWARE = "4.5.6_0181"
# Keep these labels consistent with the installation guide.
MENU = (
    ("1", "Install the modification"),
    ("2", "Examine the installation"),
    ("3", "Update the modification"),
    ("4", "Remove the modification"),
    ("5", "Make an optional computer backup"),
    ("6", "Install the computer tools"),
    ("0", "Exit"),
)
# Friendly names for installation checks. Owner choices are reported separately.
CHECKS = {
    "boot_filter_ready": "The firewall started with the monitor",
    "kernel_policy_default_deny": "The firewall blocks all servers that you did not approve",
    "ipv6_disabled": "IPv6 is off",
    "forwarding_disabled": "Network forwarding is off",
    "display_running": "The display application operates",
    "noise_sensor_running": "The noise sensor operates",
    "cloud_processes_removed": "The vendor cloud services are removed",
    "usb_debugging_persistent": "USB debugging stays on",
    "private_settings": "Only the system can read the settings files",
    "noise_uses_ram": "The noise data uses memory, not storage",
    "history_database_integrity": "The measurement history is not damaged",
}


def private_output(label: str) -> Path:
    return ROOT / ".private" / (label + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))


def run_script(name: str, *arguments: str) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / name), *map(str, arguments)], cwd=ROOT, check=True)


def adb_for(args: argparse.Namespace) -> device.ADB:
    executable = find_adb(ROOT, args.adb)
    if not executable:
        raise ValueError("ADB is not installed. First, run 'mise run setup'.")
    return device.ADB(executable, args.serial)


def doctor(args: argparse.Namespace) -> int:
    print("Qingping Local - setup check\n")
    failed = False
    print(f"Python {sys.version.split()[0]}: {'OK' if sys.version_info >= (3, 11) else 'requires 3.11+'}")
    failed |= sys.version_info < (3, 11)
    for name, command in (("Go", ["go", "version"]), ("Zig", ["zig", "version"])):
        found = shutil.which(command[0])
        if found:
            output = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
            print(f"{name}: {output}")
        else:
            print(f"{name}: not installed - run 'mise run setup'")
            failed = True
    adb = find_adb(ROOT, args.adb)
    if adb:
        output = subprocess.run([adb, "version"], capture_output=True, text=True, check=True).stdout.splitlines()
        print("ADB: " + (output[0] if output else "found"))
        print("ADB executable: " + adb)
    else:
        print("ADB: not installed - run 'mise run setup'")
        failed = True
    if args.device:
        connection = adb_for(args)
        connection.shell("test", "-f", "/qingping/bin/QingSnow2App")
        print("USB: one selected device responds with root ADB and the Qingping app is present.")
        print("The preparation compares the firmware with your backup.")
    else:
        print("The tool did not access the USB device. To examine it, run 'python qingping.py doctor --device'.")
    return 1 if failed else 0


def confirm_install(args: argparse.Namespace, manifest: dict, output: Path) -> None:
    print("\nPrepared install: " + str(output))
    print("FIRST INSTALL: changes the app, startup scripts and firewall; removes vendor services.")
    print("The app stops. After the verification, power-cycle the monitor manually.")
    print("The USB rescue start after a failed firewall policy is not tested on hardware.")
    print("Files:")
    for change in manifest["changes"]:
        print("  /" + change["path"])
    confirm(args, "The installation saves a verified stock copy on the monitor. No restart or server permission is automatic.")


def confirm(args: argparse.Namespace, summary: str, word: str = "INSTALL") -> None:
    print(summary)
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise ValueError("Examine the prepared update. Then use the interactive menu, or add --yes.")
    if input(f"Type {word} to continue, or press Enter to stop: ").strip() != word:
        raise ValueError("Stopped. The monitor did not change.")


def update(args: argparse.Namespace) -> None:
    """Bring an installed monitor to this release without a reboot."""
    import install_services as updater
    adb = adb_for(args)
    if recovery.present(adb) or not args.backup or not args.installed:
        args.backup, args.installed = recovery.fetch(adb, private_output("working-copy"))
    live = {name: adb.shell("sha256sum", "/" + name).split()[0] for name in updater.HELPER_FILES}
    if live != updater.current_helpers():
        raise ValueError("The MQTT helper on the monitor is not from a known release. Nothing was changed.")
    print("\nThe tool prepares the new app on this computer. This can take some minutes.")
    run_script("build.py", "services")
    output = private_output("runtime-update")
    try:
        manifest = updater.prepare_app_update(args.backup, output, ROOT, args.installed / "manifest.json",
                                              controller=ROOT / "build/cgs2-services")
    except ValueError as error:
        if "no update is needed" not in str(error):
            raise
        print("The app and the services helper are up to date.")
        return
    print("\nPrepared app and services update: " + str(output))
    confirm(args, "Replaces the display app and the services helper. The display restarts; the monitor does not reboot.")
    updater.install_app_update(adb, output, manifest)
    remember("installed", output, adb.serial)


def examine(adb: device.ADB) -> bool:
    """Print plain health results that stay valid after updates and owner settings."""
    import install_services
    import validate
    report = validate.run_checks(adb)
    checks = report["checks"]
    servers = checks["empty_allowlist"] is not True
    results = []
    try:
        adb.shell("sh", "-ec", install_services.boot_file_checks())
        results.append(("The startup files are the audited files", True))
    except RuntimeError:
        results.append(("The startup files are the audited files", False))
    for key, label in CHECKS.items():
        results.append((label, checks[key] is True))
    for label, passed in results:
        print(("  OK       " if passed else "  PROBLEM  ") + label)
    print("  INFO     Approved servers: " + ("yes" if servers else "none"))
    print("  INFO     Wi-Fi: " + ("off" if checks["wifi_disabled"] is True else "on"))
    return all(passed for _, passed in results)


def load_state() -> dict:
    target = ROOT / ".private/workflow.json"
    return json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}


def remember(key: str, folder: Path | None, serial: str | None = None) -> None:
    """Save a folder for the next steps, per monitor when the serial is known."""
    root = ROOT / ".private"
    device.private_directory(root)
    state = load_state()
    records = state.setdefault("monitors", {}).setdefault(serial, {}) if serial else state
    if folder is None:
        records.pop(key, None)
    else:
        # Keep folders in the project relative, so that a new release in a new
        # folder finds the copied .private folder.
        folder, project = folder.resolve(), ROOT.resolve()
        records[key] = folder.relative_to(project).as_posix() if folder.is_relative_to(project) else str(folder)
    target = root / "workflow.json"
    target.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")
    target.chmod(0o600)


def record_serial(folder: Path) -> str | None:
    for name in ("backup.json", "manifest.json"):
        try:
            return json.loads((folder / name).read_text(encoding="utf-8")).get("serial")
        except (OSError, ValueError):
            continue
    return None


def saved(key: str, serial: str | None = None, *, check_serial: bool = True) -> Path | None:
    """Return a saved folder only if it still exists and belongs to this monitor."""
    state = load_state()
    value = state.get("monitors", {}).get(serial, {}).get(key) if serial else None
    value = value or state.get(key)
    if not value:
        return None
    path = ROOT / value
    if not path.exists():
        return None
    if serial and check_serial and record_serial(path) != serial:
        return None
    return path.resolve()


def monitor_status(args: argparse.Namespace) -> dict:
    """Read the computer and monitor state without changes."""
    status = {"adb": find_adb(ROOT, args.adb), "connection": "none", "serial": None, "firmware": None,
              "installed": None, "backup": None, "bundle": None, "update": None}
    if not status["adb"]:
        return status
    listing = subprocess.run([status["adb"], "devices"], capture_output=True, text=True, timeout=30).stdout
    rows = [line.split() for line in listing.splitlines()[1:] if len(line.split()) >= 2]
    if args.serial:
        rows = [row for row in rows if row[0] == args.serial]
    ready = [row[0] for row in rows if row[1] == "device"]
    if any(row[1] == "unauthorized" for row in rows):
        status["connection"] = "unauthorized"
    if len(ready) > 1:
        status["connection"] = "several"
    if len(ready) != 1:
        return status
    try:
        adb = device.ADB(status["adb"], ready[0])
    except ValueError:
        status["connection"] = "no root"
        return status
    status["connection"], status["serial"] = "ready", adb.serial
    match = re.search(r"CLEARGRASS_VERSION=(\S+)", adb.shell("sh", "-c", "cat /qingping/etc/os-release 2>/dev/null || :"))
    status["firmware"] = match[1] if match else "unknown"
    status["installed"] = adb.shell("sh", "-c", "test -x /usr/local/sbin/cgs2-filter && echo yes || echo no").strip() == "yes"
    status["stock_copy"] = recovery.present(adb)
    status["working"] = saved("working", adb.serial)
    for key, record in (("backup", "backup"), ("bundle", "bundle"), ("update", "installed")):
        status[key] = saved(record, adb.serial)
    return status


def next_step(status: dict) -> str:
    if not status.get("adb"):
        return "Select 6 to install the computer tools."
    connection = status.get("connection")
    if connection == "unauthorized":
        return "On the monitor, allow USB debugging for this computer. Then press Enter."
    if connection == "several":
        return "Disconnect all other Android devices. Then press Enter."
    if connection == "no root":
        return "USB ADB on the monitor has no root access. The tools need the root ADB of the stock firmware."
    if connection != "ready":
        return ("Connect the monitor with a USB data cable. Make sure that Adbd Debugging is on in the "
                "Developer Options of the monitor. Then press Enter.")
    if not status["installed"] and status["firmware"] != SUPPORTED_FIRMWARE:
        return f"This monitor has firmware {status['firmware']}. The modification needs {SUPPORTED_FIRMWARE}."
    if not status["installed"]:
        return "Select 1 to install the modification. A computer backup is optional."
    return "The modification is installed. Select 2 to examine it, or 3 to update it."


def show_status(status: dict) -> None:
    connection = {"ready": f"Connected (firmware {status.get('firmware')})", "unauthorized": "Connected, not allowed",
                  "several": "More than one device", "no root": "Connected, no root access"}
    print("\nQINGPING AIR MONITOR 2 - LOCAL CONTROL\n")
    print("  Computer tools   " + ("Ready" if status["adb"] else "Not installed"))
    print("  Monitor          " + connection.get(status.get("connection"), "Not connected"))
    if status.get("connection") == "ready":
        print("  Modification     " + ("Installed" if status["installed"] else "Not installed"))
        print("  Backup           " + (f"Saved in {status['backup']}" if status["backup"] else "Not made"))
        print("  Stock copy       " + ("On the monitor" if status.get("stock_copy") else "Not saved on the monitor"))
    print("\nNext step: " + next_step(status) + "\n")
    for key, label in MENU:
        print(f"  {key}  {label}")


def ask(prompt: str, word: str) -> bool:
    return input(prompt).strip() == word


def need_monitor(status: dict) -> None:
    if status.get("connection") != "ready":
        raise ValueError(next_step(status))


def wait_for_monitor(args: argparse.Namespace, seconds: int = 180) -> device.ADB:
    executable = find_adb(ROOT, args.adb)
    for attempt in range(seconds // 5):
        try:
            return device.ADB(executable, args.serial)
        except (ValueError, RuntimeError, subprocess.CalledProcessError):
            time.sleep(5)
    raise ValueError("The monitor did not connect. Connect the USB cable. Then select 3 to examine the installation.")


def menu_backup(args: argparse.Namespace, status: dict) -> None:
    need_monitor(status)
    print("\nThe tool copies the current monitor files and its stock copy to this computer.")
    print("The monitor does not change. This can take some minutes.")
    output = private_output("backup")
    device.backup(adb_for(args), output)
    remember("backup", output, status["serial"])
    print(f"\nThe backup is in {output}.")
    print("Keep this folder private and safe. It contains credentials and the original firmware.")
    print("This optional copy is useful if the monitor's storage fails. Keep another copy outside this project folder.")


def menu_install(args: argparse.Namespace, status: dict) -> None:
    need_monitor(status)
    if status["installed"]:
        raise ValueError("The modification is already installed. Select 3 to update it.")
    if status["firmware"] != SUPPORTED_FIRMWARE:
        raise ValueError(f"This monitor has firmware {status['firmware']}. The modification needs {SUPPORTED_FIRMWARE}.")
    adb = adb_for(args)
    working = status.get("working") or status["backup"]
    if not working:
        working = private_output("stock-working-copy")
        device.backup(adb, working, minimal=True)
        remember("working", working, status["serial"])
    print("\nStep 1 of 4: Prepare the files on this computer.")
    print("The monitor does not change. This can take some minutes.")
    output = private_output("release")
    run_script("prepare.py", "--backup", working, "--output", output)
    bundle = output / "bundle"
    remember("bundle", bundle, status["serial"])
    manifest, _ = device.load_bundle(bundle, working, adb)
    print("\nStep 2 of 4: Examine the changes.")
    print("  - The display application and the startup scripts change.")
    print("  - A firewall blocks all servers that you did not approve.")
    print("  - The vendor cloud services are removed.")
    print("  - A verified stock copy lets Reset restore vendor software without this computer.")
    print("  - Wi-Fi, MQTT, time, weather and DNS stay off until you set them on.")
    print(f"  - {len(manifest['changes'])} files change. The list is in {bundle / 'manifest.json'}.")
    print("\nCAUTION: Keep the power and the USB cable connected during the installation.")
    if not ask("Type INSTALL to install, or press Enter to stop: ", "INSTALL"):
        print("Stopped. The monitor did not change.")
        return
    print("\nStep 3 of 4: Install the modification. Do not use the monitor.")
    device.install(adb, bundle, working)
    adb.shell("/usr/local/sbin/cgs2-mqtt-config", "initialize")
    adb.shell("/usr/local/sbin/cgs2-services", "initialize")
    device.verify(adb, bundle, working)
    remember("installed", bundle, status["serial"])
    print("\nStep 4 of 4: Restart the monitor.")
    print("  1. Stop the monitor with its power control. Do not use a command.")
    print("  2. Start the monitor again.")
    print("  3. Wait until the monitor shows its readings.")
    print("  4. Make sure that the USB cable is connected.")
    input("Then press Enter to examine the installation: ")
    menu_examine(args, {**status, "installed": True}, wait_for_monitor(args))


def menu_examine(args: argparse.Namespace, status: dict, adb: device.ADB | None = None) -> None:
    if adb is None:
        need_monitor(status)
    if not status["installed"]:
        raise ValueError("The modification is not installed.")
    print("\nThe tool examines the monitor. The monitor does not change.\n")
    if examine(adb or adb_for(args)):
        print("\nThe installation is correct.")
    else:
        print("\nOne or more items have a problem. Keep the backup. Refer to the user guide.")


def menu_update(args: argparse.Namespace, status: dict) -> None:
    need_monitor(status)
    if not status["installed"]:
        raise ValueError("The modification is not installed. Select 1 to install it.")
    installed = status["update"] or status["bundle"]
    working = status.get("working") or status["backup"]
    print("\nThe update replaces the app and the services helper. Your settings and approvals stay.")
    print("The display restarts. The monitor does not restart.")
    update(argparse.Namespace(adb=args.adb, serial=status["serial"], backup=working, installed=installed, yes=False))
    print("\nThe update is complete.")


def menu_remove(args: argparse.Namespace, status: dict) -> None:
    need_monitor(status)
    if not status["installed"]:
        raise ValueError("The modification is not installed.")
    if not status.get("stock_copy") and not (status.get("working") or status["backup"]):
        raise ValueError("No stock copy or complete computer backup/bundle is available. Refer to the recovery guide.")
    print("\nThis restores vendor software and removes the privacy modification.")
    print("It erases settings, passwords and reading history. Wi-Fi stays off.")
    if not ask("Type REMOVE to remove the modification, or press Enter to stop: ", "REMOVE"):
        print("Stopped. The monitor did not change.")
        return
    if status.get("stock_copy"):
        recovery.start(adb_for(args))
    else:
        device.rollback(adb_for(args), status["bundle"], status.get("working") or status["backup"])
    remember("installed", None, status["serial"])
    remember("bundle", None, status["serial"])
    print("\nThe modification is removed. Restart the monitor with its power control.")
    print("After the restart, the monitor operates as before the installation.")


def menu_tools(args: argparse.Namespace) -> None:
    install_adb(ROOT, args.adb)
    args.device = False
    doctor(args)


def menu(args: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        parser().print_help()
        return 0
    actions = {"1": menu_install, "2": menu_examine, "3": menu_update, "4": menu_remove, "5": menu_backup}
    while True:
        try:
            status = monitor_status(args)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            status = {"adb": None, "connection": "none"}
            print(f"\nThe tool cannot read the monitor: {error}")
        show_status(status)
        selection = input("\nSelect a number, or press Enter to read the status again: ").strip()
        if selection == "0":
            return 0
        if selection == "":
            continue
        try:
            if selection == "6":
                menu_tools(args)
            elif selection in actions:
                actions[selection](args, status)
            else:
                print("Select a number from the menu.")
        except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
            print(f"\nThe action stopped: {error}")
        input("\nPress Enter to go back to the menu: ")


def dispatch(args: argparse.Namespace) -> int:
    command = args.command or "start"
    if command == "start":
        return menu(args)
    if command == "setup":
        install_adb(ROOT, args.adb)
        args.device = False
        return doctor(args)
    if command == "doctor":
        return doctor(args)
    if command == "build":
        run_script("build.py", "all")
    elif command == "backup":
        output = args.output or private_output("backup")
        adb = adb_for(args)
        device.backup(adb, output)
        remember("backup", output, adb.serial)
        print("Keep this folder private. It contains device credentials and original firmware.")
    elif command == "prepare":
        output = args.output or private_output("release")
        run_script("prepare.py", "--backup", args.backup, "--output", output)
        serial = record_serial(args.backup)
        remember("backup", args.backup, serial)
        remember("bundle", output / "bundle", serial)
        print("Prepared locally. The monitor did not change. Next, examine the bundle. Then install it.")
    elif command in {"verify", "install"}:
        manifest = json.loads((args.bundle / "manifest.json").read_text(encoding="utf-8"))
        adb = adb_for(args)
        if command == "verify":
            device.verify(adb, args.bundle, args.backup)
        else:
            device.load_bundle(args.bundle, args.backup, adb)
            confirm_install(args, manifest, args.bundle)
            device.install(adb, args.bundle, args.backup)
            adb.shell("/usr/local/sbin/cgs2-mqtt-config", "initialize")
            adb.shell("/usr/local/sbin/cgs2-services", "initialize")
            device.verify(adb, args.bundle, args.backup)
            remember("installed", args.bundle, adb.serial)
            print("Settings initialized with no servers approved. Examine the result. Then power-cycle the monitor manually. Do not use adb reboot.")
    elif command == "examine":
        return 0 if examine(adb_for(args)) else 1
    elif command == "update":
        update(args)
    elif command == "remove":
        confirm(args, "Restores vendor software and erases settings, passwords and readings. Wi-Fi stays off.", "REMOVE")
        adb = adb_for(args)
        if args.backup:
            device.rollback(adb, args.bundle, args.backup)
        elif args.bundle:
            raise ValueError("A recovery bundle also needs its --backup.")
        else:
            recovery.start(adb)
    elif command == "test":
        run_script("test.py")
    return 0


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--adb", default=argparse.SUPPRESS, help="ADB path; alternatively set CGS2_ADB")
    common.add_argument("--serial", default=argparse.SUPPRESS, help="Select a device when more than one is connected")
    # Separate action objects prevent subparser defaults from overwriting an
    # explicitly supplied root-level option.
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--adb", help="ADB path; alternatively set CGS2_ADB")
    p.add_argument("--serial", help="Select a device when more than one is connected")
    sub = p.add_subparsers(dest="command")
    for name in ("start", "setup", "build", "test"):
        sub.add_parser(name, parents=[common])
    d = sub.add_parser("doctor", parents=[common], help="Check tools; optionally read the USB connection")
    d.add_argument("--device", action="store_true")
    sub.add_parser("examine", parents=[common], help="Run read-only health checks on the installed monitor")
    for name in ("backup", "prepare"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("--output", type=Path)
        if name == "prepare":
            s.add_argument("--backup", type=Path, required=True)
    s = sub.add_parser("update", parents=[common], help="Update the app and the services helper")
    s.add_argument("--backup", type=Path)
    s.add_argument("--installed", type=Path, help="Installed bundle folder, or the folder of the last update")
    s.add_argument("--yes", action="store_true", help="Explicitly approve the reviewed device update without a prompt")
    for name in ("verify", "install", "remove"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("--backup", type=Path, required=name != "remove")
        s.add_argument("--bundle", type=Path, required=name != "remove")
        if name in {"install", "remove"}:
            s.add_argument("--yes", action="store_true", help="Explicitly approve the reviewed device change without a prompt")
    return p


def main() -> int:
    try:
        return dispatch(parser().parse_args())
    except KeyboardInterrupt:
        print("\nInterrupted. If an update started, keep the USB cable connected. Examine its private log before you try again.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, tarfile.TarError, urllib.error.URLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
