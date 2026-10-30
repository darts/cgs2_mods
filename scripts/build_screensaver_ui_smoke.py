#!/usr/bin/env python3
"""Build an isolated Qt smoke test of the changed stock pages from the audited stock app.

The generated test renders the patched screen saver loader, the patched Screen
Saver settings page and the patched About page with the stock controls, mocked
device objects and fixture files. It writes its settings and screenshots only inside its runtime
directory. Extracted proprietary resources must remain private.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

from patch_app import MOD_VERSION, RELEASE_RECORD, STOCK_SHA256, resources, transform

SETTINGS = "/data/etc/cgs2-screensaver.ini"
SNAPSHOT = {
    "updated_at": "2026-10-21T12:00:00Z", "source": "Open-Meteo", "temperature_unit": "°C",
    "current": {"temperature": 13.6, "weather_code": 2, "humidity": 71, "is_day": True, "wind_speed": 9},
    "daily": [{"date": "2026-10-21", "weather_code": 2, "minimum_temperature": 7.8,
               "maximum_temperature": 15.2, "precipitation_probability": 10}],
}
SNOW = {
    # Mock enumerators only; the real values come from the native module.
    "Screen": [["AnalogClockSaver", "FlipClockSaver", "FlipClockWithReadingsSaver", "AnalogClockCustomSaver"],
               ["NeverInterval", "OneMimuteInterval", "FiveMinutesInterval", "TenMinutesInterval",
                "FifteenMinutesInterval", "ThirtyMinutesInterval"]],
    "Global": [["AIR_NONE", "AIR_PM25", "AIR_PM10", "AIR_TVOC", "AIR_CO2", "AIR_TEMP", "AIR_HUMI", "AIR_NOISE"],
               ["LANG_NONE", "LANG_US", "LANG_RU"], ["STANDARD_NONE", "STANDARD_US", "STANDARD_EU"],
               # The About page compiles the stock Developer page, which uses these in a ListModel.
               ["PROD_STANDARD", "PROD_AIRMX", "PROD_HAIER"], ["PRODUCTTION_SERVER", "STAGING_SERVER"]],
    "WiFiState": [["UnusableState", "ConnectedState"]],
}

MOCKS = r'''
    QtObject {
        id: global; property bool is24HModel: true; property int language: 1; property int standard: 2
        property var aboutList: [
            {key: "Device Name", value: "Qingping Air Monitor"}, {key: "Model", value: "CGS2"},
            {key: "Serial Number", value: "FIXTURE"}, {key: "Version", value: "4.5.6_0181"},
            {key: "Wi-Fi MAC", value: "fixture"}, {key: "PM Sensor", value: "Sensor not installed"},
            {key: "Manufacturer", value: "Qingping Technology (Beijing) Co., Ltd."}
        ]
    }
    QtObject {
        id: datetimeManager
        property QtObject datetimeData: QtObject {
            property string timeStringFormat1: global.is24HModel ? "14:37" : "2:37"
            property string apString: "PM"
            property string dateStringFormat1: "Wednesday, October 22"
        }
    }
    QtObject {
        id: airdataController
        property QtObject airTEMP: QtObject { property string valueString: "22.6"; property string unitString: "°C"; property color evalutionColor: "#3DD68C" }
        property QtObject airHUMI: QtObject { property string valueString: "48"; property string unitString: "%"; property color evalutionColor: "#3DD68C" }
        property QtObject airCO2: QtObject { property string valueString: "1240"; property string unitString: "ppm"; property color evalutionColor: "#FFB020" }
        property QtObject airPM25: QtObject { property string valueString: "7"; property string unitString: "μg/m³"; property color evalutionColor: "#3DD68C" }
        property QtObject airPM10: QtObject { property string valueString: "11"; property string unitString: "μg/m³"; property color evalutionColor: "#3DD68C" }
        property QtObject airTVOC: QtObject { property string valueString: "0.21"; property string unitString: "" }
        property QtObject airNoise: QtObject { property string valueString: "41"; property string unitString: "dB"; property color evalutionColor: "#3DD68C" }
    }
    function check(condition, label) {
        if (!condition) { console.error("CGS2_SAVER_SMOKE_FAIL " + label); Qt.exit(1); throw new Error(label); }
        assertions++;
    }
    function find(item, name) {
        if (!item) return null;
        if (item.objectName === name) return item;
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) { var result = find(children[i], name); if (result) return result; }
        return null;
    }
    function box(item) { var p = item.mapToItem(window.contentItem, 0, 0); return {x: p.x, y: p.y, right: p.x + item.width, bottom: p.y + item.height}; }
    function fit(item, label) {
        var b = box(item);
        check(b.x >= 0 && b.y >= 0 && b.right <= 720.5 && b.bottom <= 720.5, label + " fits the 720 px screen");
    }
    function screenshot(name) { surface.grabToImage(function(result) { check(result.saveToFile(@@RUNTIME@@ + "/" + name + ".png"), "screenshot " + name); }); }
    function flag(key) { var value = store.value(key, null); return value === true || value === "true"; }
    Settings { id: store; fileName: @@SETTINGS@@; category: "modern" }
'''

SAVER_HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
import Qt.labs.settings 1.0
import "saver-on" as WeatherOn
import "saver-off" as WeatherOff
Window {
    id: window
    visible: true; width: 720; height: 720
    property int assertions: 0
    property bool weatherService: true
    QtObject { id: screenManager; property int screenSaver: 1 }
''' + MOCKS + r'''
    Rectangle {
        id: surface; anchors.fill: parent; color: "black"
        Loader { id: host; anchors.fill: parent; active: false
            sourceComponent: window.weatherService ? on : off }
        Component { id: on; WeatherOn.ScreenSaverPage {} }
        Component { id: off; WeatherOff.ScreenSaverPage {} }
    }
    function set(values) {
        for (var key in values) store.setValue(key, values[key]);
        store.sync();
    }
    function shown() {
        var keys = ["temperature", "humidity", "co2", "pm25", "pm10", "tvoc", "noise"], result = [];
        for (var i = 0; i < keys.length; i++) if (find(host.item, "modernReading_" + keys[i])) result.push(keys[i]);
        return result.join(",");
    }
    function layoutChecks(label) {
        var middle = (box(find(host.item, "modernClockArea")).y + box(find(host.item, find(host.item, "modernReadings").visible ? "modernReadings" : "modernDate")).bottom) / 2;
        check(Math.abs(middle - 360) <= 24, label + ": content is centred vertically (" + Math.round(middle) + ")");
        var saver = find(host.item, "modernScreenSaver"), clock = find(saver, "modernClock"), area = find(saver, "modernClockArea");
        var date = find(saver, "modernDate"), weather = find(saver, "modernWeather"), grid = find(saver, "modernReadings");
        check(saver !== null && clock !== null && date !== null, label + ": modern screen saver loaded");
        // The clock area bounds the digits; the Text box adds line spacing above them.
        fit(area, label + " clock digits"); fit(date, label + " date");
        check(area.height >= clock.font.pixelSize * 0.6, label + ": clock area covers the digit height");
        check(Math.abs(box(clock).y + clock.baselineOffset - box(area).bottom) <= 1, label + ": digits sit on the area bottom");
        check(box(area).bottom <= box(date).y + 1, label + ": date below clock digits");
        var last = box(date).bottom;
        if (weather.visible) { fit(weather, label + " weather"); check(box(weather).y >= last - 1, label + ": weather below date"); last = box(weather).bottom; }
        if (grid.visible) {
            check(box(grid).y >= last - 1, label + ": readings below the text");
            for (var r = 0; r < grid.children.length; r++) {
                var row = grid.children[r];
                if (!row.visible || row.width === 0) continue;
                check(Math.abs((box(row).x + box(row).right) / 2 - 360) <= 8, label + ": reading row is centred");
            }
            var tiles = [];
            (function collect(item) {
                if (item.objectName && item.objectName.indexOf("modernReading_") === 0) { tiles.push(item); return; }
                for (var c = 0; c < item.children.length; c++) collect(item.children[c]);
            })(grid);
            check(tiles.length === shown().split(",").filter(function (key) { return key; }).length, label + ": every reading has a tile");
            for (var i = 0; i < tiles.length; i++) {
                var tile = tiles[i];
                fit(tile, label + " " + tile.objectName);
                var value = find(tile, "modernValue"), unit = find(tile, "modernUnit");
                check(value.contentWidth <= value.width + 0.5, label + ": " + tile.objectName + " value fits");
                check(unit.x + unit.implicitWidth <= tile.width - 8, label + ": " + tile.objectName + " unit fits");
                check(value.fontInfo.pixelSize >= value.font.pixelSize - 4, label + ": " + tile.objectName + " value keeps its size");
            }
        }
    }
    readonly property var all: ["temperature", "humidity", "co2", "pm25", "pm10", "tvoc", "noise"]
    function only(keys, weather) {
        var values = {weather: weather};
        for (var i = 0; i < all.length; i++) values[all[i]] = keys.indexOf(all[i]) >= 0;
        return values;
    }
    property var scenarios: [
        {name: "saver-default", values: {enabled: true}, service: true, h24: true,
         expect: "temperature,humidity,co2,pm25", weather: true},
        {name: "saver-all", values: only(all, true), service: true, h24: true,
         expect: all.join(","), weather: true},
        {name: "saver-five", values: only(["temperature", "humidity", "co2", "pm25", "noise"], true), service: true,
         h24: true, expect: "temperature,humidity,co2,pm25,noise", weather: true},
        {name: "saver-minimal", values: only(["co2"], false), service: true, h24: false, expect: "co2", weather: false},
        {name: "saver-clock-only", values: only([], true), service: false, h24: true, expect: "", weather: false}
    ]
    Timer {
        property int step: 0
        interval: 500; repeat: true; running: true
        onTriggered: {
            var index = Math.floor(step / 3), phase = step % 3;
            if (index < scenarios.length) {
                var scenario = scenarios[index];
                if (phase === 0) {
                    host.active = false;
                    set(scenario.values); weatherService = scenario.service; global.is24HModel = scenario.h24;
                    host.active = true;
                } else if (phase === 1) {
                    check(shown() === scenario.expect, scenario.name + " readings: " + shown());
                    check(find(host.item, "modernWeather").visible === scenario.weather, scenario.name + " weather visibility");
                    check(find(host.item, "modernClock").text === (scenario.h24 ? "14:37" : "2:37"), scenario.name + " clock text");
                    layoutChecks(scenario.name);
                    screenshot(scenario.name);
                }
            } else if (phase === 0) {
                host.active = false; set({enabled: false}); host.active = true;
            } else if (phase === 1) {
                check(find(host.item, "modernScreenSaver") === null, "stock choice does not load Modern Clock");
                check(String(host.item.children[0].source).indexOf("FlipClock/FlipClockPage.qml") > 0, "stock choice loads its own page");
                console.log("CGS2_SAVER_SMOKE_PASS " + assertions);
                Qt.quit();
            }
            step++;
        }
    }
}
'''

SETTINGS_HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
import Qt.labs.settings 1.0
import "qml/Modules/Screen" as Stock
Window {
    id: window
    visible: true; width: 720; height: 720
    property int assertions: 0
    QtObject {
        id: screenManager
        property int screenSaver: 2
        property int screenSaverInterval: 2
        property var screenSaverDisplayReadings: [1, 4, 5, 6]
        property int previews: 0
        function previewScreenSaver() { previews++; }
    }
''' + MOCKS + r'''
    Rectangle {
        id: surface; anchors.fill: parent; color: "black"
        Loader { id: host; anchors.fill: parent; sourceComponent: Component { Stock.ScreenSaverPage {} } }
    }
    function combo(item) {
        if (!item) return null;
        if (item.edit !== undefined && item.currentValue !== undefined && item.model && item.model.count === 4) return item;
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) { var result = combo(children[i]); if (result) return result; }
        return null;
    }
    function current() { return host.item.mainStackView.currentItem; }
    Timer {
        property int stage: 0
        interval: 500; repeat: true; running: true
        onTriggered: {
            var choices = combo(host.item);
            if (stage === 0) {
                check(choices !== null, "screen saver list exists");
                check(choices.model.get(0).text === "Modern Clock" && choices.model.get(0).value === -1, "Modern Clock is the first choice");
                check(choices.currentValue === 2, "stock choice stays selected");
                check(store.value("enabled", null) === null, "opening the page writes no choice");
                check(choices.height >= choices.contentHeight, "all four choices are visible without scrolling the list");
                screenshot("settings-list");
            } else if (stage === 1) {
                choices.currentValue = -1;
                check(flag("enabled"), "selecting Modern Clock saves the choice");
                check(screenManager.screenSaver === 2, "native choice is kept for a later return");
                choices.edit(-1);
            } else if (stage === 2) {
                check(current().objectName === "modernScreenSaverOptions", "edit opens the Modern Clock options");
                var weather = find(current(), "modernOption_weather"), noise = find(current(), "modernOption_noise");
                var co2 = find(current(), "modernOption_co2");
                check(weather.checked && co2.checked && !noise.checked, "switches show the defaults");
                fit(weather, "weather switch");
                screenshot("settings-options");
            } else if (stage === 3) {
                var noise1 = find(current(), "modernOption_noise"), weather1 = find(current(), "modernOption_weather");
                noise1.checked = true; weather1.checked = false;
                check(flag("noise") && !flag("weather"), "switches save immediately");
                current().exited();
            } else if (stage === 4) {
                check(choices !== null && choices.currentValue === -1, "back to the list with Modern Clock selected");
                screenshot("settings-list-selected");
            } else if (stage === 5) {
                choices.currentValue = 1;
                check(!flag("enabled") && screenManager.screenSaver === 1, "a stock choice clears Modern Clock");
                choices.currentValue = -1;
                host.active = false; host.active = true;
            } else if (stage === 6) {
                check(choices.currentValue === -1, "a saved Modern Clock choice shows as selected");
                choices.edit(-1);
            } else if (stage === 7) {
                var noise2 = find(current(), "modernOption_noise"), weather2 = find(current(), "modernOption_weather");
                check(noise2.checked && !weather2.checked, "switches show the saved options");
                console.log("CGS2_SAVER_SETTINGS_SMOKE_PASS " + assertions);
                Qt.quit();
            }
            stage++;
        }
    }
}
'''

ABOUT_HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
import Qt.labs.settings 1.0
import "qml/Modules/About" as Stock
Window {
    id: window
    visible: true; width: 720; height: 720
    property int assertions: 0
''' + MOCKS + r'''
    Settings { id: record; fileName: @@RECORD@@; category: "release" }
    Rectangle {
        id: surface; anchors.fill: parent; color: "black"
        Loader { id: host; anchors.fill: parent; sourceComponent: Component { Stock.AboutPage {} } }
    }
    function listView(item) {
        if (!item) return null;
        if (String(item).indexOf("QQuickListView") === 0) return item;
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) { var result = listView(children[i]); if (result) return result; }
        return null;
    }
    function texts(item, result) {
        if (item.text !== undefined && item.paintedWidth !== undefined && item.visible) result.push(item);
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) texts(children[i], result);
        return result;
    }
    function row(list, index, key, value) {
        var shown = texts(list.itemAtIndex(index), []);
        check(shown.length === 2 && shown[0].text === key && shown[1].text === value, "row " + (index + 1) + " shows " + key);
        check(shown[0].paintedWidth <= 200 && shown[1].lineCount === 1, key + " fits its row");
        fit(list.itemAtIndex(index), key + " row");
    }
    Timer {
        property int stage: 0
        interval: 500; repeat: true; running: true
        onTriggered: {
            var list = listView(host.item), stock = global.aboutList.length;
            if (stage === 0) {
                check(record.value("installed", null) === null, "the run starts without release.ini");
                check(list !== null && list.count === stock + 1, "without a record, only the version row is added");
                list.positionViewAtEnd();
            } else if (stage === 1) {
                row(list, stock, "Mod Version", @@VERSION@@);
                record.setValue("installed", "2026-10-23 14:05");
                record.sync();
                host.active = false; host.active = true;
            } else if (stage === 2) {
                check(list.count === stock + 2, "with a record, the installation row is added");
                for (var i = 0; i < stock; i++)
                    check(host.item.cgs2AboutList()[i].key === global.aboutList[i].key, "stock row " + (i + 1) + " keeps its place");
                list.positionViewAtEnd();
            } else if (stage === 3) {
                row(list, stock, "Mod Version", @@VERSION@@);
                row(list, stock + 1, "Mod Installed", "2026-10-23 14:05");
                screenshot("about");
            } else if (stage === 4) {
                console.log("CGS2_ABOUT_SMOKE_PASS " + assertions);
                Qt.quit();
            }
            stage++;
        }
    }
}
'''


def build(stock: Path, output: Path, runtime: Path) -> None:
    binary = stock.read_bytes()
    if hashlib.sha256(binary).hexdigest() != STOCK_SHA256:
        raise ValueError("stock application does not match audited SHA256")
    output.mkdir(parents=True, exist_ok=True)
    qml = resources(binary)
    changes = transform(qml)
    qml_url, asset_url = (runtime / "qml").as_uri() + "/", (runtime / "assets").as_uri() + "/"
    settings, record = str(runtime / "screensaver.ini"), str(runtime / "release.ini")

    def localized(text: str) -> str:
        text = re.sub(r'"qrc:/([^"]+\.qml)"', lambda m: '"' + qml_url + m[1] + '"', text)
        return text.replace('"qrc:/', '"' + asset_url).replace(SETTINGS, settings).replace(RELEASE_RECORD, record)

    for name, entry in qml.items():
        path = output / "qml" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(localized(changes.get(name, entry["payload"]).decode()))
    images = resources(binary, tree_offset=0x26F8DB, tree_size=7018,
                       name_offset=0x271445, data_offset=0x272E09, data_size=1203381)
    for name, entry in images.items():
        path = output / "assets" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["payload"])
    snow = output / "qml/Qing/Snow"
    snow.mkdir(parents=True, exist_ok=True)
    (snow / "qmldir").write_text("module Qing.Snow\n" + "".join(
        f"singleton {name} 1.0 {name}.qml\n" for name in SNOW))
    for name, groups in SNOW.items():
        enums = "".join(f"    enum Group{index} {{ {', '.join(group)} }}\n" for index, group in enumerate(groups))
        (snow / f"{name}.qml").write_text("pragma Singleton\nimport QtQuick 2.0\nQtObject {\n" + enums + "}\n")
    # Two copies of the loader read different services files, with and without weather.
    loader = (output / "qml/ScreenSaver/ScreenSaverPage.qml").read_text()
    for variant, enabled in (("saver-on", True), ("saver-off", False)):
        folder = output / variant
        for name in qml:
            if name.startswith("/ScreenSaver/"):
                target = folder / name[len("/ScreenSaver/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((output / "qml" / name.lstrip("/")).read_text())
        text = loader
        for original, fixture in (("file:///etc/cgs2-privacy/services.json", f"{variant}-config.json"),
                                  ("file:///run/cgs2-services/status.json", "status.json")):
            if text.count(original) != 1:
                raise ValueError(f"missing unique smoke isolation anchor: {original}")
            text = text.replace(original, (runtime / fixture).as_uri())
        (folder / "ScreenSaverPage.qml").write_text(text)
        (output / f"{variant}-config.json").write_text(json.dumps({"version": 1, "weather_enabled": enabled}))
    (output / "status.json").write_text(json.dumps({"weather": {"snapshot": SNAPSHOT}}, ensure_ascii=False))
    for name, template in (("SaverMain.qml", SAVER_HARNESS), ("SettingsMain.qml", SETTINGS_HARNESS),
                           ("AboutMain.qml", ABOUT_HARNESS)):
        text = template.replace("@@RUNTIME@@", json.dumps(str(runtime))).replace("@@SETTINGS@@", json.dumps(settings))
        text = text.replace("@@RECORD@@", json.dumps(record)).replace("@@VERSION@@", json.dumps(MOD_VERSION))
        (output / name).write_text(text)
    print(f"Prepared isolated Qt smoke test: {output}")
    print(f"Runtime directory: {runtime}")
    print("Remove screensaver.ini and release.ini, then run SaverMain.qml, SettingsMain.qml and AboutMain.qml, "
          "each with QML_XHR_ALLOW_FILE_READ=1, QML2_IMPORT_PATH=<runtime>/qml, QT_QUICK_BACKEND=software and "
          "qmlscene -platform offscreen.")
    print("Require CGS2_SAVER_SMOKE_PASS, CGS2_SAVER_SETTINGS_SMOKE_PASS and CGS2_ABOUT_SMOKE_PASS and no "
          "CGS2_SAVER_SMOKE_FAIL; qmlscene can exit 0 after a failed assertion.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_app", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--runtime-path", type=Path,
                        help="absolute runtime directory if different from the build output")
    args = parser.parse_args()
    output = args.output.resolve()
    runtime = args.runtime_path or output
    if not runtime.is_absolute():
        parser.error("--runtime-path must be absolute")
    build(args.stock_app, output, runtime)


if __name__ == "__main__":
    main()
