"""Host checks for the Modern Clock screen saver patch with synthetic stock text only.

The device smoke test (scripts/build_screensaver_ui_smoke.py) renders the real pages.
"""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_app

LOADER = '''import QtQuick 2.0
import QtQuick.Layouts 1.3
import Qing.Snow 1.0

Item {
    Loader {
        id: loader
        anchors.fill: parent
    }
    Component.onCompleted: {
        switch(screenManager.screenSaver) {
        default:
            break;
        }
    }
}
'''
SETTINGS = '''import QtQuick 2.0
import QtQuick.Layouts 1.3
import Qing.Frames 1.0
import Qing.Controls 1.0
import Qing.Snow 1.0

QPage {
    id: control
    initialItem: QChildPage {
        onExited: control.exited()
        ColumnLayout {
            anchors.fill: parent
            ScreenSaverComboBox {
                Layout.fillWidth: true
                Layout.fillHeight: true
                model: ListModel {
                    ListElement {
                        text: qsTr("Analog Clock Custom")
                        value: Screen.AnalogClockCustomSaver
                    }
                }
                currentValue: screenManager.screenSaver
                onCurrentValueChanged: screenManager.screenSaver = currentValue
                onEdit: function (value) {
                    screenManager.screenSaver = value
                }
            }
        }
    }
    Component {
        id: analogClockCustom
        Item {}
    }
}
'''


def balanced(text):
    start = text.index("{", text.index("\n\n"))
    return patch_app.closing_brace(text, start) == len(text.rstrip())


def patched(loader=LOADER, settings=SETTINGS):
    original = {"/ScreenSaver/ScreenSaverPage.qml": {"payload": loader.encode()},
                "/Modules/Screen/ScreenSaverPage.qml": {"payload": settings.encode()}}
    return {path: data.decode() for path, data in patch_app.modern_screen_saver(original).items()}


class InlineComponentTests(unittest.TestCase):
    def test_missing_imports_are_added_once_and_versions_must_match(self):
        host = "import QtQuick 2.0\nimport Qing.Snow 1.0\n\nItem {}\n"
        source = "import QtQuick 2.0\nimport Qt.labs.settings 1.0\n\n// note\nRectangle { id: inner }\n"
        result, component = patch_app.inline_component(host, source, "embedded")
        self.assertEqual(re.findall(r"^import .+$", result, re.M),
                         ["import QtQuick 2.0", "import Qing.Snow 1.0", "import Qt.labs.settings 1.0"])
        self.assertIn("id: embedded", component)
        self.assertNotIn("import", component)
        self.assertEqual(component.count("Rectangle { id: inner }"), 1)
        with self.assertRaisesRegex(ValueError, "differs from its host"):
            patch_app.inline_component(host, "import QtQuick 2.12\nItem {}\n", "embedded")


class ModernScreenSaverPatchTests(unittest.TestCase):
    def test_loader_checks_the_modern_choice_before_the_native_setting(self):
        text = patched()["/ScreenSaver/ScreenSaverPage.qml"]
        self.assertIn("import QtQuick 2.12\n", text)
        self.assertIn("import Qt.labs.settings 1.0\n", text)
        self.assertEqual(text.count("id: cgs2ModernSaver"), 1)
        self.assertLess(text.index("if (cgs2ModernChosen()) {"), text.index("switch(screenManager.screenSaver)"))
        self.assertIn("loader.sourceComponent = cgs2ModernSaver;", text)
        self.assertTrue(balanced(text))

    def test_settings_page_adds_a_first_choice_and_keeps_the_native_value(self):
        text = patched()["/Modules/Screen/ScreenSaverPage.qml"]
        first = text.index('text: "Modern Clock"')
        self.assertLess(first, text.index('qsTr("Analog Clock Custom")'))
        self.assertIn("value: -1", text[first:first + 80])
        self.assertIn("if (currentValue !== -1) screenManager.screenSaver = currentValue;", text)
        self.assertIn("control.mainStackView.push(cgs2ModernOptions);", text)
        self.assertEqual(text.count("id: cgs2ModernOptions"), 1)
        self.assertIn("Layout.minimumHeight: 380", text)
        flickable = text.index("QFlickable {")
        self.assertLess(text.index("ColumnLayout {"), patch_app.closing_brace(text, text.index("{", flickable)))
        self.assertGreater(text.index("ColumnLayout {"), flickable)
        self.assertTrue(balanced(text))

    def test_an_unexpected_stock_page_is_refused(self):
        for loader, settings in ((LOADER.replace("switch(screenManager", "switch (screenManager"), SETTINGS),
                                 (LOADER, SETTINGS.replace("onEdit: function (value) {", "onEdit: {"))):
            with self.subTest(), self.assertRaisesRegex(ValueError, "expected one QML anchor"):
                patched(loader, settings)

    def test_saver_and_options_agree_on_keys_and_defaults(self):
        saver = (ROOT / "ui/ModernScreenSaver.qml").read_text(encoding="utf-8")
        options = (ROOT / "ui/ModernScreenSaverOptions.qml").read_text(encoding="utf-8")
        saver_defaults = {key: value == "true" for key, value in re.findall(r'\{key: "(\w+)".*?on: (true|false)\}', saver)}
        saver_defaults["weather"] = 'flag("weather", true)' in saver
        option_defaults = {key: value == "true" for key, value in re.findall(r'flag\("(\w+)", (true|false)\)', options)}
        self.assertEqual(len(saver_defaults), 8)
        self.assertEqual(saver_defaults, option_defaults)
        self.assertEqual(set(re.findall(r'store\("(\w+)"', options)), set(option_defaults))

    def test_settings_objects_declare_no_properties(self):
        # Declared properties write their defaults back when the file lacks them.
        for name in ("ui/ModernScreenSaver.qml", "ui/ModernScreenSaverOptions.qml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for match in re.finditer(r"\bSettings \{", text):
                block = text[match.start():patch_app.closing_brace(text, text.index("{", match.start()))]
                with self.subTest(name=name):
                    self.assertNotIn("property", block)
                    self.assertIn('fileName: "/data/etc/cgs2-screensaver.ini"', block)
        self.assertNotIn("property", patch_app.MODERN_CHOICE)


if __name__ == "__main__":
    unittest.main()
