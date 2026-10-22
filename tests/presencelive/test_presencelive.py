"""presence.py follows settings changes live, offline against a fake
gateway: a config.yaml or config.discord.yaml change becomes one
Presence Update (op 3) on the open session (no reconnect), bursts are
coalesced and rate limited, an invalid edit keeps the last good status,
presence.enabled off exits cleanly, and heartbeats keep flowing while
the files are re-read.

Usage: [MUT_DIR=<tree>] python3 tests/presencelive/test_presencelive.py"""
import asyncio
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from common import MUT_DIR  # noqa: E402

# Its own fake websockets (a live fake gateway), not _lib/fakews.
sys.path.insert(0, os.path.join(HERE, "fakews"))
import websockets as fakews  # noqa: E402

POLL = 0.02
MIN_INTERVAL = 0.3
_seq = [0]


def cfg_text(status="first", kind="watching", enabled=None, extra=""):
    pres = f'presence:\n  status: "{status}"\n  kind: {kind}\n'
    if enabled is not None:
        pres += f"  enabled: {enabled}\n"
    return ('version: 1\nowners: ["111"]\ntoken_from: env:MUSE_TEST_TOKEN\n'
            + pres + extra)


def write(path, text):
    """Atomic, like an editor's save (new inode, so the watcher sees it
    even within one mtime tick)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def ops(op, conn=None):
    return [(t, p["d"]) for i, t, p in fakews.SENT
            if p.get("op") == op and (conn is None or i == conn)]


class Live(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="muse-presence-")
        self.cfg = os.path.join(self.tmp, "config.yaml")
        write(self.cfg, cfg_text())
        os.environ["MUSE_DISCORD_CONFIG"] = self.cfg
        sys.argv = ["x", "dummy-token"]
        _seq[0] += 1
        spec = importlib.util.spec_from_file_location(
            f"mut_presence_{_seq[0]}", os.path.join(MUT_DIR, "presence.py"))
        self.p = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.p)
        self.p.SETTINGS_POLL_SECONDS = POLL
        self.p.PRESENCE_MIN_INTERVAL = MIN_INTERVAL
        fakews.reset()
        self.out = io.StringIO()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drive(self, scenario, limit=8.0):
        """Run presence.run() beside scenario(task); return run()'s
        outcome: "returned", "running" (cancelled at the end) or the
        exception."""
        async def main():
            task = asyncio.create_task(self.p.run())
            await self.until(lambda: ops(2) or task.done())
            try:
                await asyncio.wait_for(scenario(task), limit)
            finally:
                running = not task.done()
                if running:
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            if running:
                return "running"
            exc = task.exception()
            return exc if exc else "returned"
        with contextlib.redirect_stdout(self.out):
            return asyncio.run(main())

    async def until(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.01)
        return False

    def identify_activities(self):
        return ops(2)[0][1]["presence"]["activities"]

    # ------------------------------------------------------------------
    async def ready(self):
        await self.until(lambda: "READY" in self.out.getvalue())

    def test_config_yaml_change_sends_one_op3_no_reconnect(self):
        async def sc(task):
            await self.ready()
            write(self.cfg, cfg_text("second", "listening"))
            await self.until(lambda: ops(3))
            await asyncio.sleep(POLL * 5)  # no repeat of the same update
        self.assertEqual(self.drive(sc), "running")
        self.assertEqual(self.identify_activities(),
                         [{"name": "first", "type": 3}])
        o3 = ops(3)
        self.assertEqual(len(o3), 1, o3)
        self.assertEqual(o3[0][1]["activities"],
                         [{"name": "second", "type": 2}])
        self.assertEqual(o3[0][1]["status"], "online")
        self.assertEqual(len(fakews.CONNECTS), 1)
        # A /settings change (config.discord.yaml) is followed too.
        self.tearDown()
        self.setUp()
        sys.path.insert(0, MUT_DIR)
        import muse_settings as MS

        async def sc(task):
            await self.ready()
            MS.set_value(self.cfg, "presence.status", "set in discord",
                         by="111", plugins_dir=os.path.join(MUT_DIR,
                                                            "plugins"))
            await self.until(lambda: ops(3))
        self.drive(sc)
        o3 = ops(3)
        self.assertEqual(len(o3), 1, o3)
        self.assertEqual(o3[0][1]["activities"][0]["name"], "set in discord")
        self.assertEqual(len(fakews.CONNECTS), 1)

    def test_rapid_changes_coalesced(self):
        async def sc(task):
            await self.ready()
            write(self.cfg, cfg_text("a"))
            await self.until(lambda: ops(3))
            for s in ("b", "c", "d"):
                await asyncio.sleep(POLL * 2)
                write(self.cfg, cfg_text(s))
            await self.until(lambda: len(ops(3)) >= 2)
        self.drive(sc)
        o3 = ops(3)
        names = [d["activities"][0]["name"] for _, d in o3]
        self.assertEqual(names, ["a", "d"])
        self.assertGreaterEqual(o3[1][0] - o3[0][0], MIN_INTERVAL - 0.01)
        self.assertEqual(len(fakews.CONNECTS), 1)

    def test_invalid_config_keeps_last_good(self):
        async def sc(task):
            await self.ready()
            write(self.cfg, cfg_text("good"))
            await self.until(lambda: ops(3))
            write(self.cfg, cfg_text("bad", "bogus-kind"))
            await asyncio.sleep(POLL * 5)
            self.assertFalse(task.done())
            write(self.cfg, "presence: [unclosed\n")  # not YAML
            await asyncio.sleep(POLL * 5)
            self.assertFalse(task.done())
            write(self.cfg, cfg_text("good again"))
            await self.until(lambda: len(ops(3)) >= 2)
        self.assertEqual(self.drive(sc), "running")
        names = [d["activities"][0]["name"] for _, d in ops(3)]
        self.assertEqual(names, ["good", "good again"])
        self.assertEqual(len(fakews.CONNECTS), 1)

    def test_enabled_off_exits_cleanly(self):
        async def sc(task):
            await self.ready()
            write(self.cfg, cfg_text(enabled="off"))
            await self.until(task.done)
        self.assertEqual(self.drive(sc), "returned")
        self.assertEqual(fakews.CLOSED_BY_CLIENT, [0])
        self.assertEqual(len(fakews.CONNECTS), 1)
        self.assertEqual(ops(3), [])

    def test_heartbeats_flow_while_rereading(self):
        hb = fakews.HB_MS
        fakews.HB_MS = 40
        self.addCleanup(setattr, fakews, "HB_MS", hb)
        orig = self.p.WATCHER.poll
        slow = []

        def slow_poll():
            r = orig()
            if r[0]:
                time.sleep(0.3)  # a slow re-read must not stall the loop
                slow.append(1)
            return r
        self.p.WATCHER.poll = slow_poll

        async def sc(task):
            await self.ready()
            for n, s in enumerate(("x", "y"), 1):
                write(self.cfg, cfg_text(s))
                await self.until(lambda: len(slow) >= n)
        self.drive(sc)
        self.assertEqual(len(slow), 2)
        beats = [t for t, _ in ops(1)]
        self.assertGreaterEqual(len(beats), 10, beats)
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        self.assertLess(max(gaps), 0.25, gaps)
        self.assertTrue(ops(3))

if __name__ == "__main__":
    unittest.main(verbosity=1)
