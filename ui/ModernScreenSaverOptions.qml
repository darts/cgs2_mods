import QtQuick 2.0
import QtQuick.Layouts 1.3
import Qt.labs.settings 1.0
import Qing.Frames 1.0
import Qing.Controls 1.0

// Options for the Modern Clock screen saver. scripts/patch_app.py inlines this
// file into the stock Screen Saver settings page; `control` is that page.
// ModernScreenSaver.qml reads these keys; keep the defaults the same.
QChildPage {
    id: modernOptionsPage
    objectName: "modernScreenSaverOptions"
    title: "Modern Clock"
    onExited: control.mainStackView.pop()

    Settings {
        id: modernOptionsStore
        fileName: "/data/etc/cgs2-screensaver.ini"
        category: "modern"
    }
    function flag(key, fallback) {
        var value = modernOptionsStore.value(key, fallback);
        return value === true || value === "true";
    }
    function store(key, value) {
        modernOptionsStore.setValue(key, value);
        modernOptionsStore.sync();
    }

    QFlickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: modernOptionsLayout.implicitHeight + 40
        ColumnLayout {
            id: modernOptionsLayout
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: 30
            anchors.rightMargin: 30
            spacing: 16
            QNoticeItem {
                Layout.fillWidth: true
                implicitHeight: 70
                text: "Select the items to show below the clock."
            }
            QSwitchItem {
                objectName: "modernOption_weather"
                Layout.fillWidth: true
                text: "Outdoor weather"
                defaultChecked: modernOptionsPage.flag("weather", true)
                onCheckedChanged: modernOptionsPage.store("weather", checked)
            }
            QNoticeItem {
                Layout.fillWidth: true
                implicitHeight: 70
                text: "The weather shows only when weather is on in Internet services."
            }
            QCategoryItem {
                text: "Readings"
            }
            QSwitchItem {
                objectName: "modernOption_temperature"
                Layout.fillWidth: true
                text: "Temperature"
                defaultChecked: modernOptionsPage.flag("temperature", true)
                onCheckedChanged: modernOptionsPage.store("temperature", checked)
            }
            QSwitchItem {
                objectName: "modernOption_humidity"
                Layout.fillWidth: true
                text: "Humidity"
                defaultChecked: modernOptionsPage.flag("humidity", true)
                onCheckedChanged: modernOptionsPage.store("humidity", checked)
            }
            QSwitchItem {
                objectName: "modernOption_co2"
                Layout.fillWidth: true
                text: "CO2"
                defaultChecked: modernOptionsPage.flag("co2", true)
                onCheckedChanged: modernOptionsPage.store("co2", checked)
            }
            QSwitchItem {
                objectName: "modernOption_pm25"
                Layout.fillWidth: true
                text: "PM2.5"
                defaultChecked: modernOptionsPage.flag("pm25", true)
                onCheckedChanged: modernOptionsPage.store("pm25", checked)
            }
            QSwitchItem {
                objectName: "modernOption_pm10"
                Layout.fillWidth: true
                text: "PM10"
                defaultChecked: modernOptionsPage.flag("pm10", false)
                onCheckedChanged: modernOptionsPage.store("pm10", checked)
            }
            QSwitchItem {
                objectName: "modernOption_tvoc"
                Layout.fillWidth: true
                text: "eTVOC"
                defaultChecked: modernOptionsPage.flag("tvoc", false)
                onCheckedChanged: modernOptionsPage.store("tvoc", checked)
            }
            QSwitchItem {
                objectName: "modernOption_noise"
                Layout.fillWidth: true
                text: "Noise"
                defaultChecked: modernOptionsPage.flag("noise", false)
                onCheckedChanged: modernOptionsPage.store("noise", checked)
            }
        }
    }
}
