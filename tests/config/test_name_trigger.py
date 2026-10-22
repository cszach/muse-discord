"""The bare-name trigger is gone, offline: an old config that still lists
`name` in respond_to and call_names loads, a bare name never triggers
(classify_message and trigger_kind), an @mention still does, and
load_state drops a leftover name_drop burst. Each case imports
monitor.py in a fresh subprocess with its own config.yaml (settings
resolve at import), no network. MUT_DIR = the package tree under test
(default: this checkout)."""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR as REPO, scratch  # noqa: E402

OLD_CFG = """bot:
  token_file: /nonexistent
behavior:
  respond_to: [mention, reply_to_bot, name]
  call_names: [Pip]
  active_guild_ids: ["G"]
"""
PROBE = r"""
import importlib.util, io, json, os, sys, contextlib
sys.argv = ["x", "dummy-token"]
with contextlib.redirect_stdout(io.StringIO()):
    spec = importlib.util.spec_from_file_location("monitor", sys.argv_repo)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
m.BOT_ID = m.BOT_USER_ID = "999"
m.api_get = lambda path: (_ for _ in ()).throw(AssertionError(path))
st = {"last_seen": {}, "replied_to": [], "history": {},
      "rate": {"day": "", "day_count": 0, "users": {}},
      "guild_joined": {"G": 0}}
def msg(mid, content, mention):
    return {"id": mid, "channel_id": "101", "guild_id": "G",
            "author": {"id": "U1", "username": "alice", "bot": False},
            "content": content, "timestamp": "2026-10-17T00:00:00+00:00",
            "mentions": [{"id": "999"}] if mention else []}
bare = msg("1300000000000000001", "Pip, look at this", False)
direct = msg("1300000000000000002", "hey look at this", True)
with contextlib.redirect_stdout(io.StringIO()):
    bare_kind = m.classify_message(st, bare, "S #a", "G")
    direct_kind = m.classify_message(st, direct, "S #a", "G")
# load_state drops a burst left behind by the old name trigger.
m.STATE_FILE = os.path.join(os.environ["TMPD"], "state.json")
with open(m.STATE_FILE, "w") as f:
    json.dump({"pending": {
        "101:U1": {"kind": "name_drop", "channel_id": "101"},
        "101:U2": {"kind": "mention", "channel_id": "101"}}}, f)
pending = sorted(m.load_state()["pending"])
print(json.dumps({"bare": bare_kind, "trigger_kind": m.trigger_kind(bare),
                  "direct": direct_kind, "pending": pending}))
"""


def run(cfg_text):
    d = scratch("muse-nametrigger-")
    cfg = os.path.join(d, "config.yaml")
    with open(cfg, "w") as f:
        f.write(cfg_text)
    env = dict(os.environ, MUSE_DISCORD_CONFIG=cfg, TMPD=d)
    code = "import sys; sys.argv_repo = %r\n" % os.path.join(
        REPO, "monitor.py") + PROBE
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class OldConfig(unittest.TestCase):
    def test_old_config_bare_name_never_triggers(self):
        r = run(OLD_CFG)
        self.assertIsNone(r["bare"])
        self.assertIsNone(r["trigger_kind"])
        self.assertEqual(r["direct"], "mention")
        self.assertEqual(r["pending"], ["101:U2"])


if __name__ == "__main__":
    unittest.main()
