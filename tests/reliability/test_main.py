"""Reliability, main()-loop scenarios (boot 401/429, gateway 4004, REST
401 mid-run, the heartbeat, a failing outbox, identity pending, a
same-batch follow-up), each in its own process (run_main.py); they all
start at once."""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# Each stops once it has seen enough (run_main.py); this alarm only
# bounds a failing one.
SECS = 20
SCENARIOS = ["boot401", "boot429", "gw4004", "rest401", "phases",
             "identity_pending", "ready_identity", "batch_fold"]
_procs = {}
_results = {}


def setUpModule():
    for name in SCENARIOS:
        _procs[name] = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "run_main.py"), name,
             str(SECS)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=os.environ.copy())


def tearDownModule():
    for p in _procs.values():
        if p.poll() is None:
            p.kill()
            p.communicate()


def run(scenario):
    if scenario not in _results:
        out, err = _procs[scenario].communicate(timeout=60)
        last = (out.strip().splitlines() or ["{}"])[-1]
        try:
            _results[scenario] = json.loads(last)
        except ValueError:
            raise AssertionError(out + err)
    return _results[scenario]


class Main(unittest.TestCase):
    def test_boot_401_exits_with_marker(self):
        r = run("boot401")
        self.assertEqual(r["exit"], 3, r["out"])
        self.assertIn("TOKEN REJECTED", r["out"])   # the watchdog's marker
        self.assertNotIn("Bot user id", r["out"])

    def test_boot_429_prints_pending_not_identity_marker(self):
        r = run("boot429")
        self.assertIn("Bot identity pending", r["out"])
        self.assertNotIn("Bot user id", r["out"])

    def test_gateway_4004_exits_main_loop(self):
        r = run("gw4004")
        self.assertEqual(r["exit"], 3, r["out"])
        self.assertIn("TOKEN REJECTED", r["out"])

    def test_rest_401_mid_run_exits(self):
        r = run("rest401")
        self.assertEqual(r["exit"], 3, r["out"])

    def test_failing_outbox_does_not_skip_cleanup(self):
        r = run("phases")
        self.assertGreater(r["calls"]["sweep_media"], 0, r["out"])
        self.assertTrue(r["heartbeat"])

    def test_identity_pending(self):
        # No DM poll without an identity; READY supplies one.
        r = run("identity_pending")
        self.assertEqual(r["calls"]["list_dm_channels"], 0, r["out"])
        r = run("ready_identity")
        self.assertIn("Bot user id: 999", r["out"])

    def test_same_batch_followup_folds_into_burst(self):
        r = run("batch_fold")
        items = r.get("queue") or []
        self.assertEqual(len(items), 1, r["out"])
        self.assertTrue(items[0]["burst"])
        self.assertIn("deploy command", items[0]["content"])


if __name__ == "__main__":
    unittest.main()
