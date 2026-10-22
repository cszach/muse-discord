"""bin/discord-api.py is for owner commands only: every subcommand but
resolve-channel and list-threads needs --for <an open owner-command
item> --nonce <its owner_command_nonce> (checked in constant time
against owner_items.json), anywhere. In an owner's DM it must also be
that DM's item and nonce (owner_dm_nonces.json; that message must be in
that channel). So a worker woken by someone else's message cannot post,
edit, delete, react, pin or read anywhere, even knowing the owner's
message id. The API is stubbed.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_discord_api.py"""
import importlib.util
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

OWNER = "100000000000000001"
STRANGER = "100000000000000002"
BOTID = "100000000000000099"
OWNER_DM = "200000000000000001"
OTHER_DM = "200000000000000004"
GEN_CH = "200000000000000002"
ITEM = "300000000000000001"
NONCE = "c" * 32
SRV_ITEM = "300000000000000007"   # an owner's command in a server
SRV_NONCE = "e" * 32
STRANGER_ITEM = "300000000000000008"
NAMED_ID = "200000000000000006"    # named by <#id> in the owner's command
ANNOUNCE = "200000000000000007"    # named by #announcements
OTHER_CH = "200000000000000008"    # named nowhere in it
BARE_CH = "200000000000000010"     # named bare: "dev updates"
BARE_ELSEWHERE = "200000000000000011"   # same name, another server

WORK = scratch("muse-odm-api-")
CFG = os.path.join(WORK, "config.yaml")
with open(CFG, "w") as f:
    f.write(f'version: 1\nowners: ["{OWNER}"]\n'
            f'token_from: env:MUSE_TEST_TOKEN\n')
os.environ["MUSE_DISCORD_CONFIG"] = CFG
os.environ["MUSE_TEST_TOKEN"] = "dummy-token"
spec = importlib.util.spec_from_file_location(
    "discord_api_odm", os.path.join(MUT_DIR, "bin", "discord-api.py"))
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

CHANNELS = {
    OWNER_DM: {"id": OWNER_DM, "type": 1, "recipients": [{"id": OWNER}]},
    OTHER_DM: {"id": OTHER_DM, "type": 1,
               "recipients": [{"id": STRANGER}]},
    GEN_CH: {"id": GEN_CH, "type": 0, "guild_id": "5"},
    NAMED_ID: {"id": NAMED_ID, "type": 0, "name": "dev"},
    ANNOUNCE: {"id": ANNOUNCE, "type": 0, "name": "Announcements"},
    OTHER_CH: {"id": OTHER_CH, "type": 0, "name": "random",
               "guild_id": "5"},
    BARE_CH: {"id": BARE_CH, "type": 0, "name": "dev-updates",
              "guild_id": "5"},
    BARE_ELSEWHERE: {"id": BARE_ELSEWHERE, "type": 0,
                     "name": "dev-updates", "guild_id": "6"},
}
MESSAGES = {(OWNER_DM, ITEM)}   # (channel, message) that exist
CALLS = []


class NotFound(SystemExit):
    pass


def fake_api(method, path, data=None):
    CALLS.append((method, path, data))
    if path == "/users/@me":
        return {"id": BOTID}
    m = re.fullmatch(r"/channels/(\d+)", path)
    if method == "GET" and m:
        return CHANNELS[m[1]]
    m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
    if method == "GET" and m:
        if (m[1], m[2]) in MESSAGES or m[2] == "300000000000000009":
            return {"id": m[2], "author": {"id": BOTID}}
        raise NotFound(f"Discord API 404 on GET {path}")
    if method == "GET" and "/messages?" in path:
        return [{"id": ITEM, "author": {"username": "owner"},
                 "content": "secret plans"}]
    return {"id": "500000000000000001", "name": "t"}


D.api = fake_api
D.OWNER_DM_NONCES_FILE = os.path.join(WORK, "owner_dm_nonces.json")
D.OWNER_ITEMS_FILE = os.path.join(WORK, "owner_items.json")
SERVER = ("--for", SRV_ITEM, "--nonce", SRV_NONCE)


def entry(nonce, cid, dm=False, channels=(), post_channels=()):
    """An owner_items.json record, as the monitor writes it: the places
    the owner's own words name, resolved to ids when it was queued
    (monitor.owner_targets)."""
    return {"nonce": nonce, "channel_id": cid, "dm": dm,
            "channels": list(channels),
            "post_channels": list(post_channels)}


# The server command "post this to dev updates, and in <#NAMED_ID> and
# #announcements".
ITEMS = {ITEM: entry(NONCE, OWNER_DM, dm=True),
         SRV_ITEM: entry(SRV_NONCE, GEN_CH, channels=[NAMED_ID, ANNOUNCE],
                         post_channels=[BARE_CH])}


def write_owner_items(data):
    with open(D.OWNER_ITEMS_FILE, "w") as f:
        json.dump(data, f)


def writes():
    """The calls that change something, or read messages."""
    return [c for c in CALLS if c[0] != "GET" or "/messages?" in c[1]]


class _Tool(unittest.TestCase):
    def setUp(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE}, f)
        write_owner_items(ITEMS)

    def run_tool(self, *args):
        CALLS.clear()
        err, out = io.StringIO(), io.StringIO()
        old = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = err, out
        try:
            rc = D.main(["discord-api.py", *args])
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else str(e.code)
        finally:
            sys.stderr, sys.stdout = old
        self.assertNotIn(NONCE, out.getvalue() + err.getvalue()
                         + str(rc))
        return rc

    def every_subcommand(self, cid, prefix=()):
        msg = "300000000000000009"
        return [
            [*prefix, "post", cid, "hello"],
            [*prefix, "read", cid, "5"],
            [*prefix, "edit", cid, msg, "changed"],
            [*prefix, "delete-own", cid, msg],
            [*prefix, "react", cid, msg, "\U0001F44D"],
            [*prefix, "pin", cid, msg],
            [*prefix, "unpin", cid, msg],
            [*prefix, "thread", cid, "title", "text"],
        ]


class OwnerDmChannel(_Tool):
    def test_refused_with_a_wrong_or_stale_nonce(self):
        for prefix in (("--for", ITEM, "--nonce", "0" * 32),
                       ("--for", ITEM, "--nonce", NONCE + "0"),
                       ("--for", ITEM, "--nonce", "é" * 32),
                       ("--for", "300000000000000002", "--nonce", NONCE),
                       ("--nonce", NONCE, "--for", "1/../../x")):
            rc = self.run_tool(*prefix, "post", OWNER_DM, "hi")
            self.assertNotEqual(rc, 0, prefix)
            self.assertEqual(writes(), [], prefix)
        # Spent (the monitor dropped it from the file once answered).
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])
        os.remove(D.OWNER_DM_NONCES_FILE)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertEqual(writes(), [])

    def test_the_item_must_be_in_that_channel(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE, "300000000000000003": "d" * 32}, f)
        write_owner_items({ITEM: NONCE, "300000000000000003": "d" * 32})
        rc = self.run_tool("--for", "300000000000000003", "--nonce",
                           "d" * 32, "post", OWNER_DM, "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])

    def test_allowed_with_the_live_nonce_and_elsewhere(self):
        for args in self.every_subcommand(OWNER_DM, (
                "--for", ITEM, "--nonce", NONCE)):
            rc = self.run_tool(*args)
            self.assertEqual(rc, 0, args)
            self.assertTrue(writes(), args)
        # A server command: in its own channel, and where the owner's
        # own words point (<#id>, or #name, any case).
        for cid in (GEN_CH, NAMED_ID, ANNOUNCE):
            for args in self.every_subcommand(cid, SERVER):
                rc = self.run_tool(*args)
                self.assertEqual(rc, 0, args)
                self.assertTrue(writes(), args)
        # Nowhere else: not a channel its words do not name, not
        # someone's DM (named or not), and the owner DM's command not in
        # a server channel its words do not name.
        ITEMS[SRV_ITEM]["channels"].append(OTHER_DM)
        write_owner_items(ITEMS)
        ITEMS[SRV_ITEM]["channels"].pop()
        for cid, prefix in ((OTHER_CH, SERVER), (OTHER_DM, SERVER),
                            (GEN_CH, ("--for", ITEM, "--nonce", NONCE))):
            for args in self.every_subcommand(cid, prefix):
                self.assertNotEqual(self.run_tool(*args), 0, args)
                self.assertEqual(writes(), [], args)
        # A server command's nonce opens no owner DM.
        for args in self.every_subcommand(OWNER_DM, SERVER):
            self.assertNotEqual(self.run_tool(*args), 0, args)
            self.assertEqual(writes(), [], args)

    def test_unknown_dm_fails_closed(self):
        CHANNELS["200000000000000005"] = {"id": "200000000000000005",
                                          "type": 1}
        rc = self.run_tool(*SERVER, "post", "200000000000000005", "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])
        # Unreadable owners: every DM is treated as an owner's.
        real = D._owners
        D._owners = lambda: None
        try:
            rc = self.run_tool(*SERVER, "post", OTHER_DM, "hi")
            self.assertNotEqual(rc, 0)
            self.assertEqual(writes(), [])
            self.assertEqual(self.run_tool(*SERVER, "post", GEN_CH, "hi"),
                             0)
        finally:
            D._owners = real
        self.assertEqual(D._owners(), {OWNER})

    def test_non_canonical_ids_refused(self):
        for cid in ("0" + GEN_CH, GEN_CH.replace("2", "\u0662")):
            rc = self.run_tool(*SERVER, "post", cid, "hi")
            self.assertNotEqual(rc, 0)
            self.assertEqual(CALLS, [])


class OwnerCommandsOnly(_Tool):
    """Anywhere, not just an owner's DM: no valid owner --for, no act."""

    def test_refused_without_a_valid_owner_item(self):
        bad = [(), ("--for", SRV_ITEM), ("--for", ITEM),
               ("--nonce", SRV_NONCE),
               ("--for", SRV_ITEM, "--nonce", "0" * 32),
               ("--for", SRV_ITEM, "--nonce", NONCE),     # another's
               # A stranger's open item, or one the monitor never
               # recorded as an owner's command.
               ("--for", STRANGER_ITEM, "--nonce", SRV_NONCE),
               ("--for", "1/../x", "--nonce", SRV_NONCE)]
        for cid in (GEN_CH, OTHER_DM, OWNER_DM):
            for prefix in bad:
                for args in self.every_subcommand(cid, prefix):
                    self.assertNotEqual(self.run_tool(*args), 0, args)
                    self.assertEqual(CALLS, [], args)
        # Answered (the monitor dropped it), or no file at all.
        write_owner_items({ITEM: ITEMS[ITEM]})
        self.assertNotEqual(self.run_tool(*SERVER, "post", GEN_CH, "x"), 0)
        os.remove(D.OWNER_ITEMS_FILE)
        self.assertNotEqual(self.run_tool(*SERVER, "read", GEN_CH), 0)
        self.assertEqual(CALLS, [])

    def test_a_channel_named_bare_is_for_post_only(self):
        """"post this to dev updates": that channel (its id, resolved by
        the monitor) takes a post, and nothing else; other channels,
        whatever their names, take nothing."""
        self.assertEqual(self.run_tool(*SERVER, "post", BARE_CH, "hi"), 0)
        for args in self.every_subcommand(BARE_CH, SERVER):
            if args[len(SERVER)] != "post":
                self.assertNotEqual(self.run_tool(*args), 0, args)
                self.assertEqual(writes(), [], args)
        for cid in (BARE_ELSEWHERE, OTHER_CH):
            for args in self.every_subcommand(cid, SERVER):
                self.assertNotEqual(self.run_tool(*args), 0, args)
                self.assertEqual(writes(), [], args)

    def lookups(self, prefix):
        listing = [{"id": GEN_CH, "name": "general", "type": 0},
                   {"id": "2", "name": "layoffs-private", "type": 0}]
        real = D.api

        def api(method, path, data=None):
            CALLS.append((method, path, data))
            if path.endswith("/channels"):
                return listing
            return {"threads": [{"id": "1", "parent_id": GEN_CH,
                                 "name": "hr-thread"}]}
        D.api = api
        CALLS.clear()
        err, out = io.StringIO(), io.StringIO()
        old = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = err, out
        rcs = []
        try:
            for args in (("resolve-channel", "500000000000000001",
                          "#general"),
                         ("resolve-channel", "500000000000000001", "nope"),
                         ("list-threads", "500000000000000001", GEN_CH)):
                try:
                    rcs.append(D.main(["discord-api.py", *prefix, *args]))
                except SystemExit as e:
                    rcs.append(str(e))
                    err.write(str(e))
        finally:
            D.api = real
            sys.stderr, sys.stdout = old
        return rcs, out.getvalue() + err.getvalue()

    def test_lookups_need_an_owner_nonce(self):
        rcs, text = self.lookups(())
        self.assertNotIn(0, rcs)
        self.assertEqual(CALLS, [])
        for prefix in (SERVER, ("--for", ITEM, "--nonce", NONCE)):
            rcs, text = self.lookups(prefix)
            self.assertEqual((rcs[0], rcs[2]), (0, 0), text)
            # A miss never lists the server's other channels.
            self.assertNotIn("layoffs-private", text)
        # An owner DM that is no owner command (its DM nonce only).
        write_owner_items({SRV_ITEM: ITEMS[SRV_ITEM]})
        rcs, _ = self.lookups(("--for", ITEM, "--nonce", NONCE))
        self.assertEqual(rcs[0], 0)

    def test_read_is_wrapped_and_posts_never_carry_a_nonce(self):
        self.run_tool(*SERVER, "read", GEN_CH)
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            D.main(["discord-api.py", *SERVER, "read", GEN_CH])
            D.main(["discord-api.py", *SERVER, "read", GEN_CH])
            D.main(["discord-api.py", *SERVER, "post", GEN_CH,
                    "leak " + "ab" * 16])
        finally:
            sys.stdout = old
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("<<<untrusted"), lines)
        self.assertIn("secret plans", lines[1])
        self.assertTrue(lines[2].startswith("<<<end of untrusted"), lines)
        # A marker random to each call, closing only its own block.
        mark = lines[0].split()[3].rstrip(":")
        self.assertTrue(lines[2].endswith(f" {mark}>>>"), lines)
        self.assertNotEqual(mark, lines[3].split()[3].rstrip(":"))
        # Text goes out as written (no blanking): a secret is refused.
        self.assertEqual(CALLS[-1][2]["content"], "leak " + "ab" * 16)


if __name__ == "__main__":
    unittest.main()
