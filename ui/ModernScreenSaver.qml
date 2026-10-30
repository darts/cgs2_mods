import QtQuick 2.12
import QtQuick.Layouts 1.3
import Qt.labs.settings 1.0

// Modern Clock screen saver. scripts/patch_app.py inlines this file into the
// stock screen saver loader, so airdataController, datetimeManager and global
// are the application's own context objects. It only reads them.
Rectangle {
    id: saver
    objectName: "modernScreenSaver"
    color: "#000000"

    // ModernScreenSaverOptions.qml writes these keys and repeats the defaults.
    // Read them with value() only: declared properties would write back.
    Settings {
        id: saverStore
        fileName: "/data/etc/cgs2-screensaver.ini"
        category: "modern"
    }
    readonly property var readings: [
        {key: "temperature", source: "airTEMP", label: "Temperature", short: "Temp", unit: "", on: true},
        {key: "humidity", source: "airHUMI", label: "Humidity", unit: "%", on: true},
        {key: "co2", source: "airCO2", label: "CO2", unit: "ppm", on: true},
        {key: "pm25", source: "airPM25", label: "PM2.5", unit: "µg/m³", on: true},
        {key: "pm10", source: "airPM10", label: "PM10", unit: "µg/m³", on: false},
        {key: "tvoc", source: "airTVOC", label: "eTVOC", unit: "", on: false},
        {key: "noise", source: "airNoise", label: "Noise", unit: "dB", on: false}
    ]
    property var shown: []
    property bool showWeather: false
    // One row up to three readings, then two rows; each row is centred.
    readonly property int columns: shown.length <= 3 ? Math.max(shown.length, 1)
        : (shown.length === 4 ? 2 : (shown.length <= 6 ? 3 : 4))
    readonly property var rows: {
        var result = [];
        for (var i = 0; i < shown.length; i += columns) result.push(shown.slice(i, i + columns));
        return result;
    }

    property bool weatherEnabled: false
    property var weatherSnapshot: null
    readonly property bool weatherShown: showWeather && weatherEnabled && weatherSnapshot !== null
        && !!weatherSnapshot.current

    function flag(key, fallback) {
        var value = saverStore.value(key, fallback);
        return value === true || value === "true";
    }
    function reading(source) {
        return airdataController ? airdataController[source] : null;
    }
    function orDefault(value, fallback) {
        return value === undefined || value === null || value === "" ? fallback : String(value);
    }
    function degrees(value) {
        return typeof value === "number" && isFinite(value) ? Math.round(value) + "°" : "--";
    }
    function condition(code) {
        if (code === 0) return "Clear";
        if (code === 1) return "Mainly clear";
        if (code === 2) return "Partly cloudy";
        if (code === 3) return "Overcast";
        if (code === 45 || code === 48) return "Fog";
        if (code >= 51 && code <= 57) return "Drizzle";
        if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "Rain";
        if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "Snow";
        if (code === 95 || code === 96 || code === 99) return "Thunderstorm";
        return "";
    }
    function range() {
        var today = (weatherSnapshot.daily || [])[0];
        return today ? "H " + degrees(today.maximum_temperature) + "   L " + degrees(today.minimum_temperature) : "";
    }
    // Same local files as the summary card: no network access from the display.
    function refreshWeather() {
        if (!showWeather) { weatherSnapshot = null; return; }
        var config = new XMLHttpRequest();
        config.open("GET", "file:///etc/cgs2-privacy/services.json");
        config.onreadystatechange = function () {
            if (config.readyState !== XMLHttpRequest.DONE) return;
            try {
                var settings = JSON.parse(config.responseText);
                weatherEnabled = settings.version === 1 && settings.weather_enabled === true;
            } catch (e) { weatherEnabled = false; }
            if (!weatherEnabled) { weatherSnapshot = null; return; }
            var status = new XMLHttpRequest();
            status.open("GET", "file:///run/cgs2-services/status.json");
            status.onreadystatechange = function () {
                if (status.readyState !== XMLHttpRequest.DONE) return;
                try { weatherSnapshot = (JSON.parse(status.responseText).weather || {}).snapshot || null; }
                catch (e) { weatherSnapshot = null; }
            };
            status.send();
        };
        config.send();
    }
    // The loader creates a new screen saver each time, so read options once.
    Component.onCompleted: {
        shown = readings.filter(function (reading) { return flag(reading.key, reading.on); });
        showWeather = flag("weather", true);
        refreshWeather();
    }
    Timer {
        interval: 300000; repeat: true; running: saver.visible && saver.showWeather
        onTriggered: saver.refreshWeather()
    }

    // Move the content a few pixels each minute so static parts do not stay
    // on the same pixels for hours.
    property int drift: 0
    Timer {
        interval: 60000; repeat: true; running: saver.visible
        onTriggered: saver.drift = (saver.drift + 1) % 9
    }

    ColumnLayout {
        id: content
        width: 624
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: (saver.drift % 3 - 1) * 6
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: (Math.floor(saver.drift / 3) - 1) * 6
        spacing: 0

        // The area is exactly as tall as the digits; the font's line spacing
        // above them stays outside the layout.
        Item {
            objectName: "modernClockArea"
            Layout.fillWidth: true
            // Reading font re-evaluates this once the font is applied.
            Layout.preferredHeight: clockMetrics.font.pixelSize > 0 ? Math.ceil(-clockMetrics.tightBoundingRect("0123456789").y) : 0
            FontMetrics { id: clockMetrics; font: clock.font }
            Text {
                id: clock
                objectName: "modernClock"
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.horizontalCenterOffset: meridiem.visible ? -(meridiem.width + 12) / 2 : 0
                anchors.baseline: parent.bottom
                text: datetimeManager.datetimeData.timeStringFormat1
                color: "#FFFFFF"
                font.family: "DIN Condensed"
                font.pixelSize: 236
                textFormat: Text.PlainText
            }
            Text {
                id: meridiem
                anchors.left: clock.right
                anchors.leftMargin: 12
                anchors.baseline: clock.baseline
                anchors.baselineOffset: -26
                visible: !global.is24HModel
                text: datetimeManager.datetimeData.apString
                color: "#8CFFFFFF"
                font.family: "Roboto"
                font.pixelSize: 34
                textFormat: Text.PlainText
            }
        }
        Text {
            objectName: "modernDate"
            Layout.fillWidth: true
            Layout.topMargin: 14
            horizontalAlignment: Text.AlignHCenter
            text: datetimeManager.datetimeData.dateStringFormat1
            color: "#A6FFFFFF"
            font.family: "Roboto"
            font.pixelSize: 30
            font.letterSpacing: 1
            elide: Text.ElideRight
            textFormat: Text.PlainText
        }

        Item {
            objectName: "modernWeather"
            visible: saver.weatherShown
            Layout.fillWidth: true
            Layout.topMargin: 34
            Layout.preferredHeight: visible ? 52 : 0
            Row {
                anchors.centerIn: parent
                spacing: 22
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: saver.weatherShown ? saver.degrees(saver.weatherSnapshot.current.temperature) : ""
                    color: "#FFFFFF"
                    font.family: "DIN Condensed"
                    font.pixelSize: 52
                    textFormat: Text.PlainText
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: saver.weatherShown ? saver.condition(saver.weatherSnapshot.current.weather_code) : ""
                    visible: text !== ""
                    color: "#D9FFFFFF"
                    font.family: "Roboto"
                    font.pixelSize: 28
                    textFormat: Text.PlainText
                }
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 1; height: 30
                    color: "#40FFFFFF"
                    visible: rangeText.text !== ""
                }
                Text {
                    id: rangeText
                    anchors.verticalCenter: parent.verticalCenter
                    text: saver.weatherShown ? saver.range() : ""
                    visible: text !== ""
                    color: "#8CFFFFFF"
                    font.family: "Roboto"
                    font.pixelSize: 24
                    textFormat: Text.PlainText
                }
            }
        }

        Column {
            id: tiles
            objectName: "modernReadings"
            visible: saver.shown.length > 0
            Layout.fillWidth: true
            Layout.topMargin: 40
            spacing: 14
            readonly property bool compact: saver.columns >= 4
            readonly property int tileWidth: Math.min(300, Math.floor((width - (saver.columns - 1) * spacing) / saver.columns))
            readonly property int tileHeight: saver.rows.length > 1 ? (compact ? 110 : 124) : 136
            readonly property int padding: compact ? 16 : 20
            readonly property int valueSize: compact ? 50 : (saver.columns === 3 ? 60 : 68)
            Repeater {
                model: saver.rows
                delegate: Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: tiles.spacing
                    Repeater {
                        model: modelData
                        delegate: Rectangle {
                            readonly property var source: saver.reading(modelData.source)
                            objectName: "modernReading_" + modelData.key
                            width: tiles.tileWidth
                            height: tiles.tileHeight
                            radius: 22
                            color: "#17FFFFFF"
                            Text {
                                anchors.left: parent.left
                                anchors.top: parent.top
                                anchors.leftMargin: tiles.padding
                                anchors.topMargin: 16
                                anchors.right: dot.left
                                anchors.rightMargin: 8
                                text: tiles.compact && modelData.short ? modelData.short : modelData.label
                                color: "#8CFFFFFF"
                                font.family: "Roboto"
                                font.pixelSize: 20
                                elide: Text.ElideRight
                                textFormat: Text.PlainText
                            }
                            Rectangle {
                                id: dot
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.rightMargin: tiles.padding - 2
                                anchors.topMargin: 22
                                width: 10; height: 10; radius: 5
                                visible: !!source && !!source.evalutionColor && String(source.evalutionColor) !== ""
                                color: visible ? source.evalutionColor : "transparent"
                            }
                            // Narrow tiles shrink a long value rather than clip it.
                            Text {
                                id: readingValue
                                objectName: "modernValue"
                                anchors.left: parent.left
                                anchors.bottom: parent.bottom
                                anchors.leftMargin: tiles.padding
                                anchors.bottomMargin: 8
                                width: parent.width - 2 * tiles.padding - 4 - unit.implicitWidth
                                text: saver.orDefault(source ? source.valueString : null, "--")
                                color: "#FFFFFF"
                                font.family: "DIN Condensed"
                                font.pixelSize: tiles.valueSize
                                fontSizeMode: Text.HorizontalFit
                                minimumPixelSize: 30
                                textFormat: Text.PlainText
                            }
                            Text {
                                id: unit
                                objectName: "modernUnit"
                                x: readingValue.x + readingValue.contentWidth + 4
                                anchors.baseline: readingValue.baseline
                                text: saver.orDefault(source ? source.unitString : null, modelData.unit)
                                color: "#8CFFFFFF"
                                font.family: "Roboto"
                                font.pixelSize: tiles.compact ? 16 : (saver.columns === 3 ? 20 : 22)
                                textFormat: Text.PlainText
                            }
                        }
                    }
                }
            }
        }
    }
}
