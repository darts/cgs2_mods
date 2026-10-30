import QtQuick 2.7
import QtQuick.Layouts 1.3
import Qt.labs.settings 1.0
import Qing.Controls 1.0
import Qing.Frames 1.0
import Qing.Keyboards 1.0

QPage {
    id: control
    objectName: "localMqttPage"
    title: "Local MQTT"
    property var draft: ({})
    property string feedback: ""
    property bool loaded: false
    property bool applying: false
    property int polls: 0
    property bool haDiscovery: true
    property string mqttStatus: "Starting"
    property string haStatus: "Not known"
    // Read with value(): a declared property would write its default back.
    Settings {
        id: haStore
        fileName: "/data/etc/cgs2-homeassistant.ini"
        category: "homeassistant"
    }
    Settings {
        id: saved
        fileName: "/data/etc/cgs2-mqtt.ini"
        category: "mqtt"
        property string host: ""
        property string port: "1883"
        property string username: ""
        property string password: ""
        property string client_id: ""
        property string pub_topic: ""
        property bool tls: false
        property bool enabled: false
        property string report_seconds: "60"
    }
    Component.onCompleted: {
        var mac = global.wifiMAC.replace(/:/g, "");
        draft = {host: saved.host, port: saved.port, username: saved.username,
            password: saved.password, client_id: saved.client_id || "qingping-" + mac,
            pub_topic: saved.pub_topic || "qingping/" + mac + "/up",
            tls: saved.tls, enabled: saved.enabled, report_seconds: saved.report_seconds || "60"};
        var discovery = haStore.value("discovery", true);
        haDiscovery = discovery !== false && discovery !== "false";
        loaded = true;
    }
    // The services helper publishes or removes the discovery within 30 seconds.
    function setDiscovery(on) {
        haDiscovery = on;
        haStore.setValue("discovery", on);
        haStore.sync();
    }
    function change(key, value) {
        var next = JSON.parse(JSON.stringify(draft));
        next[key] = value;
        draft = next;
    }
    function save() {
        if (draft.enabled) {
            var parts = draft.host.split(".");
            if (parts.length !== 4) { feedback = "Enter a numeric IPv4 address."; return; }
            for (var i = 0; i < 4; i++) {
                if (!/^(0|[1-9][0-9]{0,2})$/.test(parts[i]) || Number(parts[i]) > 255) {
                    feedback = "Enter a numeric IPv4 address."; return;
                }
            }
            if (Number(parts[0]) === 0 || Number(parts[0]) === 127 || Number(parts[0]) >= 224 ||
                    Number(parts[0]) === 169 && Number(parts[1]) === 254) {
                feedback = "Enter a unicast broker IPv4 address."; return;
            }
            if (!/^[0-9]+$/.test(draft.port) || Number(draft.port) < 1 || Number(draft.port) > 65535) {
                feedback = "The port must be 1 to 65535."; return;
            }
            if (!draft.username || !draft.password) {
                feedback = "A broker username and password are necessary."; return;
            }
            if (!draft.client_id || !draft.pub_topic) {
                feedback = "The client ID and publish topic are necessary."; return;
            }
            if (/[+#]/.test(draft.pub_topic)) {
                feedback = "The publish topic cannot contain + or #."; return;
            }
        }
        if (!/^[0-9]+$/.test(draft.report_seconds) || Number(draft.report_seconds) < 10 || Number(draft.report_seconds) > 3600) {
            feedback = "The report interval must be 10 to 3600 seconds."; return;
        }
        saved.host = draft.host; saved.port = draft.port;
        saved.username = draft.username; saved.password = draft.password;
        saved.client_id = draft.client_id; saved.pub_topic = draft.pub_topic;
        saved.tls = draft.tls;
        saved.enabled = draft.enabled; saved.report_seconds = draft.report_seconds;
        // Property changes reach Qt.labs.Settings on a delayed timer. Write
        // each value explicitly before sync so the helper reads this draft.
        var keys = ["host", "port", "username", "password", "client_id",
                    "pub_topic", "tls", "enabled", "report_seconds"];
        for (var keyIndex = 0; keyIndex < keys.length; keyIndex++)
            saved.setValue(keys[keyIndex], draft[keys[keyIndex]]);
        saved.sync();
        feedback = "The monitor applies the settings. Then the display restarts.";
        polls = 0;
        applying = true;
        developerController.runCommand("/usr/local/sbin/cgs2-mqtt-config apply");
    }
    Timer {
        interval: 2000; repeat: true; running: control.applying
        onTriggered: {
            // A successful apply restarts the display; this bounds a missing result.
            if (++control.polls > 30) {
                control.applying = false;
                control.feedback = "The MQTT helper did not send a result. Open Local MQTT again. Examine the saved settings.";
                return;
            }
            var request = new XMLHttpRequest();
            request.open("GET", "file:///tmp/cgs2-mqtt-status.json");
            request.onreadystatechange = function() {
                if (request.readyState !== XMLHttpRequest.DONE) return;
                try {
                    var status = JSON.parse(request.responseText);
                    if (status.message) control.feedback = status.message;
                    // ok is null while the helper runs, then true or false.
                    if (status.ok === true || status.ok === false) control.applying = false;
                } catch (e) {}
            };
            request.send();
        }
    }
    Timer {
        interval: 3000; repeat: true; running: true; triggeredOnStart: true
        onTriggered: {
            var request = new XMLHttpRequest();
            request.open("GET", "file:///run/cgs2-services/status.json");
            request.onreadystatechange = function() {
                if (request.readyState !== XMLHttpRequest.DONE) return;
                try {
                    var status = JSON.parse(request.responseText);
                    if (status.home_assistant) control.haStatus = status.home_assistant.message;
                    if (status.mqtt) control.mqttStatus = status.mqtt.message;
                } catch (e) {}
            };
            request.send();
        }
    }
    initialItem: QChildPage {
        title: control.title
        onExited: control.exited()
        QFlickable {
            objectName: "mqttForm"
            anchors.fill: parent
            contentHeight: layout.height
            ColumnLayout {
                id: layout
                width: parent.width
                spacing: 14
                QTextArea {
                    Layout.leftMargin: 30; Layout.rightMargin: 30
                    Layout.fillWidth: true
                    Layout.preferredHeight: contentHeight
                    font.pixelSize: 26
                    text: "When you save with MQTT on, the monitor allows the IPv4 address and TCP port of this broker. The broker must use a username and password. When you save with MQTT off, the monitor removes this permission. The approvals for time, weather and DNS do not change."
                }
                QSwitchItem {
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    text: "Enable MQTT"
                    checked: control.draft.enabled === true
                    onCheckedChanged: if (control.loaded && control.draft.enabled !== checked) control.change("enabled", checked)
                }
                Repeater {
                    model: [
                        {key: "host", label: "Broker IPv4"},
                        {key: "port", label: "TCP port"},
                        {key: "username", label: "Username"},
                        {key: "password", label: "Password"},
                        {key: "client_id", label: "Client ID"},
                        {key: "pub_topic", label: "Publish topic"},
                        {key: "report_seconds", label: "Report interval (s)"}
                    ]
                    delegate: QOptionItem {
                        objectName: "mqttField_" + modelData.key
                        Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                        name: modelData.label
                        childText: modelData.key === "password" ? (control.draft.password ? "********" : "Not set") : (control.draft[modelData.key] || "Not set")
                        onClicked: control.mainStackView.push(editor, {fieldKey: modelData.key, label: modelData.label})
                    }
                }
                QSwitchItem {
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    text: "TLS encryption"
                    checked: control.draft.tls === true
                    onCheckedChanged: if (control.loaded && control.draft.tls !== checked) control.change("tls", checked)
                }
                QButton {
                    objectName: "mqttSave"
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    Layout.preferredHeight: 80; radius: 24
                    text: control.draft.enabled ? "Save & allow broker" : "Save & disable MQTT"
                    enabled: !control.applying
                    onClicked: control.save()
                }
                QTextArea {
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    Layout.preferredHeight: contentHeight
                    text: "MQTT: " + control.mqttStatus + (control.feedback ? "\n" + control.feedback : "")
                    font.pixelSize: 26
                }
                QSwitchItem {
                    objectName: "haDiscovery"
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    text: "Home Assistant discovery"
                    checked: control.haDiscovery
                    onCheckedChanged: if (control.loaded && control.haDiscovery !== checked) control.setDiscovery(checked)
                }
                QTextArea {
                    objectName: "haStatus"
                    Layout.leftMargin: 30; Layout.rightMargin: 30; Layout.fillWidth: true
                    Layout.preferredHeight: contentHeight
                    font.pixelSize: 26
                    text: "Home Assistant finds the monitor and its sensors through MQTT discovery. The switch takes effect in 30 seconds or less with the saved broker settings.\nStatus: " + control.haStatus
                }
                Item { Layout.preferredHeight: 20 }
            }
        }
    }
    Component {
        id: editor
        QChildPage {
            property string fieldKey
            property string label
            title: label
            onExited: control.mainStackView.pop()
            Keyboard {
                id: keyboard
                objectName: "mqttKeyboard"
                anchors.fill: parent
                text: control.draft[fieldKey] || ""
                inputPlaceholderText: label
                ensureButtonText: "Done"
                charLimit: 0
                passwdEnable: fieldKey === "password"
                isPwdEchoMode: fieldKey === "password"
                onEditFinished: {
                    control.change(fieldKey, keyboard.text);
                    control.mainStackView.pop();
                }
            }
        }
    }
}
