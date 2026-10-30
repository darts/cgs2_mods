#!/usr/bin/env python3
"""Build a pinned PX30 USB RAM-loader container, entirely offline.

This script has no USB, network, subprocess, or flash-writing functionality.
It does not establish compatibility with a particular physical unit.

Component provenance:
https://github.com/rockchip-linux/rkbin/tree/3968b8eb04e365e64480a6b24f776273c6708e95
Format and encoding references:
https://github.com/rockchip-linux/u-boot/blob/1c535d65b8509f388d09e49fb6961f49fda35a1d/tools/rockchip/boot_merger.c
https://github.com/rockchip-linux/rkdeveloptool/blob/master/RKBoot.h
"""

import argparse
import hashlib
import json
import struct
from pathlib import Path


RKBIN_COMMIT = "3968b8eb04e365e64480a6b24f776273c6708e95"
COMPONENTS = (
    ("px30_ddr_333MHz_v2.07.bin", 10000,
     "55f67007fe8e01efbf7892957b7f0dc7e0703c29273fb9a831835ef9f9d5daf0", 1, 1),
    ("px30_usbplug_v1.36.bin", 168580,
     "0185c9f8a32e52b7d94c23389090d2661ec6ce2f805be81ff4e49cfc640889c2", 2, 0),
)
HEADER_SIZE = 102
ENTRY_SIZE = 57
ALIGNMENT = 2048
ROCKCHIP_KEY = bytes((124, 78, 3, 4, 85, 5, 9, 7, 45, 44, 123, 56, 23, 13, 23, 17))
READ_LIMIT_PATCHES = (
    (0x9B0C, bytes.fromhex("02030054"), bytes.fromhex("1f2003d5"),
     "READ_LBA: B.HS to 0x9b6c (0xCC fill) -> NOP"),
    (0x9C0C, bytes.fromhex("c2000054"), bytes.fromhex("1f2003d5"),
     "READ_LBA prefetch: B.HS to 0x9c24 (0xCC fill) -> NOP"),
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def rockchip_crc(data):
    """BOOT container CRC: Rockchip polynomial 0x04C10DB7, initial value zero."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C10DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


def rockchip_rc4(data):
    """USB stages use one complete RC4 stream per padded component."""
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + ROCKCHIP_KEY[i % len(ROCKCHIP_KEY)]) & 255
        state[i], state[j] = state[j], state[i]
    output = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        output.append(byte ^ state[(state[i] + state[j]) & 255])
    return bytes(output)


def patch_read_limit(raw):
    """Remove only the two reviewed READ_LBA filler branches, offline.

    The caller must authenticate the complete original USB-plug component
    before calling this helper. File offsets are used, not runtime addresses.
    The original plug copies file offset 0x800 to RAM 0x03001000; its code
    consequently runs at file offset + 0x03000800. Both branches compare the
    request LBA against RAM 0x034125c4, initialized to 0x10000 sectors.
    """
    output = bytearray(raw)
    patches = []
    for offset, before, after, purpose in READ_LIMIT_PATCHES:
        if output[offset:offset + len(before)] != before:
            raise ValueError(f"Unexpected read-limit instruction at {offset:#x}")
        output[offset:offset + len(before)] = after
        patches.append({
            "file_offset": offset, "runtime_address": offset + 0x03000800,
            "original_hex": before.hex(), "patched_hex": after.hex(),
            "purpose": purpose,
        })
    expected = {offset + index
                for offset, before, after, _ in READ_LIMIT_PATCHES
                for index, (a, b) in enumerate(zip(before, after)) if a != b}
    actual = {index for index, (a, b) in enumerate(zip(raw, output)) if a != b}
    if len(output) != len(raw) or actual != expected:
        raise ValueError("USB-plug patch changed unexpected bytes")
    return bytes(output), patches


def build(components_dir, remove_read_limit=False):
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<4sHII", header, 0, b"BOOT", HEADER_SIZE, 0x0135, 0x01030000)
    # Fixed reference DDR release date makes builds reproducible.
    struct.pack_into("<HBBBBB", header, 14, 2022, 5, 31, 0, 0, 0)
    struct.pack_into("<I", header, 21, 0x50583330)  # RKPX30 -> "PX30"
    struct.pack_into("<BIB", header, 25, 1, HEADER_SIZE, ENTRY_SIZE)
    struct.pack_into("<BIB", header, 31, 1, HEADER_SIZE + ENTRY_SIZE, ENTRY_SIZE)
    # No ENTRYLOADER / FlashData / FlashBoot records.
    struct.pack_into("<BIB", header, 37, 0, HEADER_SIZE + 2 * ENTRY_SIZE, ENTRY_SIZE)
    header[43] = 0  # Unsigned container.
    header[44] = 1  # IDB RC4_OFF flag; USB stage payloads still require RC4.

    entries, payloads, manifest = [], [], []
    offset = HEADER_SIZE + 2 * ENTRY_SIZE
    for name, expected_size, expected_hash, entry_type, delay in COMPONENTS:
        raw = (components_dir / name).read_bytes()
        if len(raw) != expected_size or sha256(raw) != expected_hash:
            raise ValueError(f"Unexpected component bytes: {name}")
        patches = []
        if remove_read_limit and entry_type == 2:
            raw, patches = patch_read_limit(raw)
        padded = raw + bytes((-len(raw)) % ALIGNMENT)
        encoded = rockchip_rc4(padded)
        short_name = Path(name).stem[:19].encode("utf-16le").ljust(40, b"\0")
        entry = struct.pack("<BI40sIII", ENTRY_SIZE, entry_type, short_name,
                            offset, len(encoded), delay)
        entries.append(entry)
        payloads.append(encoded)
        manifest.append({
            "name": name, "type": entry_type, "raw_size": len(raw),
            "raw_sha256": sha256(raw), "offset": offset,
            "encoded_size": len(encoded), "encoded_sha256": sha256(encoded),
            "delay_ms": delay,
            "source": f"https://github.com/rockchip-linux/rkbin/blob/{RKBIN_COMMIT}/bin/rk33/{name}",
        })
        if patches:
            manifest[-1].update({
                "original_raw_sha256": expected_hash,
                "patches": patches,
            })
        offset += len(encoded)
    body = bytes(header) + b"".join(entries) + b"".join(payloads)
    image = body + struct.pack("<I", rockchip_crc(body))
    report = {
        "format": "Rockchip BOOT", "purpose": "USB RAM download stages only",
        "flash_loader_entries": 0,
        "size": len(image), "sha256": sha256(image), "components": manifest,
        "note": "The USB plug can service later flash commands. Omitting flash-loader "
                "entries does not make the running USB plug intrinsically read-only.",
    }
    if remove_read_limit:
        report["read_limit_removed"] = True
        report["read_limit_patch_note"] = (
            "Only two READ_LBA branches to synthetic 0xCC filler were replaced "
            "with NOPs. The underlying read function, command dispatch, writes, "
            "DDR initialization and all other raw component bytes are unchanged. "
            "This does not make the loader read-only or validate any device read."
        )
    return image, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--remove-read-limit", action="store_true",
        help="Patch the two reviewed USBplug 1.36 READ_LBA 32 MiB filler branches",
    )
    args = parser.parse_args()
    image, manifest = build(args.components, args.remove_read_limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    args.output.chmod(0o600)
    report = args.output.with_suffix(args.output.suffix + ".json")
    report.write_text(json.dumps(manifest, indent=2) + "\n")
    report.chmod(0o600)
    print(json.dumps({"output": str(args.output), "size": len(image),
                      "sha256": manifest["sha256"], "report": str(report)}))


if __name__ == "__main__":
    main()
