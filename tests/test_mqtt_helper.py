"""Offline MQTT transaction tests; all process and policy operations are mocked."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BROKER = {"ok": True, "enabled": True, "host": "192.0.2.4", "port": 1883}
DISABLED = {"ok": True, "enabled": False, "host": "", "port": 0}
OTHER_PERMISSIONS = ["udp:203.0.113.123:123", "tcp:203.0.113.20:443",
                     "udp:9.9.9.9:53", "tcp:9.9.9.9:53"]
OLD_BROKER = "tcp:192.0.2.99:1883"


@unittest.skipUnless(shutil.which("jq"), "jq is required for shell integration tests")
class MqttHelperTests(unittest.TestCase):
    def run_helper(self, result, *, worker=False, reject_grant=False, reject_disable=False,
                   apply_result=None, app_running=False, app_wont_stop=False, existing_lock=False,
                   validation_property="valid", concurrent_lease=None, missing_controller=False,
                   validation_mode_mismatch=False, duplicate_result=False):
        with tempfile.TemporaryDirectory(prefix="cgs2-helper-test-") as temporary:
            root = Path(temporary)
            for directory in ("etc/init.d", "etc/cgs2-privacy", "usr/local/sbin", "usr/local/share/cgs2-privacy",
                              "data/etc", "tmp", "run", "qingping/bin", "mocks", "dev"):
                (root / directory).mkdir(parents=True, exist_ok=True)
            for name in ("data/etc/cgs2-mqtt.ini", "data/etc/cgs2-mqtt-applied.ini", "data/etc/setting.ini", "run/cgs2-privacy.ready"):
                (root / name).touch()
            if app_running or app_wont_stop:
                (root / "app-running").touch()
            if existing_lock:
                (root / "run/cgs2-mqtt-config.lock").mkdir()
                (root / "run/cgs2-mqtt-config.lock/owner").write_text("other worker")
            # A stock profile can reference optional unset environment variables.
            (root / "etc/profile").write_text('test -z "$CGS2_TEST_UNSET_PROFILE_OPTION"\n' +
                                               f"touch '{root}/profile-loaded'\n")
            policy = root / "etc/cgs2-privacy/allowlist.conf"
            policy.write_text(OLD_BROKER + "\n")
            policy.chmod(0o600)
            (root / "leases.json").write_text(json.dumps(OTHER_PERMISSIONS))
            (root / "runtime.json").write_text(json.dumps(sorted(OTHER_PERMISSIONS + [OLD_BROKER])))
            qml = (ROOT / "device/MqttConfig.qml").read_text()
            declaration = "    property bool validateOnly: false"
            if validation_property == "missing":
                qml = qml.replace(declaration, "")
            elif validation_property == "duplicate":
                qml = qml.replace(declaration, declaration + "\n" + declaration)
            (root / "usr/local/share/cgs2-privacy/MqttConfig.qml").write_text(qml)

            def executable(name, text, *, python=False):
                path = root / name
                path.write_text(("#!" + sys.executable if python else "#!/bin/sh") + "\n" + text)
                path.chmod(0o755)

            # Every command that could affect the host is replaced. The event
            # log makes app shutdown ordering and accidental network calls visible.
            for name in ("pkill", "killall", "ifconfig", "sync", "sleep"):
                executable("mocks/" + name, f'''printf '%s\\n' '{name}'" $*" >> '{root}/events'
if [ '{name}' = pkill ] && [ "$*" = '-x QingSnow2App' ] && [ "$CGS2_TEST_APP_STUCK" != 1 ]; then
    rm -f '{root}/app-running'
fi
exit 0
''')
            executable("mocks/pgrep", f"test -f '{root}/app-running'\n")
            executable("mocks/timeout", 'shift\nexec "$@"\n')
            executable("mocks/setsid", f"touch '{root}/setsid-called'\n" + 'exec "$@"\n')
            executable("mocks/qmlscene", f'''
import json, os
from pathlib import Path
import sys
root = Path({str(root)!r})
validate = "    property bool validateOnly: true" in Path(sys.argv[1]).read_text().splitlines()
with (root / "events").open("a") as stream:
    stream.write("qml-validate\\n" if validate else "qml-apply\\n")
if not validate:
    (root / "native-settings-written").touch()
    extra = os.environ.get("CGS2_TEST_CONCURRENT_LEASE", "")
    if extra:
        leases = json.loads((root / "leases.json").read_text())
        (root / "leases.json").write_text(json.dumps(leases + [extra]))
result = json.loads(os.environ["CGS2_TEST_RESULT" if validate else "CGS2_TEST_APPLY_RESULT"])
result["validationOnly"] = validate and os.environ["CGS2_TEST_MODE_MISMATCH"] != "1"
print("qml: CGS2_RESULT " + json.dumps(result))
if validate and os.environ["CGS2_TEST_DUPLICATE_RESULT"] == "1":
    print("qml: CGS2_RESULT " + json.dumps(result))
''', python=True)
            executable("qingping/bin/watchdog.sh", f"touch '{root}/watchdog-started'\n")
            executable("usr/local/sbin/cgs2-filter", f"touch '{root}/forbidden-filter-call'\nexit 99\n")
            executable("etc/init.d/S02privacy", f"touch '{root}/forbidden-privacy-call'\nexit 99\n")
            if not missing_controller:
                executable("usr/local/sbin/cgs2-services", f'''
import json, os
from pathlib import Path
import sys
root = Path({str(root)!r})
args = sys.argv[1:]
assert (root / "run/cgs2-mqtt-config.lock").is_dir(), "MQTT transaction lock missing"
assert not (root / "run/cgs2-services-config.lock").is_dir(), "global lock must belong to controller"
assert args[:1] == ["mqtt-policy"] and args[1] in ("disable", "set"), args
with (root / "events").open("a") as stream:
    stream.write("controller " + " ".join(args[1:]) + "\\n")
with (root / "controller-calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[1] == "set" and os.environ["CGS2_TEST_REJECT_GRANT"] == "1":
    sys.exit(3)
if args[1] == "disable" and os.environ["CGS2_TEST_REJECT_DISABLE"] == "1":
    sys.exit(4)
broker = [] if args[1] == "disable" else ["tcp:" + args[2] + ":" + args[3]]
policy = root / "etc/cgs2-privacy/allowlist.conf"
policy.write_text("" if not broker else broker[0] + "\\n")
policy.chmod(0o600)
leases = json.loads((root / "leases.json").read_text())
(root / "runtime.json").write_text(json.dumps(sorted(set(leases + broker))))
''', python=True)
            source = (ROOT / "device/cgs2-mqtt-config").read_text()
            # Rewrite known absolute paths in one pass, never nesting rewrites.
            source = re.sub(r"/(?:etc|usr/local|data|tmp|run|qingping|sys/class/net)/",
                            lambda match: str(root) + match.group(), source)
            source = source.replace("/dev/watchdog", str(root / "dev/watchdog"))
            helper = root / "helper"
            helper.write_text(source)
            helper.chmod(0o755)
            environment = dict(os.environ, PATH=str(root / "mocks") + os.pathsep + os.environ["PATH"],
                               CGS2_TEST_RESULT=json.dumps(result),
                               CGS2_TEST_APPLY_RESULT=json.dumps(apply_result if apply_result is not None else result),
                               CGS2_TEST_REJECT_GRANT=str(int(reject_grant)),
                               CGS2_TEST_REJECT_DISABLE=str(int(reject_disable)),
                               CGS2_TEST_APP_STUCK=str(int(app_wont_stop)),
                               CGS2_TEST_CONCURRENT_LEASE=concurrent_lease or "",
                               CGS2_TEST_MODE_MISMATCH=str(int(validation_mode_mismatch)),
                               CGS2_TEST_DUPLICATE_RESULT=str(int(duplicate_result)))
            environment.pop("CGS2_TEST_UNSET_PROFILE_OPTION", None)
            run = subprocess.run(["/bin/sh", str(helper), "worker" if worker else "initialize"],
                                 capture_output=True, text=True, env=environment, timeout=15)
            events = (root / "events").read_text().splitlines() if (root / "events").exists() else []
            if worker and any(event.startswith("pkill") for event in events):
                deadline = time.monotonic() + 2
                while not (root / "watchdog-started").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            status_path = root / "tmp/cgs2-mqtt-status.json"
            calls_path = root / "controller-calls.jsonl"
            diagnostics = root / "tmp/cgs2-mqtt-helper.log"
            return {"returncode": run.returncode, "policy": policy.read_text(), "mode": policy.stat().st_mode & 0o777,
                    "status": json.loads(status_path.read_text()), "ready": (root / "run/cgs2-privacy.ready").exists(),
                    "restarted": (root / "watchdog-started").exists(), "lock": (root / "run/cgs2-mqtt-config.lock").exists(),
                    "profile_loaded": (root / "profile-loaded").exists(), "detached": (root / "setsid-called").exists(),
                    "watchdog": (root / "dev/watchdog").read_text() if (root / "dev/watchdog").exists() else None,
                    "app_running": (root / "app-running").exists(),
                    "native_written": (root / "native-settings-written").exists(),
                    "runtime": json.loads((root / "runtime.json").read_text()),
                    "leases": json.loads((root / "leases.json").read_text()),
                    "calls": [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else [],
                    "diagnostic_mode": diagnostics.stat().st_mode & 0o777 if diagnostics.exists() else None,
                    "forbidden": any((root / name).exists() for name in ("forbidden-filter-call", "forbidden-privacy-call")),
                    "events": events, "output": run.stdout + run.stderr}

    def assert_services_preserved(self, result):
        for permission in OTHER_PERMISSIONS:
            self.assertIn(permission, result["runtime"])
            self.assertIn(permission, result["leases"])
        self.assertTrue(result["ready"])
        self.assertFalse(result["forbidden"])
        self.assertFalse(any(event.startswith("ifconfig") for event in result["events"]))
        for event in result["events"]:
            if event.startswith("pkill") or event.startswith("killall"):
                self.assertNotRegex(event, r"adbd|wpa_supplicant|dhcpcd|ntpd|miio")

    def test_explicit_broker_preserves_other_services_and_uses_controller(self):
        result = self.run_helper(BROKER)
        self.assertEqual(result["returncode"], 0, result["output"])
        self.assertEqual(result["policy"], "tcp:192.0.2.4:1883\n")
        self.assertEqual(result["mode"], 0o600)
        self.assertEqual(result["calls"], [["mqtt-policy", "disable"], ["mqtt-policy", "set", "192.0.2.4", "1883"]])
        self.assertTrue(result["status"]["ok"])
        self.assertFalse(result["lock"])
        self.assertEqual(result["watchdog"], "V\n")
        self.assertEqual(result["diagnostic_mode"], 0o600)
        self.assert_services_preserved(result)

    def test_disable_revokes_only_mqtt(self):
        result = self.run_helper(DISABLED)
        self.assertEqual(result["returncode"], 0, result["output"])
        self.assertEqual(result["policy"], "")
        self.assertEqual(result["runtime"], sorted(OTHER_PERMISSIONS))
        self.assertNotIn("all servers", result["status"]["message"].lower())
        self.assert_services_preserved(result)

    def test_worker_validates_then_revokes_then_stops_and_restarts(self):
        result = self.run_helper(BROKER, worker=True, app_running=True)
        self.assertEqual(result["returncode"], 0, result["output"])
        events = result["events"]
        self.assertLess(events.index("qml-validate"), events.index("controller disable"))
        self.assertLess(events.index("controller disable"), events.index("pkill -x QingSnow2App"))
        self.assertLess(events.index("pkill -x QingSnow2App"), events.index("qml-apply"))
        self.assertTrue(result["profile_loaded"])
        self.assertTrue(result["detached"])
        self.assertTrue(result["restarted"])
        self.assertFalse(result["lock"])
        self.assertEqual(result["watchdog"], "V\n")
        self.assert_services_preserved(result)

    def test_grant_failure_revokes_only_mqtt_and_restarts_display(self):
        result = self.run_helper(BROKER, worker=True, reject_grant=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["policy"], "")
        self.assertEqual(result["runtime"], sorted(OTHER_PERMISSIONS))
        self.assertTrue(result["restarted"])
        self.assertFalse(result["status"]["ok"])
        self.assertFalse(result["lock"])
        self.assertEqual(result["calls"][-1], ["mqtt-policy", "disable"])
        self.assertEqual(result["watchdog"], "V\n")
        self.assert_services_preserved(result)

    def test_invalid_draft_preserves_previous_mqtt_and_running_display(self):
        result = self.run_helper({"ok": False, "message": "Invalid MQTT field."}, worker=True, app_running=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["policy"], OLD_BROKER + "\n")
        self.assertEqual(result["calls"], [])
        self.assertTrue(result["app_running"])
        self.assertFalse(result["native_written"])
        self.assertFalse(result["restarted"])
        self.assertIsNone(result["watchdog"])
        self.assertFalse(result["status"]["ok"])
        self.assert_services_preserved(result)

    def test_changed_or_invalid_draft_during_apply_revokes_only_mqtt(self):
        result = self.run_helper(BROKER, worker=True, apply_result={"ok": False, "message": "Invalid field."})
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["calls"], [["mqtt-policy", "disable"], ["mqtt-policy", "disable"]])
        self.assertEqual(result["policy"], "")
        self.assertTrue(result["restarted"])
        self.assert_services_preserved(result)

    def test_failed_initial_revocation_never_stops_display_or_mutates_native_settings(self):
        result = self.run_helper(BROKER, worker=True, app_running=True, reject_disable=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertTrue(result["app_running"])
        self.assertFalse(result["native_written"])
        self.assertFalse(result["restarted"])
        self.assertIsNone(result["watchdog"])
        self.assertEqual(result["policy"], OLD_BROKER + "\n")
        self.assertIn("confirm MQTT revocation", result["status"]["message"])
        self.assert_services_preserved(result)

    def test_concurrent_service_update_and_shared_endpoint_survive_mqtt_disable(self):
        result = self.run_helper(DISABLED, worker=True, concurrent_lease=OLD_BROKER,
                                 apply_result={"ok": False, "message": "Changed draft."})
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["policy"], "")
        self.assertIn(OLD_BROKER, result["runtime"])
        self.assert_services_preserved(result)

    def test_initialize_refuses_live_app_without_changing_permissions(self):
        result = self.run_helper(DISABLED, app_running=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertTrue(result["app_running"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["events"], [])
        self.assert_services_preserved(result)

    def test_existing_mqtt_lock_is_preserved_and_no_work_runs(self):
        result = self.run_helper(BROKER, existing_lock=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertTrue(result["lock"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["events"], [])
        self.assert_services_preserved(result)

    def test_validation_copy_requires_exactly_one_property(self):
        for property_state in ("missing", "duplicate"):
            with self.subTest(property_state=property_state):
                result = self.run_helper(BROKER, worker=True, app_running=True, validation_property=property_state)
                self.assertNotEqual(result["returncode"], 0)
                self.assertTrue(result["app_running"])
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["events"], [])
                self.assert_services_preserved(result)

    def test_validation_mode_mismatch_or_duplicate_output_cannot_grant_or_stop_app(self):
        for option in ("validation_mode_mismatch", "duplicate_result"):
            with self.subTest(option=option):
                result = self.run_helper(BROKER, worker=True, app_running=True, **{option: True})
                self.assertNotEqual(result["returncode"], 0)
                self.assertTrue(result["app_running"])
                self.assertEqual(result["calls"], [])
                self.assert_services_preserved(result)

    def test_missing_controller_does_not_fall_back_to_global_filter(self):
        result = self.run_helper(BROKER, worker=True, app_running=True, missing_controller=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertTrue(result["app_running"])
        self.assertEqual(result["calls"], [])
        self.assert_services_preserved(result)

    def test_stuck_app_is_not_magic_closed_or_given_new_settings(self):
        result = self.run_helper(BROKER, worker=True, app_wont_stop=True)
        self.assertNotEqual(result["returncode"], 0)
        self.assertIsNone(result["watchdog"])
        self.assertFalse(result["native_written"])
        self.assertEqual(result["policy"], "")
        self.assert_services_preserved(result)


@unittest.skipUnless(shutil.which("node"), "Node is required to exercise QML JavaScript validation")
class MqttQmlTests(unittest.TestCase):
    def run_settings(self, *, validate_only, values=None):
        source = (ROOT / "device/MqttConfig.qml").read_text()
        body = re.search(r"    function run\(\) \{(.*)\n    \}\n    Timer", source, re.S).group(1)
        settings = dict(enabled=True, host="192.0.2.4", port="1883", username="private-user",
                        password="private-password", client_id="local", pub_topic="test/up", sub_topic="test/down")
        settings.update(values or {})
        harness = """
const validateOnly = INPUT.validate;
const writes = [], messages = [];
function settings(name, values = {}) {
    return {
        value(key, fallback) { return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : fallback; },
        setValue(key, value) { writes.push([name, key, value]); values[key] = value; },
        sync() { writes.push([name, 'sync']); }
    };
}
const applied = settings('applied');
const staged = settings('staged', INPUT.values), third = settings('third'), vendor = settings('vendor');
const app = settings('app'), clock = settings('clock'), location = settings('location'), pairing = settings('pairing');
const console = {log(message) {messages.push(message);}}, Qt = {quit() {}};
function run() {BODY}
run();
process.stdout.write(JSON.stringify({writes, messages}));
""".replace("INPUT", json.dumps({"validate": validate_only, "values": settings})).replace("BODY", body)
        result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_validation_reads_valid_draft_without_native_or_staged_writes(self):
        result = self.run_settings(validate_only=True)
        self.assertEqual(result["writes"], [])
        report = json.loads(result["messages"][0].removeprefix("CGS2_RESULT "))
        self.assertTrue(report["ok"])
        self.assertTrue(report["validationOnly"])
        self.assertNotIn("private-user", "".join(result["messages"]))
        self.assertNotIn("private-password", "".join(result["messages"]))

    def test_validation_rejects_bad_draft_without_writes(self):
        # Reject incomplete credentials and unusable endpoints before any writes.
        for values in ({"host": "broker.example"}, {"password": "bad\nsecret"}, {"pub_topic": "invalid/#"},
                       {"username": ""}, {"password": ""}, {"host": "169.254.1.2"}):
            with self.subTest(values=values):
                result = self.run_settings(validate_only=True, values=values)
                self.assertEqual(result["writes"], [])
                report = json.loads(result["messages"][0].removeprefix("CGS2_RESULT "))
                self.assertFalse(report["ok"])

    def test_disabled_mqtt_needs_no_credentials(self):
        result = self.run_settings(validate_only=True, values={"enabled": False, "host": "", "username": "", "password": ""})
        report = json.loads(result["messages"][0].removeprefix("CGS2_RESULT "))
        self.assertTrue(report["ok"])

    def test_tls_is_applied_only_to_the_local_publisher(self):
        # Native settings remain empty regardless of the local TLS choice.
        for staged, expected in ((True, 1), ("true", 1), (False, 0), ("false", 0)):
            with self.subTest(staged=staged):
                writes = self.run_settings(validate_only=False, values={"tls": staged})["writes"]
                self.assertIn(["applied", "tls", bool(expected)], writes)
                self.assertIn(["third", "tls", 0], writes)
                self.assertIn(["vendor", "tls", 0], writes)

    def test_apply_preserves_new_services_by_only_disabling_vendor_time_flags(self):
        result = self.run_settings(validate_only=False)
        self.assertIn(["applied", "host", "192.0.2.4"], result["writes"])
        self.assertIn(["third", "host", ""], result["writes"])
        self.assertIn(["vendor", "host", ""], result["writes"])
        self.assertIn(["clock", "is_automatically", False], result["writes"])
        self.assertIn(["clock", "timezone_auto", False], result["writes"])
        report = json.loads(result["messages"][0].removeprefix("CGS2_RESULT "))
        self.assertTrue(report["ok"])
        self.assertFalse(report["validationOnly"])


if __name__ == "__main__":
    unittest.main()
