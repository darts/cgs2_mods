#!/usr/bin/env python3
"""Run applicable host checks without ADB or a connected device."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def host_suite():
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    for case in cases(suite):
        cls = type(case)
        posix_only = cls.__name__ in {"BootTests", "MqttHelperTests"}
        # Keep the pure Python upgrade preparation test available on Windows.
        posix_only |= cls.__name__ == "ServicesInstallTests" and case._testMethodName != "test_prepare_writes_reviewable_worker_without_adb"
        if os.name == "nt" and posix_only:
            setattr(case, case._testMethodName, unittest.skip("POSIX shell/process fixture; covered on Linux and macOS")(getattr(case, case._testMethodName)))
    return suite


def main() -> int:
    if os.environ.get("CI"):
        required = ["node", "git"] + ([] if os.name == "nt" else ["jq", "cc"])
        missing = [name for name in required if not shutil.which(name)]
        try:
            import jinja2
        except ImportError:
            missing.append("Jinja2")
        if missing:
            raise RuntimeError("Required release checks need: " + ", ".join(missing))
    result = unittest.TextTestRunner(verbosity=2).run(host_suite())
    if not result.wasSuccessful():
        return 1
    if not shutil.which("jq") and os.name != "nt":
        print("NOTE: MQTT shell fixtures require optional jq; install jq to include them.")
    if shutil.which("node"):
        for name in ("build_ui_smoke.py", "build_services_ui_smoke.py"):
            subprocess.run([sys.executable, str(ROOT / "scripts" / name), "--check"], check=True)
    else:
        print("SKIP QML logic checks: install Node.js to include the settings-save regressions.")
    build.test_services(ROOT, race=False)
    if os.name != "nt" and not shutil.which("cc"):
        print("SKIP native filter sanitizer tests: install a C compiler to run these development checks.")
    else:
        build.test_filter(ROOT)
    print("Host checks passed; any skipped checks are listed above. No device access occurred.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
