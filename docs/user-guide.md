# User guide

[Project overview](../readme.md) · [Install and update](install.md)

## First setup

1. Set Wi-Fi to on in the monitor's Wi-Fi settings.
2. Configure the services that you want below.
3. Check the status of each enabled service.

Wi-Fi shows the local network connection.
The monitor does not contact an unrelated server to test Internet access.
A failed service reports its own error.

## Local MQTT

The services helper publishes current readings to the broker you approve.
The vendor MQTT clients stay disabled.
There are no history batches, remote command subscriptions or stored messages waiting for reconnection.

The broker needs a fixed IPv4 address, a username and a password.
Hostnames and `.local` names are not supported for MQTT.

### Configure MQTT

1. Open **Settings → Local MQTT**.
2. Enter the broker IPv4 address.
3. Enter its TCP port.
4. Enter the username.
5. Enter the password.
6. Check the client ID and publish topic.
7. Select the report interval.
8. Set **Enable MQTT** to on.
9. Select **Save & allow broker**.

The display restarts after a successful save.
The default report interval is 60 seconds. The permitted range is 10 to 3600 seconds.
Edits remain drafts until you save them.
Invalid drafts leave the applied configuration unchanged.

Each report has one current value per sensor, with a timestamp and format version.
Temperature uses Celsius regardless of the display unit.
A failed or unready sensor has a null value.
Readings are not retained by the MQTT publisher.

| Status | Meaning |
| --- | --- |
| Off | MQTT is disabled. |
| Publishing current readings | The broker acknowledged the latest report. |
| Waiting for fresh readings | The display has not provided a recent snapshot. |
| Could not publish readings | The message gives the connection or permission error. |

### Stop MQTT

1. Set **Enable MQTT** to off.
2. Select **Save & disable MQTT**.

This removes the broker permission. Time, weather and DNS keep their own approvals.

### TLS and credentials

**TLS encryption** requires a trusted certificate that matches the broker's IP address.
The helper verifies the certificate and its dates. It does not use unverified TLS.
The monitor has no control to import a private certificate authority.

Local settings contain the username and password without encryption.
Root access can read them. Private computer backups can contain them as well.

### Home Assistant

Enable the MQTT integration in Home Assistant before discovery.
Allow the monitor's broker account to publish its state topic and `homeassistant/#`.

**Home Assistant discovery** is on initially.
Discovery describes one monitor with eight sensors: temperature, humidity, CO2, PM2.5, PM10, eTVOC index, noise and battery.
The broker retains the discovery message.

Discovery is sent after services start, after relevant settings change, and after the reading publisher reconnects.
It is not sent with every reading.
The discovery switch takes effect within 30 seconds when the broker is available.

Live sensors expire after three report intervals, with a minimum of 180 seconds.
Only current-reading reports extend their availability.
Legacy firmware history packets cannot update these sensors.

To remove discovery, switch it off while MQTT remains enabled.
Wait for **Removed from the broker** before disabling MQTT or resetting the monitor.
If the broker is unavailable, remove its retained discovery later through Home Assistant or the broker.

## Internet services

**Settings → Internet services** contains **Time**, **Weather**, **Advanced** and **Details**.
Each service starts disabled.
Changes apply only through **Save → Approve & save**.

| Service | Preset | Normal interval |
| --- | --- | --- |
| Time | Cloudflare numeric time servers | 6 hours |
| Weather | Open-Meteo forecast service | 30 minutes |
| DNS | Quad9 resolvers | Only when an approved service needs a lookup |
| City search | Open-Meteo geocoding | One search per approval |

The presets provide no permission until you approve them.
The monitor never adds an unapproved provider after a failure.
You can change the endpoints in **Advanced**.

### Set the clock

1. Open **Time**.
2. Select the time zone.
3. Enable time synchronization.
4. Select **Save**.
5. Check the requested server access.
6. Select **Approve & save**.

Automatic corrections greater than five minutes require **Sync now**.
A large correction changes timestamps, including local history timestamps.
The helper also attempts to save the clock to the hardware clock.
A failure to save it appears in the status.

The preset time servers use numeric addresses, so they do not require DNS.
Time uses SNTP without NTS authentication.
You can keep Internet time off and set the clock manually.

### Find a weather location

1. Open **Weather → Location**.
2. Enter at least two characters of a city or postcode.
3. Select **Allow search once**.
4. Check the listed lookup and DNS providers.
5. Approve the search.
6. Select a result.

Typing alone sends no request.
A search can use the displayed draft resolvers while scheduled weather and DNS remain off.
Search approval applies only to that request.
It does not enable recurring services.

A result sets the coordinates, place label and weather time zone.
You can enter coordinates manually in **Advanced**.
A place label alone does not set the location.

### Enable weather

1. Set weather to on.
2. Select **Save**.
3. Check the requested weather and DNS access.
4. Select **Approve & save**.
5. Check the weather card and its age.

The normal interval is 30 minutes, or approximately 48 requests per day.
Startup, saving settings and **Update now** can cause additional requests.
Ordinary failures retry after 1, 2, 4, 8 and 16 minutes, up to the normal interval.

A rate-limit response waits at least one normal interval.
A valid provider retry delay can extend this wait.
Manual refresh and clock correction cannot bypass that delay.
The delay is held in memory; a full restart clears it.

An incorrect clock can prevent HTTPS certificate checks.
For **Sync time, then retry**, correct the clock before another attempt.
A successful time correction retries failed weather unless a provider delay remains active.

Custom weather and search endpoints must use the corresponding Open-Meteo JSON format over verified HTTPS.
URLs with credentials, query parameters or fragments are rejected.
Qingping and Cleargrass service names are rejected.

### Privacy

Weather sends the selected coordinates. City search sends the entered city or postcode.
The provider also sees the public IP address.
Neither request includes indoor readings or the monitor identity.
Use a city center if your exact location is unnecessary.

DNS sends queries only to the approved resolvers.
It does not use fallback resolvers from the operating system or DHCP.
An approved DNS answer does not itself grant network access.
The approval screen also explains permission for DNS address changes and private LAN destinations.

Provider terms still apply. Review the [Open-Meteo terms](https://open-meteo.com/en/terms) before use.
The weather page includes Open-Meteo and GeoNames attribution.

## Display options

**Minimum brightness** sets the lower limit for manual brightness and **Auto-Brightness**.
Choose a value from 1 to 100 on the 0–1000 scale.
The modded default is 1. The original default is 20.
Release the slider to save the value. The setting stays saved after a restart.
Choosing 20 restores the factory automatic brightness curve. Maximum brightness stays the same.
Auto-Brightness averages changes in room lighting before it adjusts the screen.
Use **Settings → Screen** or the brightness shortcut to adjust it.

**Settings → Screen → Screen Saver → Modern Clock** shows the clock and selected readings.
Use its edit icon to choose weather and sensor values.
**Preview** displays the result even when **Start After** is **Never**.
Weather appears only when the weather service is enabled.
The layout moves slightly each minute to reduce static display wear.

**Settings → About** shows **Mod Version** and **Mod Installed**.
The installation time comes from the computer that performed the last change.

## Storage

The display keeps local detail history for approximately one day and daily records for 30 days.
The firmware removes expired records at startup and during hourly cleanup.
SQLite can reuse freed space without making its database file smaller.

MQTT stores no reading backlog.
The services helper keeps one current snapshot and bounded logs in RAM.
Successful updates remove their temporary monitor files after verification.
Failed updates keep recovery files until the fault is resolved.

## Removal

Reset removes the modification and restores vendor software.
This includes the factory brightness minimum and automatic brightness curve.
It erases Wi-Fi and MQTT credentials, service settings, reading history and diagnostic logs.
Hardware identity and calibration stay in place.
Wi-Fi stays off after restoration.

1. Disable Home Assistant discovery while MQTT is still connected, if applicable.
2. Wait for **Removed from the broker**.
3. Open **Settings → Reset**.
4. Read the removal notice.
5. Select **Restore**.
6. Wait for **Restore complete**.
7. Switch the monitor off with its power control.
8. Switch the monitor on.

The computer is unnecessary for this procedure.
Keep power connected. If restoration stops, keep USB available and follow [Recovery](recovery.md).

A lost computer backup does not prevent Reset when the stock copy remains intact.
To install again, use the normal installation procedure.
The installer creates a new stock copy.

## Known limits

The firewall controls ordinary IP traffic, not all possible actions of privileged vendor code.
It does not block Wi-Fi association, ARP or raw DHCP.
It blocks network ADB but preserves USB debugging.

The restore archive is not a full flash image.
A storage failure can destroy it with the rest of the monitor.
A separate computer backup remains useful.

Host tests cannot establish hardware recovery, sensor accuracy or long-term storage behavior.
The release procedure includes monitor tests for those operating paths.
