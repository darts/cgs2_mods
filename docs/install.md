# Install and update

[Project overview](../readme.md) · [User guide](user-guide.md)

Installation requires the audited **4.5.6_0181** firmware and root USB debugging.
The tools reject unknown original files before installation.
A version label alone does not establish compatibility.

## Prepare the monitor

1. Connect the monitor to power.
2. Open **Settings → About**.
3. Check the firmware version.
4. Tap the first row seven times.
5. Set **Adbd Debugging** to on in **Developer Options**.
6. Connect the USB data cable to the computer.

A cable for power only cannot carry data.
Connect only one monitor, unless you select its serial through the command line.

## Prepare the computer

1. Install [mise](https://mise.jdx.dev/installing-mise.html).
2. Open a terminal in the project folder.
3. Run `mise trust`.
4. Run `mise run setup`.
5. Run `mise run start`.

Setup changes the computer only.
It uses an available ADB program or downloads a verified official Google copy.
On Linux ARM, use the ADB program from your distribution.
USB permissions or drivers can be necessary.

You can also open the menu with `Start.command` on macOS, `Start.cmd` on Windows, or `./start.sh` on Linux.

## Install

1. Select **Install the modification** (1).
2. Wait for preparation to finish.
3. Read the proposed changes.
4. Enter `INSTALL` to approve installation.
5. Wait for the restart instructions.
6. Switch the monitor off with its power control.
7. Switch the monitor on.
8. Wait for the readings to appear.
9. Press Enter in the computer menu.
10. Check that all examination results show `OK`.

Keep power and USB connected throughout the procedure.
Do not use `adb reboot`. A software restart can leave USB debugging unavailable.

The installer makes a small working copy on the computer automatically.
It saves verified originals on the monitor before it changes them.
The stock copy contains software files, not your Wi-Fi or MQTT settings.

After installation, Wi-Fi, MQTT, time, weather and DNS are off.
No server has permission to connect.
Use the [User guide](user-guide.md) to configure the services you want.

## Menu status

```text
  Computer tools   Ready
  Monitor          Connected (firmware 4.5.6_0181)
  Modification     Not installed
  Backup           Not made
  Stock copy       Not saved on the monitor

Next step: Select 1 to install the modification. A computer backup is optional.
```

The stock copy status changes after installation.
**Make an optional computer backup** (5) saves the current files and any stock copy separately.
This backup contains private data. It is a file backup, not a full flash image.

| Message or condition | Action |
| --- | --- |
| Not connected | Check the USB data cable and **Adbd Debugging**. |
| Connected, not allowed | Approve USB debugging on the monitor. |
| More than one device | Disconnect the other USB devices. |
| Connected, no root access | Stop. This firmware cannot use the installation tools. |
| Unsupported firmware or unknown helper | Stop. Keep the diagnostic message for review. |
| `PROBLEM` during examination | Keep USB connected. Follow the reported fault before further changes. |
| `INFO` for Wi-Fi or approved servers | No action is necessary if these are your saved choices. |

**Examine the installation** (2) only reads the monitor.
It checks the firewall even when services have approved servers.

## Update

1. Extract the new project release into a private folder.
2. Run `mise trust` in that folder.
3. Run `mise run setup`.
4. Run `mise run start`.
5. Select **Update the modification** (3).
6. Read the prepared update description.
7. Enter `INSTALL` to approve the update.
8. Wait for successful verification.

The display and services restart. The monitor does not reboot.
Settings and server approvals stay in place.
The tool can obtain its build inputs from the stock copy on the monitor.
You can keep your previous private folders for additional recovery evidence.

An update that finds identical files makes no changes.
A failed update attempts to restore the previous app and services helper.
If it reports incomplete recovery, keep USB connected and obtain help before another attempt.

Normal updates replace only the display app and services helper.
Changes to startup, the firewall or installation helpers need a separately reviewed migration.

## Removal

Use **Settings → Reset** on the monitor for normal removal.
It restores vendor software and erases settings, credentials and readings.
The computer menu provides the same operation through **Remove the modification** (4).
See [Removal](user-guide.md#removal) and [Recovery](recovery.md).
