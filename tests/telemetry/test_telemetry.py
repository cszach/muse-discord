"""Usage stats (telemetry.py), offline:

0. A copy past a release, or edited, is told apart from the release.
1. A report never carries an id, a name, message content, an error
   message or a path: a realistic monitor flow (mentions, a reply, a
   refusal, settings changes and a preset, a crash) with known ids and
   names, and none of them appear anywhere in the serialized report. The
   report is one the Worker's validation accepts (when node is here).
2. Off sends nothing, by setting (and the days it was off are never
   sent later) and by MUSE_DISCORD_TELEMETRY=off; only releases send.
3. A slow, failing or unreachable endpoint never blocks or crashes the
   monitor: tick() returns at once, the day stays waiting and is tried
   again later that day with backoff, never in a loop; unsent days are
   kept a week. Consecutive unreachable days tell the owners once; why
   a send failed never shows a proxy password; `bin/telemetry.py send`
   is honoured, and refused when nothing could be sent.
4. At most one send per UTC day, and each closed day is sent once.

Every send goes to a local fake endpoint; nothing leaves the machine.

Usage: [MUT_DIR=<tree>] python3 tests/telemetry/test_telemetry.py"""
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from common import MUT_DIR, scratch, signed  # noqa: E402
from settings_harness import (BOT, CH, G1, MS, OWNER, U1, U2,  # noqa: E402
                              author, gmsg, load_monitor, quiet, state)

sys.path.insert(0, MUT_DIR)
import telemetry as T  # noqa: E402

DAY = 86400
BASE = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
servers:
  "{G1}":
    who:
      ignore: ["{U2}"]
"""


class Endpoint:
    """A local fake Worker: records each report; mode "ok", "fail"
    (500) or "hang" (answers after `hang` seconds)."""

    def __init__(self, mode="ok", hang=1.0):
        self.mode, self.hang, self.reports = mode, hang, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.reports.append(json.loads(body))
                if outer.mode == "hang":
                    time.sleep(outer.hang)
                self.send_response(204 if outer.mode == "ok" else 500)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, args=(0.02,),
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def edit_file(tm, **kw):
    """telemetry.json edited by hand (or damaged) while running."""
    tm.flush()
    path = tm.paths()[0]
    with open(path) as f:
        d = json.load(f)
    d.update(kw)
    with open(path, "w") as f:
        json.dump(d, f)
    tm.data = None


class Clock:
    def __init__(self):
        # Noon UTC today: the jitter (up to 3 h into a day) has passed.
        self.t = T.day_start(T.utc_day()) + 12 * 3600

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    cfg = BASE

    def setUp(self):
        self.m = load_monitor(self.cfg)
        self.st = state(self.m)
        self.clock = Clock()
        self.tm = self.m.TM
        self.tm.clock = self.clock
        # On, with no endpoint unless a test serves a local fake one;
        # any post to a non-local URL fails the test.
        self.tm.environ = {T.ENV_URL: ""}
        self.tm.post = self.local_only
        self.tm.version = "v1.0.0"   # only releases send
        self.post_timeout = 1
        self.tm._find_version = lambda: None   # start() keeps it
        self.ep = None

    def tearDown(self):
        if self.ep:
            self.ep.close()

    def local_only(self, url, obj):
        if not url.startswith("http://127.0.0.1:"):
            raise AssertionError(f"a test tried to send to {url}")
        return T.post_json(url, obj, timeout=self.post_timeout)

    def serve(self, mode="ok", **kw):
        self.ep = Endpoint(mode, **kw)
        self.tm.environ = {T.ENV_URL: self.ep.url}
        self.tm.post = self.local_only
        return self.ep

    def tick(self):
        with quiet():
            self.tm.tick(self.m.settings, self.m.GUILDS,
                         {G1: 37, "x": 3}, force=True)

    def wait_send(self, secs=5):
        """Until the send thread finished and its result was taken (at
        once when no send was started: then nothing was posted)."""
        end = time.time() + secs
        while time.time() < end:
            t = self.tm._sending
            if t is None:
                return
            t.join(0.05)
            if not t.is_alive():
                self.tick()
        self.fail("the send never finished")

    def next_day(self):
        self.clock.t += DAY
        self.tick()

    def pending(self):
        return [p["day"] for p in self.tm.data["pending"]]


# ---------------------------------------------------------------------------
# 0. Copies past a release, or edited, are told apart from the release.
# ---------------------------------------------------------------------------
class BuildInfo(unittest.TestCase):

    def git(self, d, *args):
        subprocess.run(["git", "-C", d, "-c", "user.name=t",
                        "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                        "-c", "tag.gpgsign=false", *args],
                       check=True, capture_output=True)

    def test_ahead_and_modified(self):
        d = scratch("muse-telemetry-git-")
        self.git(d, "init", "-q")
        for name in ("monitor.py", "persona.md"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x\n")
        self.git(d, "add", ".")
        self.git(d, "commit", "-qm", "1")
        self.git(d, "tag", "v1.0.0")
        self.assertEqual(T.installed_version(d), "v1.0.0")
        self.assertEqual(T.build_info(d, "v1.0.0"), (0, False))
        with open(os.path.join(d, "persona.md"), "w") as f:
            f.write("my bot\n")     # the owner's to edit: not "modified"
        self.assertEqual(T.build_info(d, "v1.0.0"), (0, False))
        for i in range(2):
            self.git(d, "commit", "-qam", f"main {i}", "--allow-empty")
        with open(os.path.join(d, "monitor.py"), "w") as f:
            f.write("edited\n")
        self.assertEqual(T.build_info(d, "v1.0.0"), (2, True))


# ---------------------------------------------------------------------------
# 1. Nothing identifying in a report.
# ---------------------------------------------------------------------------
class NeverIdentifying(Base):

    def test_report_holds_no_ids_names_or_content(self):
        m, tm = self.m, self.tm
        self.tick()   # the first tick takes the settings history position
        secret = "PINEAPPLE launch code hunter2"
        alice = author(U1, name="alice_secret_name")
        mention = gmsg(U1, f"<@{BOT}> {secret}", mention=True)
        mention["author"] = alice
        chatter = gmsg(U1, "just chatting about PINEAPPLE")
        chatter["author"] = alice
        ignored = gmsg(U2, f"<@{BOT}> {secret} again", mention=True)
        with quiet():
            for msg in (mention, chatter, ignored):
                kind = m.classify_message(self.st, msg, "Lounge #general",
                                          G1)
                if kind and kind != "join":
                    m.stage_found(self.st, msg, "Lounge #general", G1, kind)
            for entry in self.st.get("pending", {}).values():
                entry["first_seen"] = 0
            q = []
            m.flush_pending(self.st, q)
            m.queue_edit(add=q)
        self.assertEqual([i["message_id"] for i in q], [mention["id"]])
        with open(m.OUTBOX_FILE, "w") as f:
            json.dump([signed(m, self.st, {
                "item": mention["id"], "channel_id": CH,
                "reply_to": mention["id"],
                "text": f"Sure alice_secret_name, {secret}"})], f)
        with quiet():
            m.process_outbox(self.st)
        self.assertTrue(m.api.channel_posts(CH))

        # Settings changes: /settings (an owner, by id), a preset, a DM
        # request and the CLI; every key names the server's id.
        ctx = m.SettingsCtx()
        place = MS.parse_place(f"server:{G1}")
        with quiet():
            ctx.write(f"servers.{G1}.style.notes", "talk about PINEAPPLE",
                      OWNER)
            ctx.apply(MS.preset_changes(m.settings(), "community", place),
                      OWNER, "owner")
            MS.apply(m.cfg_path, [MS.Change(place, "style.length", "brief")],
                     OWNER, group="nl-" + mention["id"][:12],
                     plugins_dir=m.PLUGINS_DIR, role="owner")
            MS.apply(m.cfg_path, [MS.Change(place, "who.bots", "ignore")],
                     "muse.ai", group="cli-1234abcd",
                     plugins_dir=m.PLUGINS_DIR, role="owner")
        # A crash whose message holds a secret; a wake noted by the hook.
        tm.crashed(ValueError(f"{secret} in {m.tmp}"))
        with open(m.TELEMETRY_WAKES_FILE, "w") as f:
            f.write("\n\n")
        self.tick()
        self.next_day()

        reports = [T.build_payload(p, T.read_install_id(m.TELEMETRY_ID_FILE))
                   for p in tm.data["pending"]]
        self.assertEqual(len(reports), 1)
        r = reports[0]
        # What happened was counted (so the check below is not vacuous).
        self.assertEqual(r["replies_sent"], 1)
        self.assertGreaterEqual(r["messages_seen"], 3)
        self.assertEqual(r["refusals"], {"not-allowed": 1})
        self.assertEqual(r["crashes"], {"ValueError": 1})
        self.assertEqual(r["wakes"], 2)
        self.assertEqual(r["presets_applied"], {"community": 1})
        self.assertEqual(set(r["settings_changes"]), {"settings", "dm",
                                                      "cli"})
        self.assertEqual(r["servers"], 2)
        self.assertEqual(r["members_bucket"], 32)   # 40 members
        self.assertIsNotNone(r["latency_p50"])

        text = json.dumps(reports)
        forbidden = [BOT, OWNER, U1, U2, G1, CH, mention["id"],
                     chatter["id"], ignored["id"], "alice_secret_name",
                     "Lounge", "general", "PINEAPPLE", "hunter2", "muse.ai",
                     m.tmp, MUT_DIR, os.path.expanduser("~"), "u01", "u02"]
        for word in forbidden:
            self.assertNotIn(word, text)
        self.assertIsNone(re.search(r"\d{15,}", text))
        self.assertEqual(set(r), set(EXPECTED_FIELDS))
        # Nothing finer than the day: no time of day anywhere.
        self.assertIsNone(re.search(r"\d{2}:\d{2}|T\d{2}", text))

        node = shutil.which("node")
        if node:
            js = os.path.join(MUT_DIR, "telemetry", "worker", "src",
                              "validate.js")
            out = subprocess.run(
                [node, "--input-type=module", "-e",
                 f"import {{validateReport}} from {json.dumps(js)};"
                 "const r = validateReport(JSON.parse(process.argv[1]));"
                 "console.log(JSON.stringify(r)); process.exit(r.ok?0:1)",
                 json.dumps(r)], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


EXPECTED_FIELDS = (
    "schema", "install_id", "day", "version", "ahead", "modified",
    "python", "os", "servers",
    "members_bucket", "messages_seen", "queued", "replies_sent",
    "quarantined", "refusals", "wakes", "latency_p50", "latency_p90",
    "settings_changes", "presets_applied", "media_sent", "restarts",
    "crashes", "features")


# ---------------------------------------------------------------------------
# 2. Off sends nothing.
# ---------------------------------------------------------------------------
class OffBySetting(Base):
    cfg = BASE + "telemetry:\n  usage_stats: off\n"

    def test_setting_off_sends_nothing_ever(self):
        ep = self.serve()
        self.tm.count("messages_seen", 5)
        self.tick()
        self.next_day()
        self.next_day()
        self.wait_send()
        self.assertEqual(ep.reports, [])
        self.assertEqual(self.pending(), [])
        # Turned on again (as a DM or bin/telemetry.py on would): the
        # days it was off are still never sent; the next full day is.
        with quiet():
            MS.apply(self.m.cfg_path, [MS.Change(
                MS.parse_place("defaults"), T.SETTING, "on")], "muse.ai",
                group="cli-0000aaaa", plugins_dir=self.m.PLUGINS_DIR,
                role="owner")
        self.tm.count("messages_seen", 1)
        self.tick()
        self.wait_send()
        self.assertEqual(ep.reports, [])   # today was off this morning
        self.next_day()
        self.next_day()
        self.wait_send()
        self.assertEqual(len(ep.reports), 1)
        self.assertEqual(ep.reports[0]["day"],
                         T.utc_day(self.clock.t - DAY))


class OffByEnv(Base):

    def test_env_off_sends_nothing(self):
        ep = self.serve()
        self.tm.environ = {T.ENV_URL: ep.url, T.ENV_SWITCH: "off"}
        self.tm.count("replies_sent")
        self.tick()
        self.next_day()
        self.next_day()
        self.wait_send()
        self.assertEqual(ep.reports, [])
        self.assertEqual(self.pending(), [])

    def test_only_releases_send(self):
        ep = self.serve()
        for version in ("unreleased", "unknown", None):
            self.tm.version = version
            self.tm.count("replies_sent")
            self.tick()
            self.next_day()
            self.next_day()
            self.wait_send()
            self.assertEqual(ep.reports, [], version)


# ---------------------------------------------------------------------------
# 3. A bad endpoint never blocks or crashes the monitor.
# ---------------------------------------------------------------------------
class BadEndpoint(Base):

    def test_hanging_endpoint_does_not_block(self):
        ep = self.serve("hang")
        self.post_timeout = 0.2
        self.tm.count("replies_sent")
        self.tick()
        self.clock.t += DAY
        t0 = time.time()
        self.tick()   # starts the send
        self.tick()
        self.assertLess(time.time() - t0, 0.5)
        self.wait_send()   # the post's timeout ends it
        self.assertEqual(len(ep.reports), 1)
        self.assertEqual(len(self.pending()), 1)   # kept for tomorrow

    def test_failing_endpoint_retries_later_with_backoff(self):
        ep = self.serve("fail")
        self.tick()
        self.next_day()
        self.wait_send()
        self.assertEqual(len(ep.reports), 1)
        self.assertEqual(self.tm.data["last_send"]["error"], "HTTP 500")

        def after(secs):
            self.clock.t += secs
            self.tick()
            self.wait_send()
            return len(ep.reports)
        self.assertEqual(after(3000), 1)           # no retry storm
        self.assertEqual(after(700), 2)            # an hour after
        self.assertEqual(after(3700), 2)           # then two hours
        self.assertEqual(after(3600), 3)
        self.assertEqual(len(self.pending()), 1)
        ep.mode = "ok"
        self.next_day()
        self.wait_send()
        self.assertEqual(len(ep.reports), 5)       # both waiting days
        self.assertEqual(self.pending(), [])

    def test_unreachable_days_tell_the_owners_once(self):
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9"}
        self.tick()
        notices = []
        for _ in range(T.NOTICE_AFTER_DAYS + 2):
            self.next_day()
            self.wait_send()
            with quiet():
                notices.append(self.tm.take_notice())
        self.assertEqual([bool(n) for n in notices],
                         [False] * (T.NOTICE_AFTER_DAYS - 1)
                         + [True, False, False])
        self.assertIn("127.0.0.1", notices[T.NOTICE_AFTER_DAYS - 1])
        # Reaching the endpoint again (even refused) re-arms it.
        self.serve("fail")
        self.next_day()
        self.wait_send()
        self.assertNotIn("failing", self.tm.data)
        self.assertNotIn("notice", self.tm.data)

    def test_notice_counts_only_consecutive_failing_days(self):
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9"}
        self.tick()
        self.next_day()
        self.wait_send()                     # one failing day
        self.tm.version = "v1.0.0-3-gabc"    # not a release: no sends
        for _ in range(3):
            self.next_day()
        self.tm.version = "v1.0.0"
        self.next_day()
        self.wait_send()
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9",
                           T.ENV_SWITCH: "off"}
        self.tick()                          # off drops the streak
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9"}
        self.next_day()
        self.next_day()
        self.wait_send()
        with quiet():
            self.assertIsNone(self.tm.take_notice())
        # Junk in the file starts a new streak instead of breaking it.
        edit_file(self.tm, failing={"since": "2026-02-30", "last": 5})
        notices = []
        for _ in range(T.NOTICE_AFTER_DAYS):
            self.next_day()
            self.wait_send()
            with quiet():
                notices.append(bool(self.tm.take_notice()))
        self.assertEqual(notices[-1], True)

    def test_no_notice_once_turned_off(self):
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9"}
        self.tick()
        for _ in range(T.NOTICE_AFTER_DAYS):
            self.next_day()
            self.wait_send()
        self.assertEqual(self.tm.data.get("notice"), "due")
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9",
                           T.ENV_SWITCH: "off"}   # before the next tick
        with quiet():
            self.assertIsNone(self.tm.take_notice())

    def test_negative_tries_in_the_file_is_no_retry_storm(self):
        ep = self.serve("fail")
        self.tick()
        self.next_day()
        self.wait_send()
        edit_file(self.tm, tries=-60, retry_after=self.clock.t)
        for _ in range(10):                  # five minutes of ticks
            self.clock.t += T.TICK_SECS
            self.tick()
            self.wait_send()
        self.assertLessEqual(len(ep.reports), 2)

    def test_send_asked_for_by_the_cli(self):
        ep = self.serve("fail")
        self.tick()
        self.next_day()
        self.wait_send()
        self.assertEqual(len(ep.reports), 1)
        ep.mode = "ok"
        flag = os.path.join(os.path.dirname(self.m.TELEMETRY_FILE),
                            T.SEND_NOW_FILE)
        open(flag, "w").close()
        self.tick()          # long before the retry is due
        self.wait_send()
        self.assertEqual(len(ep.reports), 2)
        self.assertEqual(self.pending(), [])
        self.assertFalse(os.path.exists(flag))

    def test_send_asked_for_during_a_send_waits_for_it(self):
        ep = self.serve("hang", hang=0.5)    # answers 500 after 0.5 s
        self.post_timeout = 2
        self.tick()
        self.clock.t += DAY
        self.tick()                          # a send in flight
        flag = os.path.join(os.path.dirname(self.m.TELEMETRY_FILE),
                            T.SEND_NOW_FILE)
        open(flag, "w").close()
        self.tick()
        self.assertTrue(os.path.exists(flag))
        self.wait_send()      # its end starts the asked-for send
        self.wait_send()
        self.assertEqual(len(ep.reports), 2)
        self.assertFalse(os.path.exists(flag))

    def test_cli_send_and_status_on_odd_states(self):
        pkg = scratch("telemetry-cli-")
        shutil.copytree(os.path.join(MUT_DIR, "bin"),
                        os.path.join(pkg, "bin"))
        shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                        os.path.join(pkg, "plugins"))
        for f in ("muse_settings.py", "telemetry.py"):
            shutil.copy(os.path.join(MUT_DIR, f), pkg)
        with open(os.path.join(pkg, "config.yaml"), "w") as f:
            f.write(BASE)
        with open(os.path.join(pkg, "telemetry.json"), "w") as f:
            json.dump({"pending": [T.new_day("2026-10-28")],
                       "last_send": {"day": T.utc_day(), "ok": False},
                       "last_attempt": T.utc_day(), "retry_after": 1e300},
                      f)

        def cli(cmd, **env):
            return subprocess.run(
                [sys.executable, os.path.join(pkg, "bin", "telemetry.py"),
                 cmd], capture_output=True, text=True, timeout=60,
                env=dict(os.environ, MUSE_DISCORD_TELEMETRY="on", **env))
        r = cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        for env in ({T.ENV_URL: ""}, {}):    # no endpoint; not a release
            r = cli("send", **env)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertFalse(os.path.exists(
                os.path.join(pkg, T.SEND_NOW_FILE)))

    def test_failure_reason_never_logs_a_proxy_password(self):
        import urllib.error
        for e in (urllib.error.URLError(
                "Tunnel connection failed: http://user:s3cret@proxy:3128"),
                  ValueError("proxy URL with no authority: "
                             "'http:/user:s3cret@127.0.0.1:3128'")):
            self.assertNotIn("s3cret", T.failure_reason(e))
            self.assertIn("proxy", T.failure_reason(e))

    def test_unreachable_endpoint_and_bad_files(self):
        self.tm.environ = {T.ENV_URL: "http://127.0.0.1:9"}
        self.tm.post = self.local_only
        with open(self.m.TELEMETRY_FILE, "w") as f:
            f.write("{not json")
        self.tm.data = None
        self.tick()
        self.next_day()
        self.wait_send()
        self.assertEqual(len(self.pending()), 1)
        # The counters never raise, whatever they are given.
        self.tm.data = None
        with open(self.m.TELEMETRY_FILE, "w") as f:
            f.write('{"today": 5, "pending": "x"}')
        with quiet():
            self.tm.latency("soon")
            self.tm.ledger(None, [None, 3])
            self.tm.crashed(None)
            self.tm.tick(lambda: 1 / 0, None, None, force=True)

    def test_unsent_days_are_kept_a_week_at_most(self):
        self.tm.environ = {T.ENV_URL: ""}   # no endpoint: kept, never sent
        self.tick()
        for _ in range(10):
            self.next_day()
        self.assertEqual(len(self.pending()), T.KEEP_DAYS)


# ---------------------------------------------------------------------------
# 4. One send per day.
# ---------------------------------------------------------------------------
class OncePerDay(Base):

    def test_each_day_sent_once(self):
        ep = self.serve()
        self.tm.count("replies_sent", 2)
        self.tick()
        self.next_day()
        self.wait_send()
        self.assertEqual([r["day"] for r in ep.reports],
                         [T.utc_day(self.clock.t - DAY)])
        for _ in range(5):
            self.clock.t += 1800
            self.tick()
        self.wait_send()
        self.assertEqual(len(ep.reports), 1)
        # A restart the same day sends nothing more.
        with quiet():
            self.tm.flush()
        self.tm.data = None
        self.tm.start()
        self.tick()
        self.wait_send()
        self.assertEqual(len(ep.reports), 1)
        self.next_day()
        self.wait_send()
        self.assertEqual(len(ep.reports), 2)
        self.assertNotEqual(ep.reports[0]["day"], ep.reports[1]["day"])
        self.assertEqual(len({r["install_id"] for r in ep.reports}), 1)


if __name__ == "__main__":
    unittest.main()
