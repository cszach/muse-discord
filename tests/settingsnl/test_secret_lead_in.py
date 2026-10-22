"""A settings request whose lead-in carries a secret is dropped at once,
like any reply that does: never retried.

Usage: [MUT_DIR=<tree>] python3 tests/settingsnl/test_secret_lead_in.py"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_settingsnl as tn  # noqa: E402

HEAD = base64.urlsafe_b64encode(b"111000000000000099").decode().rstrip("=")
TOKEN = f"{HEAD}.GxYz12.a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"


class SecretLeadIn(tn.Base):
    def test_dropped_not_retried(self):
        self.mod.TOKEN = TOKEN
        m = self.owner_dm()
        self.outbox(self.action(m, [tn.ch("set", tn.GEN, "who.replies",
                                          "off")], text=f"ok {TOKEN}"))
        self.assertEqual(self.sends(tn.DM), [])
        with open(self.mod.OUTBOX_FILE) as f:
            self.assertEqual(json.load(f), [])       # not kept to retry
        self.assertEqual(self.ledger(m["id"])["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
