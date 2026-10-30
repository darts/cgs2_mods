"""Exercise resource-cache invalidation without any proprietary firmware."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_app", ROOT / "scripts/patch_app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def synthetic_app():
    data = bytearray(app.DATA_OFFSET + app.DATA_SIZE)
    names = bytearray(b"\0" * 6)  # Empty root name.
    entries = []
    for name in ("Changed.qml", "Unchanged.qml"):
        entries.append(len(names))
        names += struct.pack(">HI", len(name), 0) + name.encode("utf-16-be")
    data[app.NAME_OFFSET:app.NAME_OFFSET + len(names)] = names
    struct.pack_into(">IHIIQ", data, app.TREE_OFFSET, 0, 2, 2, 1, 0)
    offset = 0
    for index, (name_offset, payload) in enumerate(zip(entries, (b"old QML", b"retained QML")), 1):
        struct.pack_into(">IHHHIQ", data, app.TREE_OFFSET + index * 22,
                         name_offset, 0, 0, 1, offset, 1_600_000_000_000)
        struct.pack_into(">I", data, app.DATA_OFFSET + offset, len(payload))
        data[app.DATA_OFFSET + offset + 4:app.DATA_OFFSET + offset + 4 + len(payload)] = payload
        offset += 4 + len(payload)
    return bytes(data)


class ResourceCacheTests(unittest.TestCase):
    def build(self, stock, replacement):
        with patch.object(app, "STOCK_SHA256", hashlib.sha256(stock).hexdigest()), \
             patch.object(app, "CREDENTIAL_LOG_CALLS", ()), \
             patch.object(app, "CLOUD_ENTRY_PATCHES", ()), \
             patch.object(app, "NETWORK_START_CALLS", ()), \
             patch.object(app, "patch_wifi_probe", return_value={}), \
             patch.object(app, "patch_brightness", return_value=[]), \
             patch.object(app, "transform", return_value={"/Changed.qml": replacement}):
            return app.patch(stock)

    def test_changed_source_invalidates_cache_and_untouched_source_keeps_it(self):
        stock = synthetic_app()
        output, report = self.build(stock, b"new QML")
        before = app.resources(stock)
        after = app.resources(output)
        self.assertNotEqual(after["/Changed.qml"]["last_modified"], before["/Changed.qml"]["last_modified"])
        self.assertEqual(after["/Unchanged.qml"]["last_modified"], before["/Unchanged.qml"]["last_modified"])
        self.assertEqual(after["/Changed.qml"]["payload"], b"new QML")
        self.assertEqual(after["/Unchanged.qml"]["payload"], b"retained QML")
        self.assertEqual(len(report["resource_timestamps"]), 1)
        self.assertEqual(output, self.build(stock, b"new QML")[0])

    def test_later_payload_revision_invalidates_previous_patched_cache(self):
        stock = synthetic_app()
        first = app.resources(self.build(stock, b"new QML v1")[0])
        second = app.resources(self.build(stock, b"new QML v2")[0])
        self.assertNotEqual(first["/Changed.qml"]["last_modified"], second["/Changed.qml"]["last_modified"])

    def test_no_content_change_preserves_original_cache_identity(self):
        stock = synthetic_app()
        output, report = self.build(stock, b"old QML")
        self.assertEqual(app.resources(stock)["/Changed.qml"]["last_modified"],
                         app.resources(output)["/Changed.qml"]["last_modified"])
        self.assertEqual(report["resource_timestamps"], [])


class NativePrivacyTests(unittest.TestCase):
    """Retain all known network entry guards when the resource packer changes."""

    def fixture(self):
        stock = bytearray(synthetic_app())
        for address, instruction, _, _ in (*app.CLOUD_ENTRY_PATCHES, *app.NETWORK_START_CALLS):
            struct.pack_into("<I", stock, address - 0x400000, instruction)
        return stock

    def build(self, stock):
        with patch.object(app, "STOCK_SHA256", hashlib.sha256(stock).hexdigest()), \
             patch.object(app, "CREDENTIAL_LOG_CALLS", ()), \
             patch.object(app, "patch_wifi_probe", return_value={}), \
             patch.object(app, "patch_brightness", return_value=[]), \
             patch.object(app, "transform", return_value={}):
            return app.patch(stock)

    def test_known_vendor_network_paths_return_before_starting_work(self):
        stock = self.fixture()
        output, report = self.build(stock)
        self.assertEqual({p["method"] for p in report["cloud_entry_patches"]}, {
            "LocationRunner::refreshWeather", "LocationRunner::refreshAutoCity",
            "UpdateRunner::checkUpdateAuto", "DeveloperRunner::onUploadAllLogs",
            "MqttService::connectToGateway", "DatetimeRunner::syncNetworkTime",
            "HttpRunner::send", "HttpRunner::download",
            "AirmxAppRunner::refreshMqttClient", "HydraMqttRunner::refreshMqttClient",
            "JingdongAppRunner::startService", "GoogleRunner::run",
            "MqttRunner::startWork", "MqttRunner::saveHistoryData", "QMqttClient::connectToHost",
        })
        expected_code = bytearray(stock[:app.TREE_OFFSET])
        for entry in report["cloud_entry_patches"]:
            offset = int(entry["file_offset"], 16)
            self.assertEqual(struct.unpack_from("<I", output, offset)[0], 0xD65F03C0)
            struct.pack_into("<I", expected_code, offset, 0xD65F03C0)
        self.assertEqual(len(report["network_start_patches"]), 1)
        self.assertEqual(report["network_start_patches"][0]["address"], "0x5cbd54")
        struct.pack_into("<I", expected_code, 0x5CBD54 - 0x400000, 0xD503201F)
        # Sensor code outside the audited entries remains intact.
        self.assertEqual(output[:app.TREE_OFFSET], expected_code)

    def test_changed_native_entry_is_rejected(self):
        for address, _, method, _ in app.CLOUD_ENTRY_PATCHES:
            with self.subTest(method=method):
                stock = self.fixture()
                struct.pack_into("<I", stock, address - 0x400000, 0xD503201F)
                with self.assertRaisesRegex(ValueError, "cloud method entry mismatch"):
                    self.build(stock)

    def test_changed_avahi_startup_call_is_rejected(self):
        stock = self.fixture()
        struct.pack_into("<I", stock, 0x5CBD54 - 0x400000, 0xD503201F)
        with self.assertRaisesRegex(ValueError, "network startup call mismatch"):
            self.build(stock)


if __name__ == "__main__":
    unittest.main()
