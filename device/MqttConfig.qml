import QtQuick 2.12
import Qt.labs.settings 1.0

// The helper first runs a guarded validateOnly copy while the display stays up.
// Apply mode requires the main application stopped. QSettings handles its own
// INI quoting, Unicode, locking and atomic writes; no credential shell parsing.
Item {
    width: 1; height: 1
    property bool validateOnly: false
    Settings { id: staged; fileName: "/data/etc/cgs2-mqtt.ini"; category: "mqtt" }
    Settings { id: applied; fileName: "/data/etc/cgs2-mqtt-applied.ini"; category: "mqtt" }
    Settings { id: third; fileName: "/data/etc/setting.ini"; category: "third" }
    Settings { id: vendor; fileName: "/data/etc/setting.ini"; category: "host" }
    Settings { id: app; fileName: "/data/etc/setting.ini"; category: "application" }
    Settings { id: clock; fileName: "/data/etc/setting.ini"; category: "datetime" }
    Settings { id: location; fileName: "/data/etc/setting.ini"; category: "location" }
    Settings { id: pairing; fileName: "/data/etc/setting.ini"; category: "qingping" }
    function run() {
        var enabled = staged.value("enabled", false);
        enabled = enabled === true || enabled === "true";
        var host = String(staged.value("host", ""));
        var portText = String(staged.value("port", "1883"));
        var keys = ["username", "password", "client_id", "pub_topic"];
        var values = {};
        var error = "";
        for (var i = 0; i < keys.length; i++) {
            var v = String(staged.value(keys[i], ""));
            if (v.length > 4096 || /[\x00-\x1f\x7f]/.test(v)) error = "Invalid MQTT field length or control character.";
            values[keys[i]] = v;
        }
        if (enabled) {
            var parts = host.split(".");
            if (parts.length !== 4) error = "Broker must be a numeric IPv4 address.";
            for (i = 0; i < parts.length; i++)
                if (!/^(0|[1-9][0-9]{0,2})$/.test(parts[i]) || Number(parts[i]) > 255)
                    error = "Broker must be a numeric IPv4 address.";
            // Match the services controller, which refuses link-local brokers
            // only after the display has stopped.
            if (Number(parts[0]) === 0 || Number(parts[0]) === 127 || Number(parts[0]) >= 224 ||
                    Number(parts[0]) === 169 && Number(parts[1]) === 254)
                error = "Broker must have a unicast IPv4 address.";
            if (!/^[0-9]{1,5}$/.test(portText) || Number(portText) < 1 || Number(portText) > 65535)
                error = "MQTT port must be between 1 and 65535.";
            if (!values.client_id || !values.pub_topic)
                error = "Client ID and publish topic are required.";
            if (!values.username || !values.password)
                error = "A broker username and password are required.";
            if (/[+#]/.test(values.pub_topic)) error = "Publish topic cannot contain wildcards.";
        }
        var seconds = String(staged.value("report_seconds", "60"));
        if (!/^[0-9]+$/.test(seconds) || Number(seconds) < 10 || Number(seconds) > 3600)
            error = "The report interval must be 10 to 3600 seconds.";
        if (error) {
            console.log("CGS2_RESULT " + JSON.stringify({ok: false, message: error,
                validationOnly: validateOnly}));
            Qt.quit(); return;
        }
        if (validateOnly) {
            console.log("CGS2_RESULT " + JSON.stringify({ok: true, validationOnly: true,
                enabled: enabled, host: enabled ? host : "", port: enabled ? Number(portText) : 0}));
            Qt.quit(); return;
        }
        // The firmware owns no MQTT identity, including the old third-party one.
        var nativeKeys = keys.concat(["host", "sub_topic"]);
        for (i = 0; i < nativeKeys.length; i++) {
            vendor.setValue(nativeKeys[i], ""); third.setValue(nativeKeys[i], "");
        }
        vendor.setValue("port", 0); vendor.setValue("tls", 0);
        third.setValue("port", 0); third.setValue("tls", 0);
        applied.setValue("enabled", enabled);
        applied.setValue("host", enabled ? host : "");
        applied.setValue("port", Number(portText));
        var tls = staged.value("tls", false);
        applied.setValue("tls", tls === true || tls === "true");
        applied.setValue("report_seconds", Number(seconds));
        for (i = 0; i < keys.length; i++) applied.setValue(keys[i], enabled ? values[keys[i]] : "");
        applied.sync();
        app.setValue("is_show_qingping_app_view", false);
        if (staged.value("privacy_initialized", false) !== true &&
                staged.value("privacy_initialized", false) !== "true") {
            // No server has initially been authorized. Stop unneeded Wi-Fi scans;
            // the ordinary Wi-Fi settings can enable it when the owner is ready.
            app.setValue("wifi_disabled", true);
            staged.setValue("privacy_initialized", true);
            staged.sync();
        }
        clock.setValue("is_automatically", false); clock.setValue("timezone_auto", false);
        location.setValue("is_auto", false);
        pairing.setValue("is_iot_paired", false); pairing.setValue("is_paired", false);
        vendor.sync(); third.sync(); app.sync(); clock.sync(); location.sync(); pairing.sync();
        // Public endpoint metadata only; credentials never go to stdout/logs.
        console.log("CGS2_RESULT " + JSON.stringify({ok: true, validationOnly: false, enabled: enabled,
            host: enabled ? host : "", port: enabled ? Number(portText) : 0}));
        Qt.quit();
    }
    Timer { interval: 20; running: true; onTriggered: run() }
}
