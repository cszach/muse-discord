"""/settings security, offline: forged custom ids from server admins,
members and other owners, stash tokens used by the wrong person, old
and malformed ids, and a fuzz over every id the owner's pages render.
Nothing here may write, crash, or loosen anything. Refusals are checked
by what did not happen (no write, an ephemeral answer), never by their
wording.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_security.py"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, BOB, G1, G2, G3, GENERAL, KAI, MANAGE,  # noqa
                     MEMBER, MS, MUSECHAT, OWNER, OWNER2, SAM, SPAM, UI,
                     FakeCtx, P, command, component, submit)

S1 = MS.Place.server(G1)
MC = MS.Place.channel(G1, MUSECHAT)


def owner_apply(ctx, changes):
    MS.apply(ctx.cfg, changes, OWNER, plugins_dir=H.PLUGINS, role="owner")
    ctx.refresh()


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()
        self.before = self.ctx.overlay()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        return r

    def admin(self, cid, values=None, guild=G1, **kw):
        return self.h(component(ADMIN, cid, values, guild, MANAGE, GENERAL,
                                **kw))

    def unchanged(self):
        self.assertEqual(self.ctx.overlay(), self.before)

    def assertAdminRefused(self, r):
        self.assertEqual(r["type"], 4, r)       # an ephemeral notice
        self.assertEqual(self.ctx.overlay(), self.before)


class AdminForgery(Base):
    def test_loosening_is_refused(self):
        # The owners make Muse Lounge quieter; an admin quiets #muse-chat.
        owner_apply(self.ctx, [MS.Change(S1, "who.replies",
                                         "when-addressed")])
        self.admin(f"{P}val:c{G1}.{MUSECHAT}:r:off")
        self.before = self.ctx.overlay()
        for cid, values in (
                (f"{P}val:s{G1}:r:conversational", None),
                (f"{P}val:c{G1}.{MUSECHAT}:r:conversational", None),
                (f"{P}val:s{G1}:~media.enabled:on", None),
                (f"{P}val:s{G1}:m:everything", None),
                (f"{P}set:s{G1}:r", ["conversational"]),
                (f"{P}val:s{G1}:pd:=", None),
                (f"{P}val:s{G1}:al:everyone", None),
                (f"{P}val:s{G1}:b:when-addressed", None),
                (f"{P}pre:s{G1}", ["friends"]),
                (f"{P}rs:s{G1}", None)):
            self.assertAdminRefused(self.admin(cid, values))
        self.unchanged()

    def test_other_servers_and_places(self):
        for cid in (f"{P}val:s{G3}:r:off",
                    f"{P}val:c{G3}.{H.ART}:r:off",
                    f"{P}pg:s{G3}:h", f"{P}pg:x:h", f"{P}pg:d:h",
                    f"{P}val:x:r:off", f"{P}val:d:r:off",
                    f"{P}val:c{G1}.71999999999999999999:r:off",
                    # an Undo without its group skips the stale check
                    # (review E #3): the admins' newest change stays
                    f"{P}un:s{G1}", f"{P}un:s{G1}:",
                    f"{P}un:s{G3}:g1", f"{P}un:x:g1"):
            self.assertAdminRefused(self.admin(cid))
        # their own server's panel from another server: refused too
        self.assertAdminRefused(self.admin(f"{P}pg:s{G1}:h", guild=G2))
        self.unchanged()

    def test_undoing_an_owner_entry_is_refused(self):
        # The owners set Muse Lounge's replies (seeded); an admin's reset
        # there would drop the owners' setting.
        self.assertAdminRefused(self.admin(f"{P}val:s{G1}:r:="))
        # Bob is ignored by the owners: taking him off is refused
        self.assertAdminRefused(self.admin(f"{P}rm:s{G1}:ig", [BOB]))
        self.assertAdminRefused(self.admin(f"{P}fx:s{G1}:ig:-{BOB}"))
        # The admin's Undo never reaches an owner's change
        owner_apply(self.ctx, [MS.Change(S1, "privacy.memory", "nothing")])
        theirs = H.undo_id(self.ctx, S1, admin_gid=G1)
        self.admin(theirs)                 # their own: spam1
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB])
        self.assertEqual(self.ctx.value("privacy.memory", S1), "nothing")
        self.assertIsNone(H.undo_id(self.ctx, S1, admin_gid=G1))
        self.admin(theirs)                 # nothing of theirs left
        self.assertEqual(self.ctx.value("privacy.memory", S1), "nothing")

    def test_owner_only_verbs(self):
        for cid, values in ((f"{P}pre:s{G1}", ["community"]),
                            (f"{P}set:s{G1}:r", ["when-addressed"]),
                            (f"{P}set:c{G1}.{GENERAL}:med", ["off"]),
                            (f"{P}wh:c{G1}.{GENERAL}", ["x"]),
                            (f"{P}fx:s{G1}:al:+{SAM}", None),
                            (f"{P}fx:s{G1}:r:=", None),
                            (f"{P}wy:d:{SAM}", None),
                            (f"{P}mod:d:wy", None)):
            self.assertAdminRefused(self.admin(cid, values))
        r = self.h(submit(ADMIN, f"{P}sub:s{G1}:wy", {"ids": SAM}, G1,
                          MANAGE, GENERAL))
        self.assertAdminRefused(r)
        self.unchanged()

    def test_ok_token_minted_for_an_owner(self):
        r = self.h(component(OWNER, f"{P}pz:x", None, G1, "0"))
        ok = next(c["custom_id"] for c in H.controls(r)
                  if c["custom_id"].split(":")[2] == "ok")
        tok = ok.split(":")[-1]
        for who in ((ADMIN, G1, MANAGE), (MEMBER, G1, "0"),
                    (OWNER2, None, None)):
            r = self.h(component(who[0], f"{P}ok:s{G1}:{tok}", None,
                                 who[1], who[2], GENERAL))
            self.assertEqual(r["type"], 4)
            self.unchanged()
        # A second owner is an owner, but it is not their token.
        self.ctx.owners.add(OWNER2)
        self.h(component(OWNER2, ok))
        self.unchanged()
        # still the owner's to use
        r = self.h(component(OWNER, ok, None, G1, "0"))
        self.assertTrue(MS.is_paused(self.ctx.reload(), MS.Place.defaults()))

    def test_admin_writes_are_reported(self):
        self.admin(f"{P}val:s{G1}:r:off")
        self.admin(f"{P}ppl:s{G1}:ig", [KAI],
                   resolved=H.resolved_for(7, [KAI]))
        self.admin(H.undo_id(self.ctx, S1, admin_gid=G1))
        self.assertEqual([n[:2] for n in self.ctx.notified],
                         [(G1, ADMIN)] * 3)
        # admin entries are recorded as an admin's
        e = self.ctx.overlay()["values"][f'servers."{G1}".who.replies']
        self.assertEqual(e["role"], "admin")

    def test_add_only_and_no_owners(self):
        self.admin(f"{P}ppl:s{G1}:ig", [OWNER, KAI],
                   resolved=H.resolved_for(7, [OWNER, KAI]))
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB, SPAM, KAI])
        # an empty pick from a forged select changes nothing
        self.admin(f"{P}ppl:s{G1}:ig", [])
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB, SPAM, KAI])


_OWNER_IDS = {}


def owner_ids(ctx, pages):
    """Every custom_id the owner's pages render (a short crawl, done
    once per page count: the fixtures are the same for every test)."""
    if pages not in _OWNER_IDS:
        H.install_determinism()
        ids = set()
        for ix in (command(OWNER, G1, "0"), command(OWNER)):
            actor = crawl.Actor(OWNER, ix.get("guild_id"),
                                "0" if ix.get("guild_id") else None)
            ids |= crawl.Crawl(ctx, actor, pages).run(
                "x", UI.handle(ix, ctx)).ids
        _OWNER_IDS[pages] = sorted(ids)
    return list(_OWNER_IDS[pages])


class Members(Base):
    def test_every_rendered_control_refused(self):
        ids = owner_ids(self.ctx, 80)
        self.ctx = FakeCtx()
        self.before = self.ctx.overlay()
        self.assertGreater(len(ids), 8)
        ids.append(f"mdset:open:server:{G1}")
        for cid in ids:
            for guild, perms in ((G1, "0"), (None, None)):
                r = self.h(component(MEMBER, cid, ["x"], guild, perms))
                self.assertEqual(r["type"], 4, cid)
        r = self.h(component(ADMIN, f"mdset:open:server:{G1}", None, G1,
                             MANAGE))
        self.assertEqual(r["type"], 4)
        self.unchanged()
        self.assertEqual(self.ctx.applied, [])


class Ids(Base):
    def test_old_and_malformed_ids_are_out_of_date(self):
        bad = ["mdset:p:e", "mdset:s:d:df", "mdset:u:s" + G1,
               "mdset:y:d:df:anyone", "mdset:1:pg:x:b",
               # the previous design's ids, from messages already posted
               "mdset:2:pg:x:b", f"mdset:2:set:s{G1}:r",
               f"{P}un:s{G1}", f"{P}un:s{G1}:",    # no group (review E #3)
               f"mdset:2:val:s{G1}:r:off:b", "mdset:2:pz:x",
               f"mdset:2:un:s{G1}", f"mdset:2:pre:s{G1}",
               # malformed ones in the current grammar
               f"{P[:-1]}", f"{P}pg", f"{P}pg:q:h", f"{P}pg:x:z",
               f"{P}pg:x:h:zz", f"{P}zz:x", f"{P}pg:c{G1}.{H.CAT}:h",
               f"{P}val:s{G1}:zz:on", f"{P}val:s{G1}:pk:custom",
               f"{P}val:s{G1}:r:loud", f"{P}val:s{G1}:r:off:zz",
               f"{P}fx:s{G1}:ig:-abc", f"{P}fx:s{G1}:r:?",
               f"{P}mod:x:zz", f"{P}wy:x", f"{P}ok:x",
               f"{P}pg:x:h:" + "9" * 90, "mdset:open:server:x"]
        for cid in bad:
            r = self.h(component(OWNER, cid, ["off"], G1, "0"))
            self.assertEqual(r["type"], 4, cid)
        self.unchanged()
        self.assertEqual(self.ctx.applied, [])

    def test_stash_wrong_user_keeps_the_token(self):
        tok = self.ctx.stash_put(OWNER, {"kind": "apply", "changes": [],
                                         "origin": ["x", "h"]})
        self.ctx.owners.add(OWNER2)
        self.h(component(OWNER2, f"{P}ok:x:{tok}"))
        self.assertIn(tok, self.ctx.stash)
        self.assertEqual(self.ctx.applied, [])


class Fuzz(Base):
    def test_mutated_ids_never_crash(self):
        rnd = random.Random(7)
        corpus = [c for c in owner_ids(self.ctx, 80) if c.startswith(P)]
        pieces = ["x", "d", f"s{G1}", f"c{G1}.{GENERAL}", "=", "on", "off",
                  "-" + SAM, "+" + SAM, "v", "h", "y", "", "~media.enabled",
                  "r", "df", "ig", "friends", "med", "pp", "everyone",
                  "99999999999999999999999999"]
        for _ in range(400):
            parts = rnd.choice(corpus).split(":")
            i = rnd.randrange(2, len(parts))
            parts[i] = rnd.choice(pieces)
            if rnd.random() < 0.3:
                parts.append(rnd.choice(pieces))
            cid = ":".join(parts)
            for user, guild, perms in ((OWNER, G1, "0"), (ADMIN, G1, MANAGE),
                                       (MEMBER, None, None)):
                r = self.h(component(user, cid, [rnd.choice(pieces)],
                                     guild, perms))
                self.assertIn(r["type"], (4, 7, 9), cid)
                if user == MEMBER:
                    self.assertEqual(r["type"], 4)
        self.assertEqual([a for a in self.ctx.applied if a[1] == MEMBER], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
