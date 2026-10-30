"""Audit files that a source checkout can publish; never open ignored backups."""
import configparser
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Deliberate synthetic fixtures, scoped to their exact test files.
FIXTURES = {
    "src/services/homeassistant_test.go": {"58:2d:34:00:00:01"},
    "tests/test_services_install.py": {"12345678-1234-1234-1234-123456789abc"},
    "tests/test_release_files.py": {"58:2d:34:00:00:01", "12345678-1234-1234-1234-123456789abc"},
}


def audit_text(name, data):
    text = data.decode("utf-8")
    if "\x00" in text:
        raise ValueError("binary content")
    patterns = {
        "personal computer path": r"/(?:Users|home)/[A-Za-z0-9_.-]+/",
        "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        "access token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16})\b",
        "monitor serial": r"\bMSNS2D[0-9]{8,}\b",
        "hardware address": r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b",
        "device identifier": r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b",
    }
    for category, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            if match[0] not in FIXTURES.get(name, set()):
                line = text.count("\n", 0, match.start()) + 1
                raise ValueError(f"line {line}: possible {category}")
    if name == "device/allowlist.conf" and any(line.strip() and not line.lstrip().startswith("#") for line in text.splitlines()):
        raise ValueError("the public firewall template must allow no server")
    if name == "device/cgs2-mqtt.ini":
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        expected = dict(enabled="false",host="",port="1883",username="",password="",client_id="",pub_topic="",tls="false",report_seconds="60")
        if parser.sections() != ["mqtt"] or parser.defaults() or dict(parser["mqtt"]) != expected:
            raise ValueError("the public MQTT template must have empty credentials and disabled MQTT")


class ReleaseFilesTests(unittest.TestCase):
    def test_git_candidate_contains_only_public_source(self):
        if not shutil.which("git") or not (ROOT / ".git").exists():
            self.skipTest("source archive; run this check in the release Git checkout")
        listing = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT)
        for name in sorted(set(listing.decode().split("\0")) - {""}):
            with self.subTest(file=name):
                self.assertFalse(name.startswith((".private/", ".tools/", "build/", "dist/", "firmware/", "log/", "logs/")), "private/generated file is tracked")
                file = ROOT / name
                self.assertFalse(file.is_symlink(), "source symlinks need a separate review")
                if name.startswith("docs/images/") and file.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    continue
                self.assertLess(file.stat().st_size, 2*1024*1024, "unexpected large source file")
                audit_text(name, file.read_bytes())

    def test_identity_exceptions_apply_only_to_explicit_fixtures(self):
        address = next(iter(FIXTURES["src/services/homeassistant_test.go"]))
        audit_text("src/services/homeassistant_test.go", address.encode())
        for name in ("readme.md", "ui/LocalMqtt.qml", "scripts/new.py"):
            with self.assertRaisesRegex(ValueError, "hardware address"):
                audit_text(name,address.encode())
