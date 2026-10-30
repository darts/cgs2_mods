"""Install a pinned, checksum-verified ADB locally. No device access."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile


def adb_name() -> str:
    return "adb.exe" if os.name == "nt" else "adb"


def find_adb(repo: Path, explicit: str | None = None) -> str | None:
    requested = explicit or os.environ.get("CGS2_ADB")
    if requested:
        found = shutil.which(requested)
        if not found and Path(requested).is_file():
            found = str(Path(requested).resolve())
        if not found:
            raise ValueError("The specified ADB executable was not found.")
        return found
    local = repo / ".tools/platform-tools" / adb_name()
    return str(local) if local.is_file() else shutil.which(adb_name())


def extract_tools(archive: Path, destination: Path) -> None:
    """Extract only regular entries below platform-tools, never links or escapes."""
    with zipfile.ZipFile(archive) as packed:
        seen: set[str] = set()
        total = 0
        for member in packed.infolist():
            name = PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            normalized = str(name).casefold().rstrip("/")
            if (name.is_absolute() or ".." in name.parts or "\\" in member.filename
                    or ":" in member.filename or not name.parts
                    or name.parts[0] != "platform-tools" or normalized in seen
                    or stat.S_ISLNK(mode)):
                raise ValueError("Unsafe entry in Platform Tools archive")
            seen.add(normalized)
            total += member.file_size
            if total > 150 * 1024 * 1024:
                raise ValueError("Platform Tools archive exceeds extraction limit")
            target = destination.joinpath(*name.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with packed.open(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if mode & 0o111 else 0o644)


def install_adb(repo: Path, explicit: str | None = None) -> str:
    found = find_adb(repo, explicit)
    if found:
        subprocess.run([found, "version"], check=True)
        print("Using existing ADB. No download needed.")
        return found
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine not in {"x86_64", "amd64"}:
        raise ValueError("On Linux ARM, install your distribution's adb package, then run setup again.")
    lock = json.loads((repo / "config/tool-downloads.json").read_text(encoding="utf-8"))["platform_tools"]
    if system not in lock["archives"]:
        raise ValueError("Automatic ADB setup supports Linux, macOS and Windows.")
    item = lock["archives"][system]
    if not item["url"].startswith("https://dl.google.com/android/repository/"):
        raise ValueError("ADB download must use Google's official repository")
    tools = repo / ".tools"
    tools.mkdir(exist_ok=True)
    target = tools / "platform-tools"
    if target.exists() or target.is_symlink():
        raise ValueError(".tools/platform-tools exists but has no usable ADB. Inspect it before retrying.")
    print(f"Downloading Google Platform Tools {lock['version']} for {system}.")
    print(f"Google SDK terms: {lock['license']}")
    with tempfile.TemporaryDirectory(prefix="platform-tools-", dir=tools) as temporary:
        folder = Path(temporary)
        archive = folder / "download.zip"
        digest = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(item["url"], timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > item["size"]:
                    raise ValueError("ADB download exceeds its pinned size")
                digest.update(chunk)
                output.write(chunk)
        if count != item["size"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("ADB checksum mismatch; nothing was installed")
        extract_tools(archive, folder)
        executable = folder / "platform-tools" / adb_name()
        if not executable.is_file():
            raise ValueError("Verified archive did not contain ADB")
        subprocess.run([str(executable), "version"], check=True)
        (folder / "platform-tools").rename(target)
    print("ADB installed in .tools/platform-tools. System settings and USB drivers were not changed.")
    return str(target / adb_name())
