"""Regressions for Wi-Fi status without external connectivity probes.

Synthetic instruction/QML fixtures only; no firmware, network or device access.
"""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import patch_app


HEADER = '''Item {
    source: {
                if (WiFiState.ConnectedState === wifiManager.state) {
                    return "signal" + wifiManager.rssiNum;
                } else if (WiFiState.UnusableState === wifiManager.state) {
                    return "qrc:/main/wifi/wifi_unusable.png";
                } else if (WiFiState.ConnectingState === wifiManager.state || WiFiState.ObtainingIpState === wifiManager.state) {
                    return "connecting";
                } else {
                    return "disconnected";
                }
    }
}
'''
ITEM = '''Item {
    source: control.state === WiFiState.UnusableState ? "qrc:/modules/wifi/state_unusable.png" : "signal"
}
'''
LIST = '''Item {
    onClicked: {
                        if (wifiManager.state === WiFiState.ConnectingState) {
                            // control.gotoOperateNetwork(wifi.ssid, qsTr("No internet connection"))
                        } else if (wifiManager.state === WiFiState.ConnectedState) {
                            control.gotoOperateNetwork(wifi.ssid, qsTr("Connected to internet"))
                        } else {
                            control.gotoOperateNetwork(wifi.ssid, qsTr("No internet connection"))
                        }
    }
}
'''


def ui_fixture():
    return {name: {"payload": content.encode()} for name, content in (
        ("/Main/Header/WiFiIndicator.qml", HEADER),
        ("/Modules/WiFi/WiFiItem.qml", ITEM),
        ("/Modules/WiFi/WiFiListPage.qml", LIST),
    )}


class WiFiStatusTests(unittest.TestCase):
    def test_associated_network_does_not_depend_on_external_ping(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required to evaluate the QML expressions")
        patched = patch_app.wifi_connection_ui(ui_fixture())
        header = patched["/Main/Header/WiFiIndicator.qml"].decode()
        start = header.index("{", header.index("source:"))
        icon = header[start + 1:patch_app.closing_brace(header, start) - 1]
        listing = patched["/Modules/WiFi/WiFiListPage.qml"].decode()
        start = listing.index("{", listing.index("onClicked:"))
        click = listing[start + 1:patch_app.closing_brace(listing, start) - 1]
        script = r'''
const assert = require("node:assert/strict");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const WiFiState = {UnconnectedState:0, ConnectingState:1, ObtainingIpState:2, UnusableState:3, ConnectedState:4};
const icon = new Function("WiFiState", "wifiManager", input.icon);
const click = new Function("WiFiState", "wifiManager", "control", "wifi", input.click);
for (const state of [0,1,2,3,4]) {
    const manager = {state, rssiNum:3};
    assert.equal(icon(WiFiState, manager), state >= 3 ? "signal3" : state === 0 ? "disconnected" : "connecting");
    let label;
    click(WiFiState, manager, {gotoOperateNetwork(ssid,text){label=text;}}, {ssid:"fixture"});
    if (state >= 3) assert.equal(label, "Connected to Wi-Fi");
    if (state === 0) assert.equal(label, "Not connected to Wi-Fi");
    if (state === 1) assert.equal(label, undefined);
}
'''
        subprocess.run([node, "-e", script], input=json.dumps(dict(icon=icon, click=click)), text=True, check=True)
        self.assertNotIn(b"state_unusable.png", patched["/Modules/WiFi/WiFiItem.qml"])

    def test_unknown_resource_anchor_is_rejected(self):
        fixture = ui_fixture()
        fixture["/Main/Header/WiFiIndicator.qml"]["payload"] = HEADER.replace("wifiManager.state", "other.state").encode()
        with self.assertRaisesRegex(ValueError, "expected one QML anchor"):
            patch_app.wifi_connection_ui(fixture)


class WiFiProbePatchTests(unittest.TestCase):
    def fixture(self):
        code = bytearray(0x5B7A78 - 0x400000)
        # Audited call gates, prologue and success epilogue. This fixture
        # contains only instruction words required to guard the one branch.
        words = {
            0x5B6AEC: 0xA9B77BFD, 0x5B6AF8: 0xAA0003F3,
            0x5B6B1C: 0xF9002BFB, 0x5B6B20: 0xF90037E0,
            0x5B6BA0: 0xF9402260, 0x5B6BA4: 0xB9009E7F,
            0x5B6BA8: 0x52800039, 0x5B6BAC: 0x97F97F55,
            0x5B6BB0: 0x2A1903E0, 0x5B6BC8: 0xA8C97BFD,
            0x5B6BCC: 0xD65F03C0, 0x5B6F80: 0x71000C1F,
            0x5B6F84: 0x540000E1, 0x5B6F8C: 0x97FFFED8,
            0x5B7A60: 0x51000C00, 0x5B7A64: 0x7100041F,
            0x5B7A68: 0x540007E8, 0x5B7A74: 0x97FFFC1E,
        }
        for address, word in words.items():
            struct.pack_into("<I", code, address - 0x400000, word)
        return code

    def test_branch_skips_probes_and_keeps_frame_cleanup_and_local_state_gates(self):
        code = self.fixture()
        before = bytes(code)
        report = patch_app.patch_wifi_probe(code)
        address, offset = int(report["address"], 16), int(report["file_offset"], 16)
        word = struct.unpack_from("<I", code, offset)[0]
        self.assertEqual(word >> 26, 0b000101)  # B, not BL or RET
        self.assertEqual(address + 4 * (word & 0x3FFFFFF), 0x5B6BA0)
        self.assertEqual(code[:offset], before[:offset])
        self.assertEqual(code[offset + 4:], before[offset + 4:])

    def test_modified_prologue_call_gate_or_epilogue_is_rejected_before_writing(self):
        for address in (0x5B6AF8, 0x5B6B20, 0x5B6BAC, 0x5B6BC8, 0x5B6F84, 0x5B7A68):
            with self.subTest(address=hex(address)):
                code = self.fixture()
                struct.pack_into("<I", code, address - 0x400000, 0xD503201F)
                before = bytes(code)
                with self.assertRaisesRegex(ValueError, "Wi-Fi probe instruction mismatch"):
                    patch_app.patch_wifi_probe(code)
                self.assertEqual(code, before)
