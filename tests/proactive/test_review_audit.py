"""Proactive repros from the v1.0 lean-pass audit: a person who blocked
the bot is not DMed again and again, reaction lookups stay few, and
past TARGETS_MAX nobody is left out for good.

Usage: [MUT_DIR=<tree>] python3 tests/proactive/test_review_audit.py"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_proactive as tp  # noqa: E402
from settings_harness import HTTPErr, U1, sf  # noqa: E402

HOUR = 3600


def uid(i):
    return str(222000000000000100 + i)


def dmc(i):
    return str(400000000000000100 + i)


class Audit(tp.Base):
    def people(self, n, age):
        for i in range(n):
            self.m.DM_CHANNELS[dmc(i)] = "p%d" % i
            self.dmed(uid(i), dmc(i), age=age)

    def test_blocked_person_is_not_dmed_again(self):
        self.dmed(U1, tp.DM_U1, age=3 * HOUR)
        real = self.m.api_post

        def post(path, data):
            if path == f"/channels/{tp.DM_U1}/messages":
                self.m.api.posts.append((path, data))
                raise HTTPErr(403)
            return real(path, data)
        self.m.api_post = post
        for k in range(4):
            slot = self.heartbeat(side="private") if k == 0 else \
                self.next_slot(side="private", hours=2)
            if slot is not None and tp.DM_U1 in self.targets(slot):
                self.outbox(self.post(slot, tp.DM_U1))
        self.assertEqual(len(self.sends(tp.DM_U1)), 1)

    def test_reaction_lookups_are_few(self):
        self.people(20, 3 * 24 * HOUR)
        for i in range(20):    # unanswered, 3 parts each
            self.m.PRO.data()["sent"].append({
                "at": self.now - 2 * 24 * HOUR, "slot": "x", "type": "dm",
                "channel_id": dmc(i), "user_id": uid(i),
                "msgs": [sf(2 * 24 * HOUR) for _ in range(3)]})
        calls = []
        real = self.m.api_get
        self.m.api_get = lambda path: calls.append(path) or real(path)
        for k in range(3):
            self.next_slot(side="private", hours=1) if k else \
                self.heartbeat(side="private")
        self.assertLessEqual(len([c for c in calls if "/messages/" in c]),
                             20)

    def test_everyone_gets_a_turn_past_targets_max(self):
        self.people(40, 3 * HOUR)
        self.m.PRO.rng = random.Random(1)   # no flakes
        seen = set()
        for k in range(10):
            slot = self.heartbeat(side="private") if k == 0 else \
                self.next_slot(side="private", hours=2)
            if slot is not None:
                seen |= {t.get("user_id")
                         for t in slot["proactive"]["targets"]}
        self.assertTrue({uid(i) for i in range(40)} <= seen)


if __name__ == "__main__":
    unittest.main()
