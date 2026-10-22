"""Review L (PR #53, lean/modules): fold's new id normalization
({str(k): v} over servers) drops a whole server block when config.yaml
names one server both unquoted and quoted (the loader accepts and merges
the two), so a restriction set in config.yaml silently goes.

Usage: MUT_DIR=<tree> python3 tests/settingsengine/test_review_l.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake import G1, MS, OWNER, PLUGINS, SPAM, config, load  # noqa: E402

P = MS.Place
C = MS.Change
S1 = P.server(G1)

TWO_SPELLINGS = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
servers:
  {G1}:
    who:
      ignore: ["{SPAM}"]
  "{G1}":
    name: Lounge
"""


class Fold(unittest.TestCase):
    def test_fold_keeps_a_server_written_both_ways(self):
        cfg = config(self, TWO_SPELLINGS)
        sc = MS.Scope.server(G1)
        self.assertEqual(load(cfg).get("who.ignore", sc), [SPAM])
        MS.apply(cfg, [C(S1, "limits.per_day", 5)], OWNER,
                 plugins_dir=PLUGINS, role="owner")
        MS.fold(cfg, PLUGINS)
        st = load(cfg)
        self.assertEqual(st.get("limits.per_day", sc), 5)
        # The spammer the file ignored is still ignored after a fold.
        self.assertEqual(st.get("who.ignore", sc), [SPAM])


if __name__ == "__main__":
    unittest.main()
