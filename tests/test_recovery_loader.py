"""Offline checks of the recovery loader builder, without Rockchip files in the repository."""
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_recovery_loader as loader

# The owner downloads these files; docs/recovery.md gives the expected output.
COMPONENTS = ROOT / ".private/recovery"


def synthetic_components(directory):
    ddr = bytes(range(256)) * 10
    plug = bytearray(0xA000)
    for offset, before, _, _ in loader.READ_LIMIT_PATCHES:
        plug[offset:offset + len(before)] = before
    files = {"ddr.bin": ddr, "plug.bin": bytes(plug)}
    for name, data in files.items():
        (directory / name).write_bytes(data)
    return tuple((name, len(data), hashlib.sha256(data).hexdigest(), kind, delay)
                 for (name, data), kind, delay in zip(files.items(), (1, 2), (1, 0)))


class RecoveryLoaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        context = patch.object(loader, "COMPONENTS", synthetic_components(self.directory))
        context.start()
        self.addCleanup(context.stop)

    def decoded(self, image, report, name):
        entry = next(item for item in report["components"] if item["name"] == name)
        return loader.rockchip_rc4(image[entry["offset"]:entry["offset"] + entry["encoded_size"]])[:entry["raw_size"]]

    def test_container_is_reproducible_and_has_a_valid_trailer(self):
        image, report = loader.build(self.directory)
        self.assertEqual(loader.build(self.directory)[0], image)
        self.assertEqual(image[:4], b"BOOT")
        self.assertEqual(struct.unpack("<I", image[-4:])[0], loader.rockchip_crc(image[:-4]))
        self.assertEqual(report["flash_loader_entries"], 0)
        self.assertEqual(self.decoded(image, report, "ddr.bin"), (self.directory / "ddr.bin").read_bytes())

    def test_read_limit_patch_changes_only_the_two_branches(self):
        image, report = loader.build(self.directory, remove_read_limit=True)
        original, patched = (self.directory / "plug.bin").read_bytes(), self.decoded(image, report, "plug.bin")
        changed = {index for index, (a, b) in enumerate(zip(original, patched)) if a != b}
        self.assertEqual(changed, {offset + index for offset, *_ in loader.READ_LIMIT_PATCHES for index in range(4)})
        self.assertTrue(report["read_limit_removed"])

    def test_unexpected_components_are_refused(self):
        (self.directory / "ddr.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Unexpected component bytes"):
            loader.build(self.directory)

    def test_unexpected_plug_instructions_are_refused(self):
        with self.assertRaisesRegex(ValueError, "Unexpected read-limit instruction"):
            loader.patch_read_limit(bytes(0xA000))


@unittest.skipUnless((COMPONENTS / "px30_usbplug_v1.36.bin").exists(), "needs the downloaded Rockchip files")
class PublishedLoaderTests(unittest.TestCase):
    def test_the_documented_containers_are_built(self):
        for remove, expected in ((False, "20e22d69cebeb0ce09ac6ae085a7f62672e6334886178c7d741ce28aa8292176"),
                                 (True, "2306f6b49391043eed7e734b69d4da6c715122ea309594b8e7cbbe2bce9688c8")):
            with self.subTest(remove_read_limit=remove):
                image, _ = loader.build(COMPONENTS, remove)
                self.assertEqual((len(image), hashlib.sha256(image).hexdigest()), (180444, expected))


if __name__ == "__main__":
    unittest.main()
