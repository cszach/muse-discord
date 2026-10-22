"""Review M (lean/core): the first-run ownerless-server check.

Usage: [MUT_DIR=<tree>] python3 tests/onboarding/test_review_m.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
import common  # noqa: E402,F401 (sets up sys.path for MUT_DIR)
from settings_harness import (G1, G2, CH_G2, OWNER, U1, HTTPErr,  # noqa
                              gmsg, load_monitor, quiet)

BASE = f'version: 1\nowners: ["{OWNER}"]\n'


class FirstRun(unittest.TestCase):
    def test_lost_state_and_failed_boot_discovery_keep_the_gate(self):
        """state.json is gone and the boot discovery failed (a 429, the
        network): sync_guilds returns early. The gateway's READY then
        sends GUILD_CREATE for every server; a stranger's mention in one
        no owner is in must still get no answer."""
        mod = load_monitor(BASE)
        mod.api.members[(G1, OWNER)] = []
        with quiet():
            st = mod.load_state()                  # no state.json
            mod.sync_guilds(st, {}, {})            # boot discovery failed
            for gid in (G1, G2):                   # READY's GUILD_CREATEs
                mod.on_guild_event(st, {"op": "create", "id": gid})
        stranger = gmsg(U1, mention=True, channel=CH_G2, guild=G2)
        self.assertIsNone(mod.trigger_kind(stranger, G2))

    def test_first_run_checks_stop_at_a_rate_limit(self):
        """The first run checks every server at once (every=True); after a
        429 it must stop asking (the rest stay gated and are retried),
        not send one more request per server and owner."""
        mod = load_monitor(BASE)
        guilds = {str(700000000000000000 + i): {"name": f"g{i}"}
                  for i in range(30)}
        asked = []

        def get(path):
            if "/members/" in path:
                asked.append(path)
                e = HTTPErr(429)
                mod._note_rate_limit(e)
                raise e
            raise HTTPErr(404)
        mod.api_get = get
        with quiet():
            st = mod.load_state()
            mod.sync_guilds(st, guilds, {})
        self.assertLessEqual(len(asked), 1, f"{len(asked)} member GETs "
                             f"after a 429")


if __name__ == "__main__":
    unittest.main()
