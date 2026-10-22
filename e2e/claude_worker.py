#!/usr/bin/env python3
"""claude_worker.py -- tier-2 e2e reply worker: Claude Code, headless.

Plays the production reply worker against a test package dir. It does
what hooks/mention-watch.sh does in a Muse install (poll the queue,
hand each open item to exactly one worker run, re-hand it only after
its lease runs out; owner DMs wake alone, with their owner_dm_nonce,
and so do the owners' server items, a command with its
owner_command_nonce)
and what the worker session does (follow worker-prompt.template.md),
with `claude -p` as the agent:

    python3 e2e/claude_worker.py --pkg <pkg_dir> [--log FILE]

Runs until SIGTERM. Each wake renders the PR's own
worker-prompt.template.md with bin/render-worker-prompt.py, as SETUP.md
step 5 does (persona from the wake payload, no media tools, default
judgment), appends the same payload the wake hook builds (the items
plus the persona text), and runs the CLI in the package dir with a
hard timeout. Every wake's duration, exit code, and how it ended go to
the worker log; the CLI's JSON output for each wake is kept in
claude_wakes/ next to it.

The template has the worker append each reply as soon as it is
drafted (one outbox-append call per item) and end the run right after
the last one with the single line `done` (or `done; flag: <id> ...`).
The log's `ending=` field says which: `done`, `flag`, or `other` (a
run that wrapped up with something else, which the template forbids).

Not replicated: the hook's monitor-liveness check (it starts
`watchdog.sh --from-hook` when no monitor runs). The harness owns the
monitor's lifecycle, and a restart from here would undo a scenario's
kill_monitor step.

Tool restriction: the CLI gets only the Bash and Read tools, in
`dontAsk` permission mode (anything not explicitly allowed is denied,
nobody is prompted), with no user/project settings and no MCP servers.
A PreToolUse hook (this script, run with --hook) is the allowlist:
Bash may run only `python3 <pkg>/bin/outbox-append.py` (with a quoted
heredoc or a file argument; each call is checked on its own, so the
worker may run it once per item) and `python3 <pkg>/bin/discord-api.py
<simple arguments>`; Read may open only media/ and generated/ files,
the queue, and the persona. A hook is needed because Claude Code's own
`Bash(...)` allow rules never match a heredoc command, and the
template tells the worker to append through a heredoc.

The CLI binary is $CLAUDE_BIN (default `claude`), so tests can swap in
a fake script; $E2E_CLAUDE_MODEL picks a model (default: the CLI's).
Auth comes from the environment (CLAUDE_CODE_OAUTH_TOKEN in CI).
"""
import argparse
import importlib.util
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PERSONA = os.path.join(HERE, "fixtures", "persona.md")

# The wake hook's numbers: an item handed to a worker is not handed out
# again for LEASE_SECS (it is re-handed after that, in case the worker
# died). The hook polls every 5 s.
LEASE_SECS = 600
POLL_SECS = 5
WAKE_TIMEOUT = 240

# Queue item fields the wake payload carries (mention-watch.sh's jq
# projection, in its order; a missing field is null there too).
PAYLOAD_FIELDS = (
    "message_id", "channel_id", "channel_name", "kind", "author",
    "author_id", "content", "media", "documents", "unviewable", "history",
    "may_generate", "owner_command", "reply_to", "burst",
    "burst_fragments", "from_bot", "bot_turns", "participants",
    "follow_up", "settings", "watches", "reminder", "proactive")

# Env vars the worker run has no business seeing. The SUT token stays:
# bin/discord-api.py may resolve it through token_from (env:NAME).
SCRUB_ENV = ("E2E_DRIVER_A_TOKEN", "E2E_DRIVER_B_TOKEN")

# The installed prompt is rendered by the same code a Muse install uses
# (bin/render-worker-prompt.py, SETUP.md step 5): persona from the wake
# payload, no media tools, default judgment. The package's own copy
# when it has one (the PR's code), else this checkout's.
RENDERER = "render-worker-prompt.py"
_RENDERERS = {}


def renderer(pkg):
    path = os.path.join(pkg, "bin", RENDERER)
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(HERE), "bin", RENDERER)
    if path not in _RENDERERS:
        spec = importlib.util.spec_from_file_location(
            "render_worker_prompt", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RENDERERS[path] = mod
    return _RENDERERS[path]


def claude_bin():
    return os.environ.get("CLAUDE_BIN") or "claude"


def model_args():
    model = os.environ.get("E2E_CLAUDE_MODEL")
    return ["--model", model] if model else []


def read_json(path, default):
    """Parsed JSON file, or default when it is missing or unreadable
    (the monitor writes atomically, so a bad read is a real problem,
    not a half-written file; the next poll tries again)."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# What to hand out (mention-watch.sh, in Python)
# ---------------------------------------------------------------------------

# The monitor's private {message_id: nonce} file for open owner DMs.
OWNER_DM_NONCES = "owner_dm_nonces.json"
# ... and for open owner commands (bin/discord-api.py's capability), and
# every open item's reply_nonce.
OWNER_ITEMS = "owner_items.json"
ITEM_NONCES = "item_nonces.json"
# How long an item waits for a nonce the monitor has yet to write.
NONCE_WAIT_SECS = 60


def is_owner_dm(item):
    """An owner's own DM, a reminder or web watch firing in one, or a
    heartbeat slot (all handed out alone, with their nonce: the hook's
    rule)."""
    card = item.get("settings") or {}
    notice = item.get("kind") in ("reminder", "watch-met") \
        and card.get("where") == "dm"
    return item.get("kind") == "proactive" or (
        (item.get("kind") == "dm" or notice)
        and card.get("speaker") == "owner")


def is_owner_command(item):
    return item.get("owner_command") is True


def outbox_answers(pkg):
    """[(ids, nonce, reply nonce)] for every outbox entry (queued for
    sending or being sent): the message ids it names (item, reply_to),
    the owner-DM nonce it carries (owner_dm_nonce, else its settings
    action's nonce) and its reply_nonce."""
    out = []
    for name in ("reply_outbox.json", "reply_outbox.processing.json"):
        data = read_json(os.path.join(pkg, name), [])
        for e in data if isinstance(data, list) else []:
            if not isinstance(e, dict):
                continue
            s = e.get("settings") if isinstance(e.get("settings"),
                                                dict) else {}
            nonce = next((n for n in (e.get("owner_dm_nonce"),
                                      s.get("nonce"))
                          if isinstance(n, str)), None)
            reply = e.get("reply_nonce")
            out.append(({e[k] for k in ("item", "reply_to")
                         if isinstance(e.get(k), str)}, nonce,
                        reply if isinstance(reply, str) else None))
    return out


def item_ids(item):
    """The item's own id plus its burst fragments' ids."""
    ids = [item.get("message_id")]
    for frag in item.get("burst_fragments") or []:
        if isinstance(frag, dict):
            ids.append(frag.get("message_id"))
    return [i for i in ids if isinstance(i, str)]


def open_items(pkg, answered=None):
    """(queue items no outbox entry answers yet, the owner-DM nonces).
    Read in the hook's order: the outbox BEFORE the queue (the monitor
    removes an answered item from the queue before it drops the item's
    outbox entry, so this order never sees one without the other), the
    nonces after it (the monitor writes a DM's nonce before it queues
    the DM). An item counts as answered only by an entry that carries
    its reply_nonce, and an owner's DM only by one that also carries its
    nonce: anything else is a forgery the monitor refuses.
    `answered` (a set) remembers the ids of items found answered, so
    they stay answered once the monitor takes the outbox."""
    entries = outbox_answers(pkg)
    queue = read_json(os.path.join(pkg, "reply_queue.json"), [])
    nonces = read_json(os.path.join(pkg, OWNER_DM_NONCES), {})
    nonces = nonces if isinstance(nonces, dict) else {}
    pro = read_json(os.path.join(pkg, "proactive_nonce.json"), {})
    nonces.update(pro if isinstance(pro, dict) else {})   # a live slot's
    replies = read_json(os.path.join(pkg, ITEM_NONCES), {})
    replies = replies if isinstance(replies, dict) else {}
    out = []
    for item in queue if isinstance(queue, list) else []:
        if not isinstance(item, dict) or item.get("message_id") is None:
            continue
        ids = item_ids(item)
        if answered is not None and any(i in answered for i in ids):
            continue
        n = nonces.get(str(item["message_id"]))
        r = None if item.get("kind") == "proactive" \
            else replies.get(str(item["message_id"]))   # (a slot: its nonce)
        hits = [nonce for named, nonce, reply in entries
                if any(i in named for i in ids)
                and (r is None or reply == r)]
        if n is not None:
            hits = [x for x in hits if x == n]
        elif is_owner_dm(item):
            hits = []
        if hits:
            if answered is not None:
                answered.update(ids)
            continue
        out.append(item)
    return out, nonces


def persona_file(pkg, override=None):
    """The persona path: the override (the e2e test persona) when given,
    else config.yaml `persona` (old layout: persona.file) relative to
    the package dir, default persona.md, the way the wake hook resolves
    it."""
    if override:
        return override
    name = None
    try:
        import yaml
        with open(os.path.join(pkg, "config.yaml")) as f:
            name = (yaml.safe_load(f) or {}).get("persona")
        if isinstance(name, dict):
            name = name.get("file")
        if not isinstance(name, str):
            name = None
    except Exception:
        name = None
    return os.path.join(pkg, os.path.expanduser(name or "persona.md"))


def build_payload(items, persona, caps=None):
    """The wake payload: {count, persona, items} with the hook's fields,
    plus the capabilities `caps` holds for each item ({"dm", "cmd",
    "reply"}: {message_id: nonce}; the caller passes only what this wake
    may get): owner_dm_nonce, owner_command_nonce (owner commands only)
    and reply_nonce."""
    caps = caps or {}
    out = []
    for item in items:
        p = {k: item.get(k) for k in PAYLOAD_FIELDS}
        mid = str(item.get("message_id"))
        if (caps.get("dm") or {}).get(mid):
            p["owner_dm_nonce"] = caps["dm"][mid]
        if is_owner_command(item) and (caps.get("cmd") or {}).get(mid):
            p["owner_command_nonce"] = caps["cmd"][mid]
        if (caps.get("reply") or {}).get(mid):
            p["reply_nonce"] = caps["reply"][mid]
        out.append(p)
    return {"count": len(items), "persona": persona, "items": out}


# ---------------------------------------------------------------------------
# The prompt (worker-prompt.template.md, rendered as SETUP.md step 5 does)
# ---------------------------------------------------------------------------

def render_prompt(template, pkg):
    """The installed worker prompt for package dir `pkg` (no media
    tools), plus warnings for seams the template no longer has (the PR
    may have changed it; the prompt is then used as it is)."""
    return renderer(pkg).render(template, pkg, media="none")


def run_ending(result):
    """How a run ended, from the CLI's final message: "done" (the
    template's one line), "flag" (the one line `done; flag: ...`), or
    "other" (anything else, a recap or a second line included)."""
    text = str(result or "").strip()
    if "\n" in text:
        return "other"
    text = text.strip("`*_ .")
    if text.lower() == "done":
        return "done"
    if re.match(r"done\s*;\s*flag\b", text, re.I):
        return "flag"
    return "other"


def wake_prompt(rendered, payload):
    return (rendered.rstrip() + "\n\n---\n\n"
            "Wake: muse-discord mentions pending. Payload:\n\n"
            "```json\n" + json.dumps(payload, indent=1, ensure_ascii=False)
            + "\n```\n")


# ---------------------------------------------------------------------------
# The tool allowlist (the PreToolUse hook)
# ---------------------------------------------------------------------------

# A plain argument: no quoting, globbing, or expansion.
_PLAIN_ARG = r"[A-Za-z0-9_./:@+=,-]+"
# Unquoted shell syntax that makes a command more than one simple command.
_SHELL_META = set(";&|<>()$`\n\r")


def simple_command(cmd):
    """True when cmd is one simple command: words, quoted strings, no
    operators, redirections, substitutions, or variable expansion."""
    quote = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == '"':
                quote = None
            elif c in "$`":
                return False
            elif c == "\\":
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c in _SHELL_META:
            return False
        i += 1
    return quote is None


def _tool_prefixes(pkg, name):
    return ["python3 %s/bin/%s" % (pkg, name), "python3 bin/%s" % name,
            "python3 ./bin/%s" % name]


def bash_allowed(pkg, cmd):
    """(allowed, reason) for a Bash command from the worker."""
    cmd = cmd.strip()
    first, _, rest = cmd.partition("\n")
    first = first.rstrip()
    for prefix in _tool_prefixes(pkg, "outbox-append.py"):
        # Items in a file: `python3 .../outbox-append.py FILE`.
        if re.fullmatch(re.escape(prefix) + r"\s+" + _PLAIN_ARG, cmd):
            return True, "outbox append"
        # Items in a quoted heredoc (no expansion inside), and nothing
        # after the terminator.
        m = re.fullmatch(re.escape(prefix)
                         + r"\s*<<(-?)\s*(['\"])(\w+)\2", first)
        if m and rest:
            lines = rest.split("\n")
            strip = (lambda s: s.lstrip("\t")) if m.group(1) else str
            body = [strip(ln).rstrip() for ln in lines]
            delim = m.group(3)
            if body[-1] == delim and delim not in body[:-1]:
                return True, "outbox append"
    for prefix in _tool_prefixes(pkg, "discord-api.py"):
        if cmd.startswith(prefix + " ") and simple_command(cmd):
            return True, "owner-command tool"
    return False, (
        "Only two commands are allowed, each run on its own: "
        "`python3 %s/bin/outbox-append.py <<'EOF'` (a quoted heredoc "
        "holding the JSON array) and `python3 %s/bin/discord-api.py "
        "<subcommand> <args>` (no pipes, redirections, `cd`, `&&`, or "
        "$-expansions)." % (pkg, pkg))


def wake_reads(items):
    """What a wake of these items may Read: {"media": [the files its
    items list], "items": [their message_ids] (their generated/ dirs)}.
    Never the queue file: it holds every open item's text, other
    people's DMs and the owner's included, and the payload carries this
    wake's items already."""
    media, ids = [], []
    for item in items or []:
        ids.append(str(item.get("message_id")))
        for m in item.get("media") or []:
            if isinstance(m, dict) and isinstance(m.get("path"), str):
                media.append(m["path"])
    return {"media": media, "items": ids}


def read_allowed(pkg, path, persona=None, reads=None):
    """(allowed, reason) for a Read of `path`: the persona file, and the
    current wake's own media files and generated/<its items>/ (reads,
    from wake_reads)."""
    rp = os.path.realpath(os.path.join(pkg, os.path.expanduser(path or "")))
    root = os.path.realpath(pkg)
    reads = reads or {}
    media = {os.path.realpath(os.path.join(pkg, m))
             for m in reads.get("media") or []}
    if rp in media:
        return True, "this wake's media"
    for mid in reads.get("items") or []:
        gen = os.path.join(root, "generated", mid)
        if re.fullmatch(r"[0-9]+", mid) and rp.startswith(gen + os.sep):
            return True, "this wake's generated files"
    if rp == os.path.realpath(persona_file(pkg, persona)):
        return True, "persona"
    return False, ("Read is limited to the media files listed in this "
                   "wake's items (and the persona file when the payload "
                   "lacks it).")


def check_tool(pkg, tool, tool_input, persona=None, reads=None):
    """(allowed, reason) for one tool call."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        return bash_allowed(pkg, str(tool_input.get("command") or ""))
    if tool == "Read":
        return read_allowed(pkg, str(tool_input.get("file_path") or ""),
                            persona, reads)
    return False, "%s is not available to the reply worker." % tool


def hook_main(pkg, persona=None, reads=None):
    """PreToolUse hook: read the call from stdin, print the decision."""
    try:
        call = json.load(sys.stdin)
        ok, reason = check_tool(pkg, call.get("tool_name"),
                                call.get("tool_input"), persona,
                                json.loads(reads) if reads else None)
    except Exception as e:
        ok, reason = False, "hook error: %s" % e
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if ok else "deny",
        "permissionDecisionReason": reason}}))
    return 0


def worker_args(pkg, persona=None, items=None):
    """The CLI flags for one wake of `items` (the prompt goes on stdin):
    its Read allowlist is those items' own files (wake_reads)."""
    hook = " ".join(shlex.quote(a) for a in
                    [sys.executable, os.path.abspath(__file__), "--hook",
                     "--pkg", pkg, "--reads",
                     json.dumps(wake_reads(items))]
                    + (["--persona", persona] if persona else []))
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]}}
    return [claude_bin(), "-p", "--output-format", "json",
            "--no-session-persistence", "--setting-sources", "",
            "--strict-mcp-config", "--tools", "Bash,Read",
            "--permission-mode", "dontAsk",
            "--settings", json.dumps(settings)] + model_args()


# ---------------------------------------------------------------------------
# Running wakes
# ---------------------------------------------------------------------------

def kill_group(proc):
    """Stop a CLI run and everything it started (its own session)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


class Worker:
    """The wake loop. poll() hands open, unleased items to a new wake
    (each wake runs in its own thread, so a slow wake never delays the
    next message, as with the real hook)."""

    def __init__(self, pkg, log_path=None, timeout=WAKE_TIMEOUT,
                 lease=LEASE_SECS, persona=DEFAULT_PERSONA, echo=False):
        self.pkg = os.path.abspath(pkg)
        self.echo = echo  # also print log lines to stdout
        self.log_path = log_path or os.path.join(self.pkg,
                                                 "claude_worker.log")
        self.wake_dir = os.path.join(os.path.dirname(self.log_path),
                                     "claude_wakes")
        self.timeout = timeout
        self.lease = lease
        self.persona = persona if persona and os.path.exists(persona) \
            else None
        self.leases = {}       # message_id -> when a wake took it
        self.answered = set()  # every id ever seen answered: never re-hand
        self.lock = threading.Lock()
        self.threads = []
        self.procs = set()
        self.wakes = 0
        self.stopping = False

    def log(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg)
        if self.echo:
            print(line, flush=True)
        try:
            with open(self.log_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def due(self):
        """(items for the next wake, their capabilities for
        build_payload): open items no live lease covers, as the hook
        picks them. An item whose reply_nonce, an owner DM whose nonce,
        or an owner command whose nonce the monitor has not written yet
        waits, unleased (the reply and command ones for at most
        NONCE_WAIT_SECS after it was queued). Owner DMs wake alone (real
        DMs before notices); else one owner's server item wakes alone;
        the rest wait for the next poll. Refreshes the leases (kept only
        for items still queued, like the hook's state)."""
        now = time.time()
        items, nonces = open_items(self.pkg, self.answered)

        def load(name):
            data = read_json(os.path.join(self.pkg, name), {})
            return os.path.exists(os.path.join(self.pkg, name)), \
                data if isinstance(data, dict) else {}
        cmd_file, cmds = load(OWNER_ITEMS)
        cmds = {k: (v.get("nonce") if isinstance(v, dict) else v)
                for k, v in cmds.items()}
        reply_file, replies = load(ITEM_NONCES)
        queued = set()
        for item in read_json(os.path.join(self.pkg, "reply_queue.json"),
                              []) or []:
            if isinstance(item, dict) and isinstance(item.get("message_id"),
                                                     str):
                queued.add(item["message_id"])

        def waits(i):
            mid = str(i["message_id"])
            if is_owner_dm(i) and mid not in nonces:
                return True
            age = now - (i.get("queued_at") if isinstance(
                i.get("queued_at"), (int, float)) else 0)
            return age < NONCE_WAIT_SECS and (
                (reply_file and i.get("kind") != "proactive"
                 and mid not in replies)
                or (is_owner_command(i) and cmd_file and not cmds.get(mid)))
        with self.lock:
            self.leases = {k: v for k, v in self.leases.items()
                           if k in queued}
            due = [i for i in items
                   if self.leases.get(i["message_id"], 0) + self.lease <= now
                   and not waits(i)]
            owner = [i for i in due if is_owner_dm(i)]
            caps = {"reply": replies}
            if owner:
                # The hook's rule: the owner's own messages first, else a
                # heartbeat slot alone, else the bot's notices to them.
                dms = [i for i in owner if i.get("kind") == "dm"]
                slots = [i for i in owner if i.get("kind") == "proactive"]
                owner = dms or slots[:1] or owner
                due = owner
                caps.update(dm=nonces, cmd=cmds)
            else:
                # An owner's server item (owner powers) wakes alone (the
                # hook's rule).
                held = [i for i in due if is_owner_command(i) or (
                    i.get("settings") or {}).get("speaker") == "owner"]
                if held:
                    due = held[:1]
                    caps.update(cmd=cmds)
            expired = [i for i in due if i["message_id"] in self.leases]
            for i in due:
                self.leases[i["message_id"]] = now
        if expired:
            self.log("re-waking: item lease expired for %s"
                     % [i["message_id"] for i in expired])
        return due, caps

    def poll(self):
        """One hook pass: start a wake for the due items, if any."""
        if self.stopping:
            return None
        items, caps = self.due()
        if not items:
            return None
        with self.lock:
            self.wakes += 1
            n = self.wakes
        t = threading.Thread(target=self.run_wake,
                             args=(n, items, caps), daemon=True)
        self.threads.append(t)
        t.start()
        return t

    def run_wake(self, n, items, caps=None):
        ids = [i["message_id"] for i in items]
        try:
            with open(os.path.join(self.pkg,
                                   "worker-prompt.template.md")) as f:
                template = f.read()
        except OSError as e:
            self.log("wake %d: cannot read the worker template: %s" % (n, e))
            return
        rendered, warnings = render_prompt(template, self.pkg)
        for w in warnings:
            self.log("wake %d: template warning: %s" % (n, w))
        try:
            with open(persona_file(self.pkg, self.persona)) as f:
                persona = f.read()
        except OSError:
            persona = None  # the worker then reads the file itself
        prompt = wake_prompt(rendered, build_payload(items, persona, caps))
        args = worker_args(self.pkg, self.persona, items)
        self.log("wake %d: start items=%s" % (n, ids))
        start = time.monotonic()
        env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        try:
            proc = subprocess.Popen(
                args, cwd=self.pkg, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
        except OSError as e:
            self.log("wake %d: done exit=error duration=0.0s items=%s "
                     "(cannot start %s: %s)" % (n, ids, args[0], e))
            return
        with self.lock:
            self.procs.add(proc)
        status = None
        try:
            out, err = proc.communicate(prompt, timeout=self.timeout)
            status = proc.returncode
        except subprocess.TimeoutExpired:
            kill_group(proc)
            out, err = proc.communicate()
            status = "timeout"
        finally:
            with self.lock:
                self.procs.discard(proc)
        duration = time.monotonic() - start
        summary = ""
        try:
            res = json.loads(out)
            summary = " turns=%s is_error=%s ending=%s result=%r" % (
                res.get("num_turns"), res.get("is_error"),
                run_ending(res.get("result")),
                str(res.get("result") or "")[:200])
        except (ValueError, AttributeError):
            if out.strip():
                summary = " stdout=%r" % out.strip()[:200]
        if status not in (0, "timeout") and err.strip():
            summary += " stderr=%r" % err.strip()[-300:]
        self.log("wake %d: done exit=%s duration=%.1fs items=%s%s"
                 % (n, status, duration, ids, summary))
        try:
            os.makedirs(self.wake_dir, exist_ok=True)
            with open(os.path.join(self.wake_dir, "wake-%03d.json" % n),
                      "w") as f:
                json.dump({"items": ids, "exit": status,
                           "duration": round(duration, 1), "stdout": out,
                           "stderr": err[-4000:]}, f, indent=1)
        except OSError:
            pass

    def wait(self, timeout=None):
        for t in list(self.threads):
            t.join(timeout)
        self.threads = [t for t in self.threads if t.is_alive()]

    def shutdown(self):
        self.stopping = True
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            kill_group(p)
        self.wait(10)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pkg", required=True, help="the SUT package dir")
    ap.add_argument("--log", help="worker log (default "
                    "<pkg>/claude_worker.log)")
    ap.add_argument("--timeout", type=float, default=WAKE_TIMEOUT,
                    help="seconds per wake before the CLI is killed")
    ap.add_argument("--lease", type=float, default=LEASE_SECS)
    ap.add_argument("--poll", type=float, default=POLL_SECS)
    ap.add_argument("--persona", default=DEFAULT_PERSONA,
                    help="persona file (default: the e2e test persona; "
                    "'' = config.yaml persona)")
    ap.add_argument("--hook", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--reads", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    pkg = os.path.abspath(args.pkg)
    if args.hook:
        return hook_main(pkg, args.persona or None, args.reads)

    worker = Worker(pkg, args.log, args.timeout, args.lease, args.persona,
                    echo=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker.log("claude worker up: pkg=%s claude=%s timeout=%ss persona=%s"
               % (pkg, claude_bin(), args.timeout, worker.persona or
                  "config.yaml persona"))
    while not stop.is_set():
        try:
            worker.poll()
        except Exception as e:  # keep polling; a bad pass is logged
            worker.log("poll error: %s" % e)
        stop.wait(args.poll)
    worker.log("stopping")
    worker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
