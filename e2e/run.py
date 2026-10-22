#!/usr/bin/env python3
"""run.py -- the muse-discord end-to-end test runner.

    python3 e2e/run.py --tree <checkout dir> --tier 1|2 [--worker fake|claude]
                       [--scenario ID ...] [--out out] [--dry-run]
                       [--keep-channels]

Starts the checkout's monitor.py against the private test server (see
e2e/README.md), plays every scenario of the tier in its own fresh
channel with two driver bots, then writes for each scenario
<out>/transcripts/<id>.json, runs the deterministic checks (and, for
tier 2, the judge), and writes <out>/results.json and <out>/summary.md.

Flow: check env -> load scenarios -> create one channel per scenario ->
start SUT + worker -> wait ready -> run scenarios one by one -> delete
channels (unless --keep-channels) -> stop everything -> results.

Before each scenario the monitor is (re)started when its config
differs; a config.discord.yaml overlay left by a `settings` step is
deleted first (the running monitor reloads; it is restarted only when
no reload shows), so a live change never leaks into the next
scenario. The transcript is the scenario channel's history merged
with every thread the scenario started (one list, sorted by id), and
records how many times the monitor was (re)started while the steps
ran (monitor_restarts).

Exit code: 1 when any scenario failed (or the scenarios do not load),
else 0. "warn" (tier-2 judge said no while every check passed), "flake"
(Discord or the SUT misbehaved outside the PR's control) and "skip"
never fail the job. With any of E2E_GUILD_ID, E2E_SUT_TOKEN,
E2E_DRIVER_A_TOKEN, E2E_DRIVER_B_TOKEN missing (fork PRs get no
secrets) the run is skipped and exits 0.

--dry-run loads and validates the scenarios, renders a sample config,
and prints the plan: no env, no network.

Tokens never reach any file this writes: every string that goes to
results, summary, transcripts, or the copied logs is redacted.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import checks  # noqa: E402
import discord_rest  # noqa: E402
import judge  # noqa: E402
import scenario  # noqa: E402
import sut  # noqa: E402

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(E2E_DIR, "scenarios")
FIXTURE_DIR = os.path.join(E2E_DIR, "fixtures")
REQUIRED_ENV = ("E2E_GUILD_ID", "E2E_SUT_TOKEN", "E2E_DRIVER_A_TOKEN",
                "E2E_DRIVER_B_TOKEN")
TOKEN_ENV = ("E2E_SUT_TOKEN", "E2E_DRIVER_A_TOKEN", "E2E_DRIVER_B_TOKEN",
             "CLAUDE_CODE_OAUTH_TOKEN")
SUMMARY_MARKER = "<!-- muse-discord-e2e -->"
READY_TIMEOUT = 180
POLL_SECS = 1.5           # history polling cadence while waiting
OUTBOX_POLL_SECS = 0.02   # wait_for outbox_processing (spec: <= 50 ms)
CONVERSE_SETTLE_SECS = 6  # quiet gap that ends a multi-part SUT turn
STALE_CHANNEL_SECS = 6 * 3600
LOG_TAIL_LINES = 200
# The scenario's own monitor.log (since just before its config
# restart), for log_contains / log_lacks; the last bytes when longer.
SCENARIO_LOG_BYTES = 256 * 1024
# What the fake worker records about every queue item it handles (the
# queue_item check reads it; fake_worker.WORKER_ITEMS).
WORKER_ITEMS = "e2e_worker_items.json"
# Discord bot-token shape, redacted even when it is not one of ours.
TOKEN_RE = re.compile(
    r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
SAMPLE_IDS = {"SUT": "100000000000000001", "A": "100000000000000002",
              "B": "100000000000000003", "SUT_NAME": "muse-test"}
SAMPLE_GUILD = "100000000000000000"
# settings step: how long to wait for the monitor to log the reload
# (its main loop re-checks the files at least every ~2 s), and how
# often to look.
SETTINGS_TIMEOUT = 30
SETTINGS_POLL_SECS = 0.5
RELOADED_MARKER = "Settings reloaded"
RELOAD_FAILED_MARKER = "Config re-read failed"
# Discord system messages a thread creates (THREAD_CREATED, the
# THREAD_STARTER_MESSAGE copy of the parent): not conversation.
THREAD_SYSTEM_TYPES = (18, 21)
# Driver bot permission integers (e2e/README.md): Create Public
# Threads (1 << 35) and Send Messages in Threads (1 << 38) on top of
# the original 101456 (A) / 101440 (B).
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38
DRIVER_PERMISSIONS = {
    "a": 101456 | CREATE_PUBLIC_THREADS | SEND_MESSAGES_IN_THREADS,
    "b": 101440 | SEND_MESSAGES_IN_THREADS,
}


class Flake(Exception):
    """Infrastructure trouble outside the PR's control."""


class MonitorDown(Flake):
    """The monitor could not be (re)started for a scenario: the rest of
    the run is a flake too."""


def fixture_path(name):
    """scenario.fixture_path when scenario.py has it, else fixtures/."""
    fn = getattr(scenario, "fixture_path", None)
    return fn(name) if fn else os.path.join(FIXTURE_DIR, name)


# --- redaction -------------------------------------------------------

def _secrets():
    return [os.environ[k] for k in TOKEN_ENV
            if len(os.environ.get(k, "")) >= 8]


def redact(obj):
    """obj with every token (ours, or anything shaped like a Discord
    token) replaced by [REDACTED], recursively."""
    if isinstance(obj, str):
        for s in _secrets():
            obj = obj.replace(s, "[REDACTED]")
        return TOKEN_RE.sub("[REDACTED]", obj)
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    return obj


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(redact(data), f, indent=2)
        f.write("\n")


def log(msg):
    print(redact(f"[e2e {time.strftime('%H:%M:%S')}] {msg}"), flush=True)


# --- results ---------------------------------------------------------

def scenario_status(sc, check_results, judge_result, error=None,
                    flake=False):
    """pass | fail | warn | flake, per the spec."""
    if flake:
        return "flake"
    if error or any(not c.get("ok") for c in check_results):
        return "fail"
    if int(sc.get("tier", 1)) == 2 and judge_result \
            and judge_result.get("verdict") != "pass":
        return "warn"
    return "pass"


def exit_code(results):
    return 1 if any(r["status"] == "fail" for r in results) else 0


def _cell(text, limit=300):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text.replace("|", "\\|")


def _notes(r):
    notes = []
    if r.get("error"):
        notes.append(r["error"])
    for c in r.get("checks") or []:
        if not c.get("ok"):
            notes.append(f"{c.get('name')}: {c.get('detail')}")
    j = r.get("judge")
    if j and j.get("verdict") != "pass":
        notes.append(f"judge {j.get('verdict')}")
    if r.get("status") != "pass":
        notes += r.get("notes") or []
    if not notes and r.get("duration") is not None:
        notes.append(f"{r['duration']:.0f}s")
    return "; ".join(notes)


def render_summary(results, tier, note=None):
    """summary.md: the sticky-comment marker, a table, judge reasons,
    a pointer to the transcripts."""
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if note:
        headline = note
    elif exit_code(results):
        headline = "FAIL"
    elif counts.get("flake"):
        headline = "PASS with flakes"
    else:
        headline = "PASS"
    lines = [SUMMARY_MARKER, f"## muse-discord e2e (tier {tier}): "
             f"{headline}", ""]
    if counts:
        lines.append(", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
        lines.append("")
    if counts.get("flake"):
        lines += ["> [!WARNING]",
                  f"> {counts['flake']} scenario(s) FLAKED: Discord or the "
                  "test infrastructure misbehaved, so they prove nothing "
                  "either way. Re-run the job; investigate if it repeats.",
                  ""]
    if results:
        lines += ["| scenario | tier | status | notes |",
                  "| --- | --- | --- | --- |"]
        for r in results:
            lines.append(f"| {_cell(r['scenario'])} | {r['tier']} | "
                         f"**{r['status']}** | {_cell(_notes(r))} |")
        lines.append("")
    judged = [r for r in results if r.get("judge")]
    if judged:
        lines += ["### Judge (tier 2)", ""]
        for r in judged:
            j = r["judge"]
            lines.append(f"- **{r['scenario']}**: {j.get('verdict')}: "
                         f"{_cell(j.get('reason'), 1000)}")
        lines.append("")
    where = "the `out/transcripts/` folder of this run's artifacts"
    if os.environ.get("GITHUB_RUN_ID") and os.environ.get(
            "GITHUB_REPOSITORY"):
        url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}"
               f"/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}")
        where += f" ([workflow run]({url}))"
    lines.append(f"Transcripts: {where}.")
    return redact("\n".join(lines) + "\n")


def write_outputs(out_dir, results, tier, note=None):
    write_json(os.path.join(out_dir, "results.json"), results)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(render_summary(results, tier, note))


# --- transcript ------------------------------------------------------

def author_role(author_id, ids):
    for role, key in (("sut", "SUT"), ("a", "A"), ("b", "B")):
        if author_id and author_id == ids.get(key):
            return role
    return "other"


def transcript_message(m, ids, thread=None):
    """One transcript entry; thread is the label of the thread the
    message was posted in (None: the scenario channel)."""
    author = m.get("author") or {}
    ref = m.get("message_reference") or {}
    out = {
        "id": m.get("id"),
        "author": author_role(author.get("id"), ids),
        "author_id": author.get("id"),
        "ts": m.get("timestamp"),
        "content": m.get("content") or "",
        "reply_to": ref.get("message_id") if m.get("type") == 19
        else None,
        "attachments": [a.get("filename") for a in m.get("attachments")
                        or []],
        "mentions": [u.get("id") for u in m.get("mentions") or []],
        "mention_everyone": bool(m.get("mention_everyone")),
        "mention_roles": list(m.get("mention_roles") or []),
    }
    if thread:
        out["thread"] = thread
    return out


def _state_holds(the_sut):
    """state.json's never_share holds ({hold id: {item, at, ...}}), or
    None when the state is missing or has none."""
    st = the_sut.read_json("state.json")
    return st.get("holds") if isinstance(st, dict) else None


def build_transcript(sc, channel_id, started_at, ids, labels,
                     converse_turns, messages, the_sut, log_mark=None,
                     threads=None, monitor_restarts=None):
    """threads: {thread label: thread id} for the threads the scenario
    started; messages from those channels get their `thread` label."""
    threads = dict(threads or {})
    thread_of = {str(tid): lab for lab, tid in threads.items()}
    scenario_log = ""
    if the_sut is not None and log_mark is not None \
            and hasattr(the_sut, "log_since"):
        scenario_log = the_sut.log_since(log_mark, SCENARIO_LOG_BYTES)
    return {
        "scenario": sc["id"],
        "channel_id": channel_id,
        "started_at": started_at,
        "ids": ids,
        "labels": labels,
        "converse_turns": converse_turns,
        "threads": threads,
        "monitor_restarts": monitor_restarts,
        "messages": [transcript_message(
            m, ids, thread_of.get(str(m.get("channel_id"))))
            for m in messages],
        "sut_files": {
            "quarantine": the_sut.read_json("quarantine.json")
            if the_sut else None,
            "ledger": the_sut.read_json("mention_ledger.json")
            if the_sut else None,
            "holds": _state_holds(the_sut) if the_sut else None,
            "worker_items": the_sut.read_json(WORKER_ITEMS)
            if the_sut else None,
            # config.discord.yaml (what Discord-side changes wrote),
            # parsed; None when nothing was written.
            "overlay": the_sut.read_yaml(sut.OVERLAY_NAME)
            if the_sut is not None and hasattr(the_sut, "read_yaml")
            else None,
        },
        "monitor_log_tail": the_sut.log_tail(LOG_TAIL_LINES)
        if the_sut else "",
        # monitor.log since just before this scenario's config restart.
        "monitor_log": scenario_log,
    }


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- scenario execution ------------------------------------------------

def missing_thread_permission(driver, perm, doing):
    """The Flake message for a 403 from a thread call: the driver's
    role predates the thread permissions (e2e/README.md)."""
    want = DRIVER_PERMISSIONS.get(driver, 0) | SEND_MESSAGES_IN_THREADS
    if perm == "Create Public Threads":
        want |= CREATE_PUBLIC_THREADS
    return (f"driver {driver.upper()} lacks {perm} (HTTP 403 {doing}). In "
            f"the test server, Server Settings > Roles > driver-{driver}: "
            f"enable Create Public Threads and Send Messages in Threads; "
            f"or re-invite it with permissions={want} (e2e/README.md)")


class Runner:
    """Plays scenarios against one SUT. Steps never fail a scenario on
    their own (a timeout just moves on; the checks judge the result);
    Flake and SUTError mean the run says nothing about the PR."""

    def __init__(self, the_sut, drivers, reader, ids, base_cfg,
                 sleep=time.sleep, clock=time.time):
        self.sut = the_sut
        self.drivers = drivers  # {"a": Bot, "b": Bot}
        self.reader = reader    # Bot used to read history
        self.ids = ids
        self.base_cfg = base_cfg
        self.sleep = sleep
        self.clock = clock
        self.monitor_expected = True
        self.notes = []  # per scenario: timeouts and the like
        self.threads = {}  # per scenario: thread label -> thread id
        self.starts = 0    # monitor (re)starts during the steps
        self._ms = None    # the SUT's muse_settings module

    def history(self, channel_id):
        """One channel's (or thread's) messages, oldest first. Reading
        after the channel's own id skips a thread's starter message,
        which shares the id of the message the thread was started on."""
        return [m for m in self.reader.history(channel_id, after=channel_id)
                if m.get("type") not in THREAD_SYSTEM_TYPES]

    def scenario_history(self, channel_id):
        """The channel plus every thread this scenario started, as one
        list sorted by id."""
        msgs = {}
        for cid in [channel_id] + list(self.threads.values()):
            for m in self.history(cid):
                msgs[m["id"]] = m
        return sorted(msgs.values(), key=lambda m: int(m["id"]))

    def sut_messages(self, channel_id, threads=False):
        """The SUT's messages in one channel or thread; threads=True:
        in the channel and all of its threads."""
        hist = self.scenario_history(channel_id) if threads \
            else self.history(channel_id)
        return [m for m in hist
                if (m.get("author") or {}).get("id") == self.ids["SUT"]]

    def check_alive(self):
        if self.monitor_expected and not self.sut.alive():
            raise Flake("monitor exited unexpectedly:\n"
                        + self.sut.log_tail(15))

    def overlay_path(self):
        return os.path.join(self.sut.pkg_dir, sut.OVERLAY_NAME)

    def ensure_config(self, cfg):
        """(Re)start the monitor under cfg (gateway on) unless it is
        already running exactly that. A config.discord.yaml (a
        `settings` step's live changes) never leaks into the next
        scenario: with the right config running, it is deleted and the
        monitor reloads (the file is safe to delete: everything then
        follows config.yaml), with a restart only when no reload shows;
        otherwise it is deleted while the monitor is stopped. Returns
        monitor.log's size where the scenario's log starts (None when
        nothing changed)."""
        overlay = os.path.exists(self.overlay_path())
        same = self.sut.alive() and self.sut.gateway \
            and self.sut.config == self.sut.effective_config(cfg)
        if same and not overlay:
            return None
        if same:
            mark = self.sut.log_size()
            os.unlink(self.overlay_path())
            if self._wait_reload(mark, SETTINGS_TIMEOUT,
                                 "clearing config.discord.yaml"):
                log("cleared config.discord.yaml (live reload)")
                return self.sut.log_size()
            log("no reload after clearing config.discord.yaml: restarting")
        else:
            log("restarting the monitor for this scenario's config")
        self.sut.stop()
        try:
            os.unlink(self.overlay_path())
        except FileNotFoundError:
            pass
        mark = self.sut.log_size() if hasattr(self.sut, "log_size") \
            else None
        self.sut.start(gateway=True, config=cfg)
        self.starts += 1
        self.sut.wait_ready(READY_TIMEOUT)
        self.monitor_expected = True
        return mark

    def run(self, sc, channel_id):
        """Play one scenario; returns its transcript."""
        cfg = sut.scenario_config(self.base_cfg, sc, self.ids["A"],
                                  self.ids["B"], channel_id=channel_id)
        # The scenario's log starts just before its config (re)start, so
        # it includes what the config prints at startup and nothing the
        # previous process wrote; with no restart, it starts now.
        try:
            log_mark = self.ensure_config(cfg)
        except sut.SUTError as e:
            raise MonitorDown(f"monitor could not be (re)started: {e}")
        if log_mark is None and hasattr(self.sut, "log_size"):
            log_mark = self.sut.log_size()
        self.notes = []
        self.threads = {}
        self.starts = 0  # the restart above is not the scenario's
        started_at = iso_now()
        labels = {}
        # senders: label -> driver; posted_in: label -> channel/thread id
        state = {"converse_turns": None, "senders": {}, "posted_in": {}}
        for i, step in enumerate(sc.get("steps") or []):
            self.check_alive()
            self.step(step, channel_id, labels, state, i)
        self.check_alive()
        self.sleep(float((sc.get("expect") or {}).get("settle", 0) or 0))
        msgs = self.scenario_history(channel_id)
        return build_transcript(sc, channel_id, started_at, self.ids,
                                labels, state["converse_turns"], msgs,
                                self.sut, log_mark=log_mark,
                                threads=self.threads,
                                monitor_restarts=self.starts)

    def step(self, step, channel_id, labels, state, index):
        kind, arg = next(iter(step.items()))
        if kind == "send":
            self.send(arg, channel_id, labels, state.get("senders"),
                      state.get("posted_in"))
        elif kind == "wait":
            self.sleep(float(arg))
        elif kind == "wait_for":
            self.wait_for(arg, channel_id)
        elif kind == "action":
            self.action(arg)
        elif kind == "converse":
            state["converse_turns"] = self.converse(arg, channel_id)
        elif kind == "delete":
            self.delete(arg, channel_id, labels, state.get("senders") or {},
                        state.get("posted_in") or {})
        elif kind == "thread":
            self.thread(arg, channel_id, labels)
        elif kind == "settings":
            self.settings(arg, channel_id)
        else:
            raise ValueError(f"step {index + 1}: unknown step {kind!r}")

    def _reply_target(self, ref, channel_id, labels):
        if not ref:
            return None
        if ref == "last_sut":
            sm = self.sut_messages(channel_id)
            return sm[-1]["id"] if sm else None
        if ref not in labels:
            raise ValueError(f"reply_to: unknown label {ref!r}")
        return labels[ref]

    def _thread_id(self, label):
        if label not in self.threads:
            raise ValueError(f"unknown thread label {label!r}")
        return self.threads[label]

    def send(self, arg, channel_id, labels, senders=None, posted_in=None):
        """Post as a driver, in the scenario channel or (send.in) in one
        of its threads; reply_to (and last_sut) resolve there."""
        bot = self.drivers[arg["as"]]
        text = scenario.render(arg.get("text", ""), self.ids)
        files = None
        if arg.get("attach"):
            files = [fixture_path(arg["attach"])]
        target = self._thread_id(arg["in"]) if arg.get("in") \
            else channel_id
        if arg.get("typing"):
            # Like a person writing the next line: the SUT keeps a burst
            # open while its author types, however long Discord takes to
            # deliver the post itself.
            bot.typing(target)
        try:
            msg = bot.send(target, text,
                           reply_to=self._reply_target(arg.get("reply_to"),
                                                       target, labels),
                           files=files)
        except discord_rest.DiscordError as e:
            if arg.get("in") and e.status == 403:
                raise Flake(missing_thread_permission(
                    arg["as"], "Send Messages in Threads",
                    f"posting in thread {arg['in']!r}")) from None
            raise
        if arg.get("label"):
            labels[arg["label"]] = msg["id"]
            if senders is not None:
                senders[arg["label"]] = arg["as"]
            if posted_in is not None:
                posted_in[arg["label"]] = target
        return msg

    def delete(self, label, channel_id, labels, senders, posted_in=None):
        """The driver that sent the label's message deletes it, where it
        was posted (its id stays in labels, so checks can still name
        it)."""
        if label not in labels:
            raise ValueError(f"delete: unknown label {label!r}")
        self.drivers[senders.get(label, "a")].delete_message(
            (posted_in or {}).get(label, channel_id), labels[label])

    def thread(self, arg, channel_id, labels):
        """A driver (default a) starts a public thread on a labelled
        channel message; later steps reach it by its label."""
        who = arg.get("as", "a")
        src = arg["from"]
        if src not in labels:
            raise ValueError(f"thread: unknown label {src!r}")
        try:
            th = self.drivers[who].start_thread(channel_id, labels[src],
                                                arg["name"])
        except discord_rest.DiscordError as e:
            if e.status == 403:
                raise Flake(missing_thread_permission(
                    who, "Create Public Threads",
                    f"starting a thread on {src!r}")) from None
            raise
        self.threads[arg["label"]] = th["id"]
        return th

    # --- settings step ---

    def settings_module(self):
        """The SUT's own muse_settings.py (the PR's code, in pkg),
        imported once."""
        if self._ms is None:
            path = os.path.join(self.sut.pkg_dir, "muse_settings.py")
            if not os.path.isfile(path):
                raise ValueError("settings step: the tree under test has "
                                 "no muse_settings.py")
            spec = importlib.util.spec_from_file_location(
                "_e2e_sut_muse_settings", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._ms = mod
        return self._ms

    def scope_path(self, scope, channel_id):
        """muse_settings scope path of a settings step scope."""
        if scope in ("everywhere", "dms"):
            return (scope,)
        gid = sut.guild_of(self.base_cfg)
        if not gid:
            raise ValueError("settings step: the base config names no "
                             "test server")
        if scope == "server":
            return ("servers", gid)
        if scope == "channel":
            return ("servers", gid, "channels", str(channel_id))
        raise ValueError(f"settings step: unknown scope {scope!r}")

    def settings(self, arg, channel_id):
        """Change a setting live, the way /settings does (settings_ui ->
        muse_settings.set_value / reset / undo on <pkg>/config.yaml,
        by driver A, the owner), then wait for the monitor to log the
        reload. A timeout is a note, not a failure. Returns True when
        the reload was seen."""
        ms = self.settings_module()
        config_path = os.path.join(self.sut.pkg_dir, "config.yaml")
        plugins_dir = os.path.join(self.sut.pkg_dir, "plugins")
        by = str(self.ids["A"])
        op = next(k for k in ("set", "reset", "undo") if k in arg)
        scope = arg["undo"] if op == "undo" else arg["scope"]
        path = self.scope_path(scope, channel_id)
        mark = self.sut.log_size()
        if op == "set":
            key = ms.format_key(path, arg["set"])
            ms.set_value(config_path, key, arg["value"], by, plugins_dir)
            what = f"set {key} = {arg['value']!r}"
        elif op == "reset":
            key = ms.format_key(path, arg["reset"])
            ms.reset(config_path, key, by, plugins_dir)
            what = f"reset {key}"
        else:
            key = ms.undo(config_path, ms.scope_prefix(path), by,
                          plugins_dir)
            if key is None:
                note = f"settings undo at {scope}: nothing to undo"
                log(note)
                self.notes.append(note)
                return False
            what = f"undo {key}"
        log(f"settings: {what}")
        return self._wait_reload(mark, float(arg.get("timeout",
                                                     SETTINGS_TIMEOUT)),
                                 what)

    def _wait_reload(self, mark, timeout, what):
        deadline = self.clock() + timeout
        while True:
            text = self.sut.log_since(mark)
            if RELOADED_MARKER in text:
                return True
            if RELOAD_FAILED_MARKER in text:
                note = (f"settings {what}: the monitor rejected the change "
                        f"({RELOAD_FAILED_MARKER})")
                log(note)
                self.notes.append(note)
                return False
            if self.clock() >= deadline:
                note = (f"settings {what}: no \"{RELOADED_MARKER}\" in "
                        f"monitor.log after {timeout:.0f}s")
                log(note)
                self.notes.append(note)
                return False
            self.check_alive()
            self.sleep(SETTINGS_POLL_SECS)

    def wait_for(self, arg, channel_id):
        timeout = float(arg.get("timeout", 60))
        deadline = self.clock() + timeout
        while True:
            if "sut_messages" in arg:
                # The channel and every thread the scenario started.
                done = len(self.sut_messages(channel_id, threads=True)) \
                    >= int(arg["sut_messages"])
            elif arg.get("outbox_processing"):
                done = os.path.exists(os.path.join(
                    self.sut.pkg_dir, "reply_outbox.processing.json"))
            else:
                raise ValueError(f"wait_for: nothing to wait for: {arg}")
            if done:
                return True
            if self.clock() >= deadline:
                # Not fatal: the checks decide what the timeout means.
                note = f"wait_for {arg} timed out after {timeout:.0f}s"
                log(note)
                self.notes.append(note)
                return False
            if arg.get("outbox_processing"):
                # The file lives for one POST round trip: poll fast and
                # return at once so a following kill_monitor lands
                # mid-send.
                self.sleep(OUTBOX_POLL_SECS)
                continue
            self.check_alive()
            self.sleep(POLL_SECS)

    def action(self, name):
        if name == "kill_monitor":
            self.sut.stop(kill=True)
            self.monitor_expected = False
            return
        if name == "start_monitor":
            self.sut.start(gateway=self.sut.gateway)
        elif name == "gateway_off":
            self.sut.restart(gateway=False)
        else:
            raise ValueError(f"unknown action {name!r}")
        self.starts += 1
        self.sut.wait_ready(READY_TIMEOUT)
        self.monitor_expected = True

    def converse(self, arg, channel_id):
        """Reply to each new SUT turn with the next line; returns how
        many SUT turns happened (stops when the SUT goes quiet)."""
        bot = self.drivers[arg["as"]]
        per_turn = float(arg.get("per_turn_timeout", 90))
        # SUT messages a driver already spoke after are old turns; one
        # still unanswered (the reply to the opening line) is turn 1.
        hist = self.history(channel_id)
        last_other = max((i for i, m in enumerate(hist)
                          if (m.get("author") or {}).get("id")
                          != self.ids["SUT"]), default=-1)
        seen = {m["id"] for m in hist[:last_other + 1]}
        turns = 0
        lines = list(arg.get("lines") or [])
        # One wait per line, plus one for the answer to the last line.
        for i in range(len(lines) + 1):
            new = self._wait_new_sut(channel_id, seen, per_turn)
            if not new:
                self.notes.append(f"converse: the SUT went quiet after "
                                  f"{turns} turn(s)")
                break
            turns += 1
            seen.update(m["id"] for m in new)
            if i < len(lines):
                text = scenario.render(lines[i], self.ids)
                bot.send(channel_id, text, reply_to=new[-1]["id"])
        return turns

    def _wait_new_sut(self, channel_id, seen, timeout):
        deadline = self.clock() + timeout
        while True:
            new = [m for m in self.sut_messages(channel_id)
                   if m["id"] not in seen]
            if new:
                # A multi-part reply arrives over several seconds (typing
                # beats between parts): the turn ends once no further
                # part has come for CONVERSE_SETTLE_SECS.
                for _ in range(10):
                    self.sleep(CONVERSE_SETTLE_SECS)
                    more = [m for m in self.sut_messages(channel_id)
                            if m["id"] not in seen]
                    if len(more) == len(new):
                        break
                    new = more
                return new
            if self.clock() >= deadline:
                return []
            self.check_alive()
            self.sleep(POLL_SECS)


# --- run modes ---------------------------------------------------------

def select(scenarios, wanted):
    if not wanted:
        return scenarios
    by_id = {s["id"]: s for s in scenarios}
    unknown = [w for w in wanted if w not in by_id]
    if unknown:
        raise scenario.ScenarioError(
            f"unknown scenario id(s) for this tier: {', '.join(unknown)}")
    return [by_id[w] for w in wanted]


def dry_run(args, scenarios):
    cfg = sut.scenario_config(
        sut.base_config(SAMPLE_GUILD, SAMPLE_IDS["A"], SAMPLE_IDS["B"]),
        {}, SAMPLE_IDS["A"], SAMPLE_IDS["B"])
    import yaml
    print("# Sample SUT config (ids are placeholders; sut.py also sets "
          "token_from, persona, presence, updates and "
          "rest_poll_seconds):")
    print(yaml.safe_dump(cfg, sort_keys=False).rstrip())
    print()
    print(f"# Plan: tier {args.tier}, worker {args.worker}, "
          f"{len(scenarios)} scenario(s)")
    for sc in scenarios:
        steps = sc.get("steps") or []
        kinds = [describe_step(s) for s in steps]
        expect = (sc.get("expect") or {}).get("checks") or []
        names = [next(iter(c)) if isinstance(c, dict) else str(c)
                 for c in expect]
        extra = []
        if sc.get("config"):
            extra.append("config overrides (monitor restart)")
        if sc.get("b_is_bot"):
            extra.append("B is a bot")
        print(f"- {sc['id']} (tier {sc.get('tier')}): {sc.get('title')}")
        print(f"    channel: {channel_name('<run>', sc['id'])}")
        print(f"    steps: {len(steps)} ({', '.join(kinds)})")
        print(f"    checks: {', '.join(names) or 'none'}"
              f"{'; judge' if sc.get('judge') else ''}")
        if extra:
            print(f"    {'; '.join(extra)}")
    return 0


def describe_step(step):
    """A step for the --dry-run plan: its kind, plus the details of the
    thread / settings steps and of a send into a thread."""
    kind, arg = next(iter(step.items()))
    if not isinstance(arg, dict):
        return kind
    if kind == "send" and arg.get("in"):
        return f"send in {arg['in']}"
    if kind == "thread":
        return (f"thread {arg.get('label')} on {arg.get('from')} "
                f"(as {arg.get('as', 'a')})")
    if kind == "settings":
        if "undo" in arg:
            return f"settings undo @{arg['undo']}"
        if "set" in arg:
            return (f"settings set {arg['set']}={arg.get('value')!r} "
                    f"@{arg.get('scope')}")
        if "reset" in arg:
            return f"settings reset {arg['reset']} @{arg.get('scope')}"
    return kind


def channel_name(tag, sid):
    name = f"e2e-{tag}-{sid}".lower()
    return re.sub(r"[^a-z0-9_<>-]", "-", name)[:100]


def run_tag(tree):
    if os.environ.get("GITHUB_RUN_ID"):
        tag = os.environ["GITHUB_RUN_ID"]
        if os.environ.get("GITHUB_RUN_ATTEMPT", "1") != "1":
            tag += f"-{os.environ['GITHUB_RUN_ATTEMPT']}"
        return tag
    try:
        sha = subprocess.run(["git", "-C", tree, "rev-parse", "--short",
                              "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return sha or time.strftime("%m%d%H%M%S")


def cleanup_stale_channels(bot, guild_id):
    """Delete e2e-* channels older than STALE_CHANNEL_SECS (left behind
    by a run that was killed before its cleanup). Best effort."""
    try:
        for ch in bot.guild_channels(guild_id):
            if not str(ch.get("name", "")).startswith("e2e-"):
                continue
            age = time.time() - discord_rest.snowflake_time(ch["id"])
            if age > STALE_CHANNEL_SECS:
                log(f"deleting stale channel #{ch['name']}")
                bot.delete_channel(ch["id"])
    except Exception as e:
        log(f"stale channel cleanup skipped: {e}")


def start_worker(kind, pkg_dir, log_path):
    script = os.path.join(E2E_DIR, f"{kind}_worker.py")
    env = {k: v for k, v in os.environ.items()
           if k not in ("E2E_SUT_TOKEN", "E2E_DRIVER_A_TOKEN",
                        "E2E_DRIVER_B_TOKEN")}
    if kind != "claude":
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    f = open(log_path, "a")
    try:
        return subprocess.Popen([sys.executable, script, "--pkg", pkg_dir],
                                stdout=f, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
    finally:
        f.close()


def stop_worker(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def copy_logs(run_dir, out_dir):
    """Redacted copies of monitor.log and worker.log into out/logs."""
    dest = os.path.join(out_dir, "logs")
    os.makedirs(dest, exist_ok=True)
    for src in (os.path.join(run_dir, "pkg", "monitor.log"),
                os.path.join(run_dir, "pkg", "claude_worker.log"),
                os.path.join(run_dir, "worker.log")):
        try:
            with open(src, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        with open(os.path.join(dest, os.path.basename(src)), "w") as f:
            f.write(redact(text))


def run_one(runner, sc, channel_id, out_dir, use_judge):
    """Play, record, check, judge one scenario; returns its result."""
    t0 = time.time()
    result = {"scenario": sc["id"], "tier": int(sc.get("tier", 1)),
              "status": "fail", "checks": [], "judge": None,
              "duration": 0.0, "error": None, "notes": []}
    transcript = None
    try:
        transcript = runner.run(sc, channel_id)
    except MonitorDown:
        raise  # live_run marks this and every later scenario a flake
    except (Flake, sut.SUTError) as e:
        result.update(status="flake", error=f"infrastructure: {e}")
    except discord_rest.DiscordError as e:
        if e.transient:
            result.update(status="flake", error=f"Discord: {e}")
        else:
            result.update(status="fail", error=f"Discord: {e}")
    except Exception as e:
        result.update(status="fail",
                      error=f"harness error: {type(e).__name__}: {e}")
    if transcript is not None:
        transcript = redact(transcript)
        write_json(os.path.join(out_dir, "transcripts", f"{sc['id']}.json"),
                   transcript)
        result["checks"] = checks.evaluate(sc, transcript)
        if use_judge and int(sc.get("tier", 1)) == 2:
            result["judge"] = judge.judge(sc, transcript)
        result["status"] = scenario_status(sc, result["checks"],
                                           result["judge"])
    result["notes"] = list(runner.notes)
    result["duration"] = round(time.time() - t0, 1)
    return redact(result)


def live_run(args, scenarios):
    env = {k: os.environ[k].strip() for k in REQUIRED_ENV}
    guild_id = env["E2E_GUILD_ID"]
    sut_bot = discord_rest.Bot(env["E2E_SUT_TOKEN"])
    bot_a = discord_rest.Bot(env["E2E_DRIVER_A_TOKEN"])
    bot_b = discord_rest.Bot(env["E2E_DRIVER_B_TOKEN"])
    results = []
    channels = {}
    the_sut = None
    worker = None
    run_dir = tempfile.mkdtemp(prefix="muse-e2e-")
    try:
        try:
            me_sut, me_a, me_b = sut_bot.me(), bot_a.me(), bot_b.me()
        except discord_rest.DiscordError as e:
            raise Flake(f"identity lookup failed: {e}")
        ids = {"SUT": me_sut["id"], "A": me_a["id"], "B": me_b["id"],
               "SUT_NAME": me_sut.get("username") or "muse"}
        log(f"SUT {ids['SUT_NAME']} ({ids['SUT']}), A {ids['A']}, "
            f"B {ids['B']}")
        cleanup_stale_channels(bot_a, guild_id)
        tag = run_tag(args.tree)
        for sc in scenarios:
            ch = bot_a.create_channel(guild_id, channel_name(tag, sc["id"]))
            channels[sc["id"]] = ch["id"]
            log(f"created #{ch.get('name')} ({ch['id']})")
        base = sut.base_config(guild_id, ids["A"], ids["B"])
        first = sut.scenario_config(base, {}, ids["A"], ids["B"])
        the_sut = sut.SUT(args.tree, run_dir, first,
                          token=env["E2E_SUT_TOKEN"])
        the_sut.start(gateway=True)
        worker = start_worker(args.worker, the_sut.pkg_dir,
                              os.path.join(run_dir, "worker.log"))
        the_sut.wait_ready(READY_TIMEOUT)
        log("SUT ready")
        runner = Runner(the_sut, {"a": bot_a, "b": bot_b}, bot_a, ids, base)
        for i, sc in enumerate(scenarios):
            log(f"scenario {sc['id']} ({i + 1}/{len(scenarios)})")
            r = run_one(runner, sc, channels[sc["id"]], args.out,
                        use_judge=True)
            log(f"  -> {r['status']}{': ' + r['error'] if r['error'] else ''}")
            results.append(r)
            if worker.poll() is not None:
                raise Flake(f"{args.worker} worker exited (code "
                            f"{worker.returncode})")
    except (Flake, sut.SUTError, discord_rest.DiscordError) as e:
        log(f"infrastructure trouble: {e}")
        done = {r["scenario"] for r in results}
        status = "flake"
        if isinstance(e, discord_rest.DiscordError) and not e.transient:
            status = "fail"  # e.g. 403 creating channels: setup is broken
        for sc in scenarios:
            if sc["id"] not in done:
                results.append(redact({
                    "scenario": sc["id"], "tier": int(sc.get("tier", 1)),
                    "status": status, "checks": [], "judge": None,
                    "duration": 0.0, "error": str(e)}))
    finally:
        stop_worker(worker)
        if the_sut is not None:
            try:
                the_sut.cleanup()
            except Exception as e:
                log(f"SUT stop failed: {e}")
        if not args.keep_channels:
            for sid, cid in channels.items():
                try:
                    bot_a.delete_channel(cid)
                except Exception as e:
                    log(f"could not delete channel for {sid}: {e}")
        try:
            copy_logs(run_dir, args.out)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
        write_outputs(args.out, results, args.tier)
    return exit_code(results)


def _on_sigterm(signum, frame):
    # A cancelled CI job: unwind through the finally blocks (stop the
    # SUT, delete the channels) instead of dying mid-run.
    raise SystemExit(130)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="muse-discord end-to-end test runner")
    ap.add_argument("--tree", default=".",
                    help="checkout whose monitor.py is under test")
    ap.add_argument("--tier", type=int, choices=(1, 2), required=True)
    ap.add_argument("--worker", choices=("fake", "claude"), default=None,
                    help="default: fake for tier 1, claude for tier 2")
    ap.add_argument("--scenario", action="append", default=[],
                    help="run only this scenario id (repeatable)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-channels", action="store_true")
    ap.add_argument("--scenario-dir", default=SCENARIO_DIR,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.worker = args.worker or ("fake" if args.tier == 1 else "claude")
    args.out = os.path.abspath(args.out)
    args.tree = os.path.abspath(args.tree)

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing and not args.dry_run:
        log(f"skipped: secrets missing ({', '.join(missing)})")
        os.makedirs(args.out, exist_ok=True)
        try:
            listed = select(scenario.load_all(args.scenario_dir,
                                              tier=args.tier),
                            args.scenario)
        except Exception:
            listed = []
        results = [{"scenario": s["id"], "tier": int(s.get("tier", 1)),
                    "status": "skip", "checks": [], "judge": None,
                    "duration": 0.0, "error": "secrets missing"}
                   for s in listed]
        write_outputs(args.out, results, args.tier,
                      note="skipped: secrets missing")
        return 0

    try:
        scenarios = select(scenario.load_all(args.scenario_dir,
                                             tier=args.tier),
                           args.scenario)
    except scenario.ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        if not args.dry_run:
            os.makedirs(args.out, exist_ok=True)
            write_outputs(args.out, [], args.tier,
                          note=f"FAIL: scenarios do not load: {e}")
        return 1
    if args.dry_run:
        return dry_run(args, scenarios)
    if not scenarios:
        log(f"no tier {args.tier} scenarios to run")
        os.makedirs(args.out, exist_ok=True)
        write_outputs(args.out, [], args.tier, note="no scenarios")
        return 0
    os.makedirs(os.path.join(args.out, "transcripts"), exist_ok=True)
    signal.signal(signal.SIGTERM, _on_sigterm)
    return live_run(args, scenarios)


if __name__ == "__main__":
    sys.exit(main())
