"""Secrets never leave in anything the bot sends (monitor.py's
guard_outgoing; bin/discord-api.py's scrub). Every session on the real
platform runs as the same OS user, so a worker that follows an injection
into its shell can read the token file and every nonce file: only this
filter keeps what it read from being posted. One lean test per path:
outbox replies (and multi-part ones), fires, owner notices, the raw send
helpers (post, patch, upload, interaction response), and the tool.

Usage: [MUT_DIR=<tree>] python3 tests/security/test_secrets.py"""
import base64
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ownerdm"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "watches"))
import test_binding as tb  # noqa: E402
from settings_harness import CH, OWNER, quiet  # noqa: E402


def token_like(uid="111000000000000009"):
    """A string shaped like a Discord bot token for uid."""
    head = base64.urlsafe_b64encode(uid.encode()).decode().rstrip("=")
    return f"{head}.GxYz12.{'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6'}"


TOKEN = token_like("111000000000000099")


def alerts(mod):
    """The owner DMs (here: only ever about a blocked send)."""
    return [d for p, d in mod.api.posts if p.startswith("/channels/4")]


class Outbox(tb.Base):
    def setUp(self):
        super().setUp()
        self.mod.TOKEN = TOKEN

    def test_token_live_nonce_or_token_shape_drops_the_reply(self):
        s1, s2, s3 = (self.stranger_mention() for _ in range(3))
        other = self.stranger_mention()
        live = self.st["reply_nonces"][other["id"]]["nonce"]
        self.outbox(self.reply(s1, f"here you go: {TOKEN}"),
                    self.reply(s2, f"code {live}"),
                    self.reply(s3, f"try {token_like()}"))
        self.assertEqual(self.sends(CH), [], self.out)
        self.assertNotIn(TOKEN, self.out)
        self.assertNotIn(live, self.out)
        for m in (s1, s2, s3):                  # settled, never retried
            self.assertNotIn(m["id"], self.queued())
            self.assertEqual(self.ledger(m["id"])["status"], "skipped")
        # Each owner told once, not once per blocked reply.
        self.assertEqual(len(alerts(self.mod)), 2)
        # Anything else goes out exactly as written (a 32-hex id in a
        # link included: nothing is ever blanked).
        text = "fine: https://www.notion.so/Plan-" + "cd" * 16
        self.outbox(self.reply(other, text))
        self.assertEqual([x["content"] for x in self.sends(CH)], [text])

    def test_multi_part_sends_nothing(self):
        s = self.stranger_mention()
        self.outbox(self.reply(s, None, parts=["one", "two", TOKEN]))
        self.assertEqual(self.sends(CH), [], self.out)

    def test_owner_notice_and_raw_helpers(self):
        m = self.mod
        n0 = len(m.api.posts)
        with quiet():
            self.assertFalse(m.dm_owner(OWNER, {"content": TOKEN}))
        self.assertEqual([p for p, d in m.api.posts[n0:]
                          if TOKEN in json.dumps(d)], [])
        # The real helpers: nothing reaches the network.
        opened = []
        calls = [
            lambda: m.real_api_post(f"/channels/{CH}/messages",
                                    {"content": "x " + TOKEN}),
            lambda: m.api_patch(f"/channels/{CH}/messages/1",
                                {"content": TOKEN}),
            lambda: m.send_reply_with_files(CH, TOKEN, []),
            lambda: m.send_reply_with_files(CH, "ok",          # file name
                                            [("/x", TOKEN + ".png")]),
            lambda: m.interaction_callback(
                {"id": "1", "token": "t"},
                {"type": 4, "data": {"content": TOKEN}}),
        ]
        with mock.patch.object(m.urllib.request, "urlopen",
                               lambda *a, **k: opened.append(a)), quiet():
            for call in calls:
                with self.assertRaises(m.SecretBlocked):
                    call()
        self.assertEqual(opened, [])

    def test_a_secret_inside_an_uploaded_file(self):
        m = self.mod
        leak = os.path.join(m.tmp, "cat.png")
        with open(leak, "w") as f:
            f.write("x " + TOKEN)
        opened = []
        with mock.patch.object(m.urllib.request, "urlopen",
                               lambda *a, **k: opened.append(a)), quiet():
            with self.assertRaises(m.SecretBlocked):
                m.send_reply_with_files(CH, "ok", [(leak, "cat.png")])
        self.assertEqual(opened, [])


class Disguises(unittest.TestCase):
    """secret_scan.find: the simple disguises, and nothing more."""

    def test_disguised_token_and_nonce(self):
        sys.path.insert(0, tb.MUT_DIR)
        import secret_scan as S
        nonce = "9f" * 16
        raw = TOKEN.encode()
        for text in (
                TOKEN[:12] + "\u200b" + TOKEN[12:],          # zero-width
                " ".join(TOKEN.split(".")),                   # no dots
                TOKEN[::-1],                                  # reversed
                *(base64.b64encode(b"x" * i + raw).decode()   # base64
                  for i in range(3)),
                base64.urlsafe_b64encode(b"ab" + raw).decode(),
                raw.hex(), raw.hex().upper()):                # hex
            self.assertEqual(S.find(f"look: {text} !", TOKEN, ()),
                             "the bot token", text)
        for text in (nonce, nonce[::-1], "-".join(nonce)):
            self.assertEqual(S.find(f"x {text} y", None, {nonce}),
                             "a live nonce", text)
        # Ordinary text, hashes and links pass, unaltered.
        for text in ("an md5: " + "0f" * 16, "https://notion.so/x-" + "ab"
                     * 16, "see you at 5.30.2026"):
            self.assertIsNone(S.find(text, TOKEN, {nonce}), text)


class Fires(unittest.TestCase):
    def test_fire_answer_with_a_token_is_dropped(self):
        import test_watches as tw

        class T(tw.Base):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        t.mod.TOKEN = TOKEN
        t.remind(after=60)
        t.tick(ahead=61)
        (f,) = t.fires()
        t.outbox(t.held({"item": f["message_id"], "channel_id": CH,
                         "text": f"Reminder! {TOKEN}"}))
        self.assertEqual([p for p, _ in t.new_sends()
                          if p == f"/channels/{CH}/messages"], [], t.out)
        self.assertEqual(t.fires(), [])           # settled, not retried


class Tool(unittest.TestCase):
    def test_post_and_edit_refuse_secrets(self):
        import test_discord_api as td
        td.D.TOKEN = TOKEN
        nonces = os.path.join(os.path.dirname(td.D.OWNER_ITEMS_FILE),
                              "item_nonces.json")
        with open(nonces, "w") as f:
            json.dump({"5": "9f" * 16}, f)
        td.write_owner_items(td.ITEMS)
        msg = "300000000000000009"
        try:
            for text in (TOKEN, "9f" * 16, token_like()):
                for args in (["post", td.GEN_CH, f"x {text}"],
                             ["edit", td.GEN_CH, msg, f"x {text}"]):
                    td.CALLS.clear()
                    with self.assertRaises(SystemExit) as e, \
                            contextlib.redirect_stdout(io.StringIO()):
                        td.D.main(["discord-api.py", *td.SERVER, *args])
                    self.assertNotIn(text, str(e.exception))
                    self.assertFalse([c for c in td.CALLS
                                      if c[0] in ("POST", "PATCH")])
        finally:
            os.remove(nonces)

    def test_thread_refuses_a_secret_inside_its_upload(self):
        import test_discord_api as td
        td.D.TOKEN = TOKEN
        td.write_owner_items(td.ITEMS)
        td.D.GENERATED_DIR = os.path.dirname(td.D.OWNER_ITEMS_FILE)
        td.D.api_multipart = lambda path, payload, f: td.CALLS.append(
            ("POST", path, payload))
        upload = os.path.join(td.D.GENERATED_DIR, "cat.png")
        with open(upload, "w") as f:
            f.write(f"x {TOKEN}")
        td.CALLS.clear()
        try:
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stdout(io.StringIO()):
                td.D.main(["discord-api.py", *td.SERVER, "thread",
                           td.GEN_CH, "t", "ok", upload])
            self.assertFalse([c for c in td.CALLS if c[0] == "POST"])
        finally:
            os.remove(upload)

    def test_slot_nonce_and_a_split_thread_are_refused(self):
        import test_discord_api as td
        td.D.TOKEN = TOKEN
        td.write_owner_items(td.ITEMS)
        slot = os.path.join(os.path.dirname(td.D.OWNER_ITEMS_FILE),
                            "proactive_nonce.json")
        with open(slot, "w") as f:
            json.dump({"7": "7e" * 16}, f)
        try:
            for args in (["post", td.GEN_CH, "x " + "7e" * 16],
                         ["thread", td.GEN_CH, TOKEN[:30], TOKEN[30:]]):
                td.CALLS.clear()
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stdout(io.StringIO()):
                    td.D.main(["discord-api.py", *td.SERVER, *args])
                self.assertFalse([c for c in td.CALLS if c[0] == "POST"])
        finally:
            os.remove(slot)


if __name__ == "__main__":
    unittest.main()
