"""Offline tests for e2e/scenario.py and the shipped scenarios: they
load, their configs load in the real settings code (a config the
monitor refused would make every scenario a flake, which passes), and
a broken scenario fails at load time instead of mid-run.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import ast
import copy
import os
import re
import shutil
import sys
import tempfile
import types
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)
sys.path.insert(0, REPO)

import muse_settings  # noqa: E402
import scenario  # noqa: E402
import sut  # noqa: E402

IDS = {"SUT": "111", "A": "222", "B": "333", "SUT_NAME": "muse-test"}
GUILD, A, B, CHAN = "100", "200", "300", "400"


def base():
    """A minimal valid tier-1 scenario."""
    return {
        "id": "sample", "tier": 1, "title": "Sample",
        "steps": [
            {"send": {"as": "a", "text": "<@SUT> hi", "label": "q1"}},
            {"wait_for": {"sut_messages": 1, "timeout": 30}},
        ],
        "expect": {"settle": 5, "checks": [{"sut_reply_count": {"eq": 1}},
                                           {"replies_to": "q1"}]},
    }


def injection_patterns():
    """monitor.py's INJECTION_PATTERNS, read without importing monitor
    (its import reads config and the token)."""
    with open(os.path.join(REPO, "monitor.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "INJECTION_PATTERNS"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("INJECTION_PATTERNS not found in monitor.py")


class Shipped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all = scenario.load_all(scenario.SCENARIOS_DIR)

    def test_all_load(self):
        files = sorted(f[:-5] for f in os.listdir(scenario.SCENARIOS_DIR)
                       if f.endswith(".yaml"))
        self.assertEqual(sorted(s["id"] for s in self.all), files)
        self.assertEqual({s["tier"] for s in self.all}, {1, 2})

    def test_every_scenario_config_loads(self):
        """The config.yaml sut.SUT writes for each scenario passes the
        real settings loader with the harness's fixed fields."""
        tmp = tempfile.mkdtemp(prefix="e2e-cfg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stand_in = types.SimpleNamespace(
            token_file=os.path.join(tmp, "tok"),
            persona_file=os.path.join(tmp, "persona.md"))
        for path in (stand_in.token_file, stand_in.persona_file):
            with open(path, "w") as f:
                f.write("x\n")
        import yaml
        for sc in self.all:
            with self.subTest(sc["id"]):
                cfg = sut.SUT.effective_config(stand_in, sut.scenario_config(
                    sut.base_config(GUILD, A, B), sc, A, B,
                    channel_id=CHAN))
                path = os.path.join(tmp, "config.yaml")
                with open(path, "w") as f:
                    yaml.safe_dump(cfg, f)
                st = muse_settings.load(path, os.path.join(REPO, "plugins"))
                self.assertEqual(st.owners, [A])
                self.assertFalse(st.updates_notify)
                self.assertFalse(st.presence_enabled)
                self.assertEqual(st.guild_ids(), [GUILD])
                self.assertEqual(st.raw["testing"]["treat_as_human"],
                                 [A] if sc.get("b_is_bot") else [A, B])

    def test_injection_text_only_where_quarantine_is_expected(self):
        """A text that trips the monitor's injection filter would be
        quarantined silently, so a scenario about something else would
        test nothing; and a quarantine check whose text no longer
        matches tests nothing either."""
        pats = injection_patterns()
        for sc in self.all:
            want = {c["quarantined"] for c in sc["expect"]["checks"]
                    if "quarantined" in c}
            for step in sc["steps"]:
                send = step.get("send") or {}
                lines = [send.get("text") or ""] + list(
                    (step.get("converse") or {}).get("lines") or [])
                hit = any(re.search(p, scenario.render(t, IDS).lower())
                          for t in lines for p in pats)
                self.assertEqual(hit, send.get("label") in want,
                                 (sc["id"], send.get("label")))


class Invalid(unittest.TestCase):
    def test_broken_scenarios_are_rejected_naming_the_field(self):
        scenario.validate(base())   # each case below breaks one thing
        def make(change):
            sc = copy.deepcopy(base())
            change(sc)
            return sc

        def step(s):
            return lambda sc: sc["steps"].append(s)

        def check(c):
            return lambda sc: sc["expect"]["checks"].append(c)

        cases = [
            (lambda sc: sc.update(chekcs=[]), "unknown field(s) chekcs"),
            (lambda sc: sc.update(tier=2), "judge.rubric"),
            (lambda sc: sc["expect"].update(checks=[]), "at least one"),
            (step({"sleep": 1}), "unknown step 'sleep'"),
            (step({"send": {"as": "c", "text": "x"}}), "steps[2].send.as"),
            (step({"send": {"as": "a", "text": "<@BOT> x"}}),
             "unknown mention placeholder"),
            (step({"send": {"as": "a", "text": "x", "reply_to": "q9"}}),
             "unknown label 'q9'"),
            (step({"thread": {"from": "q1", "name": "t", "label": "q1"}}),
             "defined twice"),
            (step({"action": "reboot"}), "unknown action"),
            (step({"settings": {"set": "who.replys", "value": "off",
                                "scope": "server"}}),
             "unknown setting 'who.replys'"),
            (step({"settings": {"set": "limits.per_day", "value": 1,
                                "scope": "channel"}}),
             "cannot be set at channel scope"),
            (step({"settings": {"set": "who.replies", "value": "sometimes",
                                "scope": "channel"}}), "not a valid value"),
            (check({"bogus": 1}), "unknown check 'bogus'"),
            (check({"no_sut_reply_to": "q2"}), "unknown label 'q2'"),
            (check({"content_matches": {"label": "q1", "pattern": "("}}),
             "invalid regular expression"),
            (check({"converse_turns": {"max": 1}}), "needs a converse"),
            (lambda sc: sc.update(config={"servers": {"{{GUILD}}": {}}}),
             "{{GUILD_ID}}"),
        ]
        for change, frag in cases:
            with self.subTest(frag):
                with self.assertRaises(scenario.ScenarioError) as cm:
                    scenario.validate(make(change))
                self.assertIn(frag, str(cm.exception))
        # A reply must stay in its own channel or thread.
        sc = base()
        sc["steps"] += [
            {"thread": {"from": "q1", "name": "t", "label": "t1"}},
            {"send": {"as": "b", "in": "t1", "text": "x",
                      "reply_to": "q1"}}]
        with self.assertRaises(scenario.ScenarioError):
            scenario.validate(sc)

    def test_load_all_names_the_file(self):
        tmp = tempfile.mkdtemp(prefix="e2e-scen-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        import yaml
        with open(os.path.join(tmp, "sample.yaml"), "w") as f:
            yaml.safe_dump(base(), f)
        with open(os.path.join(tmp, "other.yaml"), "w") as f:
            yaml.safe_dump(base(), f)  # id 'sample' in other.yaml
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.load_all(tmp)
        self.assertIn("other.yaml", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
