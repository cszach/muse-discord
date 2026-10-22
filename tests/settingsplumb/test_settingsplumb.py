"""/settings plumbing (settings PR 2), offline: command registration,
interaction routing to settings_ui.handle (a stub here), the 3s path
under a slow main loop, SettingsCtx (overlay writes, undo under the
lock, thread safety, the stash, outcomes kept to their place), the
admin-change digest, the join notice's Configure button, and a missing
settings_ui.

Usage: [MUT_DIR=<tree>] python3 tests/settingsplumb/test_settingsplumb.py"""
import json
import os
import subprocess
import sys
import threading
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from settings_harness import (CH, G1, G2, MS, OWNER, U1,  # noqa: E402
                              HTTPErr, load_monitor, quiet, state, tmpdir)

import yaml  # noqa: E402

CFG = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
"""
CAT = "800000000000000009"


def overlay(mod):
    with open(os.path.join(mod.tmp, "config.discord.yaml")) as f:
        return yaml.safe_load(f)


def cmd_ix(user=OWNER, guild=None, iid="1"):
    d = {"id": iid, "token": "t", "type": 2, "application_id": "app",
         "data": {"name": "settings", "type": 1}}
    if guild:
        d["guild_id"] = guild
        d["member"] = {"user": {"id": user}, "permissions": "32"}
    else:
        d["user"] = {"id": user}
    return d


def comp_ix(custom_id, user=OWNER, t=3):
    data = {"custom_id": custom_id}
    if t == 3:
        data["component_type"] = 2
    else:
        data["components"] = []
    return {"id": "2", "token": "t", "type": t, "data": data,
            "user": {"id": user}}


class Stub:
    """A stand-in settings_ui: records calls, answers a fixed body."""

    def __init__(self, body=None):
        self.calls = []
        self.body = body

    def handle(self, ix, ctx):
        self.calls.append((ix, ctx, threading.current_thread().name))
        if self.body is not None:
            return self.body
        return {"type": 4 if ix["type"] == 2 else 7,
                "data": {"content": "page", "flags": 64}}


def with_stub(mod, stub=None):
    stub = stub or Stub()
    mod._SETTINGS_UI["mod"] = stub
    return stub


# ---------------------------------------------------------------- register
class FakeCommands:
    """GET/POST/PATCH of /applications/<app>/commands."""

    def __init__(self, mod, existing=None, app="777"):
        self.mod = mod
        self.app = app
        self.cmds = list(existing or [])
        self.log = []
        self.fail = 0
        mod.api_get = self.get
        mod.api_post = self.post
        mod.api_patch = self.patch

    def get(self, path):
        self.log.append(("GET", path))
        if self.fail:
            self.fail -= 1
            raise HTTPErr(500)
        if path == "/applications/@me":
            return {"id": self.app}
        if path == f"/applications/{self.app}/commands":
            return [dict(c) for c in self.cmds]
        raise HTTPErr(404)

    def post(self, path, data):
        self.log.append(("POST", path, data))
        assert path == f"/applications/{self.app}/commands", path
        c = dict(data, id="c1", application_id=self.app, version="1")
        self.cmds.append(c)
        return c

    def patch(self, path, data):
        self.log.append(("PATCH", path, data))
        return dict(data, id=path.rsplit("/", 1)[1])


def discord_copy(mod, **over):
    """The command as Discord's GET returns it: extra fields, its own
    key order, contexts in another order."""
    c = {"id": "c1", "application_id": "777", "version": "5",
         "nsfw": False, "description_localizations": None,
         "dm_permission": True, "integration_types": [0],
         "contexts": [1, 0], "type": 1, "name": "settings",
         "description": mod.SETTINGS_COMMAND["description"],
         "default_member_permissions": None}
    c.update(over)
    return c


def why_copy(mod, **over):
    """The "Why no reply?" message command as Discord's GET returns it."""
    c = {"id": "c2", "application_id": "777", "version": "5",
         "type": 3, "name": "Why no reply?", "description": "",
         "contexts": [1, 0], "integration_types": [0],
         "default_member_permissions": "32"}
    c.update(over)
    return c


class Registration(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)

    def run_reg(self, status=None):
        with quiet() as out:
            r = self.mod.register_commands(status)
        return r, out.getvalue()

    def test_command_definitions_within_discord_limits(self):
        for c in (self.mod.SETTINGS_COMMAND, self.mod.WHY_COMMAND):
            self.assertEqual(c["contexts"], [0, 1])       # servers and DMs
            self.assertEqual(c["integration_types"], [0])
            self.assertNotIn("dm_permission", c)   # deprecated for contexts
            self.assertLessEqual(len(c["name"]), 32)
        self.assertLessEqual(len(self.mod.SETTINGS_COMMAND["description"]),
                             100)
        self.assertIsNone(self.mod.SETTINGS_COMMAND[
            "default_member_permissions"])
        why = self.mod.WHY_COMMAND
        self.assertEqual(why["type"], 3)              # a message command
        self.assertNotIn("description", why)          # none on menu commands
        self.assertEqual(why["default_member_permissions"], "32")

    def test_created_once_and_unchanged_when_equal(self):
        api = FakeCommands(self.mod)
        r, out = self.run_reg()
        self.assertEqual(r, "created")
        posts = [x for x in api.log if x[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2], self.mod.SETTINGS_COMMAND)
        self.assertEqual(posts[1][2], self.mod.WHY_COMMAND)
        # Once per run: later ticks make no request.
        n = len(api.log)
        self.mod._COMMAND_REG["next_try"] = 0
        self.assertIsNone(self.run_reg()[0])
        self.assertEqual(len(api.log), n)
        # Already there as Discord returns it: nothing written (a PATCH
        # every boot would be rate limited).
        self.mod = load_monitor(CFG)
        api = FakeCommands(self.mod, [discord_copy(self.mod),
                                      why_copy(self.mod),
                                      {"id": "x", "name": "other",
                                       "type": 1}])
        r, out = self.run_reg()
        self.assertEqual(r, "unchanged")
        self.assertEqual([x[0] for x in api.log], ["GET", "GET"])

    def test_patch_when_different(self):
        mod = self.mod
        cases = [({"contexts": [0, 1, 2]}, {}), ({"description": "old"}, {}),
                 ({"integration_types": [0, 1]}, {}),
                 ({"default_member_permissions": "32"}, {}),
                 ({"options": [{"type": 3, "name": "x"}]}, {}),
                 ({}, {"contexts": [0]})]       # the message command
        for over, why_over in cases:
            with self.subTest(over or why_over):
                mod._COMMAND_REG.update(done=False, next_try=0)
                api = FakeCommands(mod, [discord_copy(mod, **over),
                                         why_copy(mod, **why_over)])
                self.assertEqual(self.run_reg()[0], "updated")
                patches = [x for x in api.log if x[0] == "PATCH"]
                self.assertEqual(len(patches), 1)
                self.assertEqual(patches[0][1:], (
                    "/applications/777/commands/c1", mod.SETTINGS_COMMAND)
                    if over else ("/applications/777/commands/c2",
                                  mod.WHY_COMMAND))
                self.assertFalse([x for x in api.log if x[0] == "POST"])

    def test_why_name_falls_back_when_refused(self):
        """Discord answers 400 when it does not take "?" in the name: the
        command is registered as "Why no reply" instead, and that name
        counts as registered from then on."""
        api = FakeCommands(self.mod, [discord_copy(self.mod)])
        post = api.post

        def picky(path, data):
            if "?" in data["name"]:
                api.log.append(("POST", path, data))
                raise HTTPErr(400)
            return post(path, data)
        self.mod.api_post = picky
        r, out = self.run_reg()
        self.assertEqual(r, "created")
        self.assertIn("Why no reply", [c["name"] for c in api.cmds])
        # A later run finds it under the fallback name: nothing to do.
        api2 = FakeCommands(self.mod, [discord_copy(self.mod),
                                       why_copy(self.mod,
                                                name="Why no reply")])
        self.mod._COMMAND_REG.update(done=False, next_try=0)
        self.assertEqual(self.run_reg()[0], "unchanged")
        self.assertEqual([x[0] for x in api2.log], ["GET"])

    def test_retry_on_failure_never_fatal(self):
        api = FakeCommands(self.mod)
        api.fail = 2
        r, out = self.run_reg()
        self.assertIsNone(r)
        self.assertFalse(self.mod._COMMAND_REG["done"])
        # Not before the retry delay.
        n = len(api.log)
        self.assertIsNone(self.run_reg()[0])
        self.assertEqual(len(api.log), n)
        self.mod._COMMAND_REG["next_try"] = 0
        self.assertIsNone(self.run_reg()[0])       # fails again
        self.mod._COMMAND_REG["next_try"] = 0
        self.assertEqual(self.run_reg()[0], "created")
        self.assertTrue(self.mod._COMMAND_REG["done"])

# ----------------------------------------------------------------- routing
class Routing(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        self.stub = with_stub(self.mod)
        self.recs = []

    def dispatch(self, ix):
        with quiet() as out:
            r = self.mod.dispatch_interaction(ix, self.recs.append)
        self.out = out.getvalue()
        return r

    def test_command_component_modal_reach_handle(self):
        why = cmd_ix()
        why["data"] = {"name": "Why no reply?", "type": 3,
                       "target_id": "1"}
        why2 = cmd_ix(guild=G1)
        why2["data"] = {"name": "Why no reply", "type": 3,
                        "target_id": "1"}
        for ix in (cmd_ix(), cmd_ix(guild=G1, user=U1), why, why2,
                   comp_ix("mdset:open:server:" + G1),
                   comp_ix("mdset:2:pg:x:b"),
                   comp_ix("mdset:m:abc", t=5)):
            r = self.dispatch(ix)
            got, ctx, _ = self.stub.calls[-1]
            self.assertIs(got, ix)
            self.assertIsInstance(ctx, self.mod.SettingsCtx)
            self.assertEqual(r["type"], 4 if ix["type"] == 2 else 7)
        # Permissions are settings_ui's: strangers reach it too.
        n = len(self.stub.calls)
        self.dispatch(cmd_ix(user=U1))
        self.dispatch(comp_ix("mdset:x", user=U1))
        self.assertEqual(len(self.stub.calls), n + 2)

    def test_other_prefixes_and_commands_untouched(self):
        r = self.dispatch(comp_ix("mdsetx:1"))
        self.assertEqual(r["data"]["flags"], 64)
        ix = cmd_ix()
        ix["data"]["name"] = "other"
        self.dispatch(ix)
        self.assertEqual(self.stub.calls, [])
        # Autocomplete (4) is not answered.
        self.assertIsNone(self.dispatch({"id": "1", "token": "t",
                                         "type": 4, "data": {}}))

    def test_bodies_forwarded_verbatim(self):
        """A Components V2 page (flags 64|32768, no content) reaches
        Discord as settings_ui made it, for a new message and an edit."""
        for t, flags in ((4, 64 | 32768), (7, 32768)):
            body = {"type": t, "data": {
                "flags": flags, "allowed_mentions": {"parse": []},
                "components": [{"type": 17, "accent_color": 0x5865F2,
                                "components": [{"type": 10,
                                                "content": "## Page"}]}]}}
            self.stub.body = body
            r = self.dispatch(comp_ix("mdset:2:pg:x:b"))
            self.assertEqual(r, body)
            self.assertNotIn("content", r["data"])
        body = {"type": 9, "data": {"custom_id": "mdset:m:1",
                                    "title": "Edit", "components": []}}
        self.stub.body = body
        self.assertEqual(self.dispatch(comp_ix("mdset:b")), body)

    def test_bad_body_and_crash_get_ephemeral(self):
        self.stub.body = "nope"
        r = self.dispatch(cmd_ix())
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)

        def boom(ix, ctx):
            raise RuntimeError("kaput")
        self.mod._SETTINGS_UI["mod"] = types.SimpleNamespace(handle=boom)
        r = self.dispatch(cmd_ix())
        self.assertEqual(r["data"]["flags"], 64)
        self.assertNotIn("kaput", r["data"]["content"])   # no internals


class MissingUI(unittest.TestCase):
    def test_missing_ui_gets_an_ephemeral_answer(self):
        mod = load_monitor(CFG)
        mod.BASE_DIR = tmpdir("noui_")
        saved = list(sys.path)
        sys.modules.pop("settings_ui", None)
        sys.path[:] = [p for p in sys.path
                       if not os.path.isfile(os.path.join(
                           p or ".", "settings_ui.py"))]
        try:
            with quiet():
                r1 = mod.dispatch_interaction(cmd_ix(), lambda r: None)
                r2 = mod.dispatch_interaction(comp_ix("mdset:x"),
                                              lambda r: None)
        finally:
            sys.path[:] = saved
        for r in (r1, r2):
            self.assertEqual(r["type"], 4)
            self.assertEqual(r["data"]["flags"], 64)

# ------------------------------------------------------------- main loop
class MainLoop(unittest.TestCase):
    def test_settings_ack_not_behind_slow_tick(self):
        p = subprocess.run([sys.executable,
                            os.path.join(HERE, "main_scenario.py")],
                           capture_output=True, text=True, timeout=60,
                           env=os.environ.copy())
        try:
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise AssertionError(p.stdout + p.stderr)
        acks = {a["id"]: a for a in r["acks"]}
        self.assertEqual(sorted(acks), ["900", "901", "902"], r["out"])
        for a in acks.values():
            self.assertLess(a["after"], 1.0, r)
        self.assertEqual(acks["900"]["resp"]["type"], 4)
        self.assertEqual(acks["901"]["resp"]["type"], 7)
        self.assertEqual(acks["902"]["resp"]["type"], 7)
        self.assertEqual(acks["900"]["resp"]["data"]["seen_guilds"], ["G1"])
        self.assertEqual(set(r["handled_on"]), {"interactions"})


# ------------------------------------------------------------------- ctx
class Ctx(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.settings()

    def ctx(self):
        with quiet():
            return self.mod.SettingsCtx()

    def test_write_visible_at_once_and_to_main_loop(self):
        c = self.ctx()
        key = f'servers."{G1}".who.replies'
        with quiet():
            c.write(key, "off", OWNER)
        e = overlay(self.mod)["values"][key]
        self.assertEqual(e["value"], "off")
        self.assertEqual(e["by"], OWNER)
        sc = MS.Scope.server(G1, "Lounge", CH)
        self.assertEqual(c.settings.get("who.replies", sc), "off")
        with quiet():
            self.assertEqual(self.mod.settings().get("who.replies", sc),
                             "off")

    def test_invalid_write_raises(self):
        c = self.ctx()
        for key, val in (("everywhere.style.length", "huge"),
                         ("owners", [U1]),
                         ("dms.who.bot_exchange_limit", 3)):
            with self.assertRaises(MS.SettingsError):
                c.write(key, val, OWNER)

    def test_undo_delegates_to_muse_settings_under_lock(self):
        c = self.ctx()
        seen = []

        def fake_undo(config_path, prefix, by, plugins_dir=None):
            seen.append((config_path, prefix, by, plugins_dir,
                         self.mod._OVERLAY_LOCK._is_owned()))
            return f"{prefix}.style.length"
        had = getattr(self.mod.MS, "undo", None)
        self.mod.MS.undo = fake_undo
        try:
            with quiet():
                r = c.undo(f'servers."{G1}"', OWNER)
        finally:
            if had is None:
                del self.mod.MS.undo
            else:
                self.mod.MS.undo = had
        self.assertEqual(r, f'servers."{G1}".style.length')
        self.assertEqual(seen, [(self.mod._SETTINGS.config_path,
                                 f'servers."{G1}"', OWNER,
                                 self.mod.PLUGINS_DIR, True)])

    def test_reload_hooks_run_on_main_thread_only(self):
        ran = []
        self.mod._on_settings_changed = \
            lambda prev, cur: ran.append(threading.current_thread().name)
        c = self.ctx()

        def worker():
            with quiet():
                c.write("everywhere.style.length", "brief", OWNER)
        t = threading.Thread(target=worker, name="interactions")
        t.start()
        t.join()
        self.assertEqual(ran, [])
        with quiet():
            self.mod.settings()
        self.assertEqual(ran, ["MainThread"])
        with quiet():
            self.mod.settings()
        self.assertEqual(ran, ["MainThread"])

    def test_concurrent_writes_and_main_loop_reads(self):
        c = self.ctx()
        errors = []
        n = 15
        sc = MS.Scope.server(G1, "Lounge", CH)

        def writer(key, values):
            try:
                for i in range(n):
                    c.write(key, values[i % 2], OWNER)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def reader(stop):
            try:
                while not stop.is_set():
                    st = self.mod.settings()
                    st.resolve(sc)
                    self.mod.retire_overlay_changes()
            except Exception as e:  # pragma: no cover
                errors.append(e)
        stop = threading.Event()
        with quiet():
            threads = [
                threading.Thread(target=writer, args=(
                    "everywhere.style.length", ("brief", "detailed"))),
                threading.Thread(target=writer, args=(
                    f'servers."{G1}".who.replies', ("off", "conversational"))),
                threading.Thread(target=writer, args=(
                    "dms.who.dms_from", ("anyone", "owners")))]
            rd = threading.Thread(target=reader, args=(stop,))
            rd.start()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            rd.join()
        self.assertEqual(errors, [])
        doc = overlay(self.mod)
        # No write lost: every change is in history.
        self.assertEqual(len(doc["history"]), 3 * n)
        self.assertEqual(doc["values"]["everywhere.style.length"]["value"],
                         "detailed" if n % 2 == 0 else "brief")
        with quiet():
            self.assertEqual(self.mod.settings().get(
                "dms.who.dms_from".split(".", 1)[1], MS.Scope.dm()),
                "owners" if n % 2 == 0 else "anyone")

    def test_fresh_write_not_retired_by_stale_conflict(self):
        # The file moved (conflict pending); /settings writes the key
        # again before the main loop commits the retirement.
        mod = load_monitor(CFG + "everywhere:\n  style:\n    length: brief\n")
        with open(os.path.join(mod.tmp, "config.discord.yaml"), "w") as f:
            yaml.safe_dump({"version": 1, "values": {
                "everywhere.style.length": {
                    "value": "detailed", "base": "normal", "by": OWNER,
                    "at": "2026-10-01T00:00:00Z"}}, "history": []}, f)
        with quiet():
            stale = mod.settings()
        self.assertTrue(stale.retire)
        with quiet():
            mod.SettingsCtx().write("everywhere.style.length", "detailed",
                                    OWNER)
            n = mod.retire_overlay_changes()
        self.assertEqual(n, 0)
        e = overlay(mod)["values"]["everywhere.style.length"]
        self.assertEqual((e["value"], e["base"]), ("detailed", "brief"))
        self.assertEqual(mod.api.dm_posts(), [])


# ------------------------------------------------- data for settings_ui
class UIData(unittest.TestCase):
    """What the redesigned /settings reads through SettingsCtx: the
    ledger's outcomes (with author ids and reason codes), names, the
    stash, the directory's system channel and send flags."""

    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.publish_directory()
            self.ctx = self.mod.SettingsCtx()

    def test_recent_outcomes_by_place(self):
        m = self.mod
        thread = "703000000000000009"
        m.CHANNELS[thread] = {"name": "general > t", "guild_id": G1,
                              "thread": True, "parent_id": CH}
        with quiet():
            m.publish_directory()
        m.ledger_record("2001", "skipped", "settings: who.replies is off "
                        "here", CH, "sam", author_id=U1)
        m.ledger_record("2002", "skipped", "daily cap reached", thread,
                        "kai", author_id="222000000000000009",
                        detail={"key": "limits.per_day",
                                "counter": f"server:{G1}", "cap": 5})
        m.ledger_record("2003", "skipped", "settings: who.dms_from is "
                        "owners", "400000000000000077", "ann",
                        author_id="222000000000000008")
        m.ledger_record("2004", "replied", "", "704000000000000001", "x")
        ch = MS.Place.channel(G1, CH)
        got = self.ctx.recent_outcomes(ch, 10)
        self.assertEqual([o["message_id"] for o in got], ["2002", "2001"])
        self.assertEqual(got[0]["parent_id"], CH)
        self.assertEqual(got[1]["guild_id"], G1)
        self.assertEqual(
            [o["message_id"] for o in self.ctx.recent_outcomes(
                MS.Place.server(G1), 10)], ["2002", "2001"])
        self.assertEqual(
            [o["message_id"] for o in self.ctx.recent_outcomes(
                MS.Place.dms(), 10) if o["status"] == "skipped"], ["2003"])
        self.assertEqual(self.ctx.recent_outcomes(ch, 1)[0]["message_id"],
                         "2002")
        self.assertEqual(self.ctx.outcome("2001")["author_id"], U1)
        self.assertIsNone(self.ctx.outcome("9999"))
        # Review B #2: a channel registered with its category as
        # parent_id is not a thread; a channel the monitor no longer maps
        # is never listed under DMs; the ledger keeps the server it was
        # written in (#1).
        self.setUp()   # a fresh monitor and ledger
        m = self.mod
        new, cat = "705000000000000001", "800000000000000009"
        m.CHANNELS[new] = {"name": "new-room", "guild_id": G1,
                           "guild_name": "g", "can_send": None,
                           "thread": False, "parent_id": cat}
        with quiet():
            m.publish_directory()
        m.ledger_record("3001", "skipped", "settings: who.replies is off "
                        "here", new, "sam", author_id=U1)
        e = m._ledger_load()["entries"]["3001"]
        self.assertEqual((e["guild_id"], e["parent_id"]), (G1, None))
        self.assertEqual([o["message_id"] for o in self.ctx.recent_outcomes(
            MS.Place.channel(G1, new), 10)], ["3001"])
        self.assertIsNone(self.ctx.outcome("3001")["parent_id"])
        # Discovery forgets the channel: the entry still says where.
        del m.CHANNELS[new]
        with quiet():
            m.publish_directory()
        self.assertEqual(self.ctx.outcome("3001")["guild_id"], G1)
        # An older entry for an unmapped channel: not a DM.
        d = m._ledger_load()
        d["entries"]["3002"] = {"status": "skipped", "reason": "settings: "
                                "not allowed by who.allow / who.ignore",
                                "channel_id": "706000000000000001",
                                "author": "kai", "updated": 1.0}
        m._ledger_save(d)
        self.assertEqual(self.ctx.recent_outcomes(MS.Place.dms(), 10), [])
        # A DM channel the monitor polls is one.
        m.DM_CHANNELS["400000000000000077"] = "owner"
        m.ledger_record("3003", "skipped", "settings: who.replies is off "
                        "for DMs", "400000000000000077", "o", author_id=U1)
        self.assertTrue(m._ledger_load()["entries"]["3003"]["dm"])
        self.assertEqual([o["message_id"] for o in self.ctx.recent_outcomes(
            MS.Place.dms(), 10)], ["3003"])

    def test_stash_single_use_and_bound_to_its_user(self):
        c = self.ctx
        tok = c.stash_put(OWNER, {"kind": "apply", "n": 1})
        self.assertEqual(len(tok), 10)
        self.assertNotIn(":", tok)
        with self.assertRaises(PermissionError):
            c.stash_take(U1, tok)
        # another interaction's Ctx sees the same stash
        with quiet():
            other = self.mod.SettingsCtx()
        self.assertEqual(other.stash_take(OWNER, tok),
                         {"kind": "apply", "n": 1})
        self.assertIsNone(c.stash_take(OWNER, tok))     # used
        tok = c.stash_put(OWNER, {"x": 1}, ttl=-1)
        self.assertIsNone(c.stash_take(OWNER, tok))     # expired
        self.assertIsNone(c.stash_take(OWNER, "nope"))

# ---------------------------------------------------------- admin digest
class AdminDigest(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.publish_directory()
            self.ctx = self.mod.SettingsCtx()

    def age(self, secs):
        for e in self.mod._ADMIN_CHANGES.values():
            e["first"] -= secs

    def test_batched_per_guild_per_minute(self):
        c = self.ctx
        c.notify_owners_admin_change(G1, U1, "who.replies",
                                     "conversational", "off")
        c.notify_owners_admin_change(G1, U1, f'channels."{CH}".replies',
                                     "conversational", "off")
        c.notify_owners_admin_change(G1, "222000000000000009", "who.ignore",
                                     [], [U1])
        c.notify_owners_admin_change(G2, U1, "media.enabled",
                                     True, False)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.assertEqual(self.mod.api.dm_posts(), [])
        self.age(61)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 2)
        dms = self.mod.api.dm_posts()
        self.assertEqual(len(dms), 2)
        for d in dms:
            self.assertEqual(d["allowed_mentions"], {"parse": []})
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(force=True), 0)
        # A change during a window joins it.
        c.notify_owners_admin_change(G1, U1, "who.replies", "a", "off")
        self.age(30)
        c.notify_owners_admin_change(G1, U1, "who.replies", "off", "a")
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.age(31)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 1)
        self.assertEqual(len(self.mod.api.dm_posts()), 3)

    def test_undelivered_kept_and_retried(self):
        self.ctx.notify_owners_admin_change(G1, U1, "who.replies", "a", "b")
        self.age(61)
        self.mod.dm_owner = lambda uid, payload: False
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.assertIn(G1, self.mod._ADMIN_CHANGES)
        self.mod.dm_owner = lambda uid, payload: True
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 1)
        self.assertEqual(self.mod._ADMIN_CHANGES, {})

    def test_long_digest_capped(self):
        for i in range(80):
            self.ctx.notify_owners_admin_change(
                G1, U1, "who.ignore", [], [str(10 ** 17 + i)] * 3)
        text = self.mod.admin_digest_text(
            G1, self.mod._ADMIN_CHANGES[G1]["changes"])
        self.assertLessEqual(len(text), 1990)


# ---------------------------------------------------------- join notice
class JoinNotice(unittest.TestCase):
    def test_configure_button(self):
        mod = load_monitor(CFG)
        st = state(mod)
        st["known_guilds"] = {G1: "Lounge"}
        with quiet():
            mod.sync_guilds(st, mod.GUILDS, mod.CHANNELS)
        dms = mod.api.dm_posts()
        self.assertEqual(len(dms), 1)
        btn = dms[0]["components"][0]["components"][0]
        self.assertEqual(btn["type"], 2)
        self.assertEqual(btn["custom_id"], f"mdset:open:server:{G2}")
        self.assertLessEqual(len(btn["custom_id"]), 100)
        # The click is routed to settings_ui.
        stub = with_stub(mod)
        with quiet():
            mod.dispatch_interaction(comp_ix(btn["custom_id"]),
                                     lambda r: None)
        self.assertEqual(stub.calls[0][0]["data"]["custom_id"],
                         f"mdset:open:server:{G2}")

if __name__ == "__main__":
    unittest.main(verbosity=1)
