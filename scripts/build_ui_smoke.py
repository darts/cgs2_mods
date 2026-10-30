#!/usr/bin/env python3
"""Build a private on-device Qt smoke test from the owner's stock application.

Run the result with qmlscene on the CGS2; no broker connection is made and the
test's isolated /tmp INI contains only dummy credentials. Extracted proprietary
resources stay in the supplied output directory, normally under .private/.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from patch_app import ROOT, STOCK_SHA256, closing_brace, resources, transform

SOURCE = ROOT / "ui/LocalMqtt.qml"
MQTT_KEYS = ["host", "port", "username", "password", "client_id",
             "pub_topic", "tls", "enabled", "report_seconds"]

HOST_CHECK = r'''
const vm = require("node:vm");
const assert = require("node:assert/strict");
const input = JSON.parse(require("node:fs").readFileSync(process.argv[2], "utf8"));
let count = 0;
const pending = {}, cached = {}, persisted = {};
const saved = {
    setValue(key, value) { cached[key] = value; },
    sync() { Object.assign(persisted, cached); }
};
for (const key of input.keys) {
    pending[key] = cached[key] = persisted[key] = "previous " + key;
    Object.defineProperty(saved, key, {
        get() { return pending[key]; },
        set(value) { pending[key] = value; } // Qt's delayed property store
    });
}
const context = {
    console, JSON, Number, String, saved, feedback:"", applying:false,
    draft:{host:"192.0.2.10",port:"1884",username:"fixture-user",password:"dummy '# $ secret",
        client_id:"fixture-client",pub_topic:"fixture/up",tls:true,enabled:true,report_seconds:"30"},
    developerController:{runCommand(command) {
        assert.equal(command,"/usr/local/sbin/cgs2-mqtt-config apply");
        for (const key of input.keys) {
            assert.equal(persisted[key],context.draft[key],
                key+" must be on disk before helper invocation despite delayed property writes");
            count++;
        }
    }}
};
context.control = context;
vm.createContext(context);
vm.runInContext(input.functions.join("\n"),context);
context.save();
context.change("host","192.0.2.11");context.change("password","second dummy");
context.change("tls",false);context.save();
context.change("enabled",false);context.change("host","");context.save();
assert.equal(count,input.keys.length*3);
// The local publisher needs credentials; these drafts must never reach the helper.
const valid={host:"192.0.2.10",port:"1884",username:"fixture-user",password:"dummy",
    client_id:"fixture-client",pub_topic:"fixture/up",tls:false,enabled:true,report_seconds:"60"};
let rejected=0;
for (const [key,value] of [["username",""],["password",""],["host","169.254.1.2"],["pub_topic","fixture/#"],
                           ["report_seconds","9"],["report_seconds","3601"],["report_seconds","1m"]]) {
    context.draft=Object.assign({},valid,{[key]:value});context.feedback="";
    context.save();
    assert.equal(count,input.keys.length*3,key+" draft must be rejected before the helper runs");
    assert.notEqual(context.feedback,"",key+" rejection must explain the problem");
    rejected++;
}
console.log("CGS2_MQTT_HOST_PASS "+count+" immediate persistence assertions, "+rejected+" rejected drafts");
'''


def check_host(source: str, node: str | None = None) -> None:
    executable = shutil.which(node or "node")
    if executable is None:
        raise RuntimeError("Node.js is required for optional host QML logic checks")
    functions = []
    for name in ("change", "save"):
        match = re.search(r"\bfunction\s+" + name + r"\s*\(", source)
        if match is None:
            raise ValueError("missing MQTT function: " + name)
        end = closing_brace(source, source.index("{", match.start()))
        functions.append(source[match.start():end])
    with tempfile.TemporaryDirectory(prefix="cgs2-mqtt-host-") as directory:
        root = Path(directory)
        script = root / "check.js"
        fixture = root / "fixture.json"
        script.write_text(HOST_CHECK, encoding="utf-8", newline="\n")
        fixture.write_text(json.dumps({"keys":MQTT_KEYS,"functions":functions}), encoding="utf-8")
        subprocess.run([executable,str(script),str(fixture)],check=True)

HARNESS = r'''
import QtQuick 2.7
import QtQuick.Window 2.2
Window {
    id: window
    visible: true
    width: 720; height: 720
    property int diskAssertions: 0
    property int resetExits: 0
    QtObject {
        id: global
        property string wifiMAC: "001122334455"
    }
    QtObject {
        id: developerController
        property int calls: 0
        property int resetCalls: 0
        function runCommand(command) {
            if (command === "/usr/local/sbin/cgs2-reset start") { resetCalls++; return; }
            window.check(command === "/usr/local/sbin/cgs2-mqtt-config apply", "fixed helper command");
            window.checkSavedImmediately();
            calls++;
        }
    }
    Rectangle {
        id: surface
        anchors.fill: parent
        color: "black"
        LocalMqtt { id: page; anchors.fill: parent }
        Reset { id: resetPage; anchors.fill: parent; visible: false; onExited: window.resetExits++ }
    }
    function check(condition, text) {
        if (!condition) {
            console.error("CGS2_SMOKE_FAIL " + text);
            Qt.exit(1);
            throw new Error(text);
        }
    }
    function checkSavedImmediately() {
        // Synchronous file read before the event loop can flush delayed
        // Settings property writes, matching the real helper's first read.
        var request = new XMLHttpRequest();
        request.open("GET", "file:///tmp/cgs2-ui-smoke/settings.ini", false);
        request.send();
        var keys = ["host", "port", "username", "password", "client_id",
                    "pub_topic", "tls", "enabled", "report_seconds"];
        for (var i = 0; i < keys.length; i++) {
            var key = keys[i];
            var match = new RegExp("^" + key + "=(.*)$", "m").exec(request.responseText);
            window.check(match !== null, "immediate saved key " + key);
            var value = match[1].replace(/\r$/, "");
            if (value[0] === '"' && value[value.length - 1] === '"') value = value.slice(1,-1);
            window.check(value === String(page.draft[key]), "immediate persisted draft " + key);
            window.diskAssertions++;
        }
    }
    function readSettings() {
        var request = new XMLHttpRequest();
        request.open("GET", "file:///tmp/cgs2-ui-smoke/settings.ini", false);
        request.send();
        return request.responseText;
    }
    function find(item, name) {
        if (item.objectName === name) return item;
        var children = item.children || [];
        for (var i = 0; i < children.length; i++) {
            var result = find(children[i], name);
            if (result) return result;
        }
        return null;
    }
    function screenshot(name) {
        surface.grabToImage(function(result) {
            window.check(result.saveToFile("/tmp/cgs2-ui-" + name + ".png"), "save screenshot " + name);
        });
    }
    function checkResetLayout() {
        var layout = window.find(resetPage, "resetMessageLayout");
        window.check(layout !== null, "Reset has a bounded text layout");
        window.check(layout.x >= 32 && layout.y >= 24 &&
                     layout.x + layout.width <= layout.parent.width - 32 &&
                     layout.y + layout.height <= layout.parent.height - 24,
                     "Reset text stays inside the message box");
        ["resetTitle", "resetMessage"].forEach(function(name) {
            var text = window.find(resetPage, name);
            window.check(text.contentWidth <= text.width + 0.5 &&
                         text.contentHeight <= text.height + 0.5,
                         name + " wraps without clipping");
        });
    }
    Timer {
        property int stage: 0
        interval: 1000; repeat: true; running: true
        onTriggered: {
            if (stage === 0) {
                window.check(page.loaded, "configuration loaded");
                page.change("enabled", true);
                page.change("host", "cloud.example.com");
                page.save();
                window.check(developerController.calls === 0, "hostname rejected");
                page.change("host", "127.0.0.1"); page.save();
                window.check(developerController.calls === 0, "loopback rejected");
                page.change("host", "192.0.2.10"); page.change("port", "65536"); page.save();
                window.check(developerController.calls === 0, "invalid port rejected");
                page.change("port", "1884");
                page.change("username", "fixture-user");
                page.change("client_id", "fixture-client");
                page.change("pub_topic", "fixture/up");
                page.change("tls", true);
                page.change("password", "dummy '# $ secret");
                page.change("report_seconds", "5"); page.save();
                window.check(developerController.calls === 0, "too short report interval rejected");
                window.check(page.draft.report_seconds === "5" && page.feedback.indexOf("10 to 3600") >= 0, "interval rejection explains the range");
                page.change("report_seconds", "30"); page.save();
                window.check(developerController.calls === 1, "valid explicit broker saves");
                page.change("enabled", false); page.change("host", ""); page.save();
                window.check(developerController.calls === 2, "disable saves without host");
                page.applying = false;
                page.feedback = "";
                page.change("password", "");
                window.screenshot("form");
            } else if (stage === 1) {
                var form = window.find(page, "mqttForm");
                window.check(form !== null, "scrolling form exists");
                window.check(form.contentHeight > form.height, "all settings scroll");
                form.contentY = form.contentHeight - form.height;
            } else if (stage === 2) {
                var ha = window.find(page, "haDiscovery");
                window.check(ha !== null && ha.checked, "Home Assistant discovery is on by default");
                window.check(window.find(page, "haStatus").text.indexOf("Status: Published to the broker") >= 0, "discovery status shows the helper message");
                var bottom = ha.mapToItem(window.contentItem, 0, ha.height).y;
                window.check(bottom <= 720.5, "discovery switch fits after scrolling");
                ha.checked = false;
                var request = new XMLHttpRequest();
                request.open("GET", "file:///tmp/cgs2-ui-smoke/homeassistant.ini", false);
                request.send();
                window.check(/^discovery=false$/m.test(request.responseText), "switch saves at once");
                window.check(!/^discovery=/m.test(window.readSettings()), "switch is not part of the MQTT draft");
                window.screenshot("form-bottom");
            } else if (stage === 3) {
                window.find(page, "mqttField_password").clicked();
            } else if (stage === 4) {
                window.check(page.mainStackView.depth === 2, "password editor opens");
                var keyboard = window.find(page, "mqttKeyboard");
                window.check(keyboard !== null, "stock on-screen keyboard exists");
                window.check(keyboard.passwdEnable && keyboard.isPwdEchoMode, "password is masked");
                keyboard.text = "sample secret";
                keyboard.editFinished();
                window.check(page.draft.password === "sample secret", "keyboard edits draft");
            } else if (stage === 5) {
                window.find(page, "mqttField_host").clicked();
            } else if (stage === 6) {
                window.screenshot("keyboard");
            } else if (stage === 7) {
                window.check(window.diskAssertions === 18, "both saves persisted all nine fields immediately");
                page.visible = false;
                resetPage.visible = true;
                window.screenshot("reset-confirmation");
            } else if (stage === 8) {
                var confirmation = window.find(resetPage, "resetConfirmation");
                window.check(confirmation !== null, "Reset uses the real stock message control");
                window.check(confirmation.text.indexOf("erases settings") >= 0, "Reset explains data removal");
                window.checkResetLayout();
                confirmation.leftButtonClicked();
                window.check(window.resetExits === 1 && developerController.resetCalls === 0, "Cancel performs no restore");
                confirmation.rightButtonClicked();
                window.check(resetPage.restoring && developerController.resetCalls === 1, "Restore invokes only the fixed helper");
            } else if (stage === 10) {
                window.check(!resetPage.restoring && resetPage.message === "Fixture: stock copy missing", "Reset displays failure and allows exit");
                resetPage.message = "The stock copy failed verification. No files were restored. Use a verified computer backup.";
            } else if (stage === 11) {
                window.checkResetLayout();
                window.screenshot("reset-error");
            } else if (stage === 12) {
                console.log("CGS2_SMOKE_PASS 18 immediate persistence assertions, MQTT controls, keyboard, layout and Reset confirmation");
                Qt.quit();
            }
            stage++;
        }
    }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_app", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--check", action="store_true", help="run isolated host JS persistence regression only")
    parser.add_argument("--node", help="Node.js executable for optional host checks")
    args = parser.parse_args()
    if args.check:
        check_host(SOURCE.read_text(), args.node)
        return
    if args.stock_app is None or args.output is None:
        parser.error("stock_app and output are required unless --check is used")
    binary = args.stock_app.read_bytes()
    if hashlib.sha256(binary).hexdigest() != STOCK_SHA256:
        parser.error("stock application does not match audited SHA256")
    qml = resources(binary)
    changes = transform(qml)
    for name, entry in qml.items():
        if not name.startswith("/Qing/"):
            continue
        payload = changes.get(name, entry["payload"]).decode()
        # Images are extracted next to Main.qml so screenshots retain stock UI.
        payload = payload.replace('"qrc:/', '"file:///tmp/cgs2-ui-smoke/assets/')
        path = args.output / "imports" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    images = resources(binary, tree_offset=0x26F8DB, tree_size=7018,
                       name_offset=0x271445, data_offset=0x272E09, data_size=1203381)
    for name, entry in images.items():
        path = args.output / "assets" / name.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["payload"])
    snow = args.output / "imports/Qing/Snow"
    snow.mkdir(parents=True, exist_ok=True)
    (snow / "qmldir").write_text("module Qing.Snow\nsingleton WiFiState 1.0 WiFiState.qml\n")
    (snow / "WiFiState.qml").write_text("pragma Singleton\nimport QtQuick 2.0\nQtObject {}\n")
    source = SOURCE.read_text()
    source = source.replace("/data/etc/cgs2-mqtt.ini", "/tmp/cgs2-ui-smoke/settings.ini")
    source = source.replace("file:///tmp/cgs2-mqtt-status.json", "file:///tmp/cgs2-ui-smoke/status.json")
    source = source.replace("/data/etc/cgs2-homeassistant.ini", "/tmp/cgs2-ui-smoke/homeassistant.ini")
    source = source.replace("file:///run/cgs2-services/status.json", "file:///tmp/cgs2-ui-smoke/services-status.json")
    (args.output / "LocalMqtt.qml").write_text(source)
    reset = (ROOT / "ui/Reset.qml").read_text().replace("/tmp/cgs2-reset-status.json", "/tmp/cgs2-ui-smoke/reset-status.json")
    (args.output / "Reset.qml").write_text(reset)
    (args.output / "reset-status.json").write_text('{"ok":false,"message":"Fixture: stock copy missing"}\n')
    (args.output / "Main.qml").write_text(HARNESS)
    (args.output / "settings.ini").write_text("[mqtt]\nhost=\nport=1883\nusername=\npassword=\nclient_id=\npub_topic=\ntls=false\nenabled=false\n", encoding="utf-8", newline="\n")
    (args.output / "status.json").write_text('{}\n', encoding="utf-8", newline="\n")
    (args.output / "services-status.json").write_text('{"home_assistant":{"message":"Published to the broker"}}\n',
                                                     encoding="utf-8", newline="\n")
    print(f"Prepared {args.output}; push as /tmp/cgs2-ui-smoke")
    print("QML2_IMPORT_PATH=/tmp/cgs2-ui-smoke/imports QT_QUICK_BACKEND=software "
          "qmlscene -platform offscreen /tmp/cgs2-ui-smoke/Main.qml")


if __name__ == "__main__":
    main()
