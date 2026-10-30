#!/usr/bin/env python3
"""Build pinned rkdeveloptool on macOS without running it or accessing USB.

Requires an existing clean Rockchip source checkout, Apple clang++, and libusb.
This script downloads and installs nothing. The resulting upstream utility
contains both read and write commands; building it grants no device permission.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


COMMIT = "304f073752fd25c854e1bcf05d8e7f925b1f4e14"
SOURCE_URL = "https://github.com/rockchip-linux/rkdeveloptool"
CPP_FILES = (
    "RKBoot.cpp", "RKComm.cpp", "RKDevice.cpp", "RKImage.cpp",
    "RKLog.cpp", "RKScan.cpp", "crc.cpp", "main.cpp",
)
CONFIG = '#define PACKAGE_VERSION "1.32-cgs2-304f073"\n'


def checked_output(command):
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(".tools/rkdeveloptool"))
    parser.add_argument("--output-dir", type=Path, default=Path(".tools/rkdeveloptool-repro"))
    parser.add_argument("--libusb-prefix", type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This build recipe has only been verified on macOS.")
    source = args.source.resolve()
    output = args.output_dir.resolve()
    if output == source or source in output.parents:
        parser.error("Keep generated files outside the pinned source checkout.")
    if checked_output(["git", "-C", str(source), "rev-parse", "HEAD"]) != COMMIT:
        parser.error(f"Source must be checked out at {COMMIT}.")
    if checked_output(["git", "-C", str(source), "status", "--porcelain",
                       "--untracked-files=all"]):
        parser.error("Source checkout must be clean, including untracked files.")
    if sorted(p.name for p in source.glob("*.cpp")) != list(CPP_FILES):
        parser.error("Unexpected top-level C++ source files.")

    prefix = args.libusb_prefix
    if prefix is None:
        brew = shutil.which("brew")
        if brew is None:
            parser.error("Supply --libusb-prefix or make Homebrew available.")
        prefix = Path(checked_output([brew, "--prefix", "libusb"]))
    prefix = prefix.absolute()
    if not (prefix / "include/libusb-1.0/libusb.h").is_file():
        parser.error(f"Missing libusb header beneath {prefix}.")
    library = prefix / "lib/libusb-1.0.dylib"
    if not library.is_file():
        parser.error(f"Missing libusb dynamic library beneath {prefix}.")
    binary = output / "rkdeveloptool"
    if binary.exists() or binary.is_symlink():
        parser.error("Output binary already exists; choose a fresh output directory.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "config.h").write_text(CONFIG)
    command = [
        "/usr/bin/clang++", "-std=c++11", "-O2", "-fno-strict-aliasing",
        "-D_FILE_OFFSET_BITS=64", "-D_LARGE_FILE",
        "-I" + str(output), "-I" + str(source),
        "-I" + str(prefix / "include/libusb-1.0"),
        *(str(source / name) for name in CPP_FILES),
        "-L" + str(prefix / "lib"), "-lusb-1.0", "-liconv",
        "-o", str(binary),
    ]
    subprocess.run(command, check=True)
    report = {
        "source": SOURCE_URL, "commit": COMMIT, "command": command,
        "compiler": checked_output(["/usr/bin/clang++", "--version"]),
        "libusb_library": str(library.resolve()),
        "libusb_sha256": sha256(library), "binary_sha256": sha256(binary),
        "source_sha256": {name: sha256(source / name) for name in CPP_FILES},
        "binary_executed": False, "usb_operations": 0,
    }
    (output / "build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"binary": str(binary), "sha256": report["binary_sha256"]}))


if __name__ == "__main__":
    main()
