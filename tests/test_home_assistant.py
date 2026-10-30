"""Host checks for Home Assistant discovery, with synthetic stock text only.

The Go tests in src/services cover discovery publishing. The device harness
(scripts/build_ui_smoke.py) renders the Local MQTT switch.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_app

GO = (ROOT / "src/services/homeassistant.go").read_text(encoding="utf-8")
LOCAL_MQTT = (ROOT / "ui/LocalMqtt.qml").read_text(encoding="utf-8")


def settings_blocks(text):
    for match in re.finditer(r"\bSettings \{", text):
        yield text[match.start():patch_app.closing_brace(text, text.index("{", match.start()))]


class HomeAssistantTests(unittest.TestCase):
    def test_home_assistant_sees_the_about_page_version(self):
        self.assertIn(f'const modVersion = "{patch_app.MOD_VERSION}"', GO)

    def test_app_and_services_helper_read_the_same_settings(self):
        switch = re.search(r'haSwitchFile\s+= "([^"]+)"', GO).group(1)
        self.assertIn(f'fileName: "{switch}"', LOCAL_MQTT)
        self.assertIn('enabled = s["homeassistant"]["discovery"] != "false"', GO)
        helper = (ROOT / "device/MqttConfig.qml").read_text()
        publisher = (ROOT / "src/services/readings.go").read_text()
        applied = re.search(r'mqttAppliedFile = "([^"]+)"', publisher).group(1)
        self.assertIn(f'fileName: "{applied}"', helper)
        self.assertIn('conf.Seconds', GO)
        self.assertNotIn('syncRealTimeData', patch_app.REALTIME_REPORTS)

    def test_new_settings_objects_declare_no_properties(self):
        # A declared property writes its default back; setting.ini is the native file.
        blocks = list(settings_blocks(patch_app.REALTIME_REPORTS))
        blocks += [b for b in settings_blocks(LOCAL_MQTT) if "cgs2-homeassistant.ini" in b]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertNotIn("property", block)

    def test_main_page_gets_the_timer_once(self):
        text = "import QtQuick 2.0\nimport Qing.Snow 1.0\n\nItem {\n    signal goToSettingPage()\n}\n"
        patched = patch_app.realtime_reports(text)
        self.assertIn("import Qing.Snow 1.0\nimport Qt.labs.settings 1.0\n", patched)
        self.assertEqual(patched.count('objectName: "currentReadings"'), 1)
        self.assertIn("cgs2SaveReadings()", patched)
        self.assertEqual(patch_app.closing_brace(patched, patched.index("Item {") + 5), len(patched.rstrip()))
        with self.assertRaisesRegex(ValueError, "expected one QML anchor"):
            patch_app.realtime_reports(text.replace("import Qing.Snow 1.0\n", ""))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for QML logic")
    def test_only_valid_current_sensor_values_enter_the_snapshot(self):
        source = patch_app.REALTIME_REPORTS
        functions = source[source.index("    function cgs2SensorValue"):source.index("    Timer {")]
        harness = r'''
const assert = require("node:assert/strict");
const AirState = {CollectingState: 2};
const sensor = {rawValue: 21.125, valueString: "70.0", state: 2, isError: false};
const airdataController = {airTEMP:sensor, airHUMI:sensor, airCO2:sensor, airPM25:sensor, airPM10:sensor, airTVOC:sensor, airNoise:sensor};
const batteryManager = {capacity: 90};
let saved, writes=0;
const cgs2Readings = {setValue(k,v){assert.equal(k,"snapshot");saved=JSON.parse(v);writes++;},sync(){}};
FUNCTIONS
assert.equal(cgs2SensorValue(sensor), 21.13);
for (const s of [null, {...sensor,isError:true}, {...sensor,state:1}, {...sensor,rawValue:NaN}])
    assert.equal(cgs2SensorValue(s), null);
cgs2SaveReadings();
assert.equal(saved.temperature,21.13); // calibrated Celsius, not the Fahrenheit display string
assert.equal(saved.battery,90);
assert.equal(Object.keys(saved).length,10);
sensor.rawValue=22;
cgs2SaveReadings();
assert.equal(saved.temperature,22);
assert.equal(writes,2);
'''.replace("FUNCTIONS", functions)
        subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)

    def test_discovery_template_ignores_legacy_history_and_marks_failed_sensors(self):
        try:
            import jinja2
        except ImportError:
            self.skipTest("Jinja2 is required for the actual Home Assistant template")
        # Read the shipped template expression, rather than a duplicate implementation.
        match = re.search(r'return "(.*)" \+ key \+ "(.*)"', GO)
        template = jinja2.Environment().from_string(match[1] + "temperature" + match[2])
        cases = [({"version":1,"temperature":22.5},"22.5"),
                 ({"version":1,"temperature":None},"None"),
                 ({"version":1,"temperature":0},"0"),
                 ({"type":17,"sensorData":[{"temperature":{"value":99}}]},""),
                 ({"type":12,"sensorData":[{"temperature":{"value":99}}]},""),
                 ({"type":13},""), ({},"")]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(template.render(value_json=payload),expected)


if __name__ == "__main__":
    unittest.main()
