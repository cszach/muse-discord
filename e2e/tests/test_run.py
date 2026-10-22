"""Offline tests for the e2e runner: discord_rest, sut and run.py.

What they guard: a run reports what happened (pass, fail, flake; the
exit code), never leaks a token, cleans up, and never lets one
scenario's state leak into the next.

No network and no Discord tokens: Discord is a scripted fake, the
monitor is a stand-in script (sut tests) or a fake object (run tests),
and run.py's scenario / checks / judge modules are minimal stubs that
are put in sys.modules only while run.py is imported.
"""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.dirname(HERE)
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)

import discord_rest  # noqa: E402
import sut  # noqa: E402

FAKE_TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAx.GabcDE.abcdefghijklmnopqrstuvwxyz0123"


# ---------------------------------------------------------------------
# Stubs for the modules run.py imports but this branch does not own.

def _stub_modules():
    sc = types.ModuleType("scenario")

    class ScenarioError(Exception):
        pass
    sc.ScenarioError = ScenarioError
    sc.SCENARIOS = []

    def load_all(directory, tier=None):
        return [s for s in sc.SCENARIOS
                if tier is None or int(s.get("tier", 1)) == int(tier)]

    def render(text, ids):
        for k in ("SUT", "A", "B"):
            text = text.replace(f"<@{k}>", f"<@{ids[k]}>")
        return text.replace("{SUT_NAME}", ids.get("SUT_NAME", ""))
    sc.load_all, sc.render, sc.validate = load_all, render, lambda d: d

    ck = types.ModuleType("checks")

    def evaluate(scen, transcript):
        out = []
        for c in (scen.get("expect") or {}).get("checks") or []:
            name, arg = next(iter(c.items()))
            if name == "sut_reply_count":
                n = sum(1 for m in transcript["messages"]
                        if m["author"] == "sut")
                out.append({"name": name, "ok": n == arg["eq"],
                            "detail": f"{n} SUT messages"})
            else:
                out.append({"name": name, "ok": True, "detail": "stub"})
        return out
    ck.evaluate = evaluate

    jd = types.ModuleType("judge")
    jd.VERDICT = {"verdict": "pass", "reason": "stub judge"}
    jd.calls = []

    def judge(scen, transcript, timeout=300):
        jd.calls.append(scen["id"])
        return dict(jd.VERDICT)
    jd.judge = judge
    return sc, ck, jd


def load_run():
    """Import run.py against the stubs, then put sys.modules back so
    other test files see the real modules."""
    names = ("scenario", "checks", "judge", "run")
    saved = {n: sys.modules.get(n) for n in names}
    sc, ck, jd = _stub_modules()
    sys.modules.update(scenario=sc, checks=ck, judge=jd)
    sys.modules.pop("run", None)
    try:
        run = importlib.import_module("run")
    finally:
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
    return run, sc, ck, jd


# ---------------------------------------------------------------------
# A scripted Discord.

class FakeHTTP:
    """Responses for Bot._open: (status, body dict/list/None, headers)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        status, body, headers = self.responses.pop(0)
        raw = json.dumps(body).encode() if body is not None else b""
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err",
                                         headers or {}, io.BytesIO(raw))
        return _Resp(raw)


class _Resp:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Guild:
    """In-memory channels shared by FakeBots; the "SUT" answers every
    message that mentions it or replies to it, up to sut_limit replies
    per channel."""

    def __init__(self, sut_id="900", sut_limit=99):
        self.n = 1000
        self.channels = {}
        self.deleted = []
        self.deleted_messages = []  # (deleting bot's id, message id)
        self.sut_id = sut_id
        self.sut_limit = sut_limit
        self.sut_replies = {}
        self.threads = []          # (bot id, parent, message id, name)
        self.no_threads = False    # start_thread -> 403
        self.no_thread_send = False  # send in a thread -> 403

    def new_id(self):
        self.n += 1
        return str(self.n)

    def post(self, cid, author_id, content, reply_to=None, files=None):
        m = {"id": self.new_id(), "channel_id": cid, "type": 0,
             "author": {"id": author_id, "username": f"u{author_id}",
                        "bot": True},
             "content": content, "timestamp": "2026-10-17T00:00:00Z",
             "mentions": [{"id": u} for u in
                          re.findall(r"<@(\d+)>", content)],
             "mention_everyone": False, "mention_roles": [],
             "attachments": [{"filename": os.path.basename(f)}
                             for f in files or []]}
        if reply_to:
            m["type"] = 19
            m["message_reference"] = {"message_id": reply_to}
        self.channels[cid].append(m)
        if author_id != self.sut_id:
            self._maybe_answer(m)
        return m

    def _maybe_answer(self, m):
        cid = m["channel_id"]
        ref = (m.get("message_reference") or {}).get("message_id")
        to_sut = any(x["id"] == ref and x["author"]["id"] == self.sut_id
                     for x in self.channels[cid])
        mentioned = any(u["id"] == self.sut_id for u in m["mentions"])
        if (to_sut or mentioned) \
                and self.sut_replies.get(cid, 0) < self.sut_limit:
            self.sut_replies[cid] = self.sut_replies.get(cid, 0) + 1
            self.post(cid, self.sut_id, "ack 1", reply_to=m["id"])


class FakeBot:
    def __init__(self, guild, uid):
        self.guild, self.uid = guild, uid

    def me(self):
        return {"id": self.uid, "username": f"bot{self.uid}"}

    def typing(self, cid):
        return None

    def send(self, cid, content, reply_to=None, files=None):
        if self.guild.no_thread_send and any(
                t[2] == cid for t in self.guild.threads):
            raise discord_rest.DiscordError(
                "POST -> HTTP 403: Missing Permissions", status=403)
        return self.guild.post(cid, self.uid, content, reply_to, files)

    def start_thread(self, cid, mid, name):
        """Like Discord: the thread's id is the message's id, its first
        message a THREAD_STARTER_MESSAGE (type 21) with that same id,
        and the parent may get a THREAD_CREATED (type 18) notice."""
        if self.guild.no_threads:
            raise discord_rest.DiscordError(
                "POST -> HTTP 403: Missing Permissions", status=403)
        self.guild.threads.append((self.uid, cid, mid, name))
        self.guild.channels[mid] = [{
            "id": mid, "channel_id": mid, "type": 21, "content": "",
            "author": {"id": self.uid}, "message_reference": {
                "message_id": mid, "channel_id": cid}}]
        self.guild.channels[cid].append({
            "id": self.guild.new_id(), "channel_id": cid, "type": 18,
            "content": name, "author": {"id": self.uid}})
        return {"id": mid, "type": 11, "parent_id": cid, "name": name}

    def history(self, cid, after=None):
        return [m for m in self.guild.channels.get(cid, [])
                if int(m["id"]) > int(after or 0)]

    def create_channel(self, gid, name):
        cid = self.guild.new_id()
        self.guild.channels[cid] = []
        return {"id": cid, "name": name}

    def delete_channel(self, cid):
        self.guild.deleted.append(cid)

    def delete_message(self, cid, mid):
        self.guild.channels[cid] = [m for m in self.guild.channels[cid]
                                    if m["id"] != mid]
        self.guild.deleted_messages.append((self.uid, mid))

    def guild_channels(self, gid):
        return []


class FakeSUT:
    """Stands in for sut.SUT in run.py tests."""
    fail_ready = False
    instances = []

    def __init__(self, tree=None, run_dir=None, base_config=None,
                 token=None):
        self.pkg_dir = tempfile.mkdtemp(prefix="fakesut-")
        self.gateway = True
        self.config = base_config
        self.running = False
        self.calls = []
        self.die_after = None  # alive() turns False after N checks
        self.cleaned = False
        self.configs = []      # every config start() was given
        FakeSUT.instances.append(self)

    def effective_config(self, cfg):
        return cfg

    def alive(self):
        if self.die_after is not None:
            self.die_after -= 1
            if self.die_after < 0:
                self.running = False
        return self.running

    def start(self, gateway=True, config=None):
        self.calls.append(("start", gateway))
        self.gateway = gateway
        if config is not None:
            self.config = config
            self.configs.append(config)
        self.running = True

    def stop(self, kill=False):
        self.calls.append(("kill" if kill else "stop",))
        self.running = False

    def restart(self, gateway=None, config=None, kill=False):
        self.stop(kill)
        self.start(self.gateway if gateway is None else gateway, config)

    def wait_ready(self, timeout=180):
        self.calls.append(("ready",))
        if FakeSUT.fail_ready:
            raise sut.SUTError("not ready (fake)")
        return True

    def read_json(self, name):
        return {"entries": {}} if name == "mention_ledger.json" else None

    def log_tail(self, n=200):
        return f"log line; token={FAKE_TOKEN}"

    def log_size(self):
        return 7

    def log_since(self, offset, max_bytes=None):
        return f"since {offset}: Queued mention; token={FAKE_TOKEN}"

    def cleanup(self):
        self.cleaned = True
        self.running = False
        shutil.rmtree(self.pkg_dir, ignore_errors=True)


class SettingsSUT(FakeSUT):
    """A FakeSUT with the real muse_settings.py and plugins in pkg, a
    real config.yaml, and a monitor.log that, like the monitor's
    settings Watcher, logs "Settings reloaded" once per change of
    config.discord.yaml it notices (on any log read while running)."""
    reload_line = "Settings reloaded (config.yaml or config.discord.yaml " \
        "changed).\n"

    def __init__(self, tree=None, run_dir=None, base_config=None,
                 token=None):
        super().__init__(tree, run_dir, base_config, token)
        shutil.copy(os.path.join(REPO, "muse_settings.py"), self.pkg_dir)
        shutil.copytree(os.path.join(REPO, "plugins"),
                        os.path.join(self.pkg_dir, "plugins"))
        self.log = ""
        self.overlays = []  # the overlay doc at each logged reload
        self.react = self.reload_line
        self._sig = None
        if base_config is not None:
            self._write(base_config)

    def _write(self, cfg):
        token = os.path.join(self.pkg_dir, "tok")
        persona = os.path.join(self.pkg_dir, "persona.md")
        for path in (token, persona):
            with open(path, "w") as f:
                f.write("x\n")
        stand_in = types.SimpleNamespace(token_file=token,
                                         persona_file=persona)
        import yaml
        with open(os.path.join(self.pkg_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(sut.SUT.effective_config(stand_in, cfg), f)

    def overlay(self):
        return os.path.join(self.pkg_dir, sut.OVERLAY_NAME)

    def _overlay_sig(self):
        try:
            st = os.stat(self.overlay())
            return (st.st_mtime_ns, st.st_ino, st.st_size)
        except OSError:
            return None

    def start(self, gateway=True, config=None):
        super().start(gateway, config)
        if config is not None:
            self._write(config)
        self._sig = self._overlay_sig()
        self.log += "===== e2e: start monitor =====\n"

    def _poll(self):
        sig = self._overlay_sig()
        if self.running and sig != self._sig:
            self._sig = sig
            self.log += self.react
            import yaml
            try:
                with open(self.overlay()) as f:
                    self.overlays.append(yaml.safe_load(f))
            except OSError:
                self.overlays.append(None)

    def log_size(self):
        self._poll()
        return len(self.log)

    def log_since(self, offset, max_bytes=None):
        self._poll()
        return self.log[offset:]


# ---------------------------------------------------------------------

class DiscordRestTests(unittest.TestCase):
    def test_retries_and_error_classification(self):
        """429 waits retry_after; 5xx retries, then a transient error (a
        flake); 4xx is not transient (a failure) and never shows the
        token."""
        slept = []

        def bot(responses):
            b = discord_rest.Bot(FAKE_TOKEN, sleep=slept.append)
            b._open = FakeHTTP(responses)
            return b
        b = bot([(429, {"retry_after": 1.5}, {}),
                 (200, {"id": "1", "username": "x"}, {})])
        self.assertEqual(b.me()["id"], "1")
        self.assertAlmostEqual(slept[0], 1.6, places=3)
        b = bot([(502, {"message": "bad gateway"}, {})] * 5)
        with self.assertRaises(discord_rest.DiscordError) as cm:
            b.me()
        self.assertTrue(cm.exception.transient)
        b = bot([(403, {"message": "Missing Permissions"}, {})])
        with self.assertRaises(discord_rest.DiscordError) as cm:
            b.create_channel("1", "e2e-x")
        self.assertFalse(cm.exception.transient)
        self.assertNotIn(FAKE_TOKEN, str(cm.exception) + repr(b))
        # Driver replies never ping the replied-to author, or Discord
        # would list the SUT in a reply's mentions (reply != mention).
        b = bot([(200, {"id": "9"}, {})])
        b.send("55", "hi", reply_to="7")
        body = json.loads(b._open.requests[0].data)
        self.assertEqual(body["allowed_mentions"],
                         {"parse": ["users"], "replied_user": False})


FAKE_MONITOR = r'''
import json, os, sys, time
import yaml
cfg = yaml.safe_load(open(os.environ["MUSE_DISCORD_CONFIG"]))
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "env_seen.json"), "w") as f:
    json.dump(sorted(k for k in os.environ if k.startswith("E2E_")
                     or k == "CLAUDE_CODE_OAUTH_TOKEN"), f)
if (cfg.get("testing") or {}).get("crash"):
    print("boom", flush=True)
    sys.exit(2)
print("Bot user id: 1", flush=True)
print("muse-discord monitor starting. Polling 1 channels.", flush=True)
try:
    import websockets  # noqa: F401
    print("Gateway READY: realtime ingest live.", flush=True)
except ImportError as e:
    if "e2e harness" in str(e):
        print(f"GATEWAY DISABLED (thread died: {e})", flush=True)
    else:  # not installed here: pretend the gateway came up
        print("Gateway READY: realtime ingest live.", flush=True)
while True:
    time.sleep(0.2)
'''


class SUTTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-sut-")
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.tree, ".git"))
        os.makedirs(os.path.join(self.tree, "bin"))
        with open(os.path.join(self.tree, "monitor.py"), "w") as f:
            f.write(FAKE_MONITOR)
        for name in ("state.json", "config.yaml", ".git/HEAD",
                     "config.discord.yaml", "owner_dm_nonces.json",
                     "bin/outbox-append.py", "update_check.json"):
            with open(os.path.join(self.tree, name), "w") as f:
                f.write("{}\n")
        self.cfg = sut.scenario_config(sut.base_config("10", "20", "30"),
                                       {}, "20", "30")
        os.environ["E2E_SUT_TOKEN"] = FAKE_TOKEN
        self.s = sut.SUT(self.tree, os.path.join(self.tmp, "run"), self.cfg,
                         token=FAKE_TOKEN)

    def tearDown(self):
        os.environ.pop("E2E_SUT_TOKEN", None)
        self.s.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_token_and_config(self):
        """The PR's code runs from a copy without the developer's state
        or overlay; the token sits outside it, mode 600, and only
        config.yaml points at it."""
        pkg = self.s.pkg_dir
        self.assertTrue(os.path.exists(os.path.join(pkg, "bin",
                                                    "outbox-append.py")))
        for gone in (".git", "state.json", "config.discord.yaml",
                     "owner_dm_nonces.json", "update_check.json"):
            self.assertFalse(os.path.exists(os.path.join(pkg, gone)), gone)
        self.assertEqual(stat.S_IMODE(os.stat(self.s.token_file).st_mode),
                         0o600)
        self.assertFalse(self.s.token_file.startswith(pkg))
        import yaml
        with open(os.path.join(pkg, "config.yaml")) as f:
            cfg = yaml.safe_load(f)
        self.assertEqual(cfg["token_from"], self.s.token_file)
        self.assertEqual(cfg["engine"]["rest_poll_seconds"], 10)
        self.assertEqual(cfg["presence"]["enabled"], "off")
        self.assertEqual(cfg["updates"]["notify"], "off")
        self.assertNotIn(FAKE_TOKEN, json.dumps(cfg))
        self.s.cleanup()
        self.assertFalse(os.path.exists(self.s.token_file))

    def test_lifecycle_gateway_off_and_death(self):
        s = self.s
        s.start()
        self.assertTrue(s.wait_ready(20, poll=0.05))
        with open(os.path.join(s.pkg_dir, "env_seen.json")) as f:
            self.assertEqual(json.load(f), [])  # no secrets in its env
        s.restart(gateway=False)
        self.assertTrue(s.wait_ready(20, poll=0.05))
        self.assertIn("GATEWAY DISABLED", s.log_tail(5))
        s.restart(gateway=True)
        self.assertTrue(s.wait_ready(20, poll=0.05))
        s.stop(kill=True)
        self.assertFalse(s.alive())
        s.start(config=sut.deep_merge(self.cfg, {"testing": {"crash": True}}))
        with self.assertRaises(sut.SUTError):
            s.wait_ready(20, poll=0.05)


class RunFlowBase(unittest.TestCase):
    """run.py end to end against a fake Discord and a fake SUT."""

    ENV = {"E2E_GUILD_ID": "10", "E2E_SUT_TOKEN": FAKE_TOKEN,
           "E2E_DRIVER_A_TOKEN": "tokA-secret", "E2E_DRIVER_B_TOKEN":
           "tokB-secret"}

    def setUp(self):
        self.R, self.sc, self.ck, self.jd = load_run()
        self.out = tempfile.mkdtemp(prefix="e2e-out-")
        self.guild = Guild(sut_id="900")
        ids = {FAKE_TOKEN: "900", "tokA-secret": "20", "tokB-secret": "30"}
        guild = self.guild
        self.patches = [
            (self.R.discord_rest, "Bot",
             lambda token: FakeBot(guild, ids[token])),
            (self.R.sut, "SUT", FakeSUT),
            (self.R, "start_worker", lambda *a: types.SimpleNamespace(
                poll=lambda: None, pid=-1)),
            (self.R, "stop_worker", lambda proc: None),
            (self.R.time, "sleep", lambda s: None),
        ]
        self.saved = [(o, n, getattr(o, n)) for o, n, _ in self.patches]
        for o, n, v in self.patches:
            setattr(o, n, v)
        self.saved_env = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)
        FakeSUT.fail_ready = False
        FakeSUT.instances = []
        self.fake_clock()

    def tearDown(self):
        for o, n, v in self.saved:
            setattr(o, n, v)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.out, ignore_errors=True)

    def main(self, *extra, tier=1):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.R.main(["--tree", REPO, "--tier", str(tier),
                                "--out", self.out, *extra])
        return code, buf.getvalue()

    def outputs(self):
        with open(os.path.join(self.out, "results.json")) as f:
            results = json.load(f)
        with open(os.path.join(self.out, "summary.md")) as f:
            summary = f.read()
        return results, summary

    def transcript(self, sid):
        with open(os.path.join(self.out, "transcripts",
                               f"{sid}.json")) as f:
            return json.load(f)

    def assert_no_tokens(self):
        for root, _, files in os.walk(self.out):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    text = f.read()
                for tok in self.ENV.values():
                    if tok != "10":
                        self.assertNotIn(tok, text, name)

    def fake_clock(self):
        """Runners sleep on a fake clock."""
        clock = [0.0]
        orig = self.R.Runner.__init__

        def init(runner, *a, **kw):
            orig(runner, *a, **kw)
            runner.clock = lambda: clock[0]
            runner.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
        self.R.Runner.__init__ = init
        self.addCleanup(setattr, self.R.Runner, "__init__", orig)

    def runner(self, the_sut, clock=None):
        """A Runner on the_sut with a fake clock (sleep advances it)."""
        cfg = sut.scenario_config(sut.base_config("10", "20", "30"), {},
                                  "20", "30")
        t = clock or [0.0]
        r = self.R.Runner(the_sut, {}, None,
                          {"SUT": "900", "A": "20", "B": "30"}, cfg,
                          sleep=lambda s: t.__setitem__(0, t[0] + s),
                          clock=lambda: t[0])
        return r, cfg


class RunFlowTests(RunFlowBase):
    def test_full_run(self):
        self.sc.SCENARIOS = [
            {"id": "mention_basic", "tier": 1, "title": "t",
             "steps": [{"send": {"as": "a", "text": "<@SUT> hi",
                                 "label": "q1"}},
                       {"wait_for": {"sut_messages": 1, "timeout": 30}},
                       {"send": {"as": "b", "text": "and me <@SUT>",
                                 "reply_to": "last_sut", "label": "q2"}},
                       {"action": "gateway_off"},
                       {"action": "kill_monitor"},
                       {"action": "start_monitor"}],
             "expect": {"settle": 1,
                        "checks": [{"sut_reply_count": {"eq": 2}}]}},
            {"id": "too_many", "tier": 1, "title": "t", "b_is_bot": True,
             "config": {"everywhere": {"who": {"bots": "ignore"}}},
             "steps": [{"send": {"as": "a", "text": "<@SUT> hi"}}],
             "expect": {"checks": [{"sut_reply_count": {"eq": 5}}]}},
            {"id": "tier_two", "tier": 2, "title": "t", "steps": []},
        ]
        code, out = self.main()
        self.assertEqual(code, 1, out)  # too_many fails its check
        results, summary = self.outputs()
        by = {r["scenario"]: r for r in results}
        self.assertEqual(set(by), {"mention_basic", "too_many"})
        self.assertEqual(by["mention_basic"]["status"], "pass", by)
        self.assertEqual(by["too_many"]["status"], "fail")
        tr = self.transcript("mention_basic")
        self.assertEqual([m["author"] for m in tr["messages"]],
                         ["a", "sut", "b", "sut"])
        self.assertEqual(tr["messages"][2]["reply_to"],
                         tr["messages"][1]["id"])
        self.assertEqual(tr["monitor_log"],
                         "since 7: Queued mention; token=[REDACTED]")
        self.assertEqual(sorted(self.guild.deleted),
                         sorted(self.guild.channels))
        fs = FakeSUT.instances[0]
        self.assertTrue(fs.cleaned)
        self.assertIn(("start", False), fs.calls)   # gateway_off
        self.assertIn(("kill",), fs.calls)
        # too_many ran under its own config, B not treated as human.
        self.assertEqual(fs.config["everywhere"]["who"]["bots"], "ignore")
        self.assertEqual(fs.config["testing"]["treat_as_human"], ["20"])
        self.assertIn(": FAIL", summary)
        self.assert_no_tokens()

    def test_tier2_judge_and_converse(self):
        self.guild.sut_limit = 3
        self.jd.VERDICT = {"verdict": "fail", "reason": "too chatty"}
        self.sc.SCENARIOS = [
            {"id": "loop", "tier": 2, "title": "t", "b_is_bot": True,
             "steps": [{"send": {"as": "b", "text": "<@SUT> hey"}},
                       {"converse": {"as": "b", "lines": ["1", "2", "3",
                                                          "4", "5"],
                                     "per_turn_timeout": 5}}],
             "expect": {"checks": []}, "judge": {"rubric": "x"}},
        ]
        code, out = self.main(tier=2)
        self.assertEqual(code, 0, out)  # a judge's "fail" is a warning
        results, summary = self.outputs()
        self.assertEqual(results[0]["status"], "warn")
        self.assertEqual(self.transcript("loop")["converse_turns"], 3)
        self.assertIn("too chatty", summary)

    def test_monitor_trouble_is_a_flake(self):
        FakeSUT.fail_ready = True
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": []},
                             {"id": "s2", "tier": 1, "title": "t",
                              "steps": []}]
        code, out = self.main()
        self.assertEqual(code, 0, out)
        results, summary = self.outputs()
        self.assertEqual([r["status"] for r in results], ["flake", "flake"])
        self.assertIn("FLAKED", summary)
        self.assertEqual(len(self.guild.deleted), 2)
        self.assertTrue(FakeSUT.instances[0].cleaned)

    def test_sut_death_mid_scenario_is_a_flake(self):
        orig_start = FakeSUT.start

        def start(fs, gateway=True, config=None):
            orig_start(fs, gateway, config)
            fs.die_after = 3
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": [{"wait": 1}] * 10}]
        FakeSUT.start = start
        try:
            self.main()
        finally:
            FakeSUT.start = orig_start
        results, _ = self.outputs()
        self.assertEqual(results[0]["status"], "flake", results)
        self.assertIn("exited unexpectedly", results[0]["error"])

    def test_wait_for_timeouts_continue(self):
        """A step that times out is a note: the scenario goes on and the
        checks decide."""
        self.guild.sut_limit = 0  # the SUT never answers
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": [
                                  {"send": {"as": "a", "text": "<@SUT> x",
                                            "label": "q1"}},
                                  {"wait_for": {"sut_messages": 1,
                                                "timeout": 10}},
                                  {"send": {"as": "b", "text": "after"}}],
                              "expect": {"checks": [
                                  {"sut_reply_count": {"eq": 1}}]}}]
        code, out = self.main()
        self.assertEqual(code, 1)
        r = self.outputs()[0][0]
        self.assertEqual((r["status"], r["error"]), ("fail", None))
        self.assertEqual(len(r["notes"]), 1, r)
        self.assertEqual([m["author"] for m in
                          self.transcript("s1")["messages"]], ["a", "b"])

    def test_threads_join_the_transcript(self):
        self.sc.SCENARIOS = [{
            "id": "threads", "tier": 1, "title": "t",
            "steps": [
                {"send": {"as": "a", "text": "<@SUT> hi", "label": "q1"}},
                {"thread": {"from": "q1", "name": "side chat",
                            "label": "t1"}},
                {"send": {"as": "b", "in": "t1", "text": "<@SUT> in t1",
                          "label": "q2"}},
                {"send": {"as": "a", "in": "t1", "text": "thanks",
                          "reply_to": "last_sut", "label": "q3"}},
                {"wait_for": {"sut_messages": 3, "timeout": 5}}],
            "expect": {"checks": [{"sut_reply_count": {"eq": 3}}]}}]
        code, out = self.main()
        self.assertEqual(code, 0, out)
        results, _ = self.outputs()
        self.assertEqual(results[0]["notes"], [])  # wait_for saw all 3
        tr = self.transcript("threads")
        self.assertEqual(tr["threads"], {"t1": tr["labels"]["q1"]})
        # One list sorted by id; no starter / THREAD_CREATED messages.
        self.assertEqual(
            [(m["author"], m.get("thread")) for m in tr["messages"]],
            [("a", None), ("sut", None), ("b", "t1"), ("sut", "t1"),
             ("a", "t1"), ("sut", "t1")])
        # last_sut inside the thread is the SUT's reply in the thread.
        by_id = {m["id"]: m for m in tr["messages"]}
        r2 = by_id[tr["labels"]["q3"]]["reply_to"]
        self.assertEqual(by_id[r2]["reply_to"], tr["labels"]["q2"])

    def test_missing_secrets_skip(self):
        os.environ.pop("E2E_DRIVER_B_TOKEN")
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": []}]
        code, out = self.main()
        self.assertEqual(code, 0)
        results, summary = self.outputs()
        self.assertEqual(results[0]["status"], "skip")
        self.assertEqual(self.guild.channels, {})


class SettingsStepTests(RunFlowBase):
    """The settings step writes what /settings writes, and the overlay
    never outlives its scenario."""

    def setUp(self):
        super().setUp()
        self.R.sut.SUT = SettingsSUT

    def test_settings_steps_then_live_clear(self):
        self.sc.SCENARIOS = [
            {"id": "live", "tier": 1, "title": "t",
             "steps": [
                 {"settings": {"set": "who.replies", "value": "off",
                               "scope": "server"}},
                 {"settings": {"set": "who.replies",
                               "value": "when-addressed",
                               "scope": "channel"}},
                 {"settings": {"undo": "channel"}},
                 {"settings": {"undo": "dms"}}],  # nothing to undo
             "expect": {"checks": []}},
            {"id": "next", "tier": 1, "title": "t",
             "steps": [{"wait": 1}], "expect": {"checks": []}},
        ]
        code, out = self.main()
        self.assertEqual(code, 0, out)
        results, _ = self.outputs()
        self.assertEqual([r["status"] for r in results], ["pass", "pass"])
        self.assertEqual(results[0]["notes"],
                         ["settings undo at dms: nothing to undo"])
        fs = FakeSUT.instances[0]
        cid = self.transcript("live")["channel_id"]
        chan_key = f'servers."10".channels."{cid}".replies'
        values = [d["values"] if d else None for d in fs.overlays]
        # One reload per write, with the key /settings writes, by A.
        self.assertEqual(values[0]['servers."10".who.replies']["value"],
                         "off")
        self.assertEqual(values[1][chan_key]["by"], "20")
        self.assertNotIn(chan_key, values[2])          # undone
        tr = self.transcript("live")
        self.assertEqual(tr["monitor_log"].count("Settings reloaded"), 3)
        self.assertEqual(tr["monitor_restarts"], 0)
        # Before the next scenario the overlay is deleted and the
        # running monitor reloads: no restart, and the reload is not in
        # the next scenario's log.
        self.assertIsNone(values[3])
        self.assertFalse(os.path.exists(fs.overlay()))
        self.assertEqual(fs.calls.count(("ready",)), 1)  # the first start
        self.assertNotIn("Settings reloaded",
                         self.transcript("next")["monitor_log"])

    def test_no_reload_is_a_note_or_a_restart(self):
        fs = SettingsSUT(base_config=sut.base_config("10", "20", "30"))
        self.addCleanup(fs.cleanup)
        r, cfg = self.runner(fs)
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        r.ensure_config(cfg)
        fs.react = ""  # the monitor never logs the reload
        step = {"set": "who.replies", "value": "off", "scope": "server",
                "timeout": 3}
        self.assertFalse(r.settings(step, "1001"))
        self.assertIn('no "Settings reloaded"', r.notes[0])
        fs.react = "Config re-read failed; keeping the previous\n"
        step["value"] = "when-addressed"
        self.assertFalse(r.settings(step, "1001"))
        self.assertIn("rejected the change", r.notes[1])
        # An overlay the monitor does not reload away: deleted, then
        # the monitor is restarted.
        fs.react = ""
        n = len(fs.calls)
        self.assertIsNotNone(r.ensure_config(cfg))
        self.assertFalse(os.path.exists(fs.overlay()))
        self.assertEqual(fs.calls[n:], [("stop",), ("start", True),
                                        ("ready",)])

    def test_a_failed_restart_ends_the_run(self):
        self.sc.SCENARIOS = [
            {"id": "s1", "tier": 1, "title": "t", "b_is_bot": True,
             "steps": [], "expect": {"checks": []}},
            {"id": "s2", "tier": 1, "title": "t", "steps": [],
             "expect": {"checks": []}}]
        orig = FakeSUT.wait_ready

        def ready(fs, timeout=180):
            if fs.calls.count(("ready",)) >= 1:
                raise sut.SUTError("not ready (fake)")
            return orig(fs, timeout)
        FakeSUT.wait_ready = ready
        try:
            code, out = self.main()
        finally:
            FakeSUT.wait_ready = orig
        results, _ = self.outputs()
        self.assertEqual([r["status"] for r in results], ["flake", "flake"])
        self.assertIn("could not be (re)started", results[0]["error"])
        # One failed restart, not one more per remaining scenario.
        self.assertEqual(FakeSUT.instances[0].calls.count(("start", True)),
                         2)


class DryRunProcessTest(unittest.TestCase):
    """--dry-run as a real process with an empty environment: the
    shipped scenarios load and the plan prints, with no network."""

    def test_no_env_no_network(self):
        for tier in ("1", "2"):
            p = subprocess.run([sys.executable, os.path.join(E2E, "run.py"),
                                "--tree", REPO, "--tier", tier,
                                "--dry-run"],
                               env={"PATH": os.environ.get("PATH", "")},
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn(f"# Plan: tier {tier}", p.stdout)


if __name__ == "__main__":
    unittest.main()
