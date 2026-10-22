"""The wake hook (hooks/mention-watch.sh), offline: item leases. A wake
hands out only the items no worker holds, an expired lease re-wakes its
item, items already answered (in reply_outbox.json, or through a burst
fragment in reply_outbox.processing.json) are never handed out again,
the persona file is passed along (null when missing), and a corrupt
outbox is tolerated. Runs the hook with bash and jq against stubbed
hatch hook runtime functions (_lib/hook_runtime.sh).
MUT_DIR = the package tree under test (default: this checkout)."""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import HOOK_RUNTIME, MUT_DIR, scratch  # noqa: E402

HOOK = os.path.join(MUT_DIR, "hooks", "mention-watch.sh")
PERSONA = 'You are Pip.\nBe "nice".\n'


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class MentionWatchLeases(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-hook-")
        self.pkg = os.path.join(self.d, "pkg")
        os.makedirs(self.pkg)
        self.state = os.path.join(self.d, "hookstate.json")
        self.env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                        HOOK_STATE=self.state, MUSE_DISCORD_DIR=self.pkg,
                        MUSE_DISCORD_CONFIG=self.p("config.yaml"))

    def p(self, name):
        return os.path.join(self.pkg, name)

    def put(self, name, obj):
        with open(self.p(name), "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))

    def run_hook(self):
        r = subprocess.run(["bash", HOOK], env=self.env, capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, r.stderr

    def wake_ids(self, out):
        """The message ids a WAKE hands out (None for SILENT)."""
        lines = out.splitlines()
        if not lines or not lines[0].startswith("WAKE:"):
            return None
        payload = json.loads(lines[1])
        self.assertEqual(payload["count"], len(payload["items"]))
        self.last_payload = payload
        return [it["message_id"] for it in payload["items"]]

    def age_leases(self, secs=700):
        with open(self.state) as f:
            st = json.load(f)
        st["woken"] = {k: v - secs for k, v in st["woken"].items()}
        with open(self.state, "w") as f:
            json.dump(st, f)

    def test_lease_lifecycle(self):
        out, _ = self.run_hook()   # no queue file
        self.assertIsNone(self.wake_ids(out), out)
        self.put("reply_queue.json", [])
        out, _ = self.run_hook()
        self.assertIsNone(self.wake_ids(out), out)

        self.put("config.yaml", 'persona:\n  file: "mypersona.md"\n')
        self.put("mypersona.md", PERSONA)
        self.put("reply_queue.json", [
            {"message_id": "1", "channel_id": "9", "content": "hi"},
            {"message_id": "2", "channel_id": "9", "burst": True,
             "burst_fragments": [{"message_id": "3"}]}])
        out, _ = self.run_hook()
        self.assertEqual(self.wake_ids(out), ["1", "2"], out)
        self.assertEqual(self.last_payload["persona"], PERSONA)
        self.assertEqual(self.last_payload["items"][0]["content"], "hi")

        # Both leases are fresh: a second wake is held.
        out, _ = self.run_hook()
        self.assertIsNone(self.wake_ids(out), out)

        # A new item wakes alone.
        self.put("reply_queue.json", [
            {"message_id": "1", "channel_id": "9"},
            {"message_id": "2", "channel_id": "9", "burst": True,
             "burst_fragments": [{"message_id": "3"}]},
            {"message_id": "4", "channel_id": "9"}])
        out, _ = self.run_hook()
        self.assertEqual(self.wake_ids(out), ["4"], out)

        # Leases expire; 4 is answered in the outbox and 2 through its
        # fragment in the processing file: only 1 is handed out again.
        self.put("reply_outbox.json",
                 [{"item": "4", "channel_id": "9", "text": "hello"}])
        self.put("reply_outbox.processing.json",
                 [{"channel_id": "9", "reply_to": "3", "text": "x"}])
        self.age_leases()
        out, _ = self.run_hook()
        self.assertEqual(self.wake_ids(out), ["1"], out)
        with open(self.state) as f:
            self.assertIn("1", json.load(f)["woken"])

        # The persona file disappears: persona is null.
        os.remove(self.p("mypersona.md"))
        self.age_leases()
        out, _ = self.run_hook()
        self.assertEqual(self.wake_ids(out), ["1"], out)
        self.assertIsNone(self.last_payload["persona"])

        # A corrupt outbox is tolerated (4 counts as unanswered again).
        self.put("reply_outbox.json", "garbage{")
        self.age_leases()
        out, _ = self.run_hook()
        self.assertEqual(self.wake_ids(out), ["1", "4"], out)


if __name__ == "__main__":
    unittest.main()
