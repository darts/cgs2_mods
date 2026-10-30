#!/usr/bin/env python3
"""Repack audited CGS2 Qt resources without distributing the proprietary app.

Only the exact inspected stock executable is accepted. Its ELF layout, sensor
drivers and resource names stay intact. Eight audited calls that log passwords
are replaced with AArch64 NOPs. Audited vendor network methods return immediately.
The stock external Wi-Fi ping check is bypassed after local association/IP checks.
Changed and original resources are compressed into the existing allocation;
resource offsets are regenerated. Changed resources receive content-derived
timestamps so Qt rejects stale disk-cache bytecode. The newly written file is
parsed again and every payload and timestamp is verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

STOCK_SHA256 = "715abe67c1f4395c80a703d692de5fb69fa66b3e6db6213b0d6f4054ac0ec86f"
TREE_OFFSET, TREE_SIZE = 0x22F768, 6468
NAME_OFFSET = 0x2310AC
DATA_OFFSET, DATA_SIZE = 0x2334B6, 242703
ROOT = Path(__file__).resolve().parents[1]
# Shown on Settings -> About. Change it for each release: the new text changes
# the app, so the guided update installs it.
MOD_VERSION = "V0.1"
MIN_BRIGHTNESS = 1
# A small native settings reader occupies unused, executable resource padding.
# Keep the resource allocation and ELF segments unchanged.
BRIGHTNESS_CODE_OFFSET = (DATA_OFFSET + DATA_SIZE - 256) & ~7
BRIGHTNESS_CODE_ADDRESS = 0x400000 + BRIGHTNESS_CODE_OFFSET
# The installers write this file; the About page shows its time when present.
RELEASE_RECORD = "/data/etc/cgs2-release.ini"
SERVICES_RESOURCE = "/Modules/Location/LocationPage.qml"
# This hook runs in the ordinary application after its native managers exist.
# It cannot enable a service: the helper loads the owner's saved permissions.
SERVICES_STARTUP = '''
    Timer {
        objectName: "internetServicesStartup"
        interval: 1000; running: true; repeat: false
        onTriggered: {
            datetimeManager.isAutomatically = false;
            developerController.runCommand("/usr/local/sbin/cgs2-services start");
            var request = new XMLHttpRequest();
            request.open("GET", "file:///etc/cgs2-privacy/services.json");
            request.onreadystatechange = function() {
                if (request.readyState !== XMLHttpRequest.DONE) return;
                try {
                    var settings = JSON.parse(request.responseText);
                    if (settings.version !== 1) return;
                    var timezone = datetimeManager.timezoneConfig;
                    var zones = timezone.timezoneList;
                    for (var i = 0; i < zones.length; i++) {
                        if (zones[i].name === settings.time_timezone) {
                            if (timezone.name !== settings.time_timezone) timezone.timezoneIndex = i;
                            break;
                        }
                    }
                } catch (e) {}
            };
            request.send();
        }
    }
'''
SUMMARY_SERVICES_STATE = '''
    id: localSummary
    property bool localWeatherEnabled: false
    property var localWeatherSnapshot: null
    property int localWeatherInterval: 30
    property double localWeatherNow: Date.now()
    function refreshLocalWeather() {
        localWeatherNow = Date.now();
        var config = new XMLHttpRequest();
        config.open("GET", "file:///etc/cgs2-privacy/services.json");
        config.onreadystatechange = function() {
            if (config.readyState !== XMLHttpRequest.DONE) return;
            try {
                var settings = JSON.parse(config.responseText);
                localWeatherEnabled = settings.version === 1 && settings.weather_enabled === true;
                localWeatherInterval = Number(settings.weather_interval_minutes) || 30;
            } catch (e) { localWeatherEnabled = false; }
            if (!localWeatherEnabled) { localWeatherSnapshot = null; return; }
            var status = new XMLHttpRequest();
            status.open("GET", "file:///run/cgs2-services/status.json");
            status.onreadystatechange = function() {
                if (status.readyState !== XMLHttpRequest.DONE) return;
                try {
                    var result = JSON.parse(status.responseText);
                    localWeatherSnapshot = (result.weather || {}).snapshot || null;
                } catch (e) { localWeatherSnapshot = null; }
            };
            status.send();
        };
        config.send();
    }
    function localCondition(code) {
        if (code === 0) return "Clear";
        if (code === 1) return "Mainly clear";
        if (code === 2) return "Partly cloudy";
        if (code === 3) return "Overcast";
        if (code === 45 || code === 48) return "Fog";
        if (code >= 51 && code <= 57) return "Drizzle";
        if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "Rain";
        if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "Snow";
        if (code === 95 || code === 96 || code === 99) return "Thunderstorm";
        return "Unknown condition";
    }
    function localWeatherTitle() {
        var snapshot = localWeatherSnapshot;
        if (!snapshot || !snapshot.current) return "";
        var current = snapshot.current;
        var temperature = typeof current.temperature === "number" && isFinite(current.temperature)
            ? String(Math.round(current.temperature * 10) / 10) + " " + snapshot.temperature_unit : "Unavailable";
        return "Outdoor: " + temperature + " · " + localCondition(current.weather_code);
    }
    function localWeatherAge() {
        if (!localWeatherSnapshot) return "";
        var age = localWeatherNow - Date.parse(localWeatherSnapshot.updated_at);
        if (!isFinite(age) || age < -300000) return "Update time unavailable";
        var minutes = Math.max(0, Math.floor(age / 60000));
        return (minutes > Math.max(90, localWeatherInterval * 3) ? "Stale · " : "") + "Updated " + minutes + " min ago";
    }
    Component.onCompleted: refreshLocalWeather()
    Timer {
        interval: 30000; repeat: true; running: localSummary.visible
        onTriggered: localSummary.refreshLocalWeather()
    }
'''
SUMMARY_SERVICES_CARD = '''
        Rectangle {
            objectName: "localWeatherCard"
            Layout.fillWidth: true
            Layout.preferredHeight: 118
            visible: localSummary.localWeatherEnabled && localSummary.localWeatherSnapshot !== null
            radius: 20; color: "#1AFFFFFF"
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 18; spacing: 8
                QText {
                    objectName: "localWeatherTitle"
                    Layout.fillWidth: true; font.pixelSize: 30; textFormat: Text.PlainText
                    text: localSummary.localWeatherTitle()
                }
                QText {
                    objectName: "localWeatherAge"
                    Layout.fillWidth: true; font.pixelSize: 21; opacity: 0.65; textFormat: Text.PlainText
                    text: ((localSummary.localWeatherSnapshot || {}).source || "Weather") + " · " + localSummary.localWeatherAge()
                }
            }
            MouseArea {
                objectName: "localWeatherOpen"
                anchors.fill: parent
                onClicked: openPage("qrc:/Modules/Location/LocationPage.qml")
            }
        }
'''
# Shared by the screen saver loader and its settings page. Read with value():
# declared Settings properties would write their defaults back to the file.
MODERN_CHOICE = '''    Settings {
        id: cgs2ModernChoice
        fileName: "/data/etc/cgs2-screensaver.ini"
        category: "modern"
    }
    function cgs2ModernChosen() {
        var value = cgs2ModernChoice.value("enabled", false);
        return value === true || value === "true";
    }
'''
# Only a current snapshot crosses from the display into the local publisher.
# Qt settings replaces one RAM file; no native MQTT methods run.
REALTIME_REPORTS = '''
    Settings {
        id: cgs2Readings
        fileName: "/run/cgs2-readings.ini"
        category: "readings"
    }
    function cgs2SensorValue(sensor) {
        if (!sensor || sensor.isError || sensor.state !== AirState.CollectingState) return null;
        var value = Number(sensor.rawValue);
        return isFinite(value) ? Math.round(value * 100) / 100 : null;
    }
    function cgs2SaveReadings() {
        var battery = Number(batteryManager.capacity);
        var snapshot = {version: 1, timestamp: Math.floor(Date.now() / 1000),
            temperature: cgs2SensorValue(airdataController.airTEMP),
            humidity: cgs2SensorValue(airdataController.airHUMI),
            co2: cgs2SensorValue(airdataController.airCO2),
            pm25: cgs2SensorValue(airdataController.airPM25),
            pm10: cgs2SensorValue(airdataController.airPM10),
            tvoc_index: cgs2SensorValue(airdataController.airTVOC),
            noise: cgs2SensorValue(airdataController.airNoise),
            battery: isFinite(battery) && battery >= 0 && battery <= 100 ? battery : null};
        cgs2Readings.setValue("snapshot", JSON.stringify(snapshot));
        cgs2Readings.sync();
    }
    Timer {
        objectName: "currentReadings"
        interval: 5000; running: true; repeat: true; triggeredOnStart: true
        onTriggered: cgs2SaveReadings()
    }
'''
# Appended after the stock rows, so the hidden developer and test-mode taps on
# rows 1, 3 and 4 keep their indexes.
ABOUT_ROWS = '''    Settings {
        id: cgs2Release
        fileName: "%s"
        category: "release"
    }
    function cgs2AboutList() {
        var stock = global.aboutList || [];
        var rows = [];
        for (var i = 0; i < stock.length; i++) rows.push(stock[i]);
        rows.push({key: "Mod Version", value: "%s"});
        var installed = cgs2Release.value("installed", "");
        if (installed) rows.push({key: "Mod Installed", value: String(installed)});
        return rows;
    }
''' % (RELEASE_RECORD, MOD_VERSION)
# Virtual address, audited BL instruction, expected callee, purpose. Arguments
# are constructed and destroyed by each caller; the logger return is unused.
# NOP only the final print call so Wi-Fi and MQTT connection behavior remains.
CREDENTIAL_LOG_CALLS = (
    (0x502400, 0x97FC5114, 0x416850, "Joylink Wi-Fi provisioning password printf"),
    (0x55DE0C, 0x9400A879, 0x587FF0, "AIRMX MQTT username/password log"),
    (0x5B1F64, 0x97FF5823, 0x587FF0, "Wi-Fi network password log"),
    (0x5B2204, 0x97FF577B, 0x587FF0, "Hidden Wi-Fi network password log"),
    (0x5E4E4C, 0x97FE8C69, 0x587FF0, "Hydra MQTT username/password log"),
    (0x5ECA98, 0x97FE6D56, 0x587FF0, "MQTT connection username/password log"),
    (0x5F3AD4, 0x97FE5147, 0x587FF0, "Third-party MQTT configuration password log"),
    (0x5F4288, 0x97FE4F5A, 0x587FF0, "Vendor MQTT configuration password log"),
)
# The Google runner constructor starts avahi before its virtual run method.
# Keep construction intact; suppress only system("avahi-daemon ...").
NETWORK_START_CALLS = (
    (0x5CBD54, 0x97F9297F, 0x416350, "Disable native avahi-daemon startup"),
)
# Slots are confirmed void (Qt metatype 43) in their embedded metaobjects.
# GoogleRunner::run overrides the void QThread::run startup method.
# RET runs before its original prologue, so the caller's frame stays intact.
# The MQTT constructor loads local [third] settings via updateConnectionConfig
# before connectToGateway; the latter creates a distinct vendor fallback client.
# Sensor drivers remain intact. All native MQTT clients are disabled.
CLOUD_ENTRY_PATCHES = (
    (0x5EC8A0, 0xA9B57BFD, "MqttRunner::startWork", "Disable native MQTT timers, subscriptions and message handlers"),
    (0x5E6168, 0xA9B17BFD, "MqttRunner::saveHistoryData", "Disable native MQTT history queue production"),
    (0x5191E0, 0xA9BA7BFD, "QMqttClient::connectToHost", "Disable the shared native MQTT connection path, including TLS"),
    (0x581248, 0x3942C001, "LocationRunner::refreshWeather", "Disable vendor weather polling"),
    (0x581570, 0xA9B17BFD, "LocationRunner::refreshAutoCity", "Disable IP geolocation polling"),
    (0x585A7C, 0xA9B87BFD, "UpdateRunner::checkUpdateAuto", "Disable automatic vendor update checks"),
    (0x57762C, 0xD10983FF, "DeveloperRunner::onUploadAllLogs", "Disable log archive uploading"),
    (0x5F30D4, 0xA9AB7BFD, "MqttService::connectToGateway", "Disable fallback vendor MQTT client"),
    (0x59ABC8, 0xA9B47BFD, "DatetimeRunner::syncNetworkTime", "Disable native vendor HTTP time sync, including startup races"),
    (0x5D2854, 0xA9B77BFD, "HttpRunner::send", "Disable the shared vendor HTTP request path"),
    (0x5D2AE0, 0xA9BA7BFD, "HttpRunner::download", "Disable the shared vendor HTTP download path"),
    (0x55DA80, 0xA9B57BFD, "AirmxAppRunner::refreshMqttClient", "Disable the independent AIRMX MQTT client"),
    (0x5DFA70, 0xA9B97BFD, "HydraMqttRunner::refreshMqttClient", "Disable the independent Hydra MQTT client"),
    (0x565DA4, 0x39408001, "JingdongAppRunner::startService", "Disable Joylink SDK networking and provisioning startup"),
    (0x5CBD94, 0xA9B47BFD, "GoogleRunner::run", "Disable vendor mDNS identity advertisement and UDP service"),
)
# ScreenAPI::getBrightness(int) maps ambient light to the backlight level.
# Read the owner's minimum before the existing interpolation. w5 is unused by
# the stock curve. Both its floor and the next segment's start use that value,
# so choosing 20 reproduces the factory curve. Keep averaging and fading.
# setBrightness also clamps positive writes, including manual/startup/wake
# requests, while retaining zero for screen-off.
BRIGHTNESS_PATCHES = (
    (0x5A0DBC, 0x71000C3F, 0x14000000 | ((BRIGHTNESS_CODE_ADDRESS - 0x5A0DBC) // 4), "Read the saved minimum before mapping ambient light"),
    (0x5A0E78, 0x52800280, 0x2A0503E0, "Use the saved automatic brightness floor"),
    (0x5A0E44, 0x528002A0, 0x110004A0, "Start the next brightness segment at minimum plus one"),
    (0x5A1590, 0x7100245F, 0x7100005F, "Apply small automatic brightness changes so fades reach their target"),
    (0x5A1644, 0x2A0103F3, 0x14000000 | ((BRIGHTNESS_CODE_ADDRESS + 0x18 - 0x5A1644) // 4), "Clamp positive backlight writes to the saved minimum; preserve screen-off"),
)


def resources(binary: bytes, *, tree_offset: int = TREE_OFFSET,
              tree_size: int = TREE_SIZE, name_offset: int = NAME_OFFSET,
              data_offset: int = DATA_OFFSET, data_size: int = DATA_SIZE) -> dict[str, dict]:
    result = {}

    def walk(index: int, parent: str) -> None:
        node = tree_offset + index * 22
        name_entry, flags = struct.unpack_from(">IH", binary, node)
        name_len = struct.unpack_from(">H", binary, name_offset + name_entry)[0]
        start = name_offset + name_entry + 6
        name = binary[start:start + name_len * 2].decode("utf-16-be")
        path = parent + "/" + name if index else ""
        if flags & 2:
            count, first = struct.unpack_from(">II", binary, node + 6)
            if first + count > tree_size // 22:
                raise ValueError("resource directory exceeds audited tree")
            for child in range(first, first + count):
                walk(child, path)
        else:
            offset = struct.unpack_from(">I", binary, node + 10)[0]
            length = struct.unpack_from(">I", binary, data_offset + offset)[0]
            if offset + 4 + length > data_size:
                raise ValueError("resource exceeds allocation")
            payload = binary[data_offset + offset + 4:data_offset + offset + 4 + length]
            if flags & 1:
                expected = struct.unpack_from(">I", payload)[0]
                payload = zlib.decompress(payload[4:])
                if len(payload) != expected:
                    raise ValueError("resource decompression length mismatch")
            result[path] = {"node": node, "flags": flags, "payload": payload,
                            "last_modified": struct.unpack_from(">Q", binary, node + 14)[0]}

    walk(0, "")
    return result


def cache_timestamp(original: int, payload: bytes) -> int:
    """Give changed QML a reproducible, nonzero disk-cache identity.

    Qt 5.14.2 compares the resource's millisecond timestamp with the cached
    compilation unit; changing only the compressed payload leaves old bytecode
    usable. A fixed +1 would fail to invalidate subsequent patch revisions.
    A content-derived 40-bit increment changes with the QML, remains greater
    than stock, and stays within ordinary QDateTime ranges for this firmware.
    This is a cache version token, not a claimed real-world modification time.
    """
    return original + 1 + int.from_bytes(hashlib.sha256(payload).digest()[:5], "big")


def closing_brace(source: str, opening: int) -> int:
    depth, i = 0, opening
    quote = None
    while i < len(source):
        c = source[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end == -1 else end
            continue
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated QML comment")
            i = end + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced QML object")


def remove_object(source: str, kind: str, marker: str) -> str:
    candidates = []
    for match in re.finditer(r"\b" + re.escape(kind) + r"\s*\{", source):
        end = closing_brace(source, source.index("{", match.start()))
        if marker in source[match.start():end]:
            candidates.append((match.start(), end))
    if not candidates:
        raise ValueError(f"missing audited {kind} object: {marker}")
    start, end = min(candidates, key=lambda item: item[1] - item[0])
    return source[:start] + source[end:]


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"expected one QML anchor: {old!r}")
    return source.replace(old, new, 1)


def inline_component(host: str, source: str, component_id: str) -> tuple[str, str]:
    """Return the host with the source's imports, and the source as a Component.

    The resource tree cannot gain files, so a new page is embedded in an
    existing stock resource. Each import must match the host's version.
    """
    imports = re.findall(r"^import [^\n]+$", source, re.M)
    host_imports = re.findall(r"^import [^\n]+$", host, re.M)
    if not imports or not host_imports:
        raise ValueError("inline QML and its host need import statements")
    for line in imports:
        module = line.split()[1]
        present = [item for item in host_imports if item.split()[1] == module]
        if present and line not in present:
            raise ValueError(f"inline QML import differs from its host: {line}")
        if not present:
            host = replace_once(host, host_imports[-1] + "\n", host_imports[-1] + "\n" + line + "\n")
            host_imports.append(line)
    body = re.sub(r"^import [^\n]+\n", "", source, flags=re.M).strip()
    return host, f"    Component {{\n        id: {component_id}\n{body}\n    }}\n"


def transform(original: dict[str, dict]) -> dict[str, bytes]:
    changed = {}
    path = "/Setting/SettingMenu.qml"
    text = original[path]["payload"].decode()
    for marker in ['qsTr("Qingping+ App")', 'qsTr("Qingping IoT App")',
                   'qsTr("AIRMX")', 'qsTr("Automation")', 'qsTr("System update")']:
        text = remove_object(text, "SettingItem", marker)
    text = replace_once(text, 'qsTr("Private Cloud")', '"Local MQTT"')
    text = replace_once(text, 'qsTr("Time and Location")', 'qsTr("Time")')
    text = replace_once(text, '            /* 日期与时间 */', '''            SettingItem {
                icon: "qrc:/setting/icon/wifi.png"
                name: "Internet services"
                onClicked: gotoPage("qrc:/Modules/Location/LocationPage.qml")
            }
            /* 日期与时间 */''')
    changed[path] = text.encode()

    changed["/Modules/ThirdMQTT/ThirdMqttPage.qml"] = (ROOT / "ui/LocalMqtt.qml").read_bytes()
    changed[SERVICES_RESOURCE] = (ROOT / "ui/InternetServices.qml").read_bytes()
    reset = "/Modules/Reset/ResetPage.qml"
    if original[reset]["payload"].decode().count("global.doReset()") != 1:
        raise ValueError("Stock Reset action changed; review before replacing it")
    changed[reset] = (ROOT / "ui/Reset.qml").read_bytes()

    path = "/Main/MainPage.qml"
    text = original[path]["payload"].decode()
    text = remove_object(text, "NotificationBar", 'qsTr("Firmware update available")')
    text = remove_object(text, "ListElement", 'name: "appView"')
    text = replace_once(text, 'if(-1 !== global.currentMainPageIndex)',
                        'if(global.currentMainPageIndex >= 0 && global.currentMainPageIndex < model.count)')
    text = replace_once(text, '    signal goToSettingPage()',
                        '    signal goToSettingPage()\n' + SERVICES_STARTUP)
    text = realtime_reports(text)
    changed[path] = text.encode()

    path = "/Main/Views/Summary/SummaryView.qml"
    text = original[path]["payload"].decode()
    for marker in ["id: weatherBackgroud", "id: aqiCard"]:
        text = remove_object(text, "Item", marker)
    text = replace_once(text, 'Item {\n    ColumnLayout {',
                        'Item {\n' + SUMMARY_SERVICES_STATE + '\n    ColumnLayout {')
    text = replace_once(text, '        QSpacer {}', '        QSpacer {}\n' + SUMMARY_SERVICES_CARD)
    changed[path] = text.encode()

    path = "/Modules/DateTime/DateTimePage.qml"
    text = original[path]["payload"].decode()
    text = remove_object(text, "QSwitchItem", 'qsTr("Set Automatically")')
    text = remove_object(text, "QOptionItem", 'qsTr("Location")')
    text = remove_object(text, "Component", 'id: locationPage')
    text = remove_object(text, "Component", 'id: timeZoneSelectorPage')
    text = replace_once(text, 'onClicked: control.mainStackView.push(timeZoneSelectorPage)',
                        'onClicked: control.mainStackView.push(servicesPage)')
    text = replace_once(text, '    // 时区选择界面', '''    Component {
        id: servicesPage
        LocationPage { onExited: control.mainStackView.pop() }
    }
    // 时区选择界面''')
    text = replace_once(text, 'title: qsTr("Time and Location")',
                        'title: qsTr("Time")\n    Component.onCompleted: datetimeManager.isAutomatically = false')
    changed[path] = text.encode()

    path = "/Modules/Developer/DeveloperPage.qml"
    text = original[path]["payload"].decode()
    for marker in ['qsTr("Upload All Logs")', 'qsTr("Refresh MQTT Info")']:
        text = remove_object(text, "QButton", marker)
    changed[path] = text.encode()

    # Preserve the keyboard but remove its hidden command/easter-egg trigger:
    # passwords beginning with '#' must be treated only as password input.
    path = "/Qing/Keyboards/Keyboard.qml"
    text = original[path]["payload"].decode()
    text = remove_object(text, "NormalKey", 'developerController.startEggs(control.text)')
    text = replace_once(text, 'NormalKey { text: "@"; }',
                        'NormalKey { text: "@"; }\n                            NormalKey { text: "#"; }')
    changed[path] = text.encode()

    changed.update(about_rows(original))
    changed.update(modern_screen_saver(original))
    changed.update(brightness_controls(original))
    changed.update(wifi_connection_ui(original))
    return changed


def brightness_controls(original: dict[str, dict]) -> dict[str, bytes]:
    """Share the manual, automatic and minimum controls across both pages."""
    changed = {}
    source = (ROOT / "ui/BrightnessControls.qml").read_text(encoding="utf-8")
    for path in ("/Modules/Screen/ScreenPage.qml", "/Main/Views/Setting/LightAdjustPage.qml"):
        text = original[path]["payload"].decode()
        # Fail closed if the stock controls or their order change.
        for anchor in ("            QSwitchItem {", "            QBlankItem {",
                       "id: autoBrightnessSwitch", "id: adjustBrightnessSlider", "from: 20", "to: 1000"):
            if text.count(anchor) != 1:
                raise ValueError(f"expected one QML anchor: {anchor!r}")
        start = text.index("            QSwitchItem {")
        switch_end = closing_brace(text, text.index("{", start))
        slider_start = text.index("            QBlankItem {")
        if switch_end > slider_start or text[switch_end:slider_start].strip():
            raise ValueError("brightness controls are not adjacent")
        end = closing_brace(text, text.index("{", slider_start))
        text = text[:start] + '''            Loader {
                objectName: "brightnessControlsLoader"
                width: Math.min(660, parent.width)
                anchors.horizontalCenter: parent.horizontalCenter
                height: item ? item.implicitHeight : 0
                sourceComponent: cgs2BrightnessControls
            }''' + text[end:]
        text, component = inline_component(text, source, "cgs2BrightnessControls")
        text = replace_once(text, "    initialItem:", component + "    initialItem:")
        changed[path] = text.encode()
    return changed


def brightness_floor_code() -> bytes:
    """ARM64 glue using the app's existing QSettings object and Qt functions.

    The saved screen/minimum_brightness key defaults to 1 and is clamped to
    1..100. It lives in setting.ini, which stock recovery already erases.
    No new native library, service, file parser or build dependency is added.
    Stack objects follow the audited Qt 5 ABI: QString=8, QVariant=16 bytes.
    """
    words = [
        0xA9BF7BE1,  # 00 stp x1,x30,[sp,#-16]! (curve's sensor and return)
        0x9400000D,  # 04 bl get_floor (+38)
        0x2A0003E5,  # 08 mov w5,w0
        0xA8C17BE1,  # 0c ldp x1,x30,[sp],#16
        0x71000C3F,  # 10 cmp w1,#3 (displaced curve instruction)
        0,           # 14 b original curve + 4
        0x2A0103F3,  # 18 mov w19,w1 (displaced setBrightness instruction)
        0x340000D3,  # 1c cbz w19,+34 (screen-off)
        0x7101927F,  # 20 cmp w19,#100
        0x5400008A,  # 24 b.ge +34 (already above any allowed minimum)
        0x94000004,  # 28 bl get_floor
        0x6B00027F,  # 2c cmp w19,w0
        0x1A80A273,  # 30 csel w19,w19,w0,ge
        0,           # 34 b original setBrightness + 28
        0xA9BB7BFD,  # 38 stp x29,x30,[sp,#-80]!
        0x910003FD,  # 3c mov x29,sp
        0xF9000BF3,  # 40 str x19,[sp,#16]
        0x910063E0,  # 44 add x0,sp,#24 (QString key)
        0x100003C1,  # 48 adr x1,key (+c0)
        0,           # 4c bl QString(const char *)
        0x910083E0,  # 50 add x0,sp,#32 (QVariant default)
        0x52800001 | (MIN_BRIGHTNESS << 5),  # 54 mov w1,#1
        0,           # 58 bl QVariant(int)
        0x58000420,  # 5c ldr x0,g_setting address (+e0)
        0x910063E1,  # 60 add x1,sp,#24
        0x910083E2,  # 64 add x2,sp,#32
        0x9100C3E8,  # 68 add x8,sp,#48 (QVariant result)
        0,           # 6c bl QSettings::value
        0x9100C3E0,  # 70 add x0,sp,#48
        0xD2800001,  # 74 mov x1,#0
        0,           # 78 bl QVariant::toInt
        0x2A0003F3,  # 7c mov w19,w0
        0x52800021,  # 80 mov w1,#1
        0x6B01027F,  # 84 cmp w19,w1
        0x1A81A273,  # 88 csel w19,w19,w1,ge
        0x52800C81,  # 8c mov w1,#100
        0x6B01027F,  # 90 cmp w19,w1
        0x1A81D273,  # 94 csel w19,w19,w1,le
        0x9100C3E0,  # 98 add x0,sp,#48
        0,           # 9c bl QVariant destructor (result)
        0x910083E0,  # a0 add x0,sp,#32
        0,           # a4 bl QVariant destructor (default)
        0x910063E0,  # a8 add x0,sp,#24
        0,           # ac bl QString destructor
        0x2A1303E0,  # b0 mov w0,w19
        0xF9400BF3,  # b4 ldr x19,[sp,#16]
        0xA8C57BFD,  # b8 ldp x29,x30,[sp],#80
        0xD65F03C0,  # bc ret
    ]
    for offset, target in {0x14: 0x5A0DC0, 0x34: 0x5A1648, 0x4C: 0x52E340,
                           0x58: 0x4174C0, 0x6C: 0x416A50, 0x78: 0x416790,
                           0x9C: 0x416A60, 0xA4: 0x416A60, 0xAC: 0x518920}.items():
        opcode = 0x14000000 if offset in (0x14, 0x34) else 0x94000000
        words[offset // 4] = opcode | (((target - BRIGHTNESS_CODE_ADDRESS - offset) // 4) & 0x3FFFFFF)
    return (struct.pack("<48I", *words) + b"screen/minimum_brightness\0".ljust(32, b"\0")
            + struct.pack("<Q", 0x8995B0))


def patch_brightness(output: bytearray) -> list[dict]:
    patches = []
    code = brightness_floor_code()
    if len(output) < BRIGHTNESS_CODE_OFFSET + len(code) or any(output[BRIGHTNESS_CODE_OFFSET:BRIGHTNESS_CODE_OFFSET + len(code)]):
        raise ValueError("brightness code needs empty reserved resource padding")
    for address, expected, replacement, purpose in BRIGHTNESS_PATCHES:
        if struct.unpack_from("<I", output, address - 0x400000)[0] != expected:
            raise ValueError(f"brightness instruction mismatch at {address:#x}")
    for address, expected, replacement, purpose in BRIGHTNESS_PATCHES:
        offset = address - 0x400000
        struct.pack_into("<I", output, offset, replacement)
        patches.append({"address": hex(address), "file_offset": hex(offset),
                        "original": f"{expected:08x}", "replacement": f"{replacement:08x}",
                        "method": ("ScreenAPI::refreshLight" if address == 0x5A1590 else
                                   "ScreenAPI::setBrightness" if address == 0x5A1644 else
                                   "ScreenAPI::getBrightness(int)"), "purpose": purpose})
    output[BRIGHTNESS_CODE_OFFSET:BRIGHTNESS_CODE_OFFSET + len(code)] = code
    patches.append({"address": hex(BRIGHTNESS_CODE_ADDRESS), "file_offset": hex(BRIGHTNESS_CODE_OFFSET),
                    "size": len(code), "sha256": hashlib.sha256(code).hexdigest(),
                    "purpose": "Read and bound the minimum from the app's existing settings"})
    return patches


def wifi_connection_ui(original: dict[str, dict]) -> dict[str, bytes]:
    """Show association rather than the stock, firewall-blocked ping verdict.

    Stock UnusableState is an associated network with an IP whose external
    ping check failed. Internet access is assessed by each approved service;
    the Wi-Fi UI must not claim that a failed stock probe means no connection.
    No probe or network permission is added by these resource-only changes.
    """
    changed = {}
    path = "/Main/Header/WiFiIndicator.qml"
    text = original[path]["payload"].decode()
    text = replace_once(text, "if (WiFiState.ConnectedState === wifiManager.state) {",
                        "if (WiFiState.ConnectedState === wifiManager.state || WiFiState.UnusableState === wifiManager.state) {")
    text = replace_once(text, '''                } else if (WiFiState.UnusableState === wifiManager.state) {
                    return "qrc:/main/wifi/wifi_unusable.png";''', "")
    changed[path] = text.encode()

    path = "/Modules/WiFi/WiFiItem.qml"
    text = original[path]["payload"].decode()
    text = replace_once(text, 'control.state === WiFiState.UnusableState ? "qrc:/modules/wifi/state_unusable.png" : ', "")
    changed[path] = text.encode()

    path = "/Modules/WiFi/WiFiListPage.qml"
    text = original[path]["payload"].decode()
    text = replace_once(text, "} else if (wifiManager.state === WiFiState.ConnectedState) {",
                        "} else if (wifiManager.state === WiFiState.ConnectedState || wifiManager.state === WiFiState.UnusableState) {")
    text = replace_once(text, 'qsTr("Connected to internet")', '"Connected to Wi-Fi"')
    text = replace_once(text, '\n                            control.gotoOperateNetwork(wifi.ssid, qsTr("No internet connection"))',
                        '\n                            control.gotoOperateNetwork(wifi.ssid, "Not connected to Wi-Fi")')
    changed[path] = text.encode()
    return changed


def realtime_reports(text: str) -> str:
    """Add the MQTT real-time report timer to the main page."""
    text = replace_once(text, "import Qing.Snow 1.0\n", "import Qing.Snow 1.0\nimport Qt.labs.settings 1.0\n")
    return replace_once(text, "    signal goToSettingPage()\n", "    signal goToSettingPage()\n" + REALTIME_REPORTS)


def about_rows(original: dict[str, dict]) -> dict[str, bytes]:
    """Show the modification version and installation time on Settings -> About."""
    path = "/Modules/About/AboutPage.qml"
    text = original[path]["payload"].decode()
    text = replace_once(text, "import QtQuick.Layouts 1.3\n",
                        "import QtQuick.Layouts 1.3\nimport Qt.labs.settings 1.0\n")
    text = replace_once(text, "    property int clicedIndex: -1\n", "    property int clicedIndex: -1\n" + ABOUT_ROWS)
    text = replace_once(text, "model: global.aboutList", "model: control.cgs2AboutList()")
    return {path: text.encode()}


def modern_screen_saver(original: dict[str, dict]) -> dict[str, bytes]:
    """Add the Modern Clock choice to the stock screen saver loader and settings page."""
    changed = {}
    # The native screen saver setting has no spare value, so the Modern Clock
    # choice is a separate flag that the loader checks first.
    path = "/ScreenSaver/ScreenSaverPage.qml"
    text = original[path]["payload"].decode()
    # The screen saver measures its digits with FontMetrics (QtQuick 2.4+).
    text = replace_once(text, "import QtQuick 2.0\n", "import QtQuick 2.12\n")
    text, saver = inline_component(text, (ROOT / "ui/ModernScreenSaver.qml").read_text(encoding="utf-8"),
                                   "cgs2ModernSaver")
    text = replace_once(text, '''    Component.onCompleted: {
        switch(screenManager.screenSaver) {''', MODERN_CHOICE + saver + '''    Component.onCompleted: {
        if (cgs2ModernChosen()) {
            loader.sourceComponent = cgs2ModernSaver;
            return;
        }
        switch(screenManager.screenSaver) {''')
    changed[path] = text.encode()

    path = "/Modules/Screen/ScreenSaverPage.qml"
    text = original[path]["payload"].decode()
    text, options = inline_component(text, (ROOT / "ui/ModernScreenSaverOptions.qml").read_text(encoding="utf-8"),
                                     "cgs2ModernOptions")
    # The stock list is sized for three choices; scroll the page and keep four visible.
    anchor = "        onExited: control.exited()\n        ColumnLayout {\n"
    if text.count(anchor) != 1:
        raise ValueError(f"expected one QML anchor: {anchor!r}")
    start = text.index(anchor) + anchor.index("        ColumnLayout")
    end = closing_brace(text, text.index("{", start))
    text = (text[:start] + "        QFlickable {\n            anchors.fill: parent\n            contentWidth: width\n"
            "            contentHeight: 680\n" + text[start:end] + "\n        }" + text[end:])
    text = replace_once(text, "ScreenSaverComboBox {\n                Layout.fillWidth: true\n                Layout.fillHeight: true\n",
                        "ScreenSaverComboBox {\n                Layout.fillWidth: true\n                Layout.fillHeight: true\n"
                        "                Layout.minimumHeight: 380\n")
    text = replace_once(text, '''                model: ListModel {
                    ListElement {
                        text: qsTr("Analog Clock Custom")''', '''                model: ListModel {
                    ListElement {
                        text: "Modern Clock"
                        value: -1
                    }
                    ListElement {
                        text: qsTr("Analog Clock Custom")''')
    text = replace_once(text, '''                currentValue: screenManager.screenSaver
                onCurrentValueChanged: screenManager.screenSaver = currentValue
                onEdit: function (value) {
                    screenManager.screenSaver = value''', '''                currentValue: control.cgs2ModernChosen() ? -1 : screenManager.screenSaver
                onCurrentValueChanged: {
                    control.cgs2ChooseModern(currentValue === -1);
                    if (currentValue !== -1) screenManager.screenSaver = currentValue;
                }
                onEdit: function (value) {
                    if (value === -1) {
                        control.mainStackView.push(cgs2ModernOptions);
                        return;
                    }
                    screenManager.screenSaver = value''')
    text = replace_once(text, '''    Component {
        id: analogClockCustom''', MODERN_CHOICE + '''    function cgs2ChooseModern(chosen) {
        if (chosen === cgs2ModernChosen()) return;
        cgs2ModernChoice.setValue("enabled", chosen);
        cgs2ModernChoice.sync();
    }
''' + options + '''    Component {
        id: analogClockCustom''')
    changed[path] = text.encode()
    return changed


def patch_wifi_probe(output: bytearray) -> dict:
    """Bypass external pings without weakening local connection detection.

    Both callers reach verifyNetwork only for associated/IP-ready states 3/4.
    At 0x5b6b20 all callee-saved registers are saved, and no QString reference
    has been acquired. Branch to the existing success block: clear failures,
    stop the verification timer and return via the unchanged balanced epilogue.
    This also makes the failure-triggered wlan0 down/up cycle unreachable.
    """
    guards = {
        0x5B6AEC: 0xA9B77BFD,  # frame allocation
        0x5B6AF8: 0xAA0003F3,  # x19 = this
        0x5B6B1C: 0xF9002BFB,  # final callee-saved register
        0x5B6B20: 0xF90037E0,  # patch site, before QString::ref
        0x5B6BA0: 0xF9402260,  # load verification timer from this
        0x5B6BA4: 0xB9009E7F,  # reset failure count
        0x5B6BA8: 0x52800039,  # result = true
        0x5B6BAC: 0x97F97F55,  # QTimer::stop
        0x5B6BB0: 0x2A1903E0,  # return result
        0x5B6BC8: 0xA8C97BFD,  # balanced frame release
        0x5B6BCC: 0xD65F03C0,  # return
        0x5B6F80: 0x71000C1F,  # refreshConnectionInfo: state == 3
        0x5B6F84: 0x540000E1,  # otherwise skip verification
        0x5B6F8C: 0x97FFFED8,  # first caller
        0x5B7A60: 0x51000C00,  # startVerifyNetwork: state - 3
        0x5B7A64: 0x7100041F,  # <= 1 (state 3 or 4)
        0x5B7A68: 0x540007E8,  # otherwise skip verification
        0x5B7A74: 0x97FFFC1E,  # second caller
    }
    for address, expected in guards.items():
        if struct.unpack_from("<I", output, address - 0x400000)[0] != expected:
            raise ValueError(f"Wi-Fi probe instruction mismatch at {address:#x}")
    address, target = 0x5B6B20, 0x5B6BA0
    replacement = 0x14000000 | ((target - address) // 4)
    struct.pack_into("<I", output, address - 0x400000, replacement)
    return {"address": hex(address), "file_offset": hex(address - 0x400000),
            "original": "f90037e0", "replacement": f"{replacement:08x}",
            "target": hex(target), "method": "WiFiRunner::verifyNetwork",
            "purpose": "Use local Wi-Fi association/IP state; skip external pings and forced interface cycling"}


def patch(binary: bytes) -> tuple[bytes, dict]:
    digest = hashlib.sha256(binary).hexdigest()
    if digest != STOCK_SHA256:
        raise ValueError(f"unsupported app SHA256 {digest}; expected {STOCK_SHA256}")
    original = resources(binary)
    changes = transform(original)
    output = bytearray(binary)
    code_patches = []
    for address, expected_instruction, callee, purpose in CREDENTIAL_LOG_CALLS:
        offset = address - 0x400000
        instruction = struct.unpack_from("<I", binary, offset)[0]
        if instruction != expected_instruction or instruction >> 26 != 0b100101:
            raise ValueError(f"credential log instruction mismatch at {address:#x}")
        displacement = instruction & 0x3FFFFFF
        if displacement & 0x2000000:
            displacement -= 0x4000000
        if address + 4 * displacement != callee:
            raise ValueError(f"credential log call target mismatch at {address:#x}")
        struct.pack_into("<I", output, offset, 0xD503201F)
        code_patches.append({"address": hex(address), "file_offset": hex(offset),
                             "original": f"{instruction:08x}", "replacement": "d503201f",
                             "purpose": purpose})
    cloud_patches = []
    for address, expected_instruction, method, purpose in CLOUD_ENTRY_PATCHES:
        offset = address - 0x400000
        instruction = struct.unpack_from("<I", binary, offset)[0]
        if instruction != expected_instruction:
            raise ValueError(f"cloud method entry mismatch at {address:#x}")
        struct.pack_into("<I", output, offset, 0xD65F03C0)
        cloud_patches.append({"address": hex(address), "file_offset": hex(offset),
                              "original": f"{instruction:08x}", "replacement": "d65f03c0",
                              "method": method, "purpose": purpose})
    network_start_patches = []
    for address, expected_instruction, callee, purpose in NETWORK_START_CALLS:
        offset = address - 0x400000
        instruction = struct.unpack_from("<I", binary, offset)[0]
        displacement = instruction & 0x3FFFFFF
        if displacement & 0x2000000:
            displacement -= 0x4000000
        if (instruction != expected_instruction or instruction >> 26 != 0b100101
                or address + 4 * displacement != callee):
            raise ValueError(f"network startup call mismatch at {address:#x}")
        struct.pack_into("<I", output, offset, 0xD503201F)
        network_start_patches.append({"address": hex(address), "file_offset": hex(offset),
                                      "original": f"{instruction:08x}", "replacement": "d503201f",
                                      "purpose": purpose})
    wifi_probe_patch = patch_wifi_probe(output)
    packed = bytearray()
    expected = {}
    resource_timestamps = []
    for path, resource in original.items():
        payload = changes.get(path, resource["payload"])
        expected[path] = payload
        if payload != resource["payload"]:
            timestamp = cache_timestamp(resource["last_modified"], payload)
            struct.pack_into(">Q", output, resource["node"] + 14, timestamp)
            resource_timestamps.append({"path": path,
                                        "original": resource["last_modified"],
                                        "replacement": timestamp})
        compressed = struct.pack(">I", len(payload)) + zlib.compress(payload, 9)
        flags = resource["flags"] & ~1
        stored = payload
        if len(compressed) < len(payload):
            flags |= 1
            stored = compressed
        struct.pack_into(">H", output, resource["node"] + 4, flags)
        struct.pack_into(">I", output, resource["node"] + 10, len(packed))
        packed += struct.pack(">I", len(stored)) + stored
    resource_capacity = BRIGHTNESS_CODE_OFFSET - DATA_OFFSET
    if len(packed) > resource_capacity:
        raise ValueError(f"replacement resources exceed {resource_capacity} bytes")
    output[DATA_OFFSET:DATA_OFFSET + DATA_SIZE] = packed + bytes(DATA_SIZE - len(packed))
    brightness_patches = patch_brightness(output)
    verified = resources(output)
    if set(verified) != set(expected) or any(verified[p]["payload"] != expected[p] for p in expected):
        raise ValueError("resource round-trip failed")
    for path, resource in original.items():
        timestamp = (cache_timestamp(resource["last_modified"], expected[path])
                     if expected[path] != resource["payload"] else resource["last_modified"])
        if verified[path]["last_modified"] != timestamp:
            raise ValueError(f"resource timestamp round-trip failed: {path}")
    assert len(output) == len(binary)
    report = {"original_sha256": digest, "patched_sha256": hashlib.sha256(output).hexdigest(),
              "resource_bytes": len(packed), "resource_capacity": resource_capacity,
              "resources_verified": len(verified), "changed_resources": sorted(changes),
              "resource_timestamps": resource_timestamps,
              "credential_log_patches": code_patches, "cloud_entry_patches": cloud_patches,
              "network_start_patches": network_start_patches,
              "brightness_patches": brightness_patches,
              "wifi_probe_patch": wifi_probe_patch}
    return bytes(output), report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("input and output must differ; preserve the original backup")
    output, report = patch(args.input.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output)
    args.output.chmod(0o755)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
