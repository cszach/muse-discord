"""The wake hook (hooks/mention-watch.sh) and owner-DM nonces: an owner
DM's owner_dm_nonce reaches only a wake that holds nothing but owner
DMs, and an outbox entry naming an owner DM counts as answering it only
when it carries that DM's nonce, so forged entries from another wake
(which the monitor refuses) never keep the DM from its own wake. Runs
the hook with bash and jq against the stubbed hatch hook runtime.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_hook.py"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import HOOK_RUNTIME, MUT_DIR, scratch  # noqa: E402

HOOK = os.path.join(MUT_DIR, "hooks", "mention-watch.sh")
N1, N2 = "a" * 32, "b" * 32


def item(mid, kind="dm", speaker="owner", frags=()):
    return {"message_id": mid, "channel_id": "9", "kind": kind,
            "content": "x",
            "burst_fragments": [{"message_id": f} for f in frags],
            "settings": {"where": "dm" if kind == "dm" else "server",
                         "speaker": speaker}}


class _Hook(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-hook-odm-")
        self.pkg = os.path.join(self.d, "pkg")
        os.makedirs(self.pkg)
        self.env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                        HOOK_STATE=os.path.join(self.d, "hookstate.json"),
                        MUSE_DISCORD_DIR=self.pkg,
                        MUSE_DISCORD_CONFIG=os.path.join(self.pkg,
                                                         "config.yaml"))
        self.write("owner_dm_nonces.json", {"1": N1, "2": N2})

    def write(self, name, data):
        with open(os.path.join(self.pkg, name), "w") as f:
            json.dump(data, f)

    def wake(self):
        os.path.exists(self.env["HOOK_STATE"]) \
            and os.remove(self.env["HOOK_STATE"])
        r = subprocess.run(["bash", HOOK], env=self.env, capture_output=True,
                           text=True, timeout=30)
        lines = r.stdout.splitlines()
        if not lines or not lines[0].startswith("WAKE:"):
            return None
        return json.loads(lines[1])

    def ids(self):
        p = self.wake()
        return None if p is None else [i["message_id"] for i in p["items"]]


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class ForgedAnswersDoNotHideOwnerDms(_Hook):
    def test_forged_entries_do_not_count(self):
        self.write("reply_queue.json", [item("1", frags=["11"]), item("2")])
        # Any one of these counting would drop "1" (or "2") from the wake.
        forged = [{"item": "1", "text": "x"},
                  {"reply_to": "11", "text": "x"},
                  {"item": "1", "silent": True,
                   "owner_dm_nonce": N2},      # another DM's
                  {"item": "1", "settings": {"nonce": "0" * 32}},
                  {"item": "1", "owner_dm_nonce": 5}]
        for name in ("reply_outbox.json", "reply_outbox.processing.json"):
            self.write(name, forged)
            self.assertEqual(self.ids(), ["1", "2"], name)
            os.remove(os.path.join(self.pkg, name))
        # Real answers (with the DM's nonce) do count.
        self.write("reply_queue.json", [item("1", frags=["11"]), item("2")])
        self.write("reply_outbox.json", [
            {"item": "1", "reply_to": "11", "owner_dm_nonce": N1},
            {"item": "2", "settings": {"nonce": N2}}])
        self.assertIsNone(self.ids())
        self.write("reply_outbox.json", [])
        self.write("reply_outbox.processing.json", [
            {"reply_to": "11", "owner_dm_nonce": N1}])
        self.assertEqual(self.ids(), ["2"])

    def test_other_items_never_get_an_owner_dm_nonce(self):
        # (A mixed queue's owner-DM-only wake: test_binding's
        # WithTheWakeHook.) Not owner DMs: any outbox entry naming them
        # answers them, as before; a nonce never reaches their wake (not
        # even one the nonce file lists for a server item).
        self.write("owner_dm_nonces.json", {"1": N1, "7": "c" * 32})
        self.write("reply_queue.json", [item("5", "mention", "member"),
                                        item("6", "dm", "member"),
                                        item("7", "mention", "owner")])
        self.write("reply_outbox.json", [{"item": "5", "text": "x"}])
        for want in (["7"], ["6"]):   # an owner's server item: alone
            p = self.wake()
            self.assertEqual([i["message_id"] for i in p["items"]], want)
            self.assertNotIn("owner_dm_nonce", json.dumps(p))
            self.assertNotIn("c" * 32, json.dumps(p))
            self.write("reply_queue.json", [item("5", "mention", "member"),
                                            item("6", "dm", "member")])

    def test_unreadable_nonce_file(self):
        with open(os.path.join(self.pkg, "owner_dm_nonces.json"), "w") as f:
            f.write("{not json")
        self.write("reply_queue.json", [item("1")])
        self.write("reply_outbox.json", [{"item": "1", "text": "x"}])
        # No nonce known for the owner DM: it waits (never handed out or
        # leased without one), and no outbox entry counts as answering
        # it; anyone else's item still goes out.
        self.assertIsNone(self.ids())
        self.write("reply_queue.json", [item("1"),
                                        item("5", "mention", "member")])
        self.assertEqual(self.ids(), ["5"])
        self.write("owner_dm_nonces.json", {"1": N1})
        self.write("reply_queue.json", [item("1")])
        self.assertEqual(self.ids(), ["1"])


def load_discord_api(work):
    """bin/discord-api.py with a test config and a stubbed API that
    records every call."""
    import importlib.util
    cfg = os.path.join(work, "config.yaml")
    with open(cfg, "w") as f:
        f.write('version: 1\nowners: ["100000000000000001"]\n'
                'token_from: env:MUSE_TEST_TOKEN\n')
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    os.environ["MUSE_TEST_TOKEN"] = "dummy-token"
    spec = importlib.util.spec_from_file_location(
        "discord_api_hook", os.path.join(MUT_DIR, "bin", "discord-api.py"))
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    d.calls = []

    def api(method, path, data=None):
        d.calls.append((method, path))
        if method == "GET" and path.count("/") == 2:   # /channels/<id>
            return {"id": path.rsplit("/", 1)[1], "type": 1,
                    "recipients": [{"id": "100000000000000002"}]}
        return [] if "/messages?" in path else {"id": "1"}
    d.api = api
    return d


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class OwnerCommandsWakeAlone(_Hook):
    """An owner's server command (owner_command_nonce) wakes alone, one
    per wake, owner DMs first, and only that wake gets its nonce: a
    stranger's run cannot borrow the owner's open item, even though its
    history names the owner's message id. Every item's reply_nonce
    reaches only the wake holding it, and only an outbox entry carrying
    it counts as answering the item."""
    OWN = "300000000000000007"
    OWN_CH = "200000000000000005"
    C = "e" * 32

    def setUp(self):
        super().setUp()
        self.items = {self.OWN: {"nonce": self.C, "channel_id": self.OWN_CH,
                                 "dm": False, "channels": [], "names": []},
                      "1": {"nonce": N1, "channel_id": "9", "dm": True,
                            "channels": [], "names": []}}
        self.write("owner_items.json", self.items)
        now = time.time()
        own = dict(item(self.OWN, "mention", "owner"), owner_command=True,
                   queued_at=now)
        stranger = dict(item("5", "mention", "member"), queued_at=now,
                        history=[{"id": self.OWN, "author": "owner",
                                  "text": "pin that"}])
        self.queue = [stranger, own]

    def run_hook(self):
        r = subprocess.run(["bash", HOOK], env=self.env, capture_output=True,
                           text=True, timeout=30)
        lines = r.stdout.splitlines()
        return r.stdout, (json.loads(lines[1]) if lines and
                          lines[0].startswith("WAKE:") else None)

    def test_owner_command_wakes_alone_with_its_nonce(self):
        self.write("reply_queue.json", [item("1")] + self.queue)
        # Owner DMs first, then the owner's command, then the stranger.
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"))
                          for i in p["items"]], [("1", None)])
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"))
                          for i in p["items"]], [(self.OWN, self.C)])
        self.assertNotIn(N1, out)
        out, p = self.run_hook()
        self.assertEqual([i["message_id"] for i in p["items"]], ["5"])
        self.assertNotIn(self.C, out)
        self.assertNotIn(N1, out)

    def test_owner_dm_command_gets_its_dm_nonce(self):
        self.write("reply_queue.json", [dict(item("1"), owner_command=True)])
        out, p = self.run_hook()
        self.assertEqual(p["items"][0].get("owner_command_nonce"), N1)
        self.assertEqual(p["items"][0].get("owner_dm_nonce"), N1)

    def test_owner_command_without_its_nonce_waits_a_while(self):
        # No file at all (a monitor from before it): handed out, alone,
        # without a nonce (the tool then refuses to act).
        os.remove(os.path.join(self.pkg, "owner_items.json"))
        self.write("reply_queue.json", self.queue)
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"))
                          for i in p["items"]], [(self.OWN, None)])
        os.remove(self.env["HOOK_STATE"])
        # Mangled, or no entry (the author is no owner any more): it
        # waits while the monitor may still write it...
        with open(os.path.join(self.pkg, "owner_items.json"), "w") as f:
            f.write("{broken")
        self.write("reply_queue.json", self.queue)
        out, p = self.run_hook()
        self.assertEqual([i["message_id"] for i in p["items"]], ["5"])
        self.assertIsNone(self.run_hook()[1])   # 5 is leased; OWN waits
        self.write("owner_items.json", self.items)
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"))
                          for i in p["items"]], [(self.OWN, self.C)])
        # ...but not for good: a minute after it was queued, it goes out
        # without owner powers.
        os.remove(self.env["HOOK_STATE"])
        self.write("owner_items.json", {})
        self.write("reply_queue.json", [dict(self.queue[1],
                                             queued_at=time.time() - 61)])
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"))
                          for i in p["items"]], [(self.OWN, None)])

    def test_reply_nonces_reach_their_own_wake_and_mark_answers(self):
        R5, RO = "5" * 32, "7" * 32
        self.write("item_nonces.json", {"5": R5, self.OWN: RO})
        self.write("reply_queue.json", self.queue)
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("reply_nonce"))
                          for i in p["items"]], [(self.OWN, RO)])
        self.assertNotIn(R5, out)
        out, p = self.run_hook()
        self.assertEqual([(i["message_id"], i.get("reply_nonce"))
                          for i in p["items"]], [("5", R5)])
        self.assertNotIn(RO, out)
        # The stranger's run writes entries for the owner's item: they
        # do not count, so the owner's item is handed out again.
        os.remove(self.env["HOOK_STATE"])
        self.write("reply_outbox.json", [
            {"item": self.OWN, "channel_id": "9", "silent": True},
            {"item": "5", "reply_to": self.OWN, "reply_nonce": R5,
             "text": "hi"}])
        out, p = self.run_hook()
        self.assertEqual([i["message_id"] for i in p["items"]], [self.OWN])
        # Its own run's answer does.
        os.remove(self.env["HOOK_STATE"])
        self.write("reply_outbox.json", [
            {"item": self.OWN, "reply_nonce": RO, "text": "Pinned."}])
        out, p = self.run_hook()
        self.assertEqual([i["message_id"] for i in p["items"]], ["5"])

    def test_stranger_run_cannot_borrow_the_owner_item(self):
        self.write("reply_queue.json", self.queue)
        self.run_hook()                       # the owner's own wake
        out, p = self.run_hook()              # the stranger's wake
        self.assertEqual([i["message_id"] for i in p["items"]], ["5"])
        self.assertIn(self.OWN, out)          # it knows the id...
        self.assertNotIn(self.C, out)         # ...not the nonce
        d = load_discord_api(self.d)
        d.OWNER_ITEMS_FILE = os.path.join(self.pkg, "owner_items.json")
        d.OWNER_DM_NONCES_FILE = os.path.join(self.pkg,
                                              "owner_dm_nonces.json")
        dm = "200000000000000004"             # someone's private DM
        for prefix in ([], ["--for", self.OWN], ["--for", "5"],
                       ["--for", self.OWN, "--nonce", N1],
                       ["--for", self.OWN, "--nonce", "0" * 32]):
            for sub in (["read", dm], ["post", dm, "x"],
                        ["read", self.OWN_CH]):
                with self.assertRaises(SystemExit):
                    d.main(["discord-api.py", *prefix, *sub])
        self.assertEqual(d.calls, [])
        # The owner's run, holding the nonce, may read its own channel,
        # and still not someone's DM.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(["discord-api.py", "--for", self.OWN,
                                     "--nonce", self.C, "read",
                                     self.OWN_CH]), 0)
        with self.assertRaises(SystemExit):
            d.main(["discord-api.py", "--for", self.OWN, "--nonce", self.C,
                    "read", dm])


if __name__ == "__main__":
    unittest.main()
