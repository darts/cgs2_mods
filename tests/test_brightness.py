"""Check brightness patch boundaries without distributing stock firmware."""
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_app


PAGES = ("/Modules/Screen/ScreenPage.qml", "/Main/Views/Setting/LightAdjustPage.qml")
PAGE = b'''import QtQuick 2.0
import Qing.Frames 1.0
QPage {
    initialItem: QChildPage {
        Column {
            QSwitchItem { id: autoBrightnessSwitch }
            QBlankItem {
                id: adjustBrightnessSlider
                QSlider { from: 20; to: 1000 }
            }
            QOptionItem { name: "Other screen options" }
        }
    }
}
'''


class BrightnessTests(unittest.TestCase):
    def test_both_pages_share_the_same_saved_minimum_and_keep_other_options(self):
        original = {path: {"payload": PAGE} for path in PAGES}
        changed = patch_app.brightness_controls(original)
        self.assertEqual(set(changed), set(PAGES))
        for path in PAGES:
            self.assertIn(b"from: brightnessControls.minimumBrightness", changed[path])
            self.assertIn(b"sourceComponent: cgs2BrightnessControls", changed[path])
            self.assertIn(b'fileName: "/data/etc/setting.ini"', changed[path])
            self.assertIn(b'"Other screen options"', changed[path])
            self.assertIn(b"Original default: 20. Modded default: 1.", changed[path])
            self.assertNotIn(b"onValueChanged: screenManager.brightnessValue", changed[path])
            self.assertEqual(original[path]["payload"], PAGE)

    def test_unexpected_or_duplicate_slider_limits_are_rejected(self):
        for path in PAGES:
            for bad in (PAGE.replace(b"from: 20", b"from: 10"), PAGE + PAGE):
                original = {name: {"payload": PAGE} for name in PAGES}
                original[path] = {"payload": bad}
                with self.subTest(path=path, bad=bad), self.assertRaisesRegex(ValueError, "QML anchor"):
                    patch_app.brightness_controls(original)

    def stock_code(self):
        data = bytearray(patch_app.DATA_OFFSET + patch_app.DATA_SIZE)
        struct.pack_into("<I", data, 0x1A0DBC, 0x71000C3F)
        struct.pack_into("<I", data, 0x1A0E78, 0x52800280)
        struct.pack_into("<I", data, 0x1A0E44, 0x528002A0)
        struct.pack_into("<I", data, 0x1A1590, 0x7100245F)
        struct.pack_into("<I", data, 0x1A1644, 0x2A0103F3)
        return data

    def test_only_the_audited_brightness_instructions_change(self):
        data = self.stock_code()
        expected = bytearray(data)
        helper = patch_app.BRIGHTNESS_CODE_ADDRESS
        struct.pack_into("<I", expected, 0x1A0DBC, 0x14000000 | ((helper - 0x5A0DBC) // 4))
        struct.pack_into("<I", expected, 0x1A0E78, 0x2A0503E0)  # mov w0, w5
        struct.pack_into("<I", expected, 0x1A0E44, 0x110004A0)  # add w0, w5, #1
        struct.pack_into("<I", expected, 0x1A1590, 0x7100005F)  # cmp w2, #0
        struct.pack_into("<I", expected, 0x1A1644, 0x14000000 | ((helper + 0x18 - 0x5A1644) // 4))
        code = patch_app.brightness_floor_code()
        expected[patch_app.BRIGHTNESS_CODE_OFFSET:patch_app.BRIGHTNESS_CODE_OFFSET + len(code)] = code
        report = patch_app.patch_brightness(data)
        self.assertEqual(data, expected)
        self.assertEqual({item["address"] for item in report}, {"0x5a0dbc", "0x5a0e78", "0x5a0e44", "0x5a1590", "0x5a1644", hex(helper)})
        self.assertEqual(len(data), patch_app.DATA_OFFSET + patch_app.DATA_SIZE)
        self.assertLessEqual(patch_app.BRIGHTNESS_CODE_OFFSET + len(code), len(data))

    def test_unexpected_instruction_is_rejected_before_any_change(self):
        for offset in (0x1A0DBC, 0x1A0E78, 0x1A0E44, 0x1A1590, 0x1A1644):
            data = self.stock_code()
            struct.pack_into("<I", data, offset, 0xD503201F)
            before = bytes(data)
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "brightness instruction mismatch"):
                patch_app.patch_brightness(data)
            self.assertEqual(data, before)

    def test_resource_overlap_is_rejected_before_native_changes(self):
        data = self.stock_code()
        data[patch_app.BRIGHTNESS_CODE_OFFSET] = 1
        before = bytes(data)
        with self.assertRaisesRegex(ValueError, "reserved resource padding"):
            patch_app.patch_brightness(data)
        self.assertEqual(data, before)

    @unittest.skipUnless(shutil.which("node"), "Node.js checks the QML settings logic")
    def test_settings_default_bounds_persistence_and_auto_mode(self):
        source = (ROOT / "ui/BrightnessControls.qml").read_text()
        functions = []
        for name in ("boundedMinimum", "loadMinimum", "saveMinimum"):
            start = source.index("function " + name + "(")
            end = patch_app.closing_brace(source, source.index("{", start))
            functions.append(source[start:end])
        script = "\n".join(functions) + r'''
const assert = require('node:assert/strict');
let values = {}, writes = 0, minimumBrightness = 1, ready = true;
let minimumSlider = { value: 1 };
let screenManager = {isAutoBrightness:true, brightnessValue:1};
let brightnessSettings = {
  value:(k,f)=>Object.hasOwn(values,k)?values[k]:f,
  setValue:(k,v)=>{values[k]=v; writes++;}, sync:()=>{}
};
loadMinimum(); assert.equal(minimumBrightness,1); assert.equal(writes,0);
ready=false; saveMinimum(30); assert.equal(writes,0); ready=true;
saveMinimum(20); assert.equal(values.minimum_brightness,20);
assert.equal(screenManager.isAutoBrightness,true);
assert.equal(screenManager.brightnessValue,1); // Native auto controller owns fades.
saveMinimum(20); assert.equal(writes,1);
minimumBrightness=1; loadMinimum(); assert.equal(minimumSlider.value,20);
screenManager.isAutoBrightness=false;
saveMinimum(100); assert.equal(screenManager.brightnessValue,100);
saveMinimum(1); assert.equal(screenManager.brightnessValue,100);
for(const [raw,want] of [[0,1],[-10,1],[500,100],['invalid',1],[null,1],['35',35]]) {
  values.minimum_brightness=raw; loadMinimum(); assert.equal(minimumBrightness,want);
}
console.log('Brightness settings checks passed');
'''
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
