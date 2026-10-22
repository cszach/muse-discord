"""Review L (PR #53, lean/modules), two merged helpers that changed
behaviour:

1. known_spec (now muse_settings.spec_of) loads the plugin manifests for
   every key, so one unreadable plugin.yaml makes core keys such as
   who.replies unknown to a settings request.
2. new_cli_groups names an ungrouped history line "line:<n>", which is
   a valid group id (_GROUP_RE allows ':'): a real group of that name
   merges with it and loses its Undo.

Usage: [MUT_DIR=<tree>] python3 tests/settingsnl/test_review_l.py"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR  # noqa: E402

sys.path.insert(0, MUT_DIR)
import muse_settings as MS  # noqa: E402
import settings_nl as NL  # noqa: E402

PLUGINS = os.path.join(MUT_DIR, "plugins")
OWNER = "11100000000000000001"
G1 = "60000000000000000001"


def tmp(test):
    d = tempfile.mkdtemp(prefix="review-l-nl-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class KnownSpec(unittest.TestCase):
    def test_core_key_known_with_a_broken_plugin_manifest(self):
        d = tmp(self)
        os.makedirs(os.path.join(d, "broken"))
        with open(os.path.join(d, "broken", "plugin.yaml"), "w") as f:
            f.write("settings: {x: {type: nope}}\n")
        self.assertIs(NL.known_spec("who.replies", PLUGINS),
                      MS.CORE["who.replies"])
        self.assertIs(NL.known_spec("who.replies", d),
                      MS.CORE["who.replies"])


class CliGroups(unittest.TestCase):
    def test_a_group_named_like_a_line_keeps_its_undo(self):
        d = tmp(self)
        cfg = os.path.join(d, "config.yaml")
        with open(cfg, "w") as f:
            f.write(f'version: 1\nowners: ["{OWNER}"]\n'
                    f'token_from: env:MUSE_TEST_TOKEN\nservers:\n'
                    f'  "{G1}":\n    name: Lounge\n')
        state = {}
        NL.new_cli_groups(MS.load(cfg, PLUGINS), state)   # the mark
        S1 = MS.Place.server(G1)
        # An ungrouped change, then a group whose id is "line:0".
        MS.set_value(cfg, f'servers."{G1}".style.length', "brief",
                     "muse.ai", PLUGINS)
        MS.apply(cfg, [MS.Change(S1, "limits.per_day", 5)], "muse.ai",
                 group="line:0", plugins_dir=PLUGINS, role="owner")
        groups = NL.new_cli_groups(MS.load(cfg, PLUGINS), state)
        self.assertEqual(len(groups), 2, groups)
        self.assertIn(("line:0", True),
                      [(g["group"], g["undoable"]) for g in groups])


if __name__ == "__main__":
    unittest.main()
