"""Offline tests for the two e2e workers and the tier-2 judge.

- fake_worker.py (tier 1): its outbox items pass the package's own
  bin/outbox-append.py (every directive the tier-1 scenarios use), it
  answers each item once, and it records what the monitor queued (the
  queue_item check's source).
- claude_worker.py (tier 2): it hands out what hooks/mention-watch.sh
  hands out (the payload projection, owner DMs alone with their
  owner_dm_nonce), answers each item once, and lets the worker run only
  the two package tools.
- judge.py: a verdict is read only from a real verdict; anything else
  is an error, never a pass.

No network and no real Claude: CLAUDE_BIN points at small fake
`claude` scripts. Run from the repo root:

    python3 -m unittest discover -s e2e/tests -t .
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)

import claude_worker  # noqa: E402
import fake_worker  # noqa: E402
import judge  # noqa: E402
import scenario  # noqa: E402

A_ID, GID, CID = "20", "10", "77"
NONCE = "ab" * 16


def make_pkg(prefix):
    """A package dir with the real outbox tool and what it validates
    settings actions with."""
    pkg = tempfile.mkdtemp(prefix=prefix)
    os.makedirs(os.path.join(pkg, "bin"))
    shutil.copy(os.path.join(REPO, "bin", "outbox-append.py"),
                os.path.join(pkg, "bin"))
    for f in ("muse_settings.py", "settings_nl.py", "muse_watches.py",
              "worker-prompt.template.md"):
        shutil.copy(os.path.join(REPO, f), pkg)
    shutil.copytree(os.path.join(REPO, "plugins"),
                    os.path.join(pkg, "plugins"))
    with open(os.path.join(pkg, "config.yaml"), "w") as f:
        f.write(f'owners: ["{A_ID}"]\n')
    return pkg


def write_json(pkg, name, data):
    with open(os.path.join(pkg, name), "w") as f:
        json.dump(data, f)


def read_json(pkg, name, default=None):
    return fake_worker.load_json(os.path.join(pkg, name), default)


def queue_item(mid, content, fragments=(), kind="mention", **extra):
    it = {"message_id": mid, "channel_id": CID, "channel_name": "#t",
          "guild_id": GID, "kind": kind, "author": "a", "author_id": A_ID,
          "content": "\n".join([content] + [c for _, c in fragments]),
          "burst": bool(fragments),
          "burst_fragments": [{"message_id": i, "content": c}
                              for i, c in fragments]}
    it.update(extra)
    return it


# ---------------------------------------------------------------------
# fake_worker.py

class FakeWorker(unittest.TestCase):
    def setUp(self):
        self.pkg = make_pkg("e2e-fw-")
        self.addCleanup(shutil.rmtree, self.pkg, ignore_errors=True)

    def test_directives_end_to_end(self):
        """The fake worker answers every item once, through the real
        outbox tool, including every directive text the tier-1 scenarios
        send."""
        items = [
            queue_item("101", "<@900> hello"),
            queue_item("102", "first", [("103", "second"), ("104", "third")]),
            queue_item("105", "shh [[silent]]"),
            queue_item("106", "[[long]] [[mention_a]] [[everyone]] go"),
            queue_item("107", "hi", [("108", "[[silent]]")]),
            queue_item("110", "[[say:the Blue Pelican]] hi"),
            queue_item("112", "plain", owner_dm_nonce=NONCE),
            queue_item("113", "[[remind:90 the oven]] [[watch:pizza]] hi"),
            dict(queue_item("114", ""), kind="reminder",
                 reminder={"items": [{"id": "rabcde"}]}),
        ]
        n = 200
        for sc in scenario.load_all(scenario.SCENARIOS_DIR, 1):
            for step in sc["steps"]:
                text = (step.get("send") or {}).get("text") or ""
                if "[[" in text:
                    n += 1
                    items.append(queue_item(str(n), text, sc=sc["id"]))
        write_json(self.pkg, "reply_queue.json", items)
        now = [1000.0]
        w = fake_worker.Worker(self.pkg, clock=lambda: now[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            w.tick()
            now[0] += fake_worker.SLOW_SECS   # [[slow]] drafts come due
            w.tick()
            w.tick()                          # nothing answered twice
        out = out.getvalue()
        outbox = read_json(self.pkg, "reply_outbox.json", [])
        got = {o["item"]: o for o in outbox}
        self.assertEqual(len(outbox), len(items), out)
        self.assertEqual(set(got), {i["message_id"] for i in items}, out)
        self.assertEqual((got["101"]["text"], got["101"]["reply_to"]),
                         ("ack 1", "101"))
        self.assertEqual((got["102"]["text"], got["102"]["reply_to"]),
                         ("ack 3", "104"))   # under the last fragment
        self.assertTrue(got["105"]["silent"])
        self.assertNotIn("text", got["105"])
        text = got["106"]["text"]
        self.assertEqual(len(text), fake_worker.LONG_CHARS)
        for frag in (f"<@{A_ID}>", "@everyone", f"<@&{GID}>"):
            self.assertIn(frag, text[:100])  # survives the 1800 cap
        self.assertTrue(got["107"]["silent"])  # directive in a fragment
        self.assertEqual(got["110"]["text"], "ack 1 the Blue Pelican")
        self.assertEqual(got["112"]["owner_dm_nonce"], NONCE)
        self.assertEqual(got["113"]["schedule"]["changes"], [
            {"op": "add", "kind": "remind", "after": 90,
             "note": "the oven"},
            {"op": "add", "kind": "watch", "note": "pizza",
             "match": {"words": ["pizza"], "in": "here"}}])
        self.assertEqual(got["114"]["text"], "reminder 1")
        self.assertNotIn("owner_dm_nonce", got["101"])

    def test_slow_never_twice_and_records(self):
        fu = queue_item("201", "take your time [[slow]]", follow_up=True,
                        from_bot=False, settings={"speaker": "owner"})
        write_json(self.pkg, "reply_queue.json",
                   [fu, queue_item("202", "x", [("203", "y")])])
        now = [1000.0]
        w = fake_worker.Worker(self.pkg, clock=lambda: now[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(w.tick(), 1)          # 202 now, 201 later
            now[0] += fake_worker.SLOW_SECS - 1
            self.assertEqual(w.tick(), 0)
            now[0] += 1
            self.assertEqual(w.tick(), 1)
            self.assertEqual(w.tick(), 0)          # never twice
            # A restarted worker skips items the outbox already answers.
            self.assertEqual(fake_worker.Worker(
                self.pkg, clock=lambda: now[0]).tick(), 0)
        self.assertEqual([o["item"] for o in read_json(
            self.pkg, "reply_outbox.json")], ["202", "201"])
        recs = read_json(self.pkg, fake_worker.WORKER_ITEMS)
        self.assertEqual([r["message_id"] for r in recs], ["201", "202"])
        self.assertIs(recs[0]["follow_up"], True)
        self.assertEqual(recs[0]["settings"]["speaker"], "owner")
        self.assertEqual(recs[1]["fragment_ids"], ["203"])


# ---------------------------------------------------------------------
# claude_worker.py

FAKE_HEAD = """\
#!%s
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
prompt = sys.stdin.read()
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "prompt": prompt}) + "\\n")
""" % sys.executable

FAKES = {
    # A worker: answers every payload item through the append tool,
    # under the last burst fragment, one append per item, echoing the
    # item's owner_dm_nonce, then ends with the one line `done`.
    "append": FAKE_HEAD + textwrap.dedent("""\
        import subprocess
        payload = json.loads(prompt.split("```json\\n", 1)[1]
                             .split("\\n```", 1)[0])
        for it in payload["items"]:
            frags = it.get("burst_fragments") or []
            rt = frags[-1]["message_id"] if frags else it["message_id"]
            out = {"item": it["message_id"], "channel_id": it["channel_id"],
                   "reply_to": rt, "text": "ahoy " + it["author"]}
            if it.get("owner_dm_nonce"):
                out["owner_dm_nonce"] = it["owner_dm_nonce"]
            subprocess.run([sys.executable, "bin/outbox-append.py"],
                           input=json.dumps(out), text=True)
        print(json.dumps({"type": "result", "is_error": False,
                          "num_turns": 3, "result": "done"}))
        """),
    "hang": FAKE_HEAD + "import time\ntime.sleep(300)\n",
    "garbage": FAKE_HEAD + 'print("lol not json {verdict: maybe")\n',
    "verdict": FAKE_HEAD + textwrap.dedent("""\
        res = ('Here it is:\\n```json\\n{"verdict": "fail", "reason": '
               '"[2] ignored the question"}\\n```')
        print(json.dumps({"type": "result", "is_error": False,
                          "result": res}))
        """),
    "verdict_plain": FAKE_HEAD
    + 'print(\'{"verdict": "PASS", "reason": "nautical and brief"}\')\n',
    "is_error": FAKE_HEAD
    + 'print(json.dumps({"is_error": True, "result": "auth failed"}))\n',
    "exit2": FAKE_HEAD + 'sys.stderr.write("bad flag\\n"); sys.exit(2)\n',
}

QUEUE = [
    {"message_id": "1001", "channel_id": "500", "channel_name": "T #e2e",
     "guild_id": "400", "kind": "mention", "author": "driver-a",
     "author_id": "11", "content": "<@99> hello", "media": [],
     "history": [], "owner_command": False, "queued_at": 1},
    {"message_id": "1002", "channel_id": "500", "channel_name": "T #e2e",
     "guild_id": "400", "kind": "mention", "author": "driver-b",
     "author_id": "12", "content": "one\ntwo", "burst": True,
     "burst_fragments": [
         {"message_id": "1002", "content": "one", "timestamp": "t"},
         {"message_id": "1003", "content": "two", "timestamp": "t"}]},
]
OWNER_DM = {"message_id": "2001", "channel_id": "600", "kind": "dm",
            "author": "owner", "author_id": "11", "content": "be brief",
            "settings": {"where": "dm", "speaker": "owner"}}


class ClaudeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-claude-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pkg = make_pkg("e2e-claude-pkg-")
        self.addCleanup(shutil.rmtree, self.pkg, ignore_errors=True)
        os.makedirs(os.path.join(self.pkg, "media"))
        write_json(self.pkg, "reply_queue.json", QUEUE)
        self.fakes = os.path.join(self.tmp, "fakes")
        os.makedirs(self.fakes)
        old = os.environ.get("CLAUDE_BIN")
        self.addCleanup(lambda: os.environ.pop("CLAUDE_BIN", None)
                        if old is None
                        else os.environ.__setitem__("CLAUDE_BIN", old))

    def use_fake(self, name):
        path = os.path.join(self.fakes, "claude-" + name)
        with open(path, "w") as f:
            f.write(FAKES[name])
        os.chmod(path, 0o755)
        os.environ["CLAUDE_BIN"] = path

    def calls(self):
        path = os.path.join(self.fakes, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]

    def payloads(self):
        return [json.loads(c["prompt"].split("```json\n", 1)[1]
                           .split("\n```", 1)[0]) for c in self.calls()]


class ClaudeWorker(ClaudeBase):
    def test_mirrors_the_hook(self):
        """PAYLOAD_FIELDS, LEASE_SECS and the owner-DM rules follow
        hooks/mention-watch.sh."""
        with open(os.path.join(REPO, "hooks", "mention-watch.sh")) as f:
            hook = f.read()
        m = re.search(r"items: \[\.\[\] \| \{([^}]*)\}", hook)
        self.assertIsNotNone(m, "the hook's payload projection moved")
        self.assertEqual(tuple(x.strip() for x in m.group(1).split(",")),
                         claude_worker.PAYLOAD_FIELDS)
        self.assertEqual(int(re.search(r"^LEASE_SECS=(\d+)$", hook,
                                       re.M).group(1)),
                         claude_worker.LEASE_SECS)
        for rule in ("owner_dm_nonces.json", "{owner_dm_nonce: $nonces",
                     '.kind == "dm"', '.settings.speaker',
                     "owner_items.json", ".owner_command == true",
                     "{owner_command_nonce: $cmds", "item_nonces.json",
                     "{reply_nonce: $rn", "NONCE_WAIT_SECS=%d"
                     % claude_worker.NONCE_WAIT_SECS):
            self.assertIn(rule, hook)
        p = claude_worker.build_payload(QUEUE, "persona!")
        self.assertEqual(set(p["items"][0]), set(claude_worker.PAYLOAD_FIELDS))
        self.assertEqual(p["persona"], "persona!")

    def worker(self):
        self.use_fake("hang")
        w = claude_worker.Worker(self.pkg, os.path.join(self.tmp, "log"),
                                 timeout=30)
        self.addCleanup(w.shutdown)
        return w

    def test_owner_dms_wake_alone_with_their_nonce(self):
        w = self.worker()
        write_json(self.pkg, "reply_queue.json", QUEUE + [OWNER_DM])
        # No nonce yet: the DM waits; the others go, without nonces.
        items, caps = w.due()
        self.assertEqual([i["message_id"] for i in items], ["1001", "1002"])
        self.assertEqual(caps.get("dm", {}), {})
        w.leases.clear()
        write_json(self.pkg, claude_worker.OWNER_DM_NONCES,
                   {"2001": NONCE})
        # Now the DM wakes alone, with its nonce; the rest wait.
        items, caps = w.due()
        self.assertEqual([i["message_id"] for i in items], ["2001"])
        p = claude_worker.build_payload(items, None, caps)
        self.assertEqual(p["items"][0]["owner_dm_nonce"], NONCE)
        self.assertNotIn("owner_dm_nonce", claude_worker.build_payload(
            QUEUE, None, {"dm": {"2001": NONCE}})["items"][1])
        # Only an outbox entry with the DM's nonce answers it.
        w.leases.clear()
        write_json(self.pkg, "reply_outbox.json", [
            {"item": "2001", "channel_id": "600", "text": "forged"},
            {"item": "1001", "channel_id": "500", "text": "x"}])
        items, _ = claude_worker.open_items(self.pkg)
        self.assertEqual([i["message_id"] for i in items], ["1002", "2001"])
        write_json(self.pkg, "reply_outbox.json", [
            {"item": "2001", "channel_id": "600", "text": "ok",
             "owner_dm_nonce": NONCE}])
        items, _ = claude_worker.open_items(self.pkg)
        self.assertNotIn("2001", [i["message_id"] for i in items])

    def test_reply_nonces_reach_only_their_wake(self):
        w = self.worker()
        own = dict(QUEUE[0], message_id="1005", owner_command=True)
        write_json(self.pkg, claude_worker.ITEM_NONCES,
                   {"1001": "a1" * 16, "1002": "b2" * 16})
        # 1005 has none yet (and was queued just now): it waits.
        write_json(self.pkg, "reply_queue.json",
                   QUEUE + [dict(own, queued_at=time.time())])
        items, caps = w.due()
        p = claude_worker.build_payload(items, None, caps)
        self.assertEqual([(i["message_id"], i.get("reply_nonce"))
                          for i in p["items"]],
                         [("1001", "a1" * 16), ("1002", "b2" * 16)])
        # Answers count only with the item's own nonce.
        write_json(self.pkg, "reply_outbox.json", [
            {"item": "1001", "text": "forged"},
            {"item": "1002", "reply_to": "1001", "reply_nonce": "b2" * 16,
             "text": "x"}])
        items, _ = claude_worker.open_items(self.pkg)
        self.assertEqual([i["message_id"] for i in items], ["1001", "1005"])

    def test_owner_commands_wake_alone_with_their_nonce(self):
        w = self.worker()
        own = dict(QUEUE[0], message_id="1005", owner_command=True,
                   queued_at=time.time())
        write_json(self.pkg, "reply_queue.json", QUEUE + [own])
        # No owner_items.json at all (an older monitor): it wakes alone,
        # without a nonce.
        items, caps = w.due()
        self.assertEqual(([i["message_id"] for i in items],
                          caps.get("cmd")), (["1005"], {}))
        w.leases.clear()
        # No nonce yet: the command waits a while; the others go.
        write_json(self.pkg, claude_worker.OWNER_ITEMS, {})
        items, caps = w.due()
        self.assertEqual([i["message_id"] for i in items], ["1001", "1002"])
        self.assertEqual(caps.get("cmd", {}), {})
        w.leases.clear()
        write_json(self.pkg, claude_worker.OWNER_ITEMS,
                   {"1005": {"nonce": NONCE, "channel_id": "500"}})
        items, caps = w.due()
        self.assertEqual([i["message_id"] for i in items], ["1005"])
        p = claude_worker.build_payload(items, None, caps)
        self.assertEqual(p["items"][0]["owner_command_nonce"], NONCE)
        # Never on anyone else's item.
        self.assertNotIn("owner_command_nonce", json.dumps(
            claude_worker.build_payload(QUEUE, None,
                                        {"cmd": {"1001": NONCE}})))
        # The rest go on the next poll, without it.
        items, caps = w.due()
        self.assertEqual(([i["message_id"] for i in items],
                          caps.get("cmd", {})), (["1001", "1002"], {}))
        # Not for good: a minute after it was queued, a command whose
        # nonce never came goes out without it.
        w.leases.clear()
        write_json(self.pkg, claude_worker.OWNER_ITEMS, {})
        write_json(self.pkg, "reply_queue.json",
                   [dict(own, queued_at=time.time() - 61)])
        items, caps = w.due()
        self.assertEqual(([i["message_id"] for i in items],
                          caps.get("cmd")), (["1005"], {}))

    def test_owner_server_items_wake_alone(self):
        w = self.worker()
        own = dict(QUEUE[0], message_id="1005", owner_command=True)
        plain = dict(QUEUE[0], message_id="1006",
                     settings={"speaker": "owner"})
        write_json(self.pkg, "reply_queue.json", QUEUE + [own, plain])
        write_json(self.pkg, claude_worker.OWNER_ITEMS,
                   {"1005": {"nonce": NONCE}})
        seen = []
        for _ in range(3):
            items, caps = w.due()
            p = claude_worker.build_payload(items, None, caps)
            seen.append([(i["message_id"], i.get("owner_command_nonce"))
                         for i in p["items"]])
        self.assertEqual(seen, [[("1005", NONCE)], [("1006", None)],
                                [("1001", None), ("1002", None)]])

    def test_answers_each_item_once(self):
        self.use_fake("append")
        log = os.path.join(self.tmp, "worker.log")
        w = claude_worker.Worker(self.pkg, log, timeout=30)
        self.assertIsNotNone(w.poll())
        w.wait(30)
        outbox = read_json(self.pkg, "reply_outbox.json")
        self.assertEqual(sorted((o["item"], o["reply_to"]) for o in outbox),
                         [("1001", "1001"), ("1002", "1003")])
        # Answered: no second wake, even after the leases run out, nor
        # after the monitor took the outbox for sending.
        w.lease = 0
        self.assertIsNone(w.poll())
        os.rename(os.path.join(self.pkg, "reply_outbox.json"),
                  os.path.join(self.pkg, "spare.json"))
        self.assertIsNone(w.poll())
        (call,) = self.calls()
        self.assertEqual(os.path.realpath(call["cwd"]),
                         os.path.realpath(self.pkg))
        self.assertIn("Beacon", call["prompt"])  # the test persona
        self.assertNotIn("owner_dm_nonce", call["prompt"].split(
            "Wake: muse-discord")[1])
        with open(log) as f:
            self.assertIn("wake 1: done exit=0", f.read())

    def test_tool_allowlist(self):
        pkg = self.pkg
        allowed = [
            "python3 %s/bin/outbox-append.py <<'EOF'\n[{\"a\": \"$(x)\"}]\n"
            "EOF" % pkg,
            "python3 %s/bin/outbox-append.py items.json" % pkg,
            "python3 %s/bin/discord-api.py react 1 2 '🌊'" % pkg,
            "python3 %s/bin/discord-api.py --for 2001 --nonce %s post 600 "
            "hi" % (pkg, NONCE),
        ]
        denied = [
            "ls /",
            "python3 %s/bin/outbox-append.py <<EOF\n[\"$(id)\"]\nEOF" % pkg,
            "python3 %s/bin/outbox-append.py <<'EOF'\n[]\nEOF\nrm -rf /"
            % pkg,
            "cd %s && python3 bin/outbox-append.py x.json" % pkg,
            "python3 %s/bin/discord-api.py post 1 \"$(cat config.yaml)\""
            % pkg,
            "python3 %s/bin/discord-api.py post 1 hi > /tmp/x" % pkg,
            "python3 /elsewhere/bin/outbox-append.py x.json",
        ]
        for cmd in allowed:
            self.assertTrue(claude_worker.bash_allowed(pkg, cmd)[0], cmd)
        for cmd in denied:
            self.assertFalse(claude_worker.bash_allowed(pkg, cmd)[0], cmd)
        check = claude_worker.check_tool
        # Only this wake's own media and generated files.
        reads = claude_worker.wake_reads([
            {"message_id": "1002",
             "media": [{"path": pkg + "/media/1002_0.img.png"}]}])
        self.assertTrue(check(pkg, "Read", {
            "file_path": pkg + "/media/1002_0.img.png"}, None, reads)[0])
        self.assertTrue(check(pkg, "Read", {
            "file_path": pkg + "/generated/1002/a.png"}, None, reads)[0])
        for path in (pkg + "/config.yaml", pkg + "/media/../config.yaml",
                     pkg + "/" + claude_worker.OWNER_DM_NONCES,
                     pkg + "/" + claude_worker.ITEM_NONCES,
                     pkg + "/reply_queue.json",
                     pkg + "/media/2001_0.img.png",     # another wake's
                     pkg + "/generated/2001/a.png",
                     "/etc/passwd"):
            self.assertFalse(check(pkg, "Read", {"file_path": path},
                                   None, reads)[0], path)
        self.assertFalse(check(pkg, "Read", {
            "file_path": pkg + "/media/1002_0.img.png"})[0])
        self.assertFalse(check(pkg, "Write", {"file_path": "x"})[0])
        args = claude_worker.worker_args(pkg)
        self.assertEqual(args[args.index("--tools") + 1], "Bash,Read")
        self.assertEqual(args[args.index("--permission-mode") + 1],
                         "dontAsk")


# ---------------------------------------------------------------------
# judge.py

TRANSCRIPT = {
    "scenario": "persona_voice", "channel_id": "500",
    "started_at": "2026-10-17T12:00:00.000000+00:00",
    "ids": {"SUT": "99", "A": "11", "B": "12", "SUT_NAME": "muse-test"},
    "labels": {"q1": "m1"},
    "messages": [
        {"id": "m1", "author": "a", "author_id": "11",
         "ts": "2026-10-17T12:00:01.5+00:00", "content": "<@99> hi there",
         "reply_to": None},
        {"id": "m2", "author": "sut", "author_id": "99",
         "ts": "2026-10-17T12:00:09Z", "content": "Ahoy <@11>!",
         "reply_to": "m1"},
    ],
}
SCENARIO = {"id": "persona_voice", "tier": 2,
            "judge": {"rubric": "SUT replies once, in a nautical voice."}}


class Judge(ClaudeBase):
    def test_reads_a_verdict(self):
        prompt = judge.build_prompt(SCENARIO, TRANSCRIPT)
        for frag in ("nautical voice", "Beacon", "SUT -> [1]: Ahoy @A!"):
            self.assertIn(frag, prompt)
        self.use_fake("verdict")
        self.assertEqual(judge.judge(SCENARIO, TRANSCRIPT, timeout=30),
                         {"verdict": "fail",
                          "reason": "[2] ignored the question"})
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # no tools
        self.use_fake("verdict_plain")
        self.assertEqual(judge.judge(SCENARIO, TRANSCRIPT)["verdict"],
                         "pass")

    def test_anything_else_is_an_error(self):
        for fake in ("garbage", "is_error", "exit2", "hang"):
            with self.subTest(fake):
                self.use_fake(fake)
                v = judge.judge(SCENARIO, TRANSCRIPT, timeout=0.5)
                self.assertEqual(v["verdict"], "error", v)
        os.environ["CLAUDE_BIN"] = os.path.join(self.tmp, "nope")
        self.assertEqual(judge.judge(SCENARIO, TRANSCRIPT)["verdict"],
                         "error")


if __name__ == "__main__":
    unittest.main()
