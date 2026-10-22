"""Update DM suite (offline): versions are release tags (vX.Y.Z).
Watchdog version fields (a local bare "upstream" with tags), the owner
DM with buttons, the button clicks, hooks/update-watch.sh, and the
UPDATING.md commands.

Usage: [MUT_DIR=<tree>] python3 tests/updatedm/test_updatedm.py"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
import monitor_harness as H  # noqa: E402
from common import (FAKE_PROC, FAKEWS, HOOK_RUNTIME as RUNTIME,  # noqa: E402
                    MUT_DIR, copy_watchdog, scratch)

V0 = "v1.0.0"
V1 = "v1.1.0"
V2 = "v1.2.0"
OTHER = "222"
STRANGER = "333"


def tmpdir(prefix):
    return scratch("muse-upd-" + prefix)


def load(cfg_extra=""):
    tmp = tmpdir("run_")
    mod = H.load_monitor(tmp, cfg_extra)
    mod.UPDATE_CHECK_FILE = os.path.join(tmp, "update_check.json")
    mod.UPDATE_REQUEST_FILE = os.path.join(tmp, "update_request.json")
    mod.real_version_installed = mod._version_installed
    mod.installed_versions = set()
    mod._version_installed = \
        lambda v, timeout=5: v in mod.installed_versions
    return mod


def write_check(mod, latest, entries=(), action=False, installed=V0):
    with open(mod.UPDATE_CHECK_FILE, "w") as f:
        json.dump({"checked": 1, "announced": latest, "latest": latest,
                   "installed": installed, "entries": list(entries),
                   "action_needed": action}, f)


def dm_posts(mod):
    return [(p, d) for p, d in mod.api.posts if p.endswith("/messages")]


def ix(custom_id, user=H.OWNER, guild=False, content="notice text"):
    d = {"id": "9001", "token": "tok-en_1", "type": 3,
         "data": {"custom_id": custom_id, "component_type": 2},
         "message": {"id": "77", "content": content}}
    if guild:
        d["member"] = {"user": {"id": user}}
    else:
        d["user"] = {"id": user}
    return d


class Announce(unittest.TestCase):
    def test_dm_each_owner_once_with_buttons(self):
        mod = load()
        mod.OWNER_USER_IDS = {H.OWNER, OTHER}
        ents = [f"v1.{i}.0 (2026-11-0{i}): Entry {i}"
                for i in range(7, 0, -1)]
        write_check(mod, "v1.7.0", ents, action=True, installed=V0)
        state = {}
        mod.announce_update(state)
        posts = dm_posts(mod)
        self.assertEqual(len(posts), 2)
        opened = [d["recipient_id"] for p, d in mod.api.posts
                  if p == "/users/@me/channels"]
        self.assertEqual(sorted(opened), sorted([H.OWNER, OTHER]))
        body = posts[0][1]
        self.assertIn("v1.7.0", body["content"])
        self.assertLessEqual(len(body["content"]), 2000)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        row = body["components"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["type"], 1)
        btns = row[0]["components"]
        self.assertEqual([b["type"] for b in btns], [2, 2])
        self.assertEqual([b["custom_id"] for b in btns],
                         ["mdupd:now:v1.7.0", "mdupd:later:v1.7.0"])
        self.assertEqual(state["update_notice"]["version"], "v1.7.0")
        self.assertEqual(mod._update_announced["version"], "v1.7.0")
        # Same version again (and after a state round trip): no new DM.
        n = len(mod.api.posts)
        mod.announce_update(state)
        mod.announce_update(json.loads(json.dumps(state)))
        self.assertEqual(len(mod.api.posts), n)
        # An older version (a retracted tag) is not offered.
        write_check(mod, V2, ["v1.2.0 (2026-11-01): Old"])
        mod.announce_update(state)
        self.assertEqual(len(mod.api.posts), n)
        # A newer version asks again; semver order, not string order.
        write_check(mod, "v1.10.0", ["v1.10.0 (2026-12-01): Newer"],
                    installed="v1.7.0")
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 4)
        last = dm_posts(mod)[-1][1]
        self.assertIn("mdupd:now:v1.10.0", json.dumps(last["components"]))

    def test_no_entries_and_no_latest(self):
        mod = load()
        write_check(mod, "", [])
        state = {}
        mod.announce_update(state)
        self.assertEqual(mod.api.posts, [])
        os.unlink(mod.UPDATE_CHECK_FILE)
        mod.announce_update(state)
        self.assertEqual(mod.api.posts, [])
        # Not releases: ignored.
        for bad in ("v1.3.0-rc.1", "v2", "1.3.0", "v01.3.0", "a" * 40):
            write_check(mod, bad, ["x"])
            mod.announce_update(state)
        self.assertEqual(mod.api.posts, [])
        write_check(mod, V1, [])                 # a release, no entries
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 1)

    def test_check_off_no_dm(self):
        mod = load("updates:\n  check: false\n")
        write_check(mod, V1, ["v1.1.0 (2026-10-25): X"])
        mod.announce_update({})
        self.assertEqual(mod.api.posts, [])
        # Turned off while running.
        mod = load()
        write_check(mod, V1, ["v1.1.0 (2026-10-25): X"])
        state = {}
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 1)
        cfg = os.environ["MUSE_DISCORD_CONFIG"]
        with open(cfg, "a") as f:
            f.write("updates:\n  check: false\n")
        os.utime(cfg, (time.time() + 5, time.time() + 5))
        write_check(mod, V2, ["v1.2.0 (2026-11-01): Y"])
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 1)

    def test_failed_dm_retried_then_given_up(self):
        mod = load()
        mod.api.errors["/channels/DM1/messages"] = H.HTTPErr(403)
        write_check(mod, V1, ["v1.1.0 (2026-10-25): X"])
        state = {}
        with contextlib.redirect_stdout(io.StringIO()):
            mod.announce_update(state)
            self.assertEqual(state["update_notice"]["owed"], [H.OWNER])
            del mod.api.errors["/channels/DM1/messages"]
            mod.announce_update(state)
        self.assertEqual(state["update_notice"]["owed"], [])
        self.assertEqual(len(dm_posts(mod)), 2)
        mod2 = load()
        mod2.api.errors["/channels/DM1/messages"] = H.HTTPErr(403)
        write_check(mod2, V1, ["v1.1.0 (2026-10-25): X"])
        st = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(15):
                mod2.announce_update(st)
        self.assertEqual(len(dm_posts(mod2)), mod2.UPDATE_DM_TRIES)

    def test_already_installed_version_not_announced(self):
        mod = load()
        mod.installed_versions = {V1}
        write_check(mod, V1, ["v1.1.0 (2026-10-25): X"])
        state = {}
        with contextlib.redirect_stdout(io.StringIO()):
            mod.announce_update(state)
        self.assertEqual(mod.api.posts, [])
        self.assertEqual(state["update_notice"]["version"], V1)

    def test_version_installed_uses_git(self):
        mod = load()
        repo = tmpdir("git_")
        git(repo, "init", "-q")
        commit_file(repo, "a.txt", "a", "one")
        git(repo, "tag", "-a", "-m", "rel", V0)
        commit_file(repo, "a.txt", "b", "two")
        git(repo, "tag", V2, "HEAD")
        git(repo, "reset", "-q", "--hard", V0)
        orig = mod.BASE_DIR
        mod.BASE_DIR = repo
        try:
            self.assertTrue(mod.real_version_installed(V0))
            self.assertFalse(mod.real_version_installed(V2))
            self.assertFalse(mod.real_version_installed("v9.9.9"))
        finally:
            mod.BASE_DIR = orig


class Interactions(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.mod._update_announced["version"] = V2
        self.recs = []

    def dispatch(self, i):
        return self.mod.dispatch_interaction(i, self.recs.append)

    def test_owner_update_now(self):
        r = self.dispatch(ix(f"mdupd:now:{V2}"))
        self.assertEqual(r["type"], 7)
        self.assertEqual(r["data"]["components"], [])
        self.assertLessEqual(len(r["data"]["content"]), 2000)
        with open(self.mod.UPDATE_REQUEST_FILE) as f:
            req = json.load(f)
        self.assertEqual(sorted(req), ["by", "requested_at", "version"])
        self.assertEqual(req["version"], V2)
        self.assertEqual(req["by"], H.OWNER)
        self.assertIsInstance(req["requested_at"], int)
        self.assertLessEqual(abs(req["requested_at"] - time.time()), 5)
        self.assertEqual(self.recs[0]["action"], "now")
        self.assertEqual(self.recs[0]["version"], V2)
        # A second click (another owner, guild-style payload) keeps the
        # first request.
        self.mod.OWNER_USER_IDS = {H.OWNER, OTHER}
        r2 = self.dispatch(ix(f"mdupd:now:{V2}", user=OTHER, guild=True))
        self.assertEqual(r2["type"], 7)
        with open(self.mod.UPDATE_REQUEST_FILE) as f:
            self.assertEqual(json.load(f)["by"], H.OWNER)
        # A long notice stays within 2,000 characters.
        r = self.dispatch(ix(f"mdupd:later:{V2}", content="x" * 1990))
        self.assertLessEqual(len(r["data"]["content"]), 2000)

    def test_non_owner_refused(self):
        r = self.dispatch(ix(f"mdupd:now:{V2}", user=STRANGER))
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        self.assertFalse(os.path.exists(self.mod.UPDATE_REQUEST_FILE))
        self.assertEqual(self.recs, [])
        r = self.dispatch(ix(f"mdupd:now:{V2}", user=STRANGER, guild=True))
        self.assertEqual(r["type"], 4)

    def test_stale_and_unknown(self):
        for cid in (f"mdupd:now:{V1}", "mdupd:now:", "mdupd:bogus:" + V2,
                    "other:thing", "", f"mdupd:later:{V1}",
                    "mdupd:now:v1.3.0", "mdupd:now:v1.2.0-rc.1",
                    "mdupd:now:" + "a" * 40):
            r = self.dispatch(ix(cid))
            self.assertEqual(r["type"], 4, cid)
            self.assertEqual(r["data"]["flags"], 64, cid)
        self.assertFalse(os.path.exists(self.mod.UPDATE_REQUEST_FILE))
        self.assertEqual(self.recs, [])
        self.assertIsNone(self.dispatch({"id": "1", "token": "t",
                                         "type": 4}))
        # Already installed: nothing to do.
        self.mod.installed_versions = {V2}
        for action in ("now", "later"):
            r = self.dispatch(ix(f"mdupd:{action}:{V2}"))
            self.assertEqual(r["type"], 4)
            self.assertEqual(r["data"]["flags"], 64)
        self.assertFalse(os.path.exists(self.mod.UPDATE_REQUEST_FILE))
        self.assertEqual(self.recs, [])

    def test_later_then_no_reask_then_newer_asks(self):
        mod = self.mod
        write_check(mod, V1, ["v1.1.0 (2026-10-25): X"])
        state = {}
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 1)
        r = self.dispatch(ix(f"mdupd:later:{V1}"))
        self.assertEqual(r["type"], 7)
        self.assertEqual(r["data"]["components"], [])
        self.assertFalse(os.path.exists(mod.UPDATE_REQUEST_FILE))
        # The record reaches state through the gateway inbox.
        import queue
        q = queue.Queue()
        for rec in self.recs:
            q.put({"_record": rec})
        with contextlib.redirect_stdout(io.StringIO()):
            mod.drain_gateway(state, q, {})
        self.assertEqual(state["update_notice"]["choice"]["action"], "later")
        self.assertEqual(state["update_notice"]["choice"]["by"], H.OWNER)
        for _ in range(3):
            mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 1)
        write_check(mod, V2, ["v1.2.0 (2026-11-01): Y"])
        mod.announce_update(state)
        self.assertEqual(len(dm_posts(mod)), 2)
        self.assertNotIn("choice", state["update_notice"])
        # The old prompt's buttons are now stale; the new ones work.
        r = self.dispatch(ix(f"mdupd:now:{V1}"))
        self.assertEqual(r["type"], 4)
        r = self.dispatch(ix(f"mdupd:now:{V2}"))
        self.assertEqual(r["type"], 7)
        with open(mod.UPDATE_REQUEST_FILE) as f:
            self.assertEqual(json.load(f)["version"], V2)

    def test_callback_request_shape(self):
        mod = self.mod
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        def fake_urlopen(req, timeout=None):
            seen.update(url=req.full_url, data=req.data, timeout=timeout,
                        headers={k.lower(): v for k, v in req.header_items()},
                        method=req.get_method())
            return Resp()
        orig = mod.urllib.request.urlopen
        mod.urllib.request.urlopen = fake_urlopen
        try:
            mod.interaction_callback(ix("x"), {"type": 7, "data": {}})
        finally:
            mod.urllib.request.urlopen = orig
        self.assertEqual(seen["url"], "https://discord.com/api/v10/"
                         "interactions/9001/tok-en_1/callback")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(json.loads(seen["data"]), {"type": 7, "data": {}})
        self.assertNotIn("authorization", seen["headers"])
        self.assertLess(seen["timeout"], 3)


# ---------------------------------------------------------------- watchdog
def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t",
         *args], check=True, capture_output=True, text=True).stdout


def commit_file(repo, name, text, msg):
    with open(os.path.join(repo, name), "w") as f:
        f.write(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg)


CL_HEAD = "# Changelog\n\nNewest first.\n\n"
OLD = ("## 2026-10-18: Pre-release history\n\nFixed:\n\n- old\n\n"
       "**Action needed:**\n\n- Something old.\n\n")


def entry(tag, date, title, action=None):
    text = f"## {tag} ({date}): {title}\n\nFixed:\n\n- {title}\n\n"
    if action:
        text += f"**Action needed:**\n\n- {action}\n\n"
    return text


E100 = entry(V0, "2026-10-20", "First release", "None.")
E110 = entry(V1, "2026-10-25", "Second",
             "Reinstall the worker prompt (it\n  changed).")
E120 = entry(V2, "2026-11-01", "Third",
             "None; the monitor restarts with the update.")
E130 = entry("v1.3.0", "2026-11-05", "Fourth")


def head_of(text):
    return text.splitlines()[0][3:]


class Watchdog(unittest.TestCase):
    """Real watchdog.sh runs; the tests run at once (load_tests)."""

    def setUp(self):
        self.d = tmpdir("wd_")
        self.origin = os.path.join(self.d, "origin.git")
        self.seed = os.path.join(self.d, "seed")
        self.inst = os.path.join(self.d, "install")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main",
                        self.origin], check=True)
        subprocess.run(["git", "clone", "-q", self.origin, self.seed],
                       check=True, capture_output=True)
        git(self.seed, "checkout", "-q", "-b", "main")
        copy_watchdog(self.seed)
        for n in ("monitor.py", "presence.py"):
            shutil.copy(FAKE_PROC,
                        os.path.join(self.seed, n))
        with open(os.path.join(self.seed, ".gitignore"), "w") as f:
            f.write("config.yaml\nbot_token\n*.log\n*.log.1\n*.json\n"
                    "monitor.heartbeat\n.*.tmp\n")
        git(self.seed, "add", ".")
        commit_file(self.seed, "CHANGELOG.md", CL_HEAD + OLD, "first")
        git(self.seed, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", self.origin, self.inst],
                       check=True, capture_output=True)
        self.cfg(True)
        self.entries = []

    def cfg(self, check):
        tok = os.path.join(self.inst, "bot_token")
        with open(tok, "w") as f:
            f.write("dummy\n")
        with open(os.path.join(self.inst, "config.yaml"), "w") as f:
            f.write(f"bot:\n  token_file: {tok}\npresence:\n  enabled: "
                    f"false\nupdates:\n  check: {str(check).lower()}\n")

    def release(self, tag, text=None, annotated=True):
        """Commit a CHANGELOG with TEXT as the newest entry, tag it, push."""
        if text:
            self.entries.insert(0, text)
        commit_file(self.seed, "CHANGELOG.md",
                    CL_HEAD + "".join(self.entries) + OLD, f"release {tag}")
        if annotated:
            git(self.seed, "tag", "-a", "-m", f"Release {tag}", tag)
        else:
            git(self.seed, "tag", tag)
        git(self.seed, "push", "-q", "origin", "main", tag)

    def tag_only(self, *tags):
        commit_file(self.seed, "other.txt", " ".join(tags), "not a release")
        for t in tags:
            git(self.seed, "tag", t)
        git(self.seed, "push", "-q", "origin", "main", *tags)

    def run_wd(self):
        e = os.environ.copy()
        e["PYTHONPATH"] = FAKEWS
        p = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--pid", "--fork",
             "--mount-proc", "bash", "-c",
             f"cd '{self.inst}' && bash watchdog.sh"],
            capture_output=True, text=True, timeout=200, env=e)
        return p.stdout + p.stderr

    def check(self):
        with open(os.path.join(self.inst, "update_check.json")) as f:
            return json.load(f)

    def reset_checked(self):
        c = self.check()
        c["checked"] = 0
        with open(os.path.join(self.inst, "update_check.json"), "w") as f:
            json.dump(c, f)

    def has_tag(self, tag):
        return subprocess.run(
            ["git", "-C", self.inst, "rev-parse", "-q", "--verify",
             f"refs/tags/{tag}"], capture_output=True).returncode == 0

    def update_lines(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("UPDATE:")]

    def test_unreleased_install_offered_latest(self):
        """The newest release (semver order, a lightweight tag) and its
        entry only; tags that are not releases ignored; an action
        starting "None" is no action."""
        e110 = entry("v1.10.0", "2026-10-21", "Ten",
                     "None; the monitor restarts with the update.")
        self.release(V0, E100)
        self.release("v1.9.0", entry("v1.9.0", "2026-10-20", "Nine",
                                     "Do a thing."))
        self.release("v1.10.0", e110, annotated=False)
        self.tag_only("v1.11.0-rc.1", "v2", "v1.12", "v01.13.0", "1.14.0",
                      "release-15", "v2.0.0-beta", "v3.0.0.1", "nightly")
        out = self.run_wd()
        self.assertEqual(len(self.update_lines(out)), 1, out)
        c = self.check()
        self.assertEqual(c["latest"], "v1.10.0")
        self.assertEqual(c["installed"], "unreleased")
        self.assertEqual(c["announced"], "v1.10.0")
        self.assertEqual(c["entries"], [head_of(e110)])
        self.assertFalse(c["action_needed"])
        self.assertIsInstance(c["checked"], int)
        # Only the latest tag was fetched.
        self.assertTrue(self.has_tag("v1.10.0"))
        self.assertFalse(self.has_tag("v1.9.0"))
        self.assertFalse(self.has_tag(V0))

    def test_installed_offered_newer_with_entries(self):
        self.release(V0, E100)
        git(self.inst, "pull", "-q", "--ff-only")
        self.assertTrue(self.has_tag(V0))
        self.release(V1, E110)
        self.release(V2, E120)
        out = self.run_wd()
        self.assertEqual(len(self.update_lines(out)), 1, out)
        c = self.check()
        self.assertEqual(c["latest"], V2)
        self.assertEqual(c["installed"], V0)
        self.assertEqual(c["entries"], [head_of(E120), head_of(E110)])
        self.assertTrue(c["action_needed"])  # from v1.1.0 only
        # Within a day: no re-check, no second UPDATE line.
        self.assertEqual(self.update_lines(self.run_wd()), [])
        # Forced re-check, same release: fields kept, no UPDATE line.
        self.reset_checked()
        self.assertEqual(self.update_lines(self.run_wd()), [])
        self.assertEqual(self.check()["entries"], c["entries"])
        # A newer release: asked again, entries since v1.0.0.
        self.release("v1.3.0", E130)
        self.reset_checked()
        out = self.run_wd()
        self.assertEqual(len(self.update_lines(out)), 1, out)
        c = self.check()
        self.assertEqual(c["entries"], [head_of(E130), head_of(E120),
                                        head_of(E110)])
        self.assertTrue(c["action_needed"])

    def test_head_already_contains_latest(self):
        self.release(V0, E100)
        # Annotated tag, never fetched: compared by its commit (the
        # peeled ^{} line), not the tag object.
        git(self.inst, "pull", "-q", "--ff-only", "--no-tags")
        self.assertFalse(self.has_tag(V0))
        out = self.run_wd()
        self.assertEqual(self.update_lines(out), [], out)
        c = self.check()
        self.assertEqual(c["latest"], "")
        self.assertEqual(c["installed"], "unreleased")
        self.assertEqual(c["entries"], [])
        # Tracking main past the release (tag local): nothing new either.
        git(self.inst, "fetch", "-q", "--tags")
        self.tag_only("not-a-release")
        git(self.inst, "pull", "-q", "--ff-only")
        self.reset_checked()
        self.assertEqual(self.update_lines(self.run_wd()), [])
        c = self.check()
        self.assertEqual(c["latest"], "")
        self.assertEqual(c["installed"], V0)
        # An older release than installed is never offered.
        git(self.inst, "tag", "v5.0.0")
        self.release(V2, E120)
        self.reset_checked()
        self.assertEqual(self.update_lines(self.run_wd()), [])
        self.assertEqual(self.check()["installed"], "v5.0.0")

    def test_check_false_or_no_origin_writes_nothing(self):
        self.cfg(False)
        self.release(V0, E100)
        out = self.run_wd()
        self.assertEqual(self.update_lines(out), [])
        self.assertFalse(os.path.exists(
            os.path.join(self.inst, "update_check.json")))
        self.cfg(True)
        git(self.inst, "remote", "remove", "origin")
        out = self.run_wd()
        self.assertEqual(self.update_lines(out), [])
        self.assertFalse(os.path.exists(
            os.path.join(self.inst, "update_check.json")))

    def test_updating_md_commands(self):
        """The UPDATING.md commands: find the newest release by hand,
        fetch it, merge it (fast-forward; merge commit with local
        commits)."""
        with open(os.path.join(MUT_DIR, "UPDATING.md")) as f:
            doc = f.read()
        blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
        find = [b for b in blocks if "ls-remote" in b]
        self.assertEqual(len(find), 1)
        step1 = [b for b in blocks if 'fetch --no-tags origin tag' in b]
        self.assertEqual(len(step1), 1)
        self.release("v1.9.0", entry("v1.9.0", "2026-10-20", "Nine"))
        self.release("v1.10.0", entry("v1.10.0", "2026-10-21", "Ten"))
        self.tag_only("v1.11.0-rc.1", "v2", "v01.13.0")
        latest = subprocess.run(["bash", "-c", find[0]], cwd=self.inst,
                                capture_output=True, text=True).stdout
        self.assertEqual(latest.strip(), "v1.10.0")
        script = step1[0].replace("TAG=vX.Y.Z", "TAG=v1.9.0")
        p = subprocess.run(["bash", "-c", "set -e\n" + script], cwd=self.inst,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("unreleased", p.stdout)
        self.assertTrue(self.has_tag("v1.9.0"))
        self.assertFalse(self.has_tag("v1.10.0"))
        git(self.inst, "merge", "-q", "--ff-only", "v1.9.0")
        self.assertEqual(git(self.inst, "describe", "--tags", "--match",
                             "v[0-9]*", "--abbrev=0").strip(), "v1.9.0")
        # Local commit, then the next release: a merge commit.
        commit_file(self.inst, "mine.txt", "x", "local tweak")
        git(self.inst, "fetch", "-q", "--no-tags", "origin", "tag",
            "v1.10.0")
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.inst, "merge", "-q", "--ff-only", "v1.10.0")
        git(self.inst, "merge", "-q", "--no-edit", "v1.10.0")
        self.assertEqual(subprocess.run(
            ["git", "-C", self.inst, "merge-base", "--is-ancestor",
             "v1.10.0", "HEAD"]).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.inst, "mine.txt")))
        self.run_wd()
        c = self.check()
        self.assertEqual(c["installed"], "v1.10.0")
        self.assertEqual(c["latest"], "")


# ---------------------------------------------------------------- hook
class Hook(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir("hook_")
        self.pkg = os.path.join(self.d, "pkg")
        os.mkdir(self.pkg)
        self.state = os.path.join(self.d, "hookstate.json")
        bindir = os.path.join(self.d, "bin")
        os.mkdir(bindir)
        self.git_marker = os.path.join(self.d, "git-called")
        with open(os.path.join(bindir, "git"), "w") as f:
            f.write(f"#!/bin/sh\ntouch '{self.git_marker}'\nexit 1\n")
        os.chmod(os.path.join(bindir, "git"), 0o755)
        self.env = dict(os.environ, HATCH_HOOK_RUNTIME=RUNTIME,
                        HOOK_STATE=self.state, MUSE_DISCORD_DIR=self.pkg,
                        PATH=bindir + os.pathsep + os.environ["PATH"])

    def run_hook(self):
        p = subprocess.run(["bash", os.path.join(MUT_DIR, "hooks",
                                                 "update-watch.sh")],
                           capture_output=True, text=True, env=self.env,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout, p.stderr

    def request(self, version=V1, at=1000):
        with open(os.path.join(self.pkg, "update_request.json"), "w") as f:
            json.dump({"version": version, "requested_at": at,
                       "by": H.OWNER}, f)

    def test_wakes_once_per_request(self):
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("SILENT:"), out)
        self.request()
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("WAKE:"), out)
        payload = json.loads(out.splitlines()[1])
        self.assertEqual(payload["request"], {"version": V1,
                                              "requested_at": 1000,
                                              "by": H.OWNER})
        self.assertEqual(payload["package_dir"], self.pkg)
        self.assertTrue(payload["guide"].endswith("/UPDATING.md"))
        with open(self.state) as f:
            self.assertEqual(list(json.load(f)["handed"]), [f"{V1}:1000"])
        for _ in range(3):
            out, _ = self.run_hook()
            self.assertTrue(out.startswith("SILENT:"), out)
        # A newer version requested (after a newer DM): a new wake.
        self.request(V2, 2000)
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("WAKE:"), out)
        self.assertEqual(json.loads(out.splitlines()[1])["request"]
                         ["version"], V2)
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("SILENT:"), out)
        # The same version requested again later (a fresh click after
        # the first run stopped early): a new wake too.
        self.request(V2, 3000)
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("WAKE:"), out)
        # Muse deleted the file when done.
        os.unlink(os.path.join(self.pkg, "update_request.json"))
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("SILENT:"), out)
        self.assertFalse(os.path.exists(self.git_marker))
        # A lease that expired (the run died): handed out again, once.
        os.remove(self.state)
        self.request()
        self.run_hook()
        with open(self.state) as f:
            st = json.load(f)
        k = next(iter(st["handed"]))
        st["handed"][k] -= 21601
        with open(self.state, "w") as f:
            json.dump(st, f)
        out, err = self.run_hook()
        self.assertTrue(out.startswith("WAKE:"), out)
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("SILENT:"), out)

    def test_garbage_request_silent(self):
        for text in ("garbage{", "[]", '{"by": "1"}', "",
                     json.dumps({"commit": "a" * 40, "requested_at": 1}),
                     json.dumps({"version": "v1.2", "requested_at": 1}),
                     json.dumps({"version": "v1.2.0-rc.1",
                                 "requested_at": 1}),
                     json.dumps({"version": "v1.2.0; rm -rf ~",
                                 "requested_at": 1}),
                     json.dumps({"version": 120, "requested_at": 1})):
            with open(os.path.join(self.pkg, "update_request.json"),
                      "w") as f:
                f.write(text)
            out, _ = self.run_hook()
            self.assertTrue(out.startswith("SILENT:"), (text, out))
        with open(self.state, "w") as f:
            f.write("not json")
        self.request()
        out, _ = self.run_hook()
        self.assertTrue(out.startswith("WAKE:"), out)


if __name__ == "__main__":
    unittest.main()
