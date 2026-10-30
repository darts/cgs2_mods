# Qingping Air Monitor 2: local control

This project lets a Qingping Air Monitor 2 (CGS2) operate without the vendor cloud.
It keeps the display, sensors and local reading history.

## Features

- **Local MQTT and Home Assistant.** Send current readings to your broker, with automatic sensor discovery in Home Assistant.
- **Network access you control.** A firewall blocks connections you have not approved. Vendor cloud requests, MQTT clients, updates and log uploads are disabled.
- **Optional weather and time sync.** Choose which services to enable. Weather normally updates every 30 minutes. City search needs approval for each request.
- **Adjustable minimum brightness.** Set the minimum from 1 to 100 for manual brightness and Auto-Brightness. The mod defaults to 1; the factory default is 20.
- **Modern Clock.** Show the time, selected sensor readings and optional weather on the screen saver.
- **Reset to factory software.** Restore the original software from a verified copy on the monitor, including the factory brightness behavior. A computer backup is optional.

Wi-Fi and all network services are off after installation.

## Requirements

- A CGS2 with firmware version **4.5.6_0181**.
- A USB data cable and a computer with macOS, Linux or Windows.
- USB debugging enabled on the monitor.

Check the firmware at **Settings → About**.
Other firmware is not officially supported.

The monitor procedures have been tested on macOS.
The host checks cover macOS, Linux and Windows, with platform limits listed in [Development](docs/development.md).

## Quick start

1. Install [mise](https://mise.jdx.dev/installing-mise.html).
2. Extract the project into a private folder.
3. Run these commands in that folder:

   ```sh
   mise trust
   mise run setup
   mise run start
   ```

4. Follow **Install the modification** in the menu.

Installation saves and verifies the stock copy before it changes the original files.
A separate computer backup is optional.
For the complete procedure, use [Install and update](docs/install.md).

## Important precautions

- Keep power and USB connected during installation, update or removal.
- Use the physical power control to restart the monitor. Do not use `adb reboot`.
- Keep `.private/` private. It can contain firmware, passwords and monitor identifiers.
- Keep optional computer backups outside the project folder as well.

The stock copy supports normal removal. It cannot recover failed storage or a monitor that cannot start USB debugging.

## Documentation

- [Install and update](docs/install.md)
- [User guide](docs/user-guide.md)
- [Recovery](docs/recovery.md)
- [Architecture](docs/architecture.md)
- [Firmware patches](docs/firmware-patches.md)
- [Development and release checks](docs/development.md)

## License

Original project code and documentation use the [MIT license](LICENSE).
The license does not cover vendor code fragments in the patch scripts.
This project is independent of Qingping. Firmware and private backups are not distributed.
