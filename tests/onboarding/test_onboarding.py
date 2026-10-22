"""Onboarding: bin/connect-discord.py, bin/render-worker-prompt.py,
bin/doctor.py, and the bot staying quiet in a server none of its owners
is in.

Every Discord call goes to a local fake (MUSE_DISCORD_API_BASE); the
scripts run from a scratch copy of the package, with HOME in scratch, so
nothing touches the tree under test or the real token file.
"""
import base64
import http.server
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

BOT_ID = "1300000000000000001"
OWNER_ID = "1100000000000000001"
APP_ID = BOT_ID
TOKEN = (base64.urlsafe_b64encode(BOT_ID.encode()).decode().rstrip("=")
         + ".GaBcDe." + "x7" * 19)
MC_LIMITED = 1 << 19


class FakeDiscord:
    """A tiny Discord REST stand-in. app is what GET /applications/@me
    returns; patch_status 200 applies PATCH flags, anything else
    refuses it."""

    def __init__(self):
        self.requests = []
        self.app = {"id": APP_ID, "flags": 0, "bot_public": False,
                    "owner": {"id": OWNER_ID, "username": "zach"}}
        self.patch_status = 200
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                path = self.path.split("/api/v10", 1)[-1]
                fake.requests.append((method, path, body,
                                      self.headers.get("Authorization")))
                if self.headers.get("Authorization") != f"Bot {TOKEN}":
                    return self._send(401, {"code": 0,
                                            "message": "401: Unauthorized"})
                if (method, path) == ("GET", "/users/@me"):
                    return self._send(200, {"id": BOT_ID, "username": "Pip",
                                            "bot": True})
                if (method, path) == ("GET", "/applications/@me"):
                    return self._send(200, fake.app)
                if (method, path) == ("PATCH", "/applications/@me"):
                    if fake.patch_status != 200:
                        return self._send(fake.patch_status,
                                          {"code": 50013,
                                           "message": "Missing Permissions"})
                    fake.app["flags"] = body["flags"]
                    return self._send(200, fake.app)
                return self._send(404, {"code": 10004, "message": "nope"})

            def do_GET(self):
                self._handle("GET")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_POST(self):
                self._handle("POST")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, args=(0.02,),
                         daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/api/v10"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_pkg():
    """A scratch copy of what setup touches, plus a scratch HOME."""
    root = scratch("muse-onb-")
    pkg = os.path.join(root, "pkg")
    os.makedirs(pkg)
    for name in ("muse_settings.py", "config.example.yaml", "persona.md",
                 "worker-prompt.template.md"):
        shutil.copy2(os.path.join(MUT_DIR, name), pkg)
    for d in ("bin", "plugins"):
        shutil.copytree(os.path.join(MUT_DIR, d), os.path.join(pkg, d))
    home = os.path.join(root, "home")
    os.makedirs(home)
    return pkg, home


def run(pkg, home, script, args=(), stdin="", api=None):
    env = dict(os.environ, HOME=home)
    env.pop("MUSE_DISCORD_CONFIG", None)
    if api:
        env["MUSE_DISCORD_API_BASE"] = api
    return subprocess.run(
        [sys.executable, os.path.join(pkg, "bin", script), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=60)


def all_files(*roots):
    for root in roots:
        for d, _dirs, files in os.walk(root):
            for f in files:
                yield os.path.join(d, f)


def load_ms(pkg):
    spec = importlib.util.spec_from_file_location(
        f"ms_{id(pkg)}", os.path.join(pkg, "muse_settings.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConnectDiscord(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDiscord()
        self.addCleanup(self.fake.close)
        self.pkg, self.home = make_pkg()
        self.cfg = os.path.join(self.pkg, "config.yaml")
        self.token_file = os.path.join(self.home, ".config", "muse-discord",
                                       "bot_token")

    def connect(self, paste, *args):
        """Run it the SETUP.md way: the paste in a file in the private
        incoming dir, named by --token-file."""
        incoming = os.path.join(self.home, ".config", "muse-discord",
                                "incoming")
        os.makedirs(incoming, mode=0o700, exist_ok=True)
        drop = os.path.join(incoming, "token")
        with open(drop, "w") as f:
            f.write(paste + "\n")
        r = run(self.pkg, self.home, "connect-discord.py",
                ["--json", "--name", "Pip", "--token-file", drop, *args],
                "", self.fake.base)
        out = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertFalse(os.path.exists(drop), "the drop-off file stays")
        return r, out

    def test_wrong_pastes_are_refused_and_nothing_is_written(self):
        cases = {
            "id": APP_ID,
            "public_key": "ab12" * 16,
            "client_secret": "Zx9_" * 8,
            "url": "https://discord.com/developers/applications/" + APP_ID,
            "empty": "",
            "user_token": "mfa." + "a" * 80,
        }
        for kind, paste in cases.items():
            with self.subTest(kind):
                r, _out = self.connect(paste)
                self.assertEqual(r.returncode, 2, r.stderr)
                if paste:
                    self.assertNotIn(paste, r.stdout + r.stderr)
        self.assertEqual(self.fake.requests, [])  # nothing left the box
        self.assertFalse(os.path.exists(self.token_file))
        self.assertFalse(os.path.exists(self.cfg))

    def test_token_file_must_be_private(self):
        """A drop-off file other accounts can read is refused, unsent."""
        drop = os.path.join(self.home, "token")
        with open(drop, "w") as f:
            f.write(TOKEN)
        os.chmod(self.home, 0o755)
        os.chmod(drop, 0o644)
        r = run(self.pkg, self.home, "connect-discord.py",
                ["--json", "--token-file", drop], "", self.fake.base)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)["problem"], "token_file")
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_rejected_token_writes_nothing(self):
        bad = TOKEN[:-4] + "zzzz"
        r, _out = self.connect(bad)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.token_file))
        self.assertFalse(os.path.exists(self.cfg))
        self.assertNotIn(bad, r.stdout + r.stderr)

    def test_connects_and_the_token_stays_in_its_file(self):
        r, out = self.connect(f"  `{TOKEN}`  ")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out["owner_id"], OWNER_ID)
        self.assertIn(f"client_id={APP_ID}", out["invite_url"])
        self.assertIn("permissions=2252074691710016", out["invite_url"])
        self.assertIn("integration_type=0", out["invite_url"])
        # The intent was turned on through the API, and checked.
        patches = [b for m, p, b, _a in self.fake.requests if m == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0]["flags"] & MC_LIMITED)
        self.assertTrue(out["message_content"])
        # The token: only in the token file, mode 600.
        with open(self.token_file) as f:
            self.assertEqual(f.read().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode),
                         0o600)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        for path in all_files(self.pkg, self.home):
            if path == self.token_file:
                continue
            with open(path, "rb") as f:
                self.assertNotIn(TOKEN.encode(), f.read(), path)
        # The config validates, names the owner, and the monitor loads it.
        ms = load_ms(self.pkg)
        st = ms.load(self.cfg, os.path.join(self.pkg, "plugins"))
        self.assertEqual(st.owners, [OWNER_ID])
        self.assertEqual(st.token_sources(),
                         [("file", "~/.config/muse-discord/bot_token")])
        self.assertEqual(monitor_owners(self.cfg), {OWNER_ID})
        with open(os.path.join(self.pkg, "persona.md")) as f:
            self.assertIn("Your name is Pip.", f.read())
        # A persona the owner wrote is never replaced.
        mine = "# Persona\n\nYou are Pip the pirate.\n"
        with open(os.path.join(self.pkg, "persona.md"), "w") as f:
            f.write(mine)
        r, _out = self.connect(TOKEN, "--name", "Other")
        with open(os.path.join(self.pkg, "persona.md")) as f:
            self.assertEqual(f.read(), mine)

    def test_intent_refused_falls_back_to_the_portal_steps(self):
        self.fake.patch_status = 403
        r, out = self.connect(TOKEN)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertFalse(out["message_content"])
        self.assertIn("intent", [s["kind"] for s in out["owner_steps"]])
        # Everything else is still saved.
        self.assertTrue(os.path.exists(self.token_file))
        self.assertTrue(os.path.exists(self.cfg))

    def test_an_existing_config_is_never_lost(self):
        # Invalid (a typo): left untouched, nothing written at all, so
        # the running bot keeps its last good settings.
        broken = ('version: 1\nowners: ["1100000000000000009"]\n'
                  'all_servers:\n  who:\n    replies: off\n'
                  'everywhere:\n  style:\n    lenght: brief\n')
        with open(self.cfg, "w") as f:
            f.write(broken)
        r, out = self.connect(TOKEN)
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertEqual(out.get("problem"), "config")
        with open(self.cfg) as f:
            self.assertEqual(f.read(), broken)
        self.assertFalse(os.path.exists(self.token_file))
        # Valid, with another owner: backed up byte for byte, both kept.
        mine = ('version: 1\nowners: ["1100000000000000009"]\n'
                'everywhere:\n  style:\n    length: brief\n')
        with open(self.cfg, "w") as f:
            f.write(mine)
        r, _out = self.connect(TOKEN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        baks = [f for f in os.listdir(self.pkg)
                if f.startswith("config.yaml.bak-")]
        self.assertEqual(len(baks), 1)
        with open(os.path.join(self.pkg, baks[0])) as f:
            self.assertEqual(f.read(), mine)
        st = load_ms(self.pkg).load(self.cfg,
                                    os.path.join(self.pkg, "plugins"))
        self.assertEqual(set(st.owners), {OWNER_ID, "1100000000000000009"})
        # Run again: the config already lists the owner, so it is kept
        # as is, with no further backup.
        with open(self.cfg) as f:
            before = f.read()
        r, _out = self.connect(TOKEN)
        with open(self.cfg) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(len([f for f in os.listdir(self.pkg)
                              if f.startswith("config.yaml.bak-")]), 1)


def monitor_owners(cfg):
    """Import monitor.py from the tree under test against cfg (the
    settings harness way) and return the owners it loaded."""
    from settings_harness import load_monitor  # noqa: E402
    with open(cfg) as f:
        mod = load_monitor(f.read())
    return set(mod.OWNER_USER_IDS)


class RenderWorkerPrompt(unittest.TestCase):
    def test_rendered_prompt_is_complete_and_e2e_uses_it(self):
        pkg, home = make_pkg()
        r = run(pkg, home, "render-worker-prompt.py",
                ["--media", "none", "--out", "-"])
        self.assertEqual(r.returncode, 0, r.stderr)
        text = r.stdout
        for leftover in ("<package dir>", "Installed during setup",
                         "the installing Muse fills"):
            self.assertNotIn(leftover, text)
        self.assertIn(f"python3 {pkg}/bin/outbox-append.py", text)
        self.assertNotIn("=== SEAM: PERSONA ===\nInstalled", text)
        # The e2e worker renders with the same code.
        sys.path.insert(0, os.path.join(MUT_DIR, "e2e"))
        import claude_worker
        with open(os.path.join(pkg, "worker-prompt.template.md")) as f:
            rendered, warnings = claude_worker.render_prompt(f.read(), pkg)
        self.assertEqual(warnings, [])
        self.assertEqual(rendered, text)


class Doctor(unittest.TestCase):
    def test_dead_monitor_fails(self):
        fake = FakeDiscord()
        self.addCleanup(fake.close)
        pkg, home = make_pkg()
        tf = os.path.join(home, "token")
        with open(tf, "w") as f:
            f.write(TOKEN)
        os.chmod(tf, 0o600)
        with open(os.path.join(pkg, "config.yaml"), "w") as f:
            f.write(f'version: 1\nowners: ["{OWNER_ID}"]\n'
                    f'token_from: {tf}\n')
        with open(os.path.join(pkg, "monitor.pid"), "w") as f:
            f.write("999999999\n")  # no such process

        def problems():
            r = run(pkg, home, "doctor.py", ["--json", "--quick"],
                    api=fake.base)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            res = json.loads(r.stdout)
            self.assertFalse(res["ok"])
            return [x for x in res["results"] if x["status"] == "problem"]
        dead = problems()
        self.assertTrue(dead)
        # The monitor's mark of an outdated worker prompt: one problem
        # more.
        with open(os.path.join(pkg, "prompt_outdated.json"), "w") as f:
            f.write('{"since": 1}')
        self.assertEqual(len(problems()), len(dead) + 1)
        self.assertEqual(fake.requests, [])  # no "I'm online" DM


class OwnerlessServer(unittest.TestCase):
    """A public bot added to a server none of its owners is in answers
    nobody there until an owner chooses When it answers for it."""

    def test_quiet_where_no_owner_is(self):
        from settings_harness import (G1, G2, CH_G2, OWNER, U1, gmsg,
                                      load_monitor, quiet, state)
        base = f'version: 1\nowners: ["{OWNER}"]\n'
        mod = load_monitor(base)
        st = state(mod)
        st["known_guilds"] = {G1: "Lounge"}       # G2 is new: a join
        stranger = gmsg(U1, mention=True, channel=CH_G2, guild=G2)
        # Quiet from the gateway's join event on, before discovery.
        with quiet():
            mod.on_guild_event(st, {"op": "create", "id": G2})
        self.assertIsNone(mod.trigger_kind(stranger, G2))
        with quiet():
            mod.sync_guilds(st, mod.GUILDS, mod.CHANNELS)
        kind, why = mod.trigger_verdict(stranger, G2)
        self.assertIsNone(kind)
        self.assertEqual(mod.ledger_code("skipped", f"settings: {why}"),
                         "ownerless")
        # Not a shared server for DMs (who.dms_from: shared-servers).
        mod.api.members[(G2, U1)] = []
        mod._SHARED_SERVER.clear()
        self.assertFalse(mod.shares_server(U1))
        # A restart whose boot discovery fails keeps the gate.
        with quiet():
            mod.save_state(st)
        again = load_monitor(base, tmp=mod.tmp)
        with quiet():
            st_again = again.load_state()
            again.sync_guilds(st_again, {}, {})
        self.assertIsNone(again.trigger_kind(stranger, G2))
        # The owners are told of the join; answering in G1 is untouched.
        self.assertTrue(mod.api.dm_posts())
        self.assertEqual(mod.trigger_kind(gmsg(U1, mention=True), G1),
                         "mention")
        # An owner chose When it answers for that server: it answers.
        mod2 = load_monitor(base + f'servers:\n  "{G2}":\n    who:\n'
                                   f'      replies: when-addressed\n')
        st2 = state(mod2)
        st2["known_guilds"] = {G1: "Lounge"}
        with quiet():
            mod2.sync_guilds(st2, mod2.GUILDS, mod2.CHANNELS)
        self.assertEqual(mod2.trigger_kind(stranger, G2), "mention")
        # An owner is a member there: it answers as everywhere else.
        mod3 = load_monitor(base)
        mod3.api.members[(G2, OWNER)] = []
        st3 = state(mod3)
        st3["known_guilds"] = {G1: "Lounge"}
        with quiet():
            mod3.sync_guilds(st3, mod3.GUILDS, mod3.CHANNELS)
        self.assertEqual(mod3.trigger_kind(stranger, G2), "mention")


class OwnerChecks(unittest.TestCase):
    def test_failing_checks_do_not_starve_the_others(self):
        """Servers whose owner check keeps failing back off, so the
        others (the owner's own new server here) still get checked."""
        from settings_harness import HTTPErr, OWNER, load_monitor, quiet
        mod = load_monitor(f'version: 1\nowners: ["{OWNER}"]\n')
        bad = [str(610000000000000000 + i) for i in range(5)]
        good = "690000000000000001"
        guilds = {g: {"name": g} for g in bad + [good]}
        st = mod.load_state()
        real_get = mod.api_get

        def get(path):
            if any(f"/guilds/{g}/" in path for g in bad):
                raise HTTPErr(500)
            return real_get(path)
        mod.api_get = get
        mod.api.members[(good, OWNER)] = []
        with quiet():
            for g in bad:
                mod.note_guild_join_for_owners(st, g)
            mod.check_owner_presence(st, guilds)   # the 5 bad ones
            mod.note_guild_join_for_owners(st, good)
            mod.check_owner_presence(st, guilds)
        self.assertIs(mod.OWNER_IN_GUILD[good], True)
        self.assertTrue(all(mod.OWNER_IN_GUILD[g] is None for g in bad))


class WatchdogHook(unittest.TestCase):
    def test_an_alert_outlives_the_hook_run(self):
        """A watchdog run slower than the hook's wait still reaches the
        main chat, on the next hook run."""
        from common import HOOK_RUNTIME
        root = scratch("muse-wdh-")
        os.makedirs(os.path.join(root, "hooks"))
        hook = os.path.join(root, "hooks", "watchdog-hook.sh")
        shutil.copy2(os.path.join(MUT_DIR, "hooks", "watchdog-hook.sh"),
                     hook)
        go = os.path.join(root, "go")
        # The watchdog run ends only once the hook has stopped waiting.
        with open(os.path.join(root, "watchdog.sh"), "w") as f:
            f.write(f"until [ -e {go} ]; do sleep 0.02; done\n"
                    "echo 'ALERT: monitor DOWN: test'\n")
        env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                   MUSE_WATCHDOG_HOOK_WAIT="0")
        first = subprocess.run(["bash", hook], env=env, capture_output=True,
                               text=True, timeout=30).stdout
        self.assertNotIn("ALERT", first)
        open(go, "w").close()
        for _ in range(500):   # until that run has finished
            if [f for f in os.listdir(root) if f.startswith(
                    "watchdog-hook.out.") and not f.endswith(".tmp")]:
                break
            time.sleep(0.02)
        with open(os.path.join(root, "watchdog.sh"), "w") as f:
            f.write("echo 'OK monitor alive'\n")
        second = subprocess.run(["bash", hook], env=env, capture_output=True,
                                text=True, timeout=30).stdout
        self.assertIn("WAKE:", second)
        self.assertIn("ALERT: monitor DOWN: test", second)


class MediaDefault(unittest.TestCase):
    def test_fresh_config_has_media_on(self):
        pkg, _home = make_pkg()
        cfg = os.path.join(pkg, "config.yaml")
        with open(cfg, "w") as f:
            f.write('version: 1\nowners: ["1"]\n')
        ms = load_ms(pkg)
        st = ms.load(cfg, os.path.join(pkg, "plugins"))
        self.assertIs(st.resolve(ms.Scope.everywhere())["media.enabled"],
                      True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
