# Firmware patches

[Architecture](architecture.md) · [Development](development.md)

## Supported input

The public tools accept only the audited CGS2 firmware **4.5.6_0181**.
They check the original application hash, embedded resource layout and each modified instruction.
Startup scripts have independent hash checks.
A matching version string does not bypass these checks.

As of 26 October 2026, 0181 is the newest image examined by this project.
On that date, an approved check of Qingping's release update service offered no update from 4.5.6_0181.
That result applies to the tested monitor and update channel.
It does not establish availability for other regions or staged rollouts.
The [manufacturer's product page](https://shop.qingping.co/products/qingping-air-monitor) does not establish a downloadable recovery image for this exact build.
Do not describe 0181 as the latest vendor release without new evidence.

The project does not download firmware from third-party mirrors.
The installed modification does not query vendor update services.
A manual update investigation needs owner approval before it sends monitor identifiers.
An older monitor with selected 0181 files copied onto it is not a verified full 0181 installation.
Private recovery evidence for such a monitor does not establish public support.

## Disabled behavior

| Path | Reason |
| --- | --- |
| Native MQTT startup, shared connection method and history queue | The local helper owns publication. Native batches, subscriptions and remote commands must not run. |
| Vendor MQTT gateway, AIRMX and Hydra clients | Prevent independent vendor clients and fallback connections. |
| Vendor weather, city lookup and HTTP time | Owner-approved services provide these functions. |
| Shared vendor HTTP requests and downloads | Block remaining callers, including startup races and hidden controls. |
| Automatic updates and log uploads | Prevent unapproved downloads and diagnostic transmission. |
| Joylink provisioning and Google discovery | Prevent unused SDK networking and identity advertisements. |
| Native Avahi startup | Prevent advertisements started before the discovery runner executes. |
| External Wi-Fi connectivity probe | Report local Wi-Fi association without pinging an unrelated server. |
| Credential logging call sites | Prevent Wi-Fi and MQTT passwords from entering native logs. |

The patcher returns from audited void network methods before their original prologues.
Constructors, sensor acquisition and local history cleanup remain intact.
The Wi-Fi patch preserves the existing local association checks and success cleanup.
The shared MQTT connection guard covers plain and encrypted native connections.

## Interface and startup changes

The resources replace cloud controls with Local MQTT and Internet services.
The display exports a current sensor snapshot to RAM for the services helper.
It also includes Modern Clock, release information and the on-device restoration confirmation.

Both brightness pages provide a saved minimum from 1 to 100 on the 0–1000 scale.
The mod defaults to 1; the original default is 20.
The automatic curve uses this minimum and starts its next interpolation segment one level higher.
Choosing 20 restores the factory curve. Positive backlight writes also respect the saved minimum.
The small native settings reader uses unused resource padding in the existing executable segment.
It reads the existing settings store. It adds no service or network access.
The original nine-level cutoff is removed so the controller completes small brightness adjustments.
Light averaging and gradual fades remain active.
Maximum brightness and screen-off behavior stay unchanged.
These changes are contained in the display app. Reset restores the original app and clears its saved brightness settings.

Changed resources receive new cache identities derived from their contents.
This prevents Qt from running stale compiled QML after an update.
The patcher decodes the completed resource tree and checks every resource again.

Startup changes remove vendor daemons and automatic NTP.
They preserve the sensor programs and USB initialization.
The firewall runs before normal networking starts.
Noise analysis uses bounded RAM storage.

## Review the patch report

The preparation command writes a private patch report.
It records input and output hashes, changed resources, cache identities and exact instruction replacements.
The source contains the instruction addresses and expected bytes; this guide does not duplicate those tables.

1. Check the report's input hash against the supported original.
2. Check each changed resource against the intended feature.
3. Check each instruction replacement against its audited method.
4. Build the app from the original input again.
5. Compare the resulting report and executable.

Identical inputs require the same tool and compression-library versions for identical output bytes.
Never substitute a filename or version label for a hash check.

## Audit limits

The investigation covered native network call sites, embedded QML, startup scripts, DHCP hooks and observed processes.
Earlier monitor observations confirmed approved traffic and removed the public ping and system-DNS probes.
Those observations do not validate later changes automatically.

For each release, repeat the MQTT, privacy, sensor and recovery checks.
Unused proprietary code and libraries remain on the monitor.
The default-deny firewall remains necessary.
Raw captures, credentials, firmware and disassembly stay private.

For another firmware version, follow [Support another firmware](development.md#support-another-firmware).
