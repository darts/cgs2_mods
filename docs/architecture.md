# Architecture

[Project overview](../readme.md) · [Firmware patches](firmware-patches.md) · [Development](development.md)

## Responsibilities

The vendor display app still reads the sensors, draws the interface and maintains local history.
Audited patches disable its network clients and replace selected settings pages.

The services helper owns MQTT publication, Home Assistant discovery, time, weather and approved DNS.
A supervisor restarts it after a failure.
The helper uses no downloaded Go modules and no operating-system resolver fallback.

The firewall helper installs the IPv4 policy.
The early startup guard applies this policy before normal networking and application startup.
The host tools prepare changes, check file hashes and install through USB ADB.

## Network permission

All services start disabled.
Saving a valid configuration is separate from editing a draft.
A failed validation leaves the existing configuration and permissions unchanged.

MQTT has a persistent permission for one numeric broker address and TCP port.
Time, weather and city search receive temporary permissions for their approved requests.
DNS uses only the selected numeric resolvers.
Each network operation checks its service permission before connection.
Revocation or a failed policy update stops further operations.

The services helper serializes changes to shared policy state.
It checks permissions again after configuration changes.
A permission created for weather cannot authorize an MQTT connection.
The helper clears temporary permissions when requests finish and when it restarts.

HTTPS keeps certificate and date verification enabled.
It rejects redirects, proxy environment settings and URLs with embedded credentials.
The helper connects to approved addresses without an implicit DNS lookup.
A hostname can use changed DNS addresses only after the owner approves that behavior.

City search consumes one explicit approval.
Opening settings, typing a location or selecting a provider gives no network permission.
A search never enables recurring weather or DNS by itself.

## Firewall boundary

The firewall uses the stock kernel's IPv4 packet-filter interface.
It replaces the complete owned table atomically and verifies the result.
It accepts the original empty table or an exactly recognized project table.
It refuses an unknown table instead of overwriting unrelated rules.

| Traffic | Behavior |
| --- | --- |
| Ordinary outgoing IP | Allow loopback and approved protocol/address/port combinations; drop the rest. |
| Forwarded IP | Drop. |
| Network TCP port 5555 | Drop unless received through the actual loopback interface. |
| Other incoming traffic | No general inbound restriction. |
| IPv6 | Disabled before network startup, including defaults for new interfaces. |

The separate TCP/5555 restriction prevents an approved broker from reaching network ADB through its permitted source port.
USB gadget debugging remains available.

The policy does not control raw packet sockets, ARP or Wi-Fi association.
Privileged code can change the policy.
This is a boundary against ordinary unwanted connections, not a sandbox for hostile root code.

## MQTT data

The display replaces one RAM snapshot every five seconds.
It exports calibrated numeric sensor values before display-unit conversion.
A sensor that is unready or reports an error has a null value.

The services helper publishes only a recent snapshot at the applied report interval.
Its connection has MQTT keepalive and bounded retry delays.
It has no command subscription, history queue or offline replay.
Live state uses acknowledged, non-retained MQTT messages.

Discovery uses a separate short connection and retained messages.
It runs at service startup, after relevant changes and after a reading connection recovers.
It is not periodic sensor data.
Disabling discovery removes its retained message while the broker remains approved.
A disconnected or no longer approved broker requires later cleanup by its owner.

Home Assistant templates accept only the local snapshot format.
They ignore the legacy firmware's current and history packets.
They expose failed sensors as unknown and expire stale reports.
See the [Home Assistant MQTT sensor reference](https://www.home-assistant.io/integrations/sensor.mqtt/).

## Storage and scheduling

The firmware retains detail history for approximately one day and daily history for 30 days.
Its startup and hourly cleanup removes expired records.
The local publisher does not produce the native MQTT cache.
The recovery archive is created once per installation and kept through updates.

The helper keeps a bounded current snapshot, status and logs in RAM.
Each worker log has a one-MiB limit and one rotated copy.
Successful updates delete their monitor staging folder after verification and receipt storage.
Failed updates retain rollback files for diagnosis.

Weather normally updates every 30 minutes.
Ordinary failures use increasing delays up to the configured interval.
HTTP 429 waits at least that interval.
Valid Retry-After values from HTTP 429 or 503 can extend the wait.
A manual request, settings save or successful time correction cannot shorten an active provider delay.
This delay is in memory and does not survive a full restart.

## Installation, update and removal

Installation verifies the original firmware and the replacement files.
It saves a canonical archive of replaced and removed originals on the monitor before the first original-file change.
It also saves the installation receipt and an independent restore worker.
The archive excludes owner settings and readings.

The boot guard is installed first.
If installation stops before its dependencies arrive, normal startup remains blocked.
USB rescue is the intended recovery path.
Its complete failure path still requires a deliberate hardware test before release.

Updates replace only the app and services helper.
The worker preserves settings and boot files, keeps rollback copies, and observes the restarted display before declaring success.
The computer stores verification evidence and updates the on-monitor receipt.
The receipt lets a new computer retrieve verified build inputs.

Reset and USB removal use the same detached restore worker.
The worker checks archive identity, contents, paths, types and hashes before it restores files.
It stops the display and services, revokes network access, and starts an independent progress display.

Restoration erases owned settings, credentials, history and diagnostic logs.
It preserves unrelated files, calibration and hardware identity.
The original startup script is restored last, after the other files pass verification.
The stock copy is removed only after success.
No reset operation invokes the vendor reset routine or a software reboot.

If a step fails, the worker keeps its stock source and reports recovery instructions.
The monitor's physical power control is used only after successful restoration.
A lost computer backup is harmless while the on-monitor copy remains valid.
Failed storage needs a separate backup or a manufacturer recovery image.
