"""Fast loop: REST work (polls, sweeps, discovery, slow sends, hung
reads) must not block the main loop. Each main() scenario runs in its
own process (run_fast.py); they all start at once, so the suite takes
as long as the longest one. MUT_DIR = the package tree under test
(default: this checkout). Run: python3 tests/fastloop/test_fastloop.py"""
import collections
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))

# scenario -> seconds of main() at most (each stops once it has seen
# enough, see run_fast.py)
SCENARIOS = {"slow": 20, "sweep": 20, "hung": 10, "outbox": 10}
_procs = {}
_results = {}


def setUpModule():
    for name, secs in SCENARIOS.items():
        _procs[name] = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "run_fast.py"), name,
             str(secs)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=os.environ.copy())


def tearDownModule():
    for p in _procs.values():
        if p.poll() is None:
            p.kill()
            p.communicate()


def run(scenario):
    if scenario not in _results:
        out, err = _procs[scenario].communicate(
            timeout=SCENARIOS[scenario] + 60)
        last = (out.strip().splitlines() or ["{}"])[-1]
        try:
            _results[scenario] = json.loads(last)
        except ValueError:
            raise AssertionError(out[-3000:] + err[-3000:])
    return _results[scenario]


def staged_at(r):
    out = {}
    for mid, t in r["staged"]:
        out.setdefault(mid, t)
    return out


def max_gap(ts):
    return max((b - a for a, b in zip(ts, ts[1:])), default=None)


def polls_by_channel(r):
    polls = collections.defaultdict(list)
    for path, t in r["gets"]:
        if "/messages?limit=100" in path:
            polls[path.split("/")[2]].append(t)
    return polls


class Scenarios(unittest.TestCase):
    def test_slow_rest_never_blocks_the_loop(self):
        """15 channels, every read 0.15s (a slow proxy): gateway messages
        are staged at once, the loop is never blocked long, and every
        channel is still polled about every POLL_INTERVAL (3s here)."""
        r = run("slow")
        st = staged_at(r)
        self.assertEqual(len(r["put"]), 4, r["out"][-2000:])
        for mid, t in r["put"]:
            self.assertIn(mid, st, f"never staged: {mid}")
            self.assertLess(st[mid] - t, 2.0,
                            f"put at {t}, staged at {st[mid]}")
        self.assertLess(max_gap(r["drains"]), 1.2)
        polls = polls_by_channel(r)
        self.assertEqual(len(polls), 15)
        for cid, ts in polls.items():
            self.assertGreaterEqual(len(ts), 2, (cid, ts))
            self.assertLess(ts[1] - ts[0], 3 + 3, (cid, ts))

    def test_sweep_recovers_and_loop_stays_responsive(self):
        r = run("sweep")
        self.assertIn(r["lost"], [m for m, _t, ok in r["enqueued"] if ok],
                      r["out"][-2000:])
        self.assertLess(max_gap(r["drains"]), 1.0)
        st = staged_at(r)
        for mid, t in r["put"]:
            self.assertLess(st[mid] - t, 1.0)
        # the sweep pass read every channel
        swept = {p.split("/")[2] for p, _ in r["gets"]
                 if p.endswith("?limit=20")}
        self.assertGreaterEqual(len(swept - {"DM1"}), 20)
        for cid, ts in polls_by_channel(r).items():
            self.assertGreaterEqual(len(ts), 2, (cid, ts))

    def test_hung_read_stalls_at_most_the_read_timeout(self):
        r = run("hung")
        (mid, t), = r["put"]
        st = staged_at(r)
        self.assertIn(mid, st, r["out"])
        self.assertLess(st[mid] - t, 1 + 1.5)  # READ_TIMEOUT_SECS + 1.5

    def test_outbox_written_mid_wait_is_sent_promptly(self):
        r = run("outbox")
        sends = [t for p, t in r["posts"] if p.endswith("/messages")]
        self.assertEqual(len(sends), 2, r["out"])
        for w, s in zip(r["outbox_written"], sends):
            self.assertLess(s - w, 0.5, (w, s))


class SlicedJobs(unittest.TestCase):
    """Discovery and the thread refresh run one request per step and
    are applied on completion."""

    def setUp(self):
        import monitor_harness as H
        self.H = H
        mod = self.mod = H.load_monitor()
        mod.api.routes.update({
            "/users/@me/guilds": [{"id": H.G1, "name": "Srv"}],
            f"/guilds/{H.G1}?with_counts=true": {"roles": []},
            f"/guilds/{H.G1}/members/{H.BOT}": {"roles": [],
                                                 "joined_at": None},
            f"/guilds/{H.G1}/channels": [
                {"id": "C1", "type": 0, "name": "general"},
                {"id": "C2", "type": 0, "name": "new"}],
            f"/guilds/{H.G1}/threads/active": {"threads": [
                {"id": "T1", "parent_id": "C1", "name": "post"}]},
        })
        mod.CHANNELS = {"C1": H.chan(), "OLD": H.chan("old")}
        self.st = {"last_seen": {"C1": "5", "OLD": "5"}, "replied_to": [],
                   "history": {}, "guild_joined": {H.G1: 0},
                   "known_guilds": {H.G1: "Srv"},
                   "rate": {"day": "", "day_count": 0, "users": {}}}

    def _run(self, job, between=None):
        steps = 0
        with contextlib.redirect_stdout(io.StringIO()):
            while not job.done():
                # a zero budget: one request per slice
                job.run_slice(self.st, time.time(), True)
                steps += 1
                if between and steps == 2:
                    between()
        return steps

    def test_discovery_job_sliced_and_keeps_gateway_channels(self):
        mod, H = self.mod, self.H
        job = mod.GenJob("Channel refresh", mod.discovery_iter(self.st))

        def gateway_registers():
            mod.CHANNELS["GW"] = H.chan("gw")
        steps = self._run(job, gateway_registers)
        self.assertGreaterEqual(steps, 6)
        self.assertEqual(set(mod.CHANNELS), {"C1", "C2", "T1", "GW"})
        self.assertIn("C2", self.st["last_seen"])  # seeded
        self.assertEqual(self.st["last_seen"]["C1"], "5")  # untouched

    def test_thread_refresh_job_and_backoff(self):
        mod = self.mod
        job = mod.GenJob("Thread refresh", mod.thread_refresh_iter(self.st))
        mod._rl_until = time.time() + 60    # REST back-off: the job waits
        self.assertEqual(job.run_slice(self.st, time.time() + 1, True), 0)
        self.assertFalse(job.done())
        mod._rl_until = 0
        self.assertGreaterEqual(self._run(job), 3)
        self.assertIn("T1", mod.CHANNELS)
        self.assertIn("T1", self.st["last_seen"])


if __name__ == "__main__":
    unittest.main()
