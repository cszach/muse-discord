"""/settings behaviour, click by click, offline: an owner's change
applies and can be undone (a stale Undo never applies, a group's Undo is
judged by its whole outcome), Pause everywhere asks first and undoes, a
server admin can only make things quieter and undo their own change, a
stranger is refused, an unreadable overlay writes nothing, and a crash
is an apology. Controls are found by their custom_id (verb and place),
never by their label or the page's words, so the design can change any
copy.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_walkthroughs.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, CAT, G1, GENERAL, KAI, MANAGE,  # noqa: E402
                     MEMBER, MS, MUSECHAT, OWNER, UI, FakeCtx, P, command,
                     component, context_command)

S1 = MS.Place.server(G1)
MC = MS.Place.channel(G1, MUSECHAT)
DEF = MS.Place.defaults()
DMS = MS.Place.dms()


def verb(c):
    return c["custom_id"].split(":")[2]


def control(resp, which):
    """The first control whose verb is which (or whose custom_id starts
    with which, when it has a colon)."""
    for c in H.controls(resp):
        if (":" in which and c["custom_id"].startswith(which)) \
                or (":" not in which and verb(c) == which):
            return c
    return None


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        self.assertEqual(crawl.check(r, "response"), [])
        return r

    def owner(self, cid, values=None, guild=G1, **kw):
        return self.h(component(OWNER, cid, values, guild,
                                "0" if guild else None, **kw))

    def admin(self, cid, values=None, **kw):
        return self.h(component(ADMIN, cid, values, G1, MANAGE, MUSECHAT,
                                **kw))

    def confirm_if_asked(self, r, who=None):
        """A risky outcome asks first: press its confirmation."""
        ok = control(r, "ok")
        if ok is not None:
            r = (who or self.owner)(ok["custom_id"])
        return r

    def val(self, key, place):
        return self.ctx.value(key, place)


class Owner(Base):
    def test_change_applies_and_undo_reverts(self):
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        style = control(r, f"{P}pre:s{G1}")
        self.assertIsNotNone(style)
        before = self.val("who.replies", S1)
        r = self.owner(style["custom_id"], ["community"])
        self.assertEqual(self.val("who.replies", S1), "when-addressed")
        self.assertEqual(self.val("limits.per_person_per_day", S1), 20)
        r = self.confirm_if_asked(self.owner(control(r, "un")["custom_id"]))
        self.assertEqual(self.val("who.replies", S1), before)
        self.assertIsNone(self.val("limits.per_person_per_day", S1))
        # A stale Undo (the setting changed since) never applies.
        r = self.owner(style["custom_id"], ["community"])
        stale = control(r, "un")["custom_id"]
        self.confirm_if_asked(self.owner(style["custom_id"], ["work"]))
        self.owner(stale)
        self.assertEqual(self.val("privacy.memory", S1), "nothing")

    def test_a_setting_applies_and_bad_values_write_nothing(self):
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        here = control(r, f"{P}set:c{G1}.{MUSECHAT}:r")
        r = self.owner(here["custom_id"], ["when-addressed"])
        self.assertEqual(self.val("who.replies", MC), "when-addressed")
        self.confirm_if_asked(self.owner(control(r, "un")["custom_id"]))
        self.assertIsNone(self.ctx.overlay()["values"].get(
            f'servers."{G1}".channels."{MUSECHAT}".replies'))
        # The server's page, through the Where select.
        r = self.owner(control(r, "wh")["custom_id"], [f"s{G1}"])
        cap = control(r, f"{P}set:s{G1}:pp")
        self.assertIsNotNone(cap)
        before = self.ctx.overlay()
        for bad in ("lots", "-5", "100001", "=", ""):
            self.owner(cap["custom_id"], [bad])
        self.owner(f"{P}set:c{G1}.{MUSECHAT}:ln", ["brief"])  # not here
        self.owner(f"{P}wh:c{G1}.{MUSECHAT}", [f"s{H.G3}"])    # elsewhere
        self.assertEqual(self.ctx.overlay(), before)
        self.confirm_if_asked(self.owner(cap["custom_id"], ["20"]))
        self.assertEqual(self.val("limits.per_person_per_day", S1), 20)

    def test_customized_and_forged_styles_write_nothing(self):
        """The style select's "Customized" option (shown while no style
        matches) and a forged style value are no-ops."""
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        style = control(r, f"{P}pre:s{G1}")
        others = [o["value"] for o in style["options"]
                  if o["value"] not in MS.PRESETS]
        self.assertTrue(others)             # Muse Lounge matches none
        before = self.ctx.overlay()
        for v in others + ["chaos", "Friends", ""]:
            self.owner(style["custom_id"], [v])
        self.assertEqual(self.ctx.overlay(), before)
        self.assertEqual(self.ctx.applied, [])

    def test_pause_here_and_resume(self):
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        r = self.owner(control(r, f"{P}pz:c{G1}.{MUSECHAT}")["custom_id"])
        self.assertEqual(self.val("who.replies", MC), "off")
        self.assertNotEqual(self.val("who.replies", S1), "off")
        self.confirm_if_asked(self.owner(
            control(r, f"{P}rs:c{G1}.{MUSECHAT}")["custom_id"]))
        self.assertNotEqual(self.val("who.replies", MC), "off")

    def test_undo_of_a_group_is_judged_whole(self):
        """Undo on a server page undoes the newest group there, all of it:
        when that would open DMs to anyone again it asks first."""
        def apply(*changes):
            MS.apply(self.ctx.cfg, list(changes), OWNER,
                     plugins_dir=H.PLUGINS, role="owner")
        apply(MS.Change(DMS, "who.dms_from", "anyone"))
        apply(MS.Change(DMS, "who.dms_from", "owners"),
              MS.Change(S1, "style.length", "brief"))
        r = self.owner(H.undo_id(self.ctx, S1))
        self.assertEqual(self.val("who.dms_from", DMS), "owners")  # asked
        self.owner(control(r, "ok")["custom_id"])
        self.assertEqual(self.val("who.dms_from", DMS), "anyone")
        self.assertEqual(self.val("style.length", S1), "normal")

    def test_pause_everywhere_asks_first_and_undoes(self):
        r = self.owner(f"{P}pg:x:h", guild=None)
        r = self.owner(control(r, f"{P}pz:x")["custom_id"], guild=None)
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))  # asked
        self.assertEqual(self.ctx.applied, [])
        ok = control(r, "ok")["custom_id"]
        # An expired confirmation (the stash is gone) changes nothing.
        stash = dict(self.ctx.stash)
        self.ctx.stash.clear()
        self.owner(ok)
        self.assertEqual(self.ctx.applied, [])
        self.ctx.stash.update(stash)
        r = self.owner(ok)
        self.assertTrue(MS.is_paused(self.ctx.reload(), DEF))
        self.confirm_if_asked(self.owner(control(r, "un")["custom_id"]))
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))

    def test_unreadable_overlay_writes_nothing(self):
        self.ctx = FakeCtx(broken_overlay=True)
        with open(self.ctx.overlay_path) as f:
            before = f.read()
        r = self.owner(f"{P}pg:c{G1}.{MUSECHAT}:h")
        self.assertEqual({verb(c) for c in H.controls(r)}
                         & set(UI.WRITE_VERBS), set())
        self.owner(f"{P}pre:s{G1}", ["work"])     # a stale page
        with open(self.ctx.overlay_path) as f:
            self.assertEqual(f.read(), before)

    def test_crash_is_an_ephemeral_apology(self):
        class Boom(FakeCtx):
            def guilds(self):
                raise RuntimeError("boom")

            @property
            def owners(self):
                raise RuntimeError("boom")

            @owners.setter
            def owners(self, v):
                pass
        ctx = Boom()
        r = UI.handle(command(OWNER, G1, "0"), ctx)
        self.assertEqual(r["type"], 4)
        self.assertTrue(r["data"]["flags"] & 64)
        self.assertNotIn("boom", str(r))            # no internals shown
        self.assertTrue(crawl.crashed(ctx))         # but logged


class Admin(Base):
    def test_quieter_only_and_undo(self):
        r = self.h(command(ADMIN, G1, MANAGE, MUSECHAT))
        # Every write the panel offers makes things quieter: none sets
        # Chatty or turns anything on.
        for c in H.controls(r):
            self.assertNotIn(":conversational", c["custom_id"])
            self.assertFalse(c["custom_id"].endswith(":on"))
        # (Refusals and the owners' notice: test_security.AdminForgery.)
        r = self.admin(f"{P}val:c{G1}.{MUSECHAT}:r:off")
        self.assertEqual(self.val("who.replies", MC), "off")
        self.admin(control(r, "un")["custom_id"])
        self.assertNotEqual(self.val("who.replies", MC), "off")

    def test_admin_turns_back_on_what_an_admin_turned_off(self):
        """Review E #6: images & video turned off by an admin can be
        turned back on from the panel, without undoing the admins'
        later changes first, and only as far as the owners' value."""
        MS.apply(self.ctx.cfg, [MS.Change(S1, "media.enabled", "on")],
                 OWNER, plugins_dir=H.PLUGINS, role="owner")
        self.admin(f"{P}val:s{G1}:~media.enabled:off")
        self.admin(f"{P}ppl:s{G1}:ig", [KAI],
                   resolved=H.resolved_for(7, [KAI]))
        r = self.h(command(ADMIN, G1, MANAGE, MUSECHAT))
        back = control(r, f"{P}val:s{G1}:~media.enabled:=")
        self.assertIsNotNone(back)
        self.admin(back["custom_id"])
        self.assertTrue(self.val("media.enabled", S1))
        self.assertIn(KAI, self.val("who.ignore", S1))


class Category(Base):
    def test_a_channel_follows_its_category(self):
        """Review E #4: #general is in the Staff category, which turns
        replies off. Its follow option ("=") gives the category's value,
        not the server's."""
        GEN = MS.Place.channel(G1, GENERAL, CAT)
        MS.apply(self.ctx.cfg, [MS.Change(GEN, "who.replies",
                                          "when-addressed")],
                 OWNER, plugins_dir=H.PLUGINS, role="owner")
        r = self.h(command(OWNER, G1, "0", GENERAL))
        sel = control(r, f"{P}set:c{G1}.{GENERAL}:r")
        self.assertIn("=", [o["value"] for o in sel["options"]])
        self.owner(sel["custom_id"], ["="], channel=GENERAL)
        self.assertEqual(self.val("who.replies", GEN), "off")


class Stranger(Base):
    def test_member_is_refused_everywhere(self):
        for ix in (command(MEMBER, G1, "0"), command(MEMBER),
                   command(ADMIN),                   # an admin in a DM
                   context_command(MEMBER, H.OUTCOMES[1]["message_id"],
                                   perms="0"),
                   component(MEMBER, f"mdset:open:server:{G1}")):
            r = self.h(ix)
            self.assertEqual(r["type"], 4)
            self.assertEqual(H.controls(r), [])
        self.assertEqual(self.ctx.applied, [])
        r = self.h(command(ADMIN, G1, MANAGE, GENERAL))   # an admin: panel
        self.assertTrue(H.controls(r))


if __name__ == "__main__":
    unittest.main()
