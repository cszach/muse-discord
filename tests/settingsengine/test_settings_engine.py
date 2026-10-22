"""The settings engine (muse_settings.py), offline: where writes land,
risk by outcome (what asks first), grouped writes and undo, the no-op
guard, the server-admin rule (quieter only, never past the owners),
who.dms_from (and the monitor's DM gate), presets, pause, the
bin/settings.py commands, old overlay files, the review findings, and
a fuzz of the admin rule.

Usage: MUT_DIR=<tree> python3 tests/settingsengine/test_settings_engine.py
(MUT_DIR defaults to this repo)."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake import (ADMIN, ADMIN2, ANN, BASE, CAT, CH, CH2,  # noqa: E402
                  CH_G2, G1, G2, G3, INFO, MS, MUT, NAMES, OWNER,
                  PLUGINS, SAM, SPAM, config, doc, load,
                  load_monitor)

P = MS.Place
C = MS.Change
DEF, DMS = P.defaults(), P.dms()
S1, S2, S3 = P.server(G1), P.server(G2), P.server(G3)
GEN = P.channel(G1, CH, CAT)
RND = P.channel(G1, CH2)
KS = f'servers."{G1}".who.replies'
KS_GEN = f'servers."{G1}".channels."{CH}".replies'


def apply(cfg, changes, by=OWNER, **kw):
    kw.setdefault("role", "owner")
    return MS.apply(cfg, changes, by, plugins_dir=PLUGINS, **kw)


def value(cfg, place, key):
    return MS.place_value(load(cfg), place, key)


# ---------------------------------------------------------------------------
# 1. Places
# ---------------------------------------------------------------------------
class Places(unittest.TestCase):
    def test_write_paths_and_defaults_split(self):
        st = load(config(self))
        pp = MS.place_path
        self.assertEqual(pp(st, DEF, "who.replies"), ("all_servers",))
        self.assertEqual(pp(st, DEF, "media.enabled"), ("all_servers",))
        self.assertEqual(pp(st, DEF, "privacy.memory"), ("everywhere",))
        self.assertEqual(pp(st, DEF, "who.dms_from"), ("dms",))
        self.assertEqual(pp(st, DEF, "presence.status"), ("top",))
        self.assertEqual(pp(st, DMS, "style.notes"), ("dms",))
        self.assertEqual(pp(st, S1, "limits.per_day"), ("servers", G1))
        self.assertEqual(pp(st, GEN, "who.replies"),
                         ("servers", G1, "channels", CH))
        for place, key in ((GEN, "privacy.memory"), (DMS, "presence.status"),
                           (S1, "who.dms_from"), (DMS,
                                                  "who.bot_exchange_limit"),
                           (DEF, "privacy.never_share")):
            with self.assertRaises(MS.SettingsError):
                pp(st, place, key)

    def test_hidden_keys_stay_valid_and_overlay_keeps_them(self):
        """The compat trap: hiding is UI-only; presence.kind keeps
        working from the overlay."""
        ov = ("version: 1\nvalues:\n  presence.kind: {value: playing, by: "
              f"'{OWNER}', at: x}}\n")
        st = load(config(self, overlay=ov))
        self.assertEqual(st.retire, [])
        self.assertEqual(st.top("presence.kind"), "playing")
        self.assertIn("everywhere", MS.CORE["who.allow"].scopes)


# ---------------------------------------------------------------------------
# 2. Risk by outcome: what needs a confirmation
# ---------------------------------------------------------------------------
BIG_DEFAULT = BASE.replace("servers:", "all_servers:\n  who:\n"
                           "    replies: when-addressed\nservers:")


class Risk(unittest.TestCase):
    def test_risky_needs_confirmation(self):
        """A change is risky (asks first) by its outcome: who can reach
        the bot, what it may say, where it gets louder or loses a cap.
        Only .risky is checked, never the warning text."""
        dms_anyone = BASE + "dms:\n  who:\n    dms_from: anyone\n"
        s1_cap = BASE + "    limits:\n      per_day: 100\n" \
            f'  "{G3}":\n    limits:\n      per_day: 10\n'
        bx = BASE + "    who:\n      bot_exchange_limit: 4\n"
        pp = BASE.replace("servers:", "everywhere:\n  limits:\n"
                          "    per_person_per_day: 5\nservers:")
        pause_cfg = BASE + "    who:\n      replies: when-addressed\n"
        media = [C(S1, "media.enabled", True)]
        cases = [
            # (config, applied first, changes, info, risky)
            (BASE, [], [C(DMS, "who.dms_from", "anyone")], INFO, True),
            (BASE, [], [C(DMS, "who.dms_from", [SAM])], INFO, True),
            (BASE, [C(DMS, "who.replies", "off")],
             [C(DMS, "who.dms_from", "anyone")], INFO, False),
            (dms_anyone, [], [C(DMS, "who.dms_from", "shared-servers")],
             INFO, False),
            (dms_anyone, [], [C(DMS, "who.dms_from", [SAM])], INFO, False),
            (dms_anyone, [], [C(DMS, "who.dms_from", "owners")], INFO,
             False),
            (dms_anyone, [], [C(DMS, "style.length", "brief")], INFO, False),
            (BASE, [], [C(DMS, "who.allow", [SAM])], INFO, False),
            (BASE, [], [C(S1, "privacy.memory", "everything")], INFO, True),
            (BASE, [], [C(S3, "privacy.memory", "everything")], INFO, True),
            (BASE, [], [C(DMS, "privacy.memory", "everything")], INFO,
             False),
            (BASE, [C(DMS, "who.dms_from", "anyone")],
             [C(DMS, "privacy.memory", "everything")], INFO, True),
            (BASE, [], [C(S2, "privacy.memory", "nothing")], INFO, False),
            (BASE, [], media, INFO, False),
            (BASE, [], media + [C(S1, "media.who", "everyone")],
             INFO, True),
            (BASE, [], media + [C(S1, "media.who", [SAM]),
                                C(S1, "media.per_day", 5)], INFO,
             False),
            (BASE, [], media + [C(S1, "media.who", [SAM]),
                                C(S1, "media.per_day", 21)], INFO,
             True),
            (BASE, [], [C(DEF, "who.allow", [SAM])], INFO, True),
            (BASE, [C(S2, "who.allow", [SAM])],
             [C(S2, "who.allow", [SAM, ANN])], INFO, False),
            (pause_cfg, [], MS.pause_changes(DEF), INFO, True),
            (pause_cfg, [], MS.pause_changes(DMS), INFO, False),
            (pause_cfg, [], MS.pause_changes(S1), INFO, False),
            (pause_cfg, [], MS.pause_changes(GEN), INFO, False),
            (s1_cap, [], [C(S1, "limits.per_day", None)], INFO, True),
            (s1_cap, [], [C(S3, "limits.per_day", None)], INFO, False),
            (s1_cap, [], [C(S1, "limits.per_day", 200)], INFO, False),
            (pp, [], [C(DEF, "limits.per_person_per_day", None)], INFO,
             True),
            (pp, [], [C(DEF, "limits.per_person_per_day", None)],
             {"guilds": []}, True),                   # sizes unknown
            (BIG_DEFAULT, [], [C(S2, "who.replies", "conversational")],
             INFO, True),
            (BIG_DEFAULT, [], [C(S3, "who.replies", "conversational")],
             INFO, False),
            (BIG_DEFAULT, [], [C(S2, "who.replies", "conversational")],
             None, True),                             # no counts: closed
            (bx, [], [C(S1, "who.bot_exchange_limit", None)], INFO, True),
            (bx, [], [C(S1, "who.bot_exchange_limit", 11)], INFO, True),
            (bx, [], [C(S1, "who.bot_exchange_limit", 10)], INFO, False),
        ]
        for i, (text, first, changes, info, risky) in enumerate(cases):
            with self.subTest(i, changes=changes):
                cfg = config(self, text)
                if first:
                    apply(cfg, first)
                self.assertIs(MS.assess(load(cfg), changes, info,
                                        NAMES).risky, risky)
        # An Undo is judged by its outcome too.
        cfg = config(self, BIG_DEFAULT)
        apply(cfg, [C(S3, "style.length", "brief")])
        st = load(cfg)
        self.assertFalse(MS.assess(st, MS.undo_plan(st, S3), INFO).risky)
        # an undo that reopens Chatty in a big server is risky
        apply(cfg, [C(S2, "who.replies", "conversational")], OWNER)
        apply(cfg, [C(S2, "who.replies", "off")], OWNER)
        st = load(cfg)
        self.assertTrue(MS.assess(st, MS.undo_plan(st, S2), INFO).risky)


# ---------------------------------------------------------------------------
# 4. Grouped writes and undo
# ---------------------------------------------------------------------------
class Groups(unittest.TestCase):
    def test_apply_is_one_write_one_group(self):
        cfg = config(self)
        r = apply(cfg, [C(S1, "who.replies", "when-addressed"),
                        C(S1, "limits.per_day", 50),
                        C(GEN, "who.replies", "off")])
        self.assertTrue(r.changed)
        hist = doc(cfg)["history"]
        self.assertEqual(len(hist), 3)
        self.assertEqual({h["group"] for h in hist}, {r.group})
        self.assertEqual({h["by"] for h in hist}, {OWNER})
        # one bad change: nothing at all is written
        before = doc(cfg)
        for bad in ([C(S1, "who.replies", "off"),
                     C(S1, "limits.per_day", -5)],
                    [C(S1, "who.replies", "off"),
                     C(S1, "who.replies", "when-addressed")],
                    [C(S1, "who.replies", "off"),
                     C(GEN, "privacy.memory", "nothing")],
                    [C(S1, "privacy.suspicious", "answer")]):
            with self.assertRaises(MS.SettingsError):
                apply(cfg, bad)
            self.assertEqual(doc(cfg), before)

    def test_undo_whole_group_and_children(self):
        cfg = config(self)
        apply(cfg, [C(S1, "style.length", "brief")])
        r = apply(cfg, [C(S1, "who.replies", "when-addressed"),
                        C(GEN, "who.replies", "off")])
        st = load(cfg)
        names = MS.names_from(INFO, st)
        text, ident = MS.undo_preview(st, S1, True, names)
        self.assertEqual(ident, r.group)
        done = MS.undo_changes(cfg, S1, OWNER, PLUGINS, children=True,
                               expect=ident)
        self.assertEqual(sorted(done), sorted([KS, KS_GEN]))
        st = load(cfg)
        self.assertEqual(MS.place_value(st, GEN, "who.replies"),
                         "conversational")
        self.assertEqual(MS.place_value(st, S1, "style.length"), "brief")
        # the undo itself is never offered (no redo); the older change is
        self.assertEqual([c.key for c in MS.undo_plan(st, S1, True)],
                         ["style.length"])
        # A channel's change is undone from its server only when asked.
        cfg = config(self)
        apply(cfg, [C(GEN, "who.replies", "off")])
        st = load(cfg)
        self.assertEqual(MS.undo_preview(st, S1), (None, None))
        self.assertIsNone(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS))
        self.assertEqual(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS,
                                 children=True), KS_GEN)

    def test_group_reverted_wherever_it_landed(self):
        """Pause everywhere writes DMs and All servers; Undo at Defaults
        undoes both."""
        cfg = config(self)
        apply(cfg, MS.pause_changes(DEF))
        st = load(cfg)
        self.assertTrue(MS.is_paused(st, DEF))
        self.assertEqual(len(MS.undo_plan(st, DEF)), 2)
        MS.undo(cfg, "defaults", OWNER, PLUGINS)
        self.assertFalse(MS.is_paused(load(cfg), DEF))
        self.assertEqual(value(cfg, DMS, "who.replies"), "conversational")

    def test_undo_never_clobbers_a_newer_change(self):
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off")])
        _t, ident = MS.undo_preview(load(cfg), S1)
        apply(cfg, [C(S1, "style.length", "brief")])
        with self.assertRaises(MS.SettingsError):
            MS.undo(cfg, S1, OWNER, PLUGINS, expect=ident)
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # A group's undo skips keys changed since.
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off"),
                    C(S1, "style.length", "brief")])
        apply(cfg, [C(S1, "style.length", "detailed")])
        apply(cfg, [C(S1, "style.length", "brief")])   # same as the group
        MS.undo(cfg, S1, OWNER, PLUGINS)   # the newest: back to detailed
        self.assertEqual(value(cfg, S1, "style.length"), "detailed")
        plan = MS.undo_plan(load(cfg), S1)
        self.assertEqual([c.key for c in plan], ["style.length"])

    def test_undo_group_and_group_current(self):
        cfg = config(self)
        r = apply(cfg, [C(S1, "who.replies", "off"),
                        C(S1, "style.length", "brief")], group="nl-77")
        self.assertTrue(MS.group_current(load(cfg), "nl-77"))
        self.assertFalse(MS.group_current(load(cfg), "nl-78"))
        apply(cfg, [C(S1, "style.length", "detailed")])
        self.assertFalse(MS.group_current(load(cfg), r.group))
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, "nl-77", OWNER, PLUGINS, role="owner")
        r2 = apply(cfg, [C(S2, "who.replies", "off")], group="nl-79")
        self.assertEqual(MS.undo_group(cfg, "nl-79", OWNER, PLUGINS,
                                       role="owner"),
                         [f'servers."{G2}".who.replies'])
        self.assertFalse(MS.group_current(load(cfg), r2.group))
        with self.assertRaises(MS.SettingsError):   # undone already
            MS.undo_group(cfg, "nl-79", OWNER, PLUGINS, role="owner")
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, "nl-nope", OWNER, PLUGINS, role="owner")
        # group_undo_plan: what Undo of a settings request would do
        cfg = config(self)
        apply(cfg, [C(GEN, "who.replies", "off"),
                    C(S1, "style.length", "brief")], group="nl-600")
        plan = MS.group_undo_plan(load(cfg), "nl-600")
        self.assertEqual({(c.place, c.key, c.value) for c in plan},
                         {(S1, "style.length", "inherit"),
                          (P.channel(G1, CH), "who.replies", "inherit")})
        self.assertEqual(MS.group_undo_plan(load(cfg), "nl-nope"), [])
        # an undo's own group is not undoable
        apply(cfg, [C(S3, "who.replies", "off"),
                    C(S3, "style.length", "brief")])
        MS.undo(cfg, S3, OWNER, PLUGINS)
        ugroup = doc(cfg)["history"][-1]["group"]
        self.assertTrue(doc(cfg)["history"][-1]["undo"])
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, ugroup, OWNER, PLUGINS, role="owner")

    def test_old_ungrouped_history_loads_and_undoes(self):
        """A config.discord.yaml from before groups: entries and history
        lines without group/role still load and undo one at a time."""
        ov = f"""# old file
version: 1
values:
  servers."{G1}".who.replies:
    value: when-addressed
    base: null
    by: '{OWNER}'
    at: '2026-10-18T10:00:00Z'
  dms.style.length:
    value: brief
    base: null
    by: '{OWNER}'
    at: '2026-10-18T10:01:00Z'
history:
- key: servers."{G1}".who.replies
  from: inherit
  to: 'off'
  by: '{OWNER}'
  at: '2026-10-18T09:00:00Z'
- key: servers."{G1}".who.replies
  from: 'off'
  to: when-addressed
  by: '{OWNER}'
  at: '2026-10-18T10:00:00Z'
- key: dms.style.length
  from: inherit
  to: brief
  by: '{OWNER}'
  at: '2026-10-18T10:01:00Z'
"""
        cfg = config(self, overlay=ov)
        st = load(cfg)
        self.assertEqual(st.retire, [])
        self.assertEqual(MS.place_value(st, S1, "who.replies"),
                         "when-addressed")
        text, ident = MS.undo_preview(st, S1)
        self.assertEqual(MS.undo(cfg, S1, OWNER, PLUGINS, expect=ident), KS)
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        self.assertEqual(value(cfg, DMS, "style.length"), "brief")
        self.assertEqual(MS.undo(cfg, S1, OWNER, PLUGINS), KS)
        self.assertEqual(value(cfg, S1, "who.replies"), "conversational")
        self.assertIsNone(MS.undo(cfg, S1, OWNER, PLUGINS))
        self.assertEqual(MS.undo(cfg, "dms", OWNER, PLUGINS),
                         "dms.style.length")


# ---------------------------------------------------------------------------
# 5. The no-op guard
# ---------------------------------------------------------------------------
class NoOp(unittest.TestCase):
    def test_same_value_writes_nothing(self):
        cfg = config(self)
        apply(cfg, [C(S1, "style.length", "brief")])
        n = len(doc(cfg)["history"])
        r = apply(cfg, [C(S1, "style.length", "brief")])
        self.assertFalse(r.changed)
        self.assertEqual(r.status, MS.NO_CHANGE)
        self.assertEqual(len(doc(cfg)["history"]), n)
        # same effective value with nothing set at that layer
        r = apply(cfg, [C(RND, "who.replies", "conversational")])
        self.assertIs(r.status, MS.NO_CHANGE)
        # a reset where nothing is set
        self.assertFalse(apply(cfg, [C(S2, "who.replies",
                                       "inherit")]).changed)
        self.assertIs(MS.reset(cfg, f'servers."{G2}".who.replies', OWNER,
                               PLUGINS), MS.NO_CHANGE)
        self.assertIs(MS.set_value(cfg, f'servers."{G1}".style.length',
                                   "brief", OWNER, PLUGINS), MS.NO_CHANGE)
        self.assertEqual(len(doc(cfg)["history"]), n)
        # mixed: only the real change is written
        r = apply(cfg, [C(S1, "style.length", "brief"),
                        C(S1, "who.replies", "off")])
        self.assertEqual([p.key for p in r.written], ["who.replies"])
        self.assertEqual([p.key for p in r.unchanged], ["style.length"])

    def test_reset_and_file_value(self):
        # A reset over a config.yaml value still writes "inherit".
        cfg = config(self, BASE + "    who:\n      replies: off\n")
        r = apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertTrue(r.changed)
        self.assertEqual(doc(cfg)["values"][KS]["value"], "inherit")
        self.assertEqual(value(cfg, S1, "who.replies"), "conversational")
        MS.set_value(cfg, f'servers."{G2}".who.replies', "off", OWNER,
                     PLUGINS)
        self.assertEqual(MS.reset(cfg, f'servers."{G2}".who.replies',
                                  OWNER, PLUGINS), "inherit")
        self.assertEqual(doc(cfg)["values"][f'servers."{G2}".who.replies']
                         ["value"], "inherit")
        # Setting the file's own value again drops the entry (undoable).
        cfg = config(self, BASE + "    who:\n      replies: off\n")
        apply(cfg, [C(S1, "who.replies", "when-addressed")])
        self.assertIn(KS, doc(cfg)["values"])
        apply(cfg, [C(S1, "who.replies", "off")])
        self.assertNotIn(KS, doc(cfg)["values"])
        self.assertTrue(doc(cfg)["history"][-1]["dropped"])
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # still undoable
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertEqual(value(cfg, S1, "who.replies"), "when-addressed")
        # the same through set_value
        MS.set_value(cfg, KS, "off", OWNER, PLUGINS)
        self.assertNotIn(KS, doc(cfg)["values"])

    def test_channel_with_unknown_category_is_written(self):
        cfg = config(self, BASE + f'    channels:\n      "{CAT}":\n'
                     "        replies: off\n")
        # #general is in category Staff (off); without knowing that, a
        # "conversational" write must not be skipped
        r = apply(cfg, [C(P.channel(G1, CH), "who.replies",
                          "conversational")])
        self.assertTrue(r.changed)
        self.assertEqual(value(cfg, GEN, "who.replies"), "conversational")


# ---------------------------------------------------------------------------
# 6. Server admins: quieter, or revert their own changes
# ---------------------------------------------------------------------------
class Admin(unittest.TestCase):
    def setUp(self):
        self.cfg = config(self, BASE + "    who:\n      replies: "
                          "when-addressed\n      ignore: "
                          f'["{SPAM}"]\n')

    def admin(self, changes, gid=G1, info=INFO, by=ADMIN):
        return apply(self.cfg, changes, by, role="admin", admin_gid=gid,
                     info=info)

    def refused(self, changes, **kw):
        before = doc(self.cfg)
        with self.assertRaises(MS.SettingsError):
            self.admin(changes, **kw)
        self.assertEqual(doc(self.cfg), before)

    def test_quieter_ok_and_marked(self):
        self.admin([C(GEN, "who.replies", "off")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"), "off")
        e = doc(self.cfg)["values"][KS_GEN]
        self.assertEqual(e["role"], "admin")
        self.assertEqual(e["below"], {})
        self.assertEqual(doc(self.cfg)["history"][-1]["role"], "admin")
        # The /settings set_value path marks an admin's write too.
        MS.set_value(self.cfg, KS_GEN, "off", ADMIN, PLUGINS)
        self.assertEqual(doc(self.cfg)["values"][KS_GEN]["role"], "admin")
        MS.set_value(self.cfg, f'servers."{G1}".style.length', "brief",
                     OWNER, PLUGINS)
        self.assertNotIn("role", doc(self.cfg)["values"]
                         [f'servers."{G1}".style.length'])

    def test_revert_own_change_and_back_to_owner(self):
        self.admin([C(S1, "who.replies", "off")])
        self.admin([C(S1, "who.replies", "inherit")])   # reset = revert
        self.assertEqual(value(self.cfg, S1, "who.replies"),
                         "when-addressed")   # the file's value again
        self.admin([C(GEN, "who.replies", "off")])
        self.admin([C(GEN, "who.replies", "inherit")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "when-addressed")
        apply(self.cfg, [C(GEN, "who.replies", "conversational")], OWNER)
        self.admin([C(GEN, "who.replies", "when-addressed")])
        self.admin([C(GEN, "who.replies", "off")])        # a chain
        self.assertEqual(doc(self.cfg)["values"][KS_GEN]["below"],
                         {"value": "conversational"})
        # reverting goes back to the owner's value, not past it
        self.admin([C(GEN, "who.replies", "inherit")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "conversational")
        # picking the owners' value explicitly is a revert too
        self.admin([C(GEN, "who.replies", "off")])
        self.admin([C(GEN, "who.replies", "conversational")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "conversational")

    def test_owner_entries_are_out_of_reach(self):
        self.admin([C(S1, "who.replies", "off")])
        # conversational is louder than both now (off) and the owners'
        # when-addressed
        self.refused([C(S1, "who.replies", "conversational")])
        self.refused([C(GEN, "who.replies", "conversational")])
        self.setUp()
        apply(self.cfg, [C(GEN, "who.replies", "off")], OWNER)
        self.refused([C(GEN, "who.replies", "inherit")])
        self.refused([C(GEN, "who.replies", "when-addressed")])
        # nor the file's
        self.refused([C(S1, "who.ignore", [])])

    def test_legacy_and_named_entries_are_owner_level(self):
        """Recognised only by role: admin. An old entry by a non-owner id
        without a role, and a non-numeric by, are owner-level."""
        ov = ("version: 1\nvalues:\n"
              f"  {KS_GEN}: {{value: 'off', base: null, by: '{ADMIN2}', "
              f"at: x}}\n"
              f'  servers."{G1}".channels."{CH2}".replies: {{value: \'off\','
              f" base: null, by: muse.ai, at: x}}\n")
        self.cfg = config(self, BASE, overlay=ov)
        self.refused([C(GEN, "who.replies", "inherit")])
        self.refused([C(RND, "who.replies", "inherit")])
        self.assertEqual(MS.entry_role({"by": ADMIN2}), "owner")
        self.assertEqual(MS.entry_role({"by": "cli", "role": "admin"}),
                         "admin")

    def test_ignore_list(self):
        self.admin([C(S1, "who.ignore", [SPAM, SAM])])
        # removing someone the admins added is a revert
        self.admin([C(S1, "who.ignore", [SPAM])])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [SPAM])
        self.admin([C(S1, "who.ignore", [SPAM, SAM, ANN])])
        self.admin([C(S1, "who.ignore", [SPAM, ANN])])
        # the owners' entries cannot be removed: an admin's list is
        # always joined with the owners'
        self.admin([C(S1, "who.ignore", [ANN])])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [ANN, SPAM])
        # a reset reverts to the owners' list
        self.admin([C(S1, "who.ignore", "inherit")])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [SPAM])

    def test_forged_scope_key_and_value(self):
        self.refused([C(S2, "who.replies", "off")])          # other guild
        self.refused([C(P.channel(G1, CH_G2), "who.replies", "off")])
        self.refused([C(P.channel(G2, CH_G2), "who.replies", "off")])
        self.refused([C(P.channel(G1, "70000000000000000555"),
                        "who.replies", "off")])                   # unknown
        self.refused([C(GEN, "who.replies", "off")], info=None)  # no list
        for pl in (DEF, DMS):
            self.refused([C(pl, "who.replies", "off")])
        self.refused([C(S1, "who.allow", [SAM])])
        self.refused([C(S1, "privacy.memory", "nothing")])
        self.refused([C(GEN, "who.ignore", [SAM])])
        self.refused([C(S1, "media.enabled", True)])
        self.refused([C(S1, "who.replies", "conversational")])
        # a forged layer path on an innocent-looking change
        self.refused([C(S1, "who.replies", "off", path=("all_servers",))])
        self.refused([C(S1, "who.replies", "off",
                        path=("servers", G2))])
        # a preset is an owner's tool
        st = load(self.cfg)
        self.refused(MS.preset_changes(st, "work", S1, INFO))
        # a mixed batch is refused whole
        self.refused([C(S1, "media.enabled", False),
                      C(S1, "who.replies", "conversational")])

    def test_admin_undo_sees_only_admin_changes(self):
        apply(self.cfg, [C(S1, "who.replies", "off")], OWNER)
        st = load(self.cfg)
        self.assertEqual(MS.undo_plan(st, S1, admin_gid=G1), [])
        self.assertIsNone(MS.undo(self.cfg, S1, ADMIN, PLUGINS,
                                  admin_gid=G1, info=INFO))
        # back to the file's value: the owner's entry goes away
        apply(self.cfg, [C(S1, "who.replies", "when-addressed")], OWNER)
        self.assertNotIn(KS, doc(self.cfg)["values"])
        self.admin([C(GEN, "who.replies", "off")])
        apply(self.cfg, [C(S1, "style.length", "brief")], OWNER)
        st = load(self.cfg)
        self.assertEqual(MS.undo(self.cfg, S1, ADMIN, PLUGINS, children=True,
                                 admin_gid=G1, info=INFO), KS_GEN)
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "when-addressed")
        self.assertEqual(doc(self.cfg)["history"][-1]["role"], "admin")
        # another server's admin cannot undo it
        self.admin([C(GEN, "who.replies", "off")])
        self.assertIsNone(MS.undo(self.cfg, P.server(G2), ADMIN, PLUGINS,
                                  admin_gid=G2, info=INFO))

    def test_admin_undo_never_reverts_an_owner_rewrite(self):
        """An owner's pause written over an admin's same value: the
        admin's Undo no longer reaches it."""
        self.admin([C(S1, "who.replies", "off")])
        apply(self.cfg, [C(S1, "who.replies", "off")], OWNER)
        self.assertIsNone(MS.undo(self.cfg, S1, ADMIN, PLUGINS,
                                  children=True, admin_gid=G1, info=INFO))
        self.assertTrue(MS.is_paused(load(self.cfg), S1))

    def test_fold_keeps_admin_entries_and_unquoted_ids(self):
        self.admin([C(S1, "who.replies", "off")])
        apply(self.cfg, [C(S1, "style.length", "brief")], OWNER)
        MS.fold(self.cfg, PLUGINS)
        # in config.yaml it would be the owners' own value
        self.assertEqual(doc(self.cfg)["values"][KS]["role"], "admin")
        self.cfg = config(self, BASE.replace(f'"{G1}"', G1)
                          + f'    who:\n      ignore: ["{SPAM}"]\n')
        apply(self.cfg, [C(S1, "who.ignore", "inherit")], OWNER)
        MS.fold(self.cfg, PLUGINS)
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [])

# ---------------------------------------------------------------------------
# 7. who.dms_from lists
# ---------------------------------------------------------------------------
class DmsFrom(unittest.TestCase):
    def test_choices(self):
        with self.assertRaises(MS.SettingsError):
            load(config(self, BASE + 'dms:\n  who:\n    dms_from: ["sam"]\n'))
        cfg = config(self)
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "owners")
        apply(cfg, MS.dms_from_changes(st, "nobody"))
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "nobody")
        self.assertEqual(value(cfg, DMS, "who.replies"), "off")
        apply(cfg, MS.dms_from_changes(st, "list", [SAM]))
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "list")
        self.assertEqual(value(cfg, DMS, "who.dms_from"), [SAM])
        self.assertEqual(value(cfg, DMS, "who.replies"), "conversational")
        with self.assertRaises(MS.SettingsError):
            MS.dms_from_changes(st, "list", [])

    def test_monitor_dm_gate(self):
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         "]\n")
        sam = {"id": SAM, "username": "sam"}
        ann = {"id": ANN, "username": "ann"}
        owner = {"id": OWNER, "username": "zach"}
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertTrue(m.dm_sender_allowed(owner))
        self.assertFalse(m.dm_sender_allowed(ann))
        # dms.who.allow / ignore still apply on top of the list
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         f', "{ANN}"]\n    ignore: ["{ANN}"]\n')
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertFalse(m.dm_sender_allowed(ann))
        m = load_monitor(self, BASE + "dms:\n  who:\n    dms_from: anyone\n"
                         f'    allow: ["{SAM}"]\n')
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertFalse(m.dm_sender_allowed(ann))
        # Nobody: DMs off, owners included.
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         "]\n    replies: 'off'\n")
        self.assertFalse(m.dm_sender_allowed(sam))
        self.assertFalse(m.dm_sender_allowed(owner))


# ---------------------------------------------------------------------------
# 8. Presets
# ---------------------------------------------------------------------------
class Presets(unittest.TestCase):
    def test_presets(self):
        st = load(config(self))
        with self.assertRaises(MS.SettingsError):   # 1,240 members
            MS.preset_changes(st, "friends", S1, INFO)
        ch = MS.preset_changes(st, "friends", S3, INFO)
        self.assertIn(C(S3, "privacy.memory", "everything"), ch)
        self.assertTrue(MS.assess(st, ch, INFO).risky)  # confirm first
        for pl in (DEF, DMS, GEN):
            with self.assertRaises(MS.SettingsError):
                MS.preset_changes(st, "work", pl)
        with self.assertRaises(MS.SettingsError):
            MS.preset_changes(st, "party", S1)
        # A preset is one group: one Undo reverts it.
        cfg = config(self)
        st = load(cfg)
        self.assertIsNone(MS.matching_preset(st, S1))
        r = apply(cfg, MS.preset_changes(st, "community", S1, INFO))
        self.assertEqual(MS.matching_preset(load(cfg), S1), "Community")
        self.assertEqual(MS.preset_changes(load(cfg), "community", S1), [])
        self.assertEqual(len({h["group"] for h in doc(cfg)["history"]}), 1)
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertIsNone(MS.matching_preset(load(cfg), S1))
        self.assertFalse(MS.group_current(load(cfg), r.group))
        apply(cfg, MS.preset_changes(load(cfg), "work", S1))
        self.assertEqual(MS.matching_preset(load(cfg), S1), "Work")
        apply(cfg, MS.preset_changes(load(cfg), "friends", S3, INFO))
        self.assertEqual(MS.matching_preset(load(cfg), S3), "Friends")


# ---------------------------------------------------------------------------
# 9. Pause
# ---------------------------------------------------------------------------
class Pause(unittest.TestCase):
    def test_pause_and_resume(self):
        cfg = config(self, BASE + "    who:\n      replies: when-addressed\n")
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, S1), [])
        apply(cfg, MS.pause_changes(S1))
        st = load(cfg)
        self.assertTrue(MS.is_paused(st, S1))
        self.assertFalse(MS.is_paused(st, GEN))     # paused at the server
        self.assertEqual(MS.resume_changes(st, GEN), [])
        self.assertEqual(MS.resume_changes(st, S1),
                         [C(S1, "who.replies", "when-addressed")])
        apply(cfg, MS.resume_changes(st, S1))
        self.assertEqual(value(cfg, S1, "who.replies"), "when-addressed")
        apply(cfg, MS.pause_changes(GEN))
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        self.assertEqual(value(cfg, GEN, "who.replies"), "off")
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, GEN),
                         [C(GEN, "who.replies", "inherit")])
        # Pause everywhere and resume
        cfg = config(self)
        apply(cfg, MS.pause_changes(DEF))
        st = load(cfg)
        self.assertEqual(len(MS.resume_changes(st, DEF)), 2)
        apply(cfg, MS.resume_changes(st, DEF))
        self.assertFalse(MS.is_paused(load(cfg), DEF))


# ---------------------------------------------------------------------------
# 10. bin/settings.py
# ---------------------------------------------------------------------------
class CLI(unittest.TestCase):
    def setUp(self):
        self.cfg = config(self, state={"known_guilds": {G1: "Lounge",
                                                        G2: "Pixel"},
                                       "guild_members": {G1: 1240,
                                                         G2: 48213}})

    def run_cli(self, *args):
        r = subprocess.run([sys.executable,
                            os.path.join(MUT, "bin", "settings.py"),
                            "--config", self.cfg] + list(args),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_risky_needs_yes(self):
        rc, out = self.run_cli("set", "who.dms_from", "anyone", "--place",
                               "dms")
        self.assertEqual(rc, 3)
        self.assertNotIn("dms.who.dms_from", doc(self.cfg)["values"])
        rc, out = self.run_cli("set", "who.dms_from", "anyone", "--place",
                               "dms", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(value(self.cfg, DMS, "who.dms_from"), "anyone")
        rc, out = self.run_cli("set", "who.dms_from", "Nobody", "--place",
                               "dms")
        self.assertEqual(rc, 0, out)
        self.assertEqual(MS.dm_access(load(self.cfg)), "nobody")

    def test_errors(self):
        for args in (["set", "who.replies", "loud", "--place", "dms"],
                     ["set", "who.replies", "off", "--place", "nowhere"],
                     ["set", "volume", "11", "--place", "dms"]):
            rc, out = self.run_cli(*args)
            self.assertEqual(rc, 1, (args, out))
        self.assertEqual(doc(self.cfg)["values"], {})
        rc, out = self.run_cli("set", "who.replies", "off")
        self.assertEqual(rc, 2)   # --place is required
        # --by is a name: a user id (an owner's or anyone's) is refused,
        # so a CLI change can never pass for a Discord-side one.
        for by in (ADMIN, OWNER, " "):
            rc, out = self.run_cli("set", "style.length", "brief", "--place",
                                   f"server:{G1}", "--by", by)
            self.assertEqual(rc, 2, out)
        self.assertEqual(doc(self.cfg)["values"], {})


# ---------------------------------------------------------------------------
# 12. Review findings (2026-10-19): one regression test each
# ---------------------------------------------------------------------------
def admin_apply(cfg, changes, gid=G1, info=INFO):
    return MS.apply(cfg, changes, ADMIN, plugins_dir=PLUGINS, role="admin",
                    admin_gid=gid, info=info)


class ReviewHigh(unittest.TestCase):
    def test_h1_owner_resetting_an_admin_value_makes_it_the_owners(self):
        """poc1: an owner re-setting what an admin set is not a no-op; the
        entry becomes the owners', so the admin cannot revert it."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        admin_apply(cfg, [C(S1, "who.replies", "off")])
        r = apply(cfg, [C(S1, "who.replies", "off")])          # owner
        self.assertTrue(r.changed)
        e = doc(cfg)["values"][KS]
        self.assertNotIn("role", e)
        self.assertNotIn("below", e)
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # Never answer
        cfg = config(self)
        admin_apply(cfg, [C(S1, "who.ignore", [SPAM])])
        self.assertTrue(apply(cfg, [C(S1, "who.ignore", [SPAM])]).changed)
        for v in ([], "inherit"):
            with self.assertRaises(MS.SettingsError):
                admin_apply(cfg, [C(S1, "who.ignore", v)])
        self.assertEqual(value(cfg, S1, "who.ignore"), [SPAM])
        # an Extra
        cfg = config(self, BASE + "all_servers:\n  media:\n"
                     "    enabled: on\n")
        admin_apply(cfg, [C(S1, "media.enabled", False)])
        self.assertTrue(apply(cfg, [C(S1, "media.enabled",
                                      False)]).changed)
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "media.enabled", "inherit")])
        self.assertIs(value(cfg, S1, "media.enabled"), False)
        # the legacy set_value path (today's /settings)
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        MS.set_value(cfg, KS, "off", ADMIN, PLUGINS)
        self.assertEqual(MS.set_value(cfg, KS, "off", OWNER, PLUGINS), "off")
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "who.replies", "inherit")])
        with self.assertRaises(MS.SettingsError):
            MS.set_value(cfg, KS, "inherit", ADMIN, PLUGINS)

    def test_h3_roles_are_explicit(self):
        """poc2: who undoes is always stated; an admin never reverts an
        owners' group."""
        cfg = config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n")
        with self.assertRaises(MS.SettingsError):
            MS.apply(cfg, [C(S1, "style.length", "brief")], OWNER,
                     plugins_dir=PLUGINS)
        res = apply(cfg, [C(DMS, "who.dms_from", "owners")])
        for role in (None, "admin"):
            with self.assertRaises(MS.SettingsError):
                MS.undo_group(cfg, res.group, ADMIN, PLUGINS, role=role)
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")
        with self.assertRaises(MS.SettingsError):
            MS.undo_changes(cfg, DMS, ADMIN, PLUGINS, role="admin")

    def test_h2_admin_cannot_drop_an_owners_pin(self):
        """poc4: resetting an owner's entry was allowed whenever it changed
        nothing at that moment; a later parent change then leaked."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        apply(cfg, [C(RND, "who.replies", "when-addressed")])
        apply(cfg, [C(S1, "who.replies", "when-addressed")])
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        # quieter over the pin is fine, and stays bounded by it
        admin_apply(cfg, [C(RND, "who.replies", "off")])
        admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        # a pin in config.yaml is a pin too
        cfg = config(self, BASE + f'    channels:\n      "{CH2}":\n'
                     "        replies: when-addressed\n")
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])

    def test_h2_seed257(self):
        """poc3: category off by an admin, an owner's channel pin, then the
        admin resets the pin, sets it off, and undoes."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "when-addressed\n")
        CATP = P.channel(G1, CAT)
        admin_apply(cfg, [C(CATP, "who.replies", "off")])
        apply(cfg, [C(RND, "who.replies", "when-addressed")])
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        admin_apply(cfg, [C(RND, "who.replies", "off")])
        apply(cfg, [C(DEF, "who.replies", "off")])
        MS.undo(cfg, S1, ADMIN, PLUGINS, children=True, admin_gid=G1,
                info=INFO)
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")

class ReviewMedium(unittest.TestCase):
    def test_m4_two_processes_lose_nothing(self):
        """poc6: the CLI and the monitor write at once."""
        import multiprocessing as mp
        cfg = config(self)
        ctx = mp.get_context("fork")
        procs = [ctx.Process(target=_writer, args=(cfg, g, 25))
                 for g in (G1, G2)]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(60)
        self.assertEqual([pr.exitcode for pr in procs], [0, 0])
        lines = [h for h in doc(cfg)["history"]
                 if h.get("key", "").endswith("limits.per_day")]
        self.assertEqual(len(lines), 50)
        self.assertEqual(value(cfg, S1, "limits.per_day"), 1024)
        self.assertEqual(value(cfg, S2, "limits.per_day"), 1024)

    def test_m5_unreadable_overlay_is_never_wiped(self):
        for bad in ("values: [\n", "- a list\n", "values: 5\n"):
            cfg = config(self, overlay=bad)
            ov = MS.overlay_path_for(cfg)
            st = load(cfg)       # still loads (the bot keeps running)
            self.assertEqual(st.overlay, {})
            for fn in (lambda: apply(cfg, [C(S1, "who.replies", "off")]),
                       lambda: MS.set_value(cfg, KS, "off", OWNER, PLUGINS),
                       lambda: MS.undo(cfg, S1, OWNER, PLUGINS),
                       lambda: MS.undo_group(cfg, "g1", OWNER, PLUGINS,
                                             role="owner")):
                with self.assertRaises(MS.SettingsError):
                    fn()
            self.assertEqual(open(ov).read(), bad)
        cfg = config(self, overlay="values: [\n")
        r = subprocess.run([sys.executable, os.path.join(MUT, "bin",
                                                         "settings.py"),
                            "--config", cfg, "set", "who.replies", "off",
                            "--place", f"server:{G1}"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(open(MS.overlay_path_for(cfg)).read(), "values: [\n")

    def test_m6_owner_tightening_reaches_admin_entries(self):
        """poc5: an owner's pause reaches a channel an admin set."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        admin_apply(cfg, [C(GEN, "who.replies", "when-addressed")])
        apply(cfg, MS.pause_changes(S1))
        self.assertEqual(value(cfg, GEN, "who.replies"), "off")
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertEqual(value(cfg, GEN, "who.replies"), "when-addressed")
        # an admin's server entry does not outlive Pause everywhere
        admin_apply(cfg, [C(S1, "who.replies", "when-addressed")])
        apply(cfg, MS.pause_changes(DEF))
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # lists join; Extras need both on
        cfg = config(self, BASE + f'    who:\n      ignore: ["{SPAM}"]\n'
                     "all_servers:\n  media:\n    enabled: on\n")
        admin_apply(cfg, [C(S1, "who.ignore", [SPAM, SAM])])
        apply(cfg, [C(S1, "who.ignore", [SPAM, ANN])])
        self.assertEqual(value(cfg, S1, "who.ignore"), [SPAM, ANN])
        admin_apply(cfg, [C(GEN, "media.enabled", False)])
        apply(cfg, [C(DEF, "media.enabled", False)])
        apply(cfg, [C(DEF, "media.enabled", True)])
        self.assertIs(value(cfg, GEN, "media.enabled"), False)

    def test_m7_listed_people_are_polled_and_swept(self):
        text = BASE.replace("servers:", "everywhere:\n  privacy:\n    "
                            "never_share: [\"secret\"]\nservers:")
        m = load_monitor(self, text + f'dms:\n  who:\n    dms_from: '
                         f'["{SAM}"]\n')
        self.assertIn(SAM, m.dm_poll_user_ids())
        self.assertTrue(m.dm_swept_author(SAM))
        self.assertFalse(m.dm_swept_author(ANN))
        chans = dict(m.list_dm_channels())
        sam_dm = "4" + SAM[1:]
        own_dm = "4" + OWNER[1:]
        self.assertIn(sam_dm, chans)
        self.assertEqual(m.DM_CHANNEL_USER[sam_dm], SAM)
        # posts were only channel lookups (nothing sent)
        self.assertTrue(all(p == "/users/@me/channels"
                            for p, _d in m.api.posts))
        # never_share: still private for the owner's DM only
        st = {"hold_released": {}, "item_meta": {}}
        self.assertFalse(m.never_share_hit(st, {"channel_id": own_dm}, None,
                                           ["the secret"]))
        self.assertTrue(m.never_share_hit(st, {"channel_id": sam_dm}, None,
                                          ["the secret"]))
        # the sweep recovers a lost DM from Sam, not one from Ann
        import time as _t
        old = int(((_t.time() - 600) * 1000 - 1420070400000)) << 22
        sam_msg = {"id": str(old), "channel_id": sam_dm, "content": "hi",
                   "author": {"id": SAM, "username": "sam"}, "type": 0,
                   "timestamp": "2026-10-19T00:00:00+00:00", "mentions": []}
        ann_msg = dict(sam_msg, id=str(old + 1),
                       author={"id": ANN, "username": "ann"})
        m.api.history[sam_dm] = [sam_msg, ann_msg]
        with contextlib.redirect_stdout(io.StringIO()):
            state = m.load_state()
            q = []
            m.sweep_mentions(state, q, {}, dms=True)
        self.assertEqual([i.get("message_id") for i in q], [sam_msg["id"]])
        # a refused DM from a listed person is ledgered (never answered
        # late by the sweep)
        with open(m.cfg_path, "a") as f:
            f.write("    replies: 'off'\n")
        os.utime(m.cfg_path, (_t.time() + 5, _t.time() + 5))
        with contextlib.redirect_stdout(io.StringIO()):
            m.note_dm_refused(dict(sam_msg, id="123456789012345679"))
        self.assertEqual(m._ledger_load()["entries"]["123456789012345679"]
                         ["status"], "skipped")


def _writer(cfg, gid, n):
    for i in range(n):
        MS.apply(cfg, [C(P.server(gid), "limits.per_day", 1000 + i)], OWNER,
                 plugins_dir=PLUGINS, role="owner")


class ReviewLow(unittest.TestCase):
    @mock.patch.object(MS, "HISTORY_CAP", 9)   # odd: cuts land in a group
    def test_history_trim_keeps_groups_whole_and_resume_value(self):
        cfg = config(self, BASE + "    who:\n      replies: "
                     "when-addressed\n")
        apply(cfg, MS.pause_changes(S1))
        for i in range(8):
            apply(cfg, [C(S2, "limits.per_day", 10 + i),
                        C(S3, "limits.per_day", 10 + i)])
        hist = doc(cfg)["history"]
        self.assertLessEqual(len(hist), MS.HISTORY_CAP)
        groups = [h.get("group") for h in hist]
        self.assertNotEqual(groups[0], None)
        self.assertEqual(groups.count(groups[0]), 2)   # not split
        self.assertFalse(any(h.get("key") == KS for h in hist))
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, S1),
                         [C(S1, "who.replies", "when-addressed")])

# ---------------------------------------------------------------------------
# 13. Fuzz: server admins can never leave the bot louder than an
#     owners-only replay of the same owner actions.
# ---------------------------------------------------------------------------
_RANK = {"off": 0, "when-addressed": 1, "conversational": 2}
_UNL = "70000000000000000777"
_FUZZ_SCOPES = {
    "server": MS.Scope.server(G1),
    "#general": MS.Scope.server(G1, channel_id=CH, category_id=CAT),
    "#random": MS.Scope.server(G1, channel_id=CH2),
    "unlisted": MS.Scope.server(G1, channel_id=_UNL),
    "unlisted-in-cat": MS.Scope.server(G1, channel_id=_UNL,
                                       category_id=CAT),
}


class FuzzAdmin(unittest.TestCase):
    SEEDS = int(os.environ.get("FUZZ_SEEDS", "200"))
    STEPS = 10

    def eff(self, st):
        return {n: st.get("who.replies", sc)
                for n, sc in _FUZZ_SCOPES.items()}

    def test_admins_never_louder_than_owners_alone(self):
        import random
        CATP = P.channel(G1, CAT)
        oplaces = [DEF, S1, GEN, RND, CATP]
        aplaces = [S1, GEN, RND, CATP]
        vals = ["off", "when-addressed", "conversational", "inherit"]
        text = BASE + "all_servers:\n  who:\n    replies: when-addressed\n"
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            real, truth = config(self, text), config(self, text)
            st_real, st_truth = load(real), load(truth)
            log = []
            for _ in range(self.STEPS):
                kind = rnd.choice(["owner", "admin", "admin", "aundo"])
                try:
                    if kind == "owner":
                        ch = [C(rnd.choice(oplaces), "who.replies",
                                rnd.choice(vals))]
                        # an owner's action the two worlds would record
                        # differently (the no-op guard looks at what is
                        # in force) is left out of both
                        nr = [p.noop for p in MS.plan_changes(st_real, ch)]
                        nt = [p.noop for p in MS.plan_changes(st_truth, ch)]
                        if nr != nt:
                            continue
                        apply(real, ch)
                        apply(truth, ch)
                    elif kind == "admin":
                        ch = [C(rnd.choice(aplaces), "who.replies",
                                rnd.choice(vals))]
                        admin_apply(real, ch)
                    else:
                        ch = "undo"
                        MS.undo(real, S1, ADMIN, PLUGINS, children=True,
                                admin_gid=G1, info=INFO)
                    log.append(f"{kind} {ch}")
                except MS.SettingsError:
                    log.append(f"{kind} refused")
                # reload a world only after it may have been written
                st_real = load(real)
                if kind == "owner":
                    st_truth = load(truth)
                er, et = self.eff(st_real), self.eff(st_truth)
                bad = {n: (er[n], et[n]) for n in _FUZZ_SCOPES
                       if _RANK[er[n]] > _RANK[et[n]]}
                self.assertEqual(bad, {}, f"seed {seed}: " + "; ".join(log))


if __name__ == "__main__":
    unittest.main(verbosity=1)
