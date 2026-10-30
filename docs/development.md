# Development

[Project overview](../readme.md) · [Architecture](architecture.md) · [Firmware patches](firmware-patches.md)

The host tools use Python's standard library.
Zig builds the static ARM64 firewall helper. Go builds the static services helper without external modules.
[mise.toml](../mise.toml) pins the tool versions.
Jinja2 is a test dependency for the Home Assistant templates, not an installed monitor dependency.

## Build and test

```sh
mise run build
mise run test
mise exec -- go test -race ./src/services
mise exec -- go vet ./src/services
```

These commands do not access a monitor or public weather servers.
Network tests use local fake MQTT, DNS, NTP and HTTPS servers.
The tests do not set the computer clock.

Full development checks require Node.js, Jinja2, a C compiler and, on POSIX hosts, `jq`.
Install the template dependency with `mise exec -- python -m pip install Jinja2==3.1.6`.
The test runner reports unavailable optional checks locally. CI requires its platform's test dependencies.

Windows runs portable Python checks and cross-builds the helpers.
POSIX process fixtures and native C/Go tests require Linux or macOS.
Firewall tests use address and undefined-behavior sanitizers.

## Source responsibilities

| Area | Responsibility |
| --- | --- |
| `qingping.py` | Owner menu and common commands |
| `scripts/device.py`, `prepare.py`, `recovery.py` | Verified inputs, installation and shared restoration |
| `scripts/install_services.py` | Guarded app and services updates |
| `scripts/patch_app.py`, `patch_services.py` | Audited firmware changes |
| `src/services/` | Current MQTT readings, discovery, time, weather, DNS and permission control |
| `src/cgs2-filter.c`, `device/S02privacy` | Firewall and early startup guard |
| `ui/`, `device/` | QML interfaces and installed helpers |
| `tests/` | Host regressions and release source checks |

Generated binaries belong in `build/`; tools and caches belong in `.tools/`.
Firmware, prepared bundles, UI fixtures, captures and backups belong in `.private/`.
These folders must remain outside the release.
On Windows, use a private account folder with appropriate access controls.

**Do not run `git clean -x`: it deletes ignored backups, including `.private/`.**

## Commands

`python qingping.py` opens the menu.
Use `python qingping.py COMMAND --help` for each command's arguments.
The common commands are `setup`, `doctor`, `backup`, `prepare`, `install`, `examine`, `verify`, `update`, `remove` and `test`.

The menu creates installation working files automatically.
For a separately reviewable manual installation:

```sh
python qingping.py backup --output .private/backup
python qingping.py prepare --backup .private/backup --output .private/release
python qingping.py install --backup .private/backup --bundle .private/release/bundle
```

Inspect the bundle and patch report before installation.
Use the monitor's physical power control when instructed.
Then run `python qingping.py examine`.

`verify` compares an installation with its exact bundle, so it is appropriate before subsequent updates.
`examine` checks current operating health after updates and configuration changes.

Normal update and removal can use the monitor's stock copy:

```sh
python qingping.py update
python qingping.py remove
```

A computer backup taken after installation includes the stock copy.
It can provide removal through `remove --backup PATH` if the monitor's copy is damaged.
A backup from before installation also needs its prepared `--bundle PATH`.
The same restore worker erases owner data in every removal mode.
There is no settings-preserving removal mode.

Use `--adb PATH` or `CGS2_ADB` to select ADB.
Use `--serial SERIAL` when more than one monitor is connected.
Write commands require a confirmation word unless the caller supplies `--yes`.

## Update boundary

Normal updates replace only the display app and services helper.
They preserve startup files, USB configuration, approved endpoints and owner settings.
A verified successful update records its receipt and removes its monitor staging folder.
A failed update retains rollback files and diagnostics.

Changes to `device/`, the restore worker, startup scripts, the firewall or Zig require reviewed migration code before release.
Do not make the ordinary updater accept unknown helpers or hashes.
The lower-level app-only update remains available for reviewed development use.
`scripts/install_services.py --help` describes the app-only and runtime preparation options.

The audited firewall hash depends on the pinned Zig build.
A compiler change requires a fresh audit.
For an unexpected cache-dependent hash, clear only the Zig cache folders before rebuilding with the pinned compiler.

## Actual Qt checks

Host JavaScript tests do not reproduce keyboard behavior or QML property timing.
Keep the real Qt fixtures alongside their host checks.

```sh
python scripts/build_ui_smoke.py .private/release/original/qingping/bin/QingSnow2App .private/ui-smoke
python scripts/build_services_ui_smoke.py .private/release/original/qingping/bin/QingSnow2App .private/services-ui-smoke
python scripts/build_screensaver_ui_smoke.py .private/release/original/qingping/bin/QingSnow2App .private/screensaver-ui-smoke
```

Each builder prints its runtime command.
Clear the named fixture settings before each screen-saver harness.
The fixtures use the monitor's Qt controls and mock service calls.
They make no provider requests.
Run them with `QML_DISABLE_DISK_CACHE=1` and keep generated vendor resources private.

The release check must exercise both keyboard modes, city-search enablement, saved MQTT settings and Reset confirmation.
It must also check the actual display export and live MQTT payload.
A JavaScript-only pass cannot replace these checks.

## Support another firmware

A new version requires a verified image with a known source and a test monitor.
The version label alone is insufficient.

1. Identify the hardware revision and firmware provenance.
2. Check root ADB, partition layout, kernel filtering and Qt compatibility.
3. Locate the resource tree and instruction symbols again.
4. Audit the network clients, credentials, startup scripts and reset behavior.
5. Update the input hashes and guarded anchors only after that audit.
6. Rebuild from the new original image.
7. Run the host and real Qt checks.
8. Test installation, update, privacy and restoration on the target monitor.

Do not copy another monitor's whole storage image.
It can replace identity, calibration and credentials.
The current on-monitor archive restores only files this project changed or removed.
It is not a replacement operating-system image.

## Release checklist

The Git source tree is the release. GitHub can produce its archive from the release tag.
There is no separate source archive generator.

1. Set `MOD_VERSION` in the app patcher and `modVersion` in the services helper to the intended release.
2. Run all required host checks, Go race checks and `go vet`.
3. Build and inspect a private bundle from the audited stock image.
4. Run the actual Qt fixtures on the test monitor.
5. Observe MQTT beyond the former 15-minute batch interval.
6. Test disabled services and traffic to each approved endpoint.
7. Test Reset with the computer backup unavailable.
8. Verify restored stock hashes, erased owner data and preserved identity.
9. Restart with the physical power control.
10. Reinstall the candidate for the owner's intended use.
11. Check documentation links and terminology.
12. Review procedural sentences against the 20-word limit and descriptive sentences against the 25-word limit.
13. Review documentation for accuracy, clear instructions and consistent ASD-STE100 terminology.
14. Inspect `git ls-files -co --exclude-standard` in a clean source checkout.
15. Run `python -m unittest discover -s tests -p test_release_files.py` there.
16. Review the archive that the intended Git tag will contain.
17. Create the tag only after all release gates pass and the owner approves publication.

Keep the source revision, tool versions, patch report, bundle manifest and hardware results in the private release evidence.
Do not include firmware, monitor identifiers, passwords or raw logs in Git.
