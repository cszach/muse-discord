"""muse_watches repro from the v1.0 lean-pass audit: an ex-owner's web
watch ends instead of being checked for 30 more days.

Usage: [MUT_DIR=<tree>] python3 tests/watches/test_review_audit.py"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "muse_watches", os.path.join(MUT_DIR, "muse_watches.py"))
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)


class Audit(unittest.TestCase):
    def test_ex_owner_web_watch_ends(self):
        data = W.empty()
        data["items"]["wabcde"] = {
            "kind": "watch-web", "requester": "111", "owner": True,
            "until": 2000.0, "every": 1800, "next_check": 0, "check": None}
        gone = W.web_issue(data, now=1000.0, owners={"222"})
        self.assertEqual([i for i, _r in gone], ["wabcde"])
        self.assertEqual(data["items"], {})


if __name__ == "__main__":
    unittest.main()
