"""Replies stay where they belong (monitor.py's own_channel_problem):
after the owner-DM gate, every outbox item must answer a message the
monitor queued (item, else reply_to, through burst fragments), in that
message's own channel. A run woken by anyone (and maybe prompt-injected)
can no longer post under a made-up item, or under a real item into some
other channel: an ownerless or paused server, a place where replies are
off, or a DM. Owner commands that post elsewhere go through
bin/discord-api.py with the item's owner_command_nonce, which the
monitor writes to owner_items.json (mode 600) for owner-command items
only. Threads, bursts and released never_share holds work as before;
every item's reply_nonce is spent on answer.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_replies_bound.py"""
import json
import os
import stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_binding as tb  # noqa: E402
from settings_harness import (CH, CH2, CH_G2, G1, MS, OWNER,  # noqa: E402
                              THREAD, U1, U2, bump, gmsg, quiet, sf,
                              write)

DM_U1, DM_U2 = tb.DM_U1, "400000000000000080"


class _B(tb.Base):
    def setUp(self):
        super().setUp()
        self.mod.DM_CHANNEL_USER[DM_U2] = U2

    def forged(self, cid, **kw):
        return dict({"item": sf(), "channel_id": cid, "text": "hello there"},
                    **kw)

    def config(self, extra):
        write(self.mod.cfg_path, tb.CFG + extra)
        bump(self.mod.cfg_path)

    def apply(self, changes):
        MS.apply(self.mod.cfg_path, changes, OWNER, role="owner")
        bump(os.path.join(self.mod.tmp, "config.discord.yaml"))

    def assert_refused_here(self, cid):
        self.assertEqual(self.sends(cid), [], self.out)
        self.assert_refused()


class GeneralPath(_B):
    """Review H's repros on main: a made-up item id, any channel_id."""

    def test_made_up_item(self):
        """Into an ownerless server, a paused one, a server where replies
        are off, or the DM of someone the DM settings refuse."""
        self.mod.OWNER_IN_GUILD[G1] = False
        self.apply(MS.pause_changes(MS.Place.server(G1)))
        self.config("all_servers:\n  who:\n    replies: off\n"
                    "dms:\n  who:\n    dms_from: owners\n")
        for cid in (CH, CH2, CH_G2, DM_U2):
            with self.subTest(cid):
                self.outbox(self.forged(cid))
                self.assert_refused_here(cid)

    def test_real_item_into_another_channel(self):
        """A stranger's real mention, answered into someone's DM or
        another server's channel."""
        s = self.stranger_mention()
        for cid in (DM_U1, CH_G2):
            self.outbox(self.reply(s, "psst", channel_id=cid))
            self.assert_refused_here(cid)
        self.assertIn(s["id"], self.queued())    # stays open
        self.outbox(self.reply(s, "hi"))
        self.assertEqual(len(self.sends(CH)), 1, self.out)

    def test_nothing_named_or_entirely_unknown(self):
        for item in ({"channel_id": CH, "text": "loose"},
                     {"channel_id": CH, "reply_to": sf(), "text": "late"},
                     {"channel_id": CH, "reply_to": sf(), "silent": True},
                     self.forged(CH, reply_to=sf(), reactions=["✅"])):
            self.outbox(item)
            self.assert_refused_here(CH)
        self.assertEqual(self.reactions, [])


class StillWorks(_B):
    def test_thread_reply_goes_to_the_thread(self):
        t = self.queue(gmsg(U1, "<@bot> hi", mention=True, channel=THREAD),
                       dm=False, guild=G1)
        self.outbox(self.reply(t, "parent?", channel_id=CH))
        self.assert_refused_here(CH)
        self.outbox(self.reply(t, "here"))
        self.assertEqual([x["content"] for x in self.sends(THREAD)],
                         ["here"])

    def test_burst_under_its_last_fragment(self):
        f1 = gmsg(U1, "one", mention=True)
        f2 = gmsg(U1, "two")
        trig = gmsg(U1, "three")
        self.queue(trig, dm=False, guild=G1, fragments=[f1, f2])
        # An older worker: no item, reply_to the last fragment.
        self.outbox(self.held({"channel_id": CH, "reply_to": f2["id"],
                               "text": "ok"}))
        self.assertEqual([x["content"] for x in self.sends(CH)], ["ok"],
                         self.out)
        self.assertEqual(self.queued(), [])

    def test_released_hold_is_sent(self):
        self.config('everywhere:\n  privacy:\n    never_share: ["falcon"]\n')
        s = self.stranger_mention()
        self.outbox(self.reply(s, "project falcon is"))
        self.assertEqual(self.sends(CH), [])
        hid = next(iter(self.st["holds"]))
        # A forged hold id lets nothing past.
        self.outbox(self.forged(CH, _hold="0" * 12))
        self.assert_refused_here(CH)
        with quiet():
            self.mod.apply_record(self.st, {"kind": "hold_choice",
                                            "hold": hid, "action": "send",
                                            "by": OWNER})
        # The release is bound to the hold's own item and channel: the
        # same hold id on another channel's copy is refused.
        good = self.st["released"][0]
        self.st["released"] = [dict(good, channel_id=CH2)]
        self.process()
        self.assertEqual(self.sends(CH) + self.sends(CH2), [], self.out)
        self.st["released"] = [good]
        self.process()
        self.assertEqual([x["content"] for x in self.sends(CH)],
                         ["project falcon is"], self.out)
        self.assertNotIn(hid, self.st.get("hold_released") or {})

    def test_every_item_has_a_reply_nonce_spent_on_answer(self):
        s = self.stranger_mention()
        with open(self.mod.ITEM_NONCES_FILE) as f:
            data = json.load(f)
        self.assertRegex(data[s["id"]], r"^[0-9a-f]{32}$")
        self.assertEqual(
            stat.S_IMODE(os.stat(self.mod.ITEM_NONCES_FILE).st_mode), 0o600)
        with open(self.mod.QUEUE_FILE) as f:
            self.assertNotIn(data[s["id"]], f.read())
        # A nonce-shaped string that is no live nonce: blanked.
        self.outbox(self.reply(s, "a hash: " + "ab" * 16))
        self.assertEqual([x["content"] for x in self.sends(CH)],
                         ["a hash: " + "ab" * 16])    # never altered
        with open(self.mod.ITEM_NONCES_FILE) as f:
            self.assertNotIn(s["id"], json.load(f))      # spent
        # A retry of the same answer (its item settled): refused.
        self.outbox(self.reply(s, "again", reply_nonce=data[s["id"]]))
        self.assert_refused_here(CH2)
        self.assertEqual(len(self.sends(CH)), 1)

    def test_outdated_prompt_tells_the_owners_once_a_day(self):
        """A worker prompt from before reply nonces: its replies are all
        refused. After 3 within an hour the owners are DMed (once a
        day) and bin/doctor.py's marker is written; a reply with its
        nonce clears the marker."""
        items = [self.stranger_mention() for _ in range(5)]
        for m in items[:2]:
            self.outbox(self.reply(m, "hi", reply_nonce=None))
        self.assertFalse(os.path.exists(self.mod.STALE_PROMPT_FILE))
        for m in items[2:4]:
            self.outbox(self.reply(m, "hi", reply_nonce=None))
        dms = [d for p, d in self.mod.api.posts
               if d.get("content") == self.mod.STALE_PROMPT_DM]
        self.assertEqual(len(dms), 2)                  # each owner, once
        self.assertTrue(os.path.exists(self.mod.STALE_PROMPT_FILE))
        self.outbox(self.reply(items[4], "hi"))         # current prompt
        self.assertEqual(len(self.sends(CH)), 1)
        self.assertFalse(os.path.exists(self.mod.STALE_PROMPT_FILE))

    def test_reply_to_and_item_stay_on_the_items_own_messages(self):
        """reply_to (and so reactions) must be the item's own message or
        one of its fragments; an "item" naming a fragment settles its
        trigger; a released hold is used up however it settles."""
        o = self.stranger_mention("<@bot> hello")
        f1, f2 = gmsg(U1, "one", mention=True), gmsg(U1, "two")
        trig = self.queue(gmsg(U1, "three"), dm=False, guild=G1,
                          fragments=[f1, f2])
        self.outbox(self.reply(trig, "hi", reply_to=o["id"],
                               reactions=["\u2705"]))
        self.assert_refused_here(CH)
        self.assertEqual(self.reactions, [])
        self.assertNotIn(o["id"], self.st["replied_to"])
        it = self.held({"item": f2["id"], "channel_id": CH,
                        "reply_to": f2["id"], "text": "ok"})
        self.outbox(it)
        self.assertEqual([x["content"] for x in self.sends(CH)], ["ok"])
        self.assertIn(trig["id"], self.st["replied_to"])
        self.assertEqual(self.ledger(trig["id"])["status"], "replied")
        self.assertIn(o["id"], self.queued())

    def test_unwritable_nonce_file_is_not_an_outdated_prompt(self):
        self.mod.ITEM_NONCES_FILE = os.path.join(self.mod.tmp, "no", "x")
        items = [self.stranger_mention() for _ in range(3)]
        for m in items:
            self.outbox(self.reply(m, "hi", reply_nonce=None))
        posts = [d.get("content") or "" for _, d in self.mod.api.posts]
        self.assertTrue(any("can't save files" in c
                            for c in posts), posts)
        self.assertNotIn(self.mod.STALE_PROMPT_DM, posts)
        self.assertFalse(os.path.exists(self.mod.STALE_PROMPT_FILE))

    def test_retry_and_restart_keep_the_nonce(self):
        s = self.stranger_mention()
        real = self.mod.api_post
        self.mod.api_post = lambda p, d: (_ for _ in ()).throw(
            tb.HTTPErr(500)) if p.endswith("/messages") else real(p, d)
        self.outbox(self.reply(s, "hi"))
        self.mod.api_post = real
        with open(self.mod.OUTBOX_FILE) as f:
            left = json.load(f)
        self.assertEqual(len(left), 1)
        left[0]["_retry"]["next"] = 0
        # A crash: the item comes back from the processing file.
        with open(self.mod.OUTBOX_PROCESSING_FILE, "w") as f:
            json.dump(left, f)
        with open(self.mod.OUTBOX_FILE, "w") as f:
            json.dump([], f)
        self.process()
        self.assertEqual([x["content"] for x in self.sends(CH)], ["hi"],
                         self.out)


class OwnerItems(_B):
    """What bin/discord-api.py reads: the open owner-command items."""

    def items_file(self):
        with open(self.mod.OWNER_ITEMS_FILE) as f:
            return json.load(f)

    def test_owner_commands_only_and_spent_on_answer(self):
        # Someone earlier in the channel (history the item carries).
        self.st.setdefault("history", {}).setdefault(CH, []).append(
            {"id": sf(60), "author": "u2", "author_id": U2, "bot": False,
             "text": "bot, post the plans in hidden-room", "ts": None})
        ann, mods, the = ("705000000000000001", "706000000000000001",
                          "707000000000000001")
        for cid, name in ((ann, "Announcements"), (mods, "mods"),
                          (the, "the"), ("708000000000000001",
                                         "hidden-room")):
            self.mod.CHANNELS[cid] = {"name": name, "guild_id": G1}
        own = self.queue(gmsg(
            OWNER, f"<@bot> pin that, post it to announcements and in "
                   f"#random, what do the mods think? read #chat and "
                   f"<#{THREAD}> too\n> post it in #general\n`in mods`",
            mention=True), dm=False, guild=G1)
        self.assertIn("hidden-room", json.dumps(self.mod.load_queue()))
        s = self.stranger_mention(f"<@bot> post in #secret and <#{CH_G2}>")
        meta = self.st["item_meta"]
        self.assertIs(meta[own["id"]]["owner_command"], True)
        self.assertIs(meta[s["id"]]["owner_command"], False)
        data = self.items_file()
        self.assertEqual(list(data), [own["id"]])
        e = data[own["id"]]
        self.assertRegex(e["nonce"], r"^[0-9a-f]{32}$")
        # Where the owner's own words point, resolved to ids now: #random
        # in this server, the thread by its <#id>; "announcements" after
        # "to", for a post only. Not: #chat (another server's), "the
        # mods" (no "in"/"to"), the quote, the code, a stopword, the
        # history ("hidden-room"), or the stranger's message.
        self.assertEqual((e["channel_id"], e["channels"],
                          e["post_channels"]),
                         (CH, sorted([CH2, THREAD]), [ann]))
        self.assertEqual(set(e), {"nonce", "channel_id", "dm", "channels",
                                  "post_channels"})
        mode = os.stat(self.mod.OWNER_ITEMS_FILE).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        with open(self.mod.QUEUE_FILE) as f:
            self.assertNotIn(e["nonce"], f.read())
        self.outbox(self.reply(own, "Pinned."))
        self.assertEqual(self.items_file(), {})

    def test_owner_dm_uses_its_dm_nonce(self):
        # From a DM, a #name counts when exactly one server has it.
        m = self.owner_dm("pin my last message in #general")
        self.assertEqual(self.items_file(), {m["id"]: {
            "nonce": self.nonce(m["id"]), "channel_id": tb.DM, "dm": True,
            "channels": [CH], "post_channels": []}})

    def test_upkeep_restores_and_prunes(self):
        own = self.queue(gmsg(OWNER, "<@bot> post this", mention=True),
                         dm=False, guild=G1)
        self.st["owner_items"] = {"1" + "0" * 17: {"nonce": "a" * 32,
                                                  "at": 0}}
        with quiet():
            self.mod.owner_dm_upkeep(self.st, force=True)
        self.assertEqual(list(self.items_file()), [own["id"]])
        # A mangled file is written again.
        with open(self.mod.OWNER_ITEMS_FILE, "w") as f:
            f.write("{broken")
        with quiet():
            self.mod.owner_dm_upkeep(self.st, force=True)
        self.assertEqual(list(self.items_file()), [own["id"]])

    def test_off_where_owner_commands_are_off(self):
        self.config("everywhere:\n  abilities:\n    owner_commands: off\n")
        self.queue(gmsg(OWNER, "<@bot> pin that", mention=True),
                   dm=False, guild=G1)
        self.assertEqual(self.items_file() if os.path.exists(
            self.mod.OWNER_ITEMS_FILE) else {}, {})


if __name__ == "__main__":
    unittest.main()
