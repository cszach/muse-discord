"""Fast monitor restart from the wake hook (hooks/mention-watch.sh) and
the watchdog.sh run lock.

Runs inside its own user+PID namespace (re-execs itself under unshare),
so pgrep/pkill only ever see this suite's fakes and every fake dies with
the namespace. MUT_DIR = the package tree under test.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from common import (FAKEWS, HOOK_RUNTIME as RUNTIME, MUT_DIR,  # noqa: E402
                    copy_watchdog)

if os.environ.get("FR_IN_NS") != "1":
    env = dict(os.environ, FR_IN_NS="1")
    # bash (not python) as PID 1: it reaps the orphaned daemons.
    r = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork",
         "--mount-proc", "bash", "-c",
         f"python3 '{os.path.abspath(__file__)}' \"$@\"; exit $?", "x"]
        + sys.argv[1:], env=env)
    sys.exit(r.returncode)


PATS = ("monitor.py", "presence.py", "watchdog.sh")


def killall():
    for pat in PATS:
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    end = time.time() + 5
    while time.time() < end and any(
            subprocess.run(["pgrep", "-f", pat],
                           capture_output=True).returncode == 0
            for pat in PATS):
        time.sleep(0.02)


class Base(unittest.TestCase):
    real_watchdog = False

    def setUp(self):
        killall()
        self.procs = []
        self.d = tempfile.mkdtemp(prefix="muse-fastrestart-")
        os.makedirs(os.path.join(self.d, "hooks"))
        shutil.copy(os.path.join(MUT_DIR, "hooks", "mention-watch.sh"),
                    os.path.join(self.d, "hooks"))
        if self.real_watchdog:
            # Also poll the run lock every 0.05 s instead of 1 s.
            wd = copy_watchdog(self.d)
            with open(wd) as f:
                text = f.read()
            with open(wd, "w") as f:
                f.write(text.replace("        time.sleep(1)\n",
                                     "        time.sleep(0.05)\n"))
        else:
            shutil.copy(os.path.join(HERE, "fake_watchdog.sh"),
                        os.path.join(self.d, "watchdog.sh"))
        for n in ("monitor.py", "presence.py"):
            shutil.copy(os.path.join(HERE, "fake_proc.py"),
                        os.path.join(self.d, n))
        tok = os.path.join(self.d, "bot_token")
        with open(tok, "w") as f:
            f.write("dummy\n")
        with open(os.path.join(self.d, "config.yaml"), "w") as f:
            f.write(f"bot:\n  token_file: {tok}\n"
                    "presence:\n  enabled: false\n"
                    "updates:\n  check: false\n")
        self.state = os.path.join(self.d, "hookstate.json")
        self.env = dict(os.environ,
                        HATCH_HOOK_RUNTIME=RUNTIME, HOOK_STATE=self.state,
                        MUSE_DISCORD_DIR=self.d,
                        PYTHONPATH=FAKEWS, FAKE_WD_SECS="1.2")
        self.env.pop("MUSE_DISCORD_CONFIG", None)

    def tearDown(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()
        killall()
        shutil.rmtree(self.d, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.d, name)

    def hook(self):
        """Run the hook in its own process group; return (secs, output).
        Afterwards SIGKILL that group, as a hook runtime enforcing a
        timeout would: a restart it started must survive that."""
        t = time.monotonic()
        proc = subprocess.Popen(
            ["bash", self.p("hooks/mention-watch.sh")], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        out, _ = proc.communicate(timeout=30)
        secs = time.monotonic() - t
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.last_hook_pid = proc.pid
        return secs, out

    def calls(self):
        try:
            with open(self.p("wd_calls")) as f:
                return [ln for ln in f.read().splitlines()
                        if ln.startswith("start")]
        except FileNotFoundError:
            return []

    def wait_calls(self, n, secs=10.0):
        # Returns as soon as n calls are in; the long default only
        # matters on a loaded machine (CI), where a detached start can
        # take a few seconds.
        end = time.time() + secs
        while time.time() < end and len(self.calls()) < n:
            time.sleep(0.05)
        return self.calls()

    def get_state(self):
        try:
            with open(self.state) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def put_state(self, st):
        with open(self.state, "w") as f:
            json.dump(st, f)

    def expire(self):
        st = self.get_state()
        st["restart"]["next"] = int(time.time()) - 1
        self.put_state(st)

    def start_monitor(self, argv0_path, pidfile=True):
        proc = subprocess.Popen(["python3", argv0_path], cwd=self.d,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=self.env)
        self.procs.append(proc)
        end = time.time() + 5
        while time.time() < end and not os.path.exists(
                self.p("monitor.pid")):
            time.sleep(0.05)
        if not pidfile:
            os.remove(self.p("monitor.pid"))
        return proc

    def starts(self, name="monitor.py"):
        try:
            with open(self.p("starts.log")) as f:
                return [ln for ln in f.read().splitlines()
                        if ln.startswith(name + " ")]
        except FileNotFoundError:
            return []


class HookTrigger(Base):
    def test_dead_monitor_triggers_once_fast_and_silent(self):
        secs, out = self.hook()
        self.assertLess(secs, 1.0, out)
        self.assertNotIn("WAKE", out)
        self.assertIn("SILENT", out)
        calls = self.wait_calls(1)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("args=--from-hook", calls[0])
        # Its own session: not the hook's group, which we SIGKILLed.
        sid = int(calls[0].split("sid=")[1].split()[0])
        self.assertNotEqual(sid, self.last_hook_pid)
        # A second run right away is rate limited.
        secs, out = self.hook()
        self.assertEqual(self.get_state()["restart"]["fails"], 1)
        # The restart survived the kill of the hook's process group and
        # the hook did not wait for it (it takes 1.2s).
        end = time.time() + 6
        while time.time() < end:
            with open(self.p("wd_calls")) as f:
                if "done" in f.read():
                    break
            time.sleep(0.1)
        with open(self.p("wd_calls")) as f:
            self.assertIn("done", f.read())
        self.assertEqual(len(self.calls()), 1)

    def test_queue_work_still_wakes_and_keeps_both_states(self):
        with open(self.p("reply_queue.json"), "w") as f:
            json.dump([{"message_id": "1", "channel_id": "9"}], f)
        secs, out = self.hook()
        self.assertIn("\nWAKE:", "\n" + out)
        self.assertEqual(len(self.wait_calls(1)), 1)
        st = self.get_state()
        self.assertIn("1", st["woken"])
        self.assertEqual(st["restart"]["fails"], 1)
        # And a later queue-only run keeps .restart.
        secs, out = self.hook()
        self.assertIn("SILENT", out)
        self.assertEqual(self.get_state()["restart"]["fails"], 1)

    def test_live_monitor_no_trigger(self):
        # Absolute path: watchdog's pgrep pattern does not match it, so
        # only the pidfile can tell the hook it is alive.
        mon = self.start_monitor(self.p("monitor.py"))
        self.hook()
        self.assertEqual(self.get_state(), {})
        mon.kill()
        mon.wait()
        # No pidfile: pgrep finds it.
        self.start_monitor("monitor.py", pidfile=False)
        self.hook()
        self.assertEqual(self.get_state(), {})
        self.assertEqual(self.wait_calls(1, 0.3), [])

    def test_stale_pidfile_with_reused_pid_triggers(self):
        sl = subprocess.Popen(["sleep", "30"])
        with open(self.p("monitor.pid"), "w") as f:
            f.write(str(sl.pid))
        self.hook()
        self.assertEqual(len(self.wait_calls(1)), 1)
        sl.kill()
        sl.wait()

    def test_backoff_while_failing(self):
        expect = [60, 120, 300, 600, 600]
        for i, wait in enumerate(expect, 1):
            now = int(time.time())
            self.hook()
            self.assertEqual(len(self.wait_calls(i)), i)
            r = self.get_state()["restart"]
            self.assertEqual(r["fails"], i)
            self.assertTrue(now + wait <= r["next"] <= now + wait + 2, r)
            # Within the window: no new attempt.
            self.hook()
            self.hook()
            self.assertEqual(self.get_state()["restart"], r)
            self.expire()
        self.assertEqual(len(self.wait_calls(len(expect) + 1, 0.3)),
                         len(expect))

    def test_recovery_resets_backoff(self):
        now = int(time.time())
        self.put_state({"restart": {"next": now + 100, "fails": 3}})
        mon = self.start_monitor(self.p("monitor.py"))
        secs, out = self.hook()
        # Alive but its window has not passed yet: keep the record.
        self.assertEqual(self.get_state()["restart"]["fails"], 3)
        self.expire()
        self.hook()
        self.assertNotIn("restart", self.get_state())
        mon.kill()
        mon.wait()
        os.remove(self.p("monitor.pid"))
        now = int(time.time())
        self.hook()
        self.assertEqual(len(self.wait_calls(1)), 1)
        r = self.get_state()["restart"]
        self.assertEqual(r["fails"], 1)
        self.assertTrue(now + 60 <= r["next"] <= now + 62, r)

    def test_token_rejection_no_restart(self):
        # The monitor's own log says the token was rejected.
        with open(self.p("monitor.log"), "w") as f:
            f.write("Bot user id: 1\n" * 2000 + "TOKEN REJECTED: Discord "
                    "refused the bot token. Exiting.\n")
        for _ in range(3):
            self.hook()
        now = int(time.time())
        r = self.get_state()["restart"]
        self.assertEqual(r["fails"], 0)
        self.assertTrue(now + 590 <= r["next"] <= now + 601, r)
        # The watchdog's alert state says so.
        os.remove(self.p("monitor.log"))
        self.put_state({})
        with open(self.p("watchdog_state.json"), "w") as f:
            json.dump({"last_alert": "monitor DOWN: Discord rejected the "
                       "token; it was probably reset."}, f)
        self.hook()
        self.assertEqual(self.get_state()["restart"]["fails"], 0)
        self.assertEqual(self.wait_calls(1, 0.3), [])
        # Any other alert still restarts.
        self.put_state({})
        with open(self.p("watchdog_state.json"), "w") as f:
            json.dump({"last_alert": "monitor DOWN: restart attempted but "
                       "the process is not healthy"}, f)
        self.hook()
        self.assertEqual(len(self.wait_calls(1)), 1)


class RealWatchdog(Base):
    real_watchdog = True

    def wait_wd_idle(self, secs=60):
        """Wait until no watchdog.sh run is left."""
        end = time.time() + secs
        while time.time() < end:
            r = subprocess.run(["pgrep", "-f", "watchdog.sh"],
                               capture_output=True)
            if r.returncode != 0:
                return
            time.sleep(0.05)
        self.fail("watchdog.sh still running")

    def wd(self, *args):
        return subprocess.run(["bash", self.p("watchdog.sh"), *args],
                              cwd=self.d, env=self.env, capture_output=True,
                              text=True, timeout=400)

    def test_hook_brings_monitor_back_and_lock_is_released(self):
        secs, out = self.hook()
        self.assertLess(secs, 1.0)
        end = time.time() + 20
        while time.time() < end and not self.starts():
            time.sleep(0.1)
        self.assertEqual(len(self.starts()), 1)
        self.wait_wd_idle()
        # The hook-run neither consumed nor wrote alert state.
        self.assertFalse(os.path.exists(self.p("watchdog_state.json")))
        # The daemonized monitor does not hold the run lock: a scheduled
        # run goes straight through.
        t = time.monotonic()
        r = self.wd()
        self.assertLess(time.monotonic() - t, 10, r.stdout)
        self.assertIn("OK monitor alive", r.stdout)
        self.assertNotIn("skipped", r.stdout)
        self.assertEqual(len(self.starts()), 1)

    def test_token_rejection_no_restart_loop_and_cron_still_alerts(self):
        self.env["FAKE_MODE"] = "token"
        self.hook()
        end = time.time() + 20
        while time.time() < end and not self.starts():
            time.sleep(0.1)
        self.wait_wd_idle()
        self.assertEqual(len(self.starts()), 1)
        # The hook run left the alert for the scheduled run.
        self.assertFalse(os.path.exists(self.p("watchdog_state.json")))
        for _ in range(3):
            self.expire()
            now = int(time.time())
            self.hook()
            r = self.get_state()["restart"]
            self.assertEqual(r["fails"], 1)   # no new attempt
            self.assertGreaterEqual(r["next"], now + 590)
        time.sleep(0.3)  # a detached start would show by now
        self.wait_wd_idle()
        self.assertEqual(len(self.starts()), 1)
        r = self.wd()
        self.assertIn("ALERT: monitor DOWN: Discord rejected the token",
                      r.stdout)

    def test_two_concurrent_runs_start_one_monitor(self):
        with open(self.p("config.yaml")) as f:
            cfg = f.read().replace("enabled: false", "enabled: true")
        with open(self.p("config.yaml"), "w") as f:
            f.write(cfg)
        a = subprocess.Popen(["bash", self.p("watchdog.sh")], cwd=self.d,
                             env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        b = subprocess.Popen(["bash", self.p("watchdog.sh")], cwd=self.d,
                             env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        oa, _ = a.communicate(timeout=400)
        ob, _ = b.communicate(timeout=400)
        self.assertEqual(len(self.starts("monitor.py")), 1, oa + ob)
        self.assertEqual(len(self.starts("presence.py")), 1, oa + ob)
        self.assertNotIn("ALERT", oa + ob)

    def test_hook_run_and_scheduled_run_together_start_one_monitor(self):
        self.hook()
        r = self.wd()
        self.wait_wd_idle()
        self.assertEqual(len(self.starts()), 1, r.stdout)
        self.assertNotIn("ALERT", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
