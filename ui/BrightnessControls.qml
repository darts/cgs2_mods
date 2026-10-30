import QtQuick 2.0
import QtQuick.Layouts 1.3
import Qt.labs.settings 1.0
import Qing.Controls 1.0
import Qing.Frames 1.0

// Inlined into both stock brightness pages. The native controller reads the
// same setting; no timer or second brightness controller is needed here.
Column {
    id: brightnessControls
    objectName: "brightnessControls"
    spacing: 20
    property int minimumBrightness: 1
    property bool ready: false

    Settings {
        id: brightnessSettings
        fileName: "/data/etc/setting.ini"
        category: "screen"
    }
    function boundedMinimum(value) {
        var number = Number(value);
        return isFinite(number) ? Math.max(1, Math.min(100, Math.floor(number))) : 1;
    }
    function loadMinimum() {
        brightnessSettings.sync();
        minimumBrightness = boundedMinimum(brightnessSettings.value("minimum_brightness", 1));
        minimumSlider.value = minimumBrightness;
    }
    function saveMinimum(value) {
        if (!ready) return;
        var minimum = boundedMinimum(Math.round(value));
        if (minimum === minimumBrightness) return;
        brightnessSettings.setValue("minimum_brightness", minimum);
        brightnessSettings.sync();
        minimumBrightness = minimum;
        if (!screenManager.isAutoBrightness && screenManager.brightnessValue < minimum) {
            screenManager.brightnessValue = minimum;
        }
    }
    Component.onCompleted: { loadMinimum(); ready = true; }
    onVisibleChanged: if (visible && ready) loadMinimum()

    QSwitchItem {
        objectName: "autoBrightnessSwitch"
        width: parent.width
        text: qsTr("Auto-Brightness")
        checked: screenManager.isAutoBrightness
        onCheckedChanged: {
            if (brightnessControls.ready && checked !== screenManager.isAutoBrightness)
                screenManager.isAutoBrightness = checked;
        }
    }
    QBlankItem {
        width: parent.width
        showBackground: !screenManager.isAutoBrightness
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 30
            anchors.rightMargin: 30
            spacing: 0
            Image { source: "qrc:/modules/screen/backlight_min.png" }
            QSlider {
                objectName: "manualBrightnessSlider"
                Layout.fillWidth: true
                Layout.fillHeight: true
                from: brightnessControls.minimumBrightness
                to: 1000
                stepSize: 1
                enabled: !screenManager.isAutoBrightness
                value: screenManager.brightnessValue
                // A range/binding update must not stop the native auto timer.
                onMoved: screenManager.brightnessValue = Math.round(value)
            }
            Image { source: "qrc:/modules/screen/backlight_max.png" }
        }
    }
    QBlankItem {
        objectName: "minimumBrightnessCard"
        width: parent.width
        height: 184
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                QText { text: "Minimum brightness"; font.pixelSize: 28 }
                QSpacer {}
                QText {
                    objectName: "minimumBrightnessValue"
                    text: String(Math.round(minimumSlider.value))
                    font.pixelSize: 28
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 64
                spacing: 12
                QText { text: "1"; font.pixelSize: 22; opacity: 0.65 }
                QSlider {
                    id: minimumSlider
                    objectName: "minimumBrightnessSlider"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    from: 1
                    to: 100
                    stepSize: 1
                    snapMode: QSlider.SnapAlways
                    value: 1
                    // Save once on release, or on a keyboard adjustment.
                    onPressedChanged: if (!pressed) brightnessControls.saveMinimum(value)
                    onMoved: if (!pressed) brightnessControls.saveMinimum(value)
                }
                QText { text: "100"; font.pixelSize: 22; opacity: 0.65 }
            }
            QText {
                objectName: "minimumBrightnessNote"
                Layout.fillWidth: true
                text: "Original default: 20. Modded default: 1."
                font.pixelSize: 22
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                opacity: 0.65
            }
        }
    }
}
