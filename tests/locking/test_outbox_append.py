"""bin/outbox-append.py, offline: the worker's way to append to
reply_outbox.json. Good items are appended exactly as given (an
owner_dm_nonce and a settings action included); a batch with any bad
item (a reply in the wrong field, a malformed settings action, a
monitor-only field, a nonce that is not a copied value) appends nothing
and exits 1. MUT_DIR = the package tree under test."""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

N = "ab" * 16
CHANGE = {"op": "set", "place": "defaults", "key": "who.replies",
          "value": "off"}


def settings_item(changes, nonce=N, **kw):
    it = {"item": "123", "channel_id": "456", "reply_to": "123",
          "text": "Sure.", "owner_dm_nonce": nonce,
          "settings": {"v": 1, "nonce": nonce, "changes": changes}}
    it.update(kw)
    return it


class OutboxAppend(unittest.TestCase):
    def setUp(self):
        self.pkg = scratch("muse-append-")
        os.makedirs(os.path.join(self.pkg, "bin"))
        shutil.copy(os.path.join(MUT_DIR, "bin", "outbox-append.py"),
                    os.path.join(self.pkg, "bin"))
        for f in ("muse_settings.py", "settings_nl.py"):
            shutil.copy(os.path.join(MUT_DIR, f), self.pkg)
        shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                        os.path.join(self.pkg, "plugins"))

    def append(self, items):
        r = subprocess.run([sys.executable, os.path.join(
            self.pkg, "bin", "outbox-append.py")], input=json.dumps(items),
            capture_output=True, text=True, timeout=30)
        return r.returncode, r.stderr

    def outbox(self):
        try:
            with open(os.path.join(self.pkg, "reply_outbox.json")) as f:
                return json.load(f)
        except OSError:
            return []

    def test_good_items_appended_as_is(self):
        items = [{"item": "1", "channel_id": "2", "reply_to": "1",
                  "text": "hi", "owner_dm_nonce": N},
                 {"item": "3", "channel_id": "2", "reply_to": "3",
                  "silent": True, "owner_dm_nonce": N},
                 settings_item([{"op": "set", "place":
                                 "channel:500000000000000001:"
                                 "701000000000000001", "key": "who.replies",
                                 "value": "when-addressed"}])]
        rc, err = self.append(items)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.outbox(), items)
        # A single object (not a list) is one item.
        rc, err = self.append({"item": "5", "channel_id": "2",
                               "reply_to": "5", "silent": True})
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.outbox()), 4)

    def test_a_mis_copied_nonce_is_refused_at_once(self):
        # A worker once wrote 23 of a reply_nonce's 32 characters; the
        # monitor refused the reply and the message waited 10 minutes.
        live = "5b5d" + "c" * 28
        with open(os.path.join(self.pkg, "item_nonces.json"), "w") as f:
            json.dump({"7": live}, f)
        reply = {"item": "7", "channel_id": "2", "reply_to": "7",
                 "text": "hi"}
        code, err = self.append([dict(reply, reply_nonce=live[:23])])
        self.assertEqual((code, self.outbox()), (1, []), err)
        self.assertIn("reply_nonce", err)
        code, err = self.append([dict(reply, reply_nonce=live)])
        self.assertEqual(code, 0, err)
        # Left to the monitor: a missing nonce (an out-of-date prompt,
        # which the monitor tells the owners about) and an owner-DM
        # nonce carried only as settings.nonce.
        with open(os.path.join(self.pkg, "owner_dm_nonces.json"), "w") as f:
            json.dump({"7": N}, f)
        only_settings = settings_item([CHANGE], item="7", reply_to="7",
                                      reply_nonce=live)
        del only_settings["owner_dm_nonce"]
        code, err = self.append([reply, only_settings])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.outbox()), 3)

    def test_bad_items_append_nothing(self):
        good = {"item": "1", "channel_id": "2", "reply_to": "1",
                "text": "fine"}
        bad = [
            {"item": "2", "channel_id": "5", "reply_to": "2",
             "content": "the reply in the wrong field"},
            {"item": "1", "channel_id": "2", "text": "hi", "_nl": {}},
            settings_item([{"op": "set", "place": "here",
                            "key": "who.replies", "value": "off"}]),
            settings_item([{"op": "set", "place": "dms",
                            "key": "who.replies", "value": "quiet"}]),
            settings_item([{"op": "undo"}, {"op": "pause",
                                            "place": "dms"}]),
            settings_item([CHANGE], reactions=["\U0001F44D"]),
            settings_item([CHANGE], nonce=None),
            settings_item([CHANGE], nonce=12345),
            settings_item([CHANGE], nonce=N.upper()),
            dict(settings_item([CHANGE]), owner_dm_nonce="cd" * 16),
        ]
        for it in bad:
            rc, _err = self.append([good, it])
            self.assertEqual(rc, 1, it)
            self.assertEqual(self.outbox(), [], it)


if __name__ == "__main__":
    unittest.main()
