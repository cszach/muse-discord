"""sut.py -- lifecycle of the system under test (the PR's monitor.py).

    cfg = base_config(guild_id, a_id, b_id)
    s = SUT(tree, run_dir, cfg, token=os.environ["E2E_SUT_TOKEN"])
    s.start()                 # gateway=True
    s.wait_ready(180)
    s.restart(gateway=False)  # REST-only mode (see below)
    s.stop()

SUT copies the PR checkout (minus .git and runtime files) into
<run_dir>/pkg and runs monitor.py from there, so the PR's own code,
bin/ tools, and plugins are what gets tested. The token goes to
<run_dir>/sut_token (mode 600), outside pkg, and config.yaml points at
it; monitor.py's stdout and stderr append to <pkg>/monitor.log.

Ready signal. The monitor prints, in order: "Bot user id: <id>"
(identity), "muse-discord monitor starting." (channel discovery done,
so the scenario channels, created before the start, are known and
their watermarks are seeded right after), then the gateway thread's
outcome: "Gateway READY: realtime ingest live." or, in REST-only mode,
"GATEWAY DISABLED (...)". (When /users/@me fails at boot, the identity
line comes later, as "Bot user id: <id> (from gateway READY)" or from
an in-loop retry.) wait_ready() waits for the identity line, the
discovery line, and the gateway line for the requested mode, all in
log output written since this start. Every message sent after that is
either delivered by the gateway or read by the next REST poll.

Config. config.test.yaml is in the settings layout muse_settings.py
reads (owners, token_from, everywhere/dms/servers, engine);
effective_config() adds the fields the harness always controls.

Overlay. <pkg>/config.discord.yaml is what /settings writes (and what
a scenario's `settings` step writes, through the PR's own
muse_settings.py). It is never copied from the tree, and run.py
deletes it before every scenario (the running monitor reloads; a
restart only when it does not), so a live change never leaks into the
next one.

What the SUT never does. config.yaml always turns updates off
(updates.notify off), so the monitor never DMs the owners (driver A)
about a release; the pkg copy also leaves out update_check.json and
update_request.json.
Nothing here runs watchdog.sh or hooks/mention-watch.sh: the harness
starts and stops monitor.py itself, so the wake hook's restart of a
dead monitor (watchdog.sh --from-hook) never fights a kill_monitor
step. (A real install's hook or watchdog on the same machine does not
see the SUT either: their pgrep matches only `python3 monitor.py`
started from a package dir, and the SUT runs as `<python path>
<pkg>/monitor.py`.)

Gateway off. monitor.py imports `websockets` inside the gateway thread
only, and when that thread dies it logs "GATEWAY DISABLED (thread
died: ...)" and keeps REST polling. start(gateway=False) puts a
directory holding a stub `websockets` package whose import raises
ImportError first on PYTHONPATH, so the product runs its own degraded
mode with no code change.
"""
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import yaml

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(E2E_DIR, "config.test.yaml")
LOG_NAME = "monitor.log"
# The overlay /settings writes (muse_settings.OVERLAY_NAME). Never
# copied from the tree; the runner deletes it between scenarios.
OVERLAY_NAME = "config.discord.yaml"
# The REST poll floor in monitor.py; REST-only scenarios rely on it.
REST_POLL_SECONDS = 10
# Not copied from the tree: VCS data, the developer's own config and
# runtime files, and harness output.
COPY_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "config.yaml", OVERLAY_NAME, "config.yaml.bak-*",
    "state.json", "reply_queue.json",
    "reply_outbox*.json", "reply.lock", ".*.tmp", "quarantine.json",
    "mention_ledger.json", "monitor.pid", "monitor.heartbeat",
    "watchdog_state.json", "watchdog.lock", "update_check.json",
    "update_request.json", "directory.json", "owner_dm_nonces.json",
    "owner_items.json", "item_nonces.json", "prompt_outdated.json",
    "proactive.json", "proactive.json.bad-*", "proactive_nonce.json",
    "nl_journal.json", "telemetry.json", "telemetry_id", "telemetry.wakes",
    "watches.json", "watches.json.bad-*", "watches.lock",
    "web_watches_due.json", "web_watch_results.json",
    "*.log", "*.log.1", "media", "generated", "out", ".venv", ".seen-*",
    "worker-prompt.md", "watchdog-hook.out.*")
READY_IDENTITY = "Bot user id: "
READY_DISCOVERY = "muse-discord monitor starting."
READY_GATEWAY_ON = "Gateway READY: realtime ingest live."
READY_GATEWAY_OFF = "GATEWAY DISABLED"
# Lines after which the monitor will not become ready.
FATAL_MARKERS = ("TOKEN REJECTED", "Another monitor holds the pidfile",
                 "No bot token")
# Secrets the monitor never needs in its environment.
SECRET_ENV = ("E2E_SUT_TOKEN", "E2E_DRIVER_A_TOKEN", "E2E_DRIVER_B_TOKEN",
              "CLAUDE_CODE_OAUTH_TOKEN", "MUSE_DISCORD_CONFIG")
STUB_WEBSOCKETS = (
    '"""e2e stub: makes monitor.py run without its gateway."""\n'
    'raise ImportError("websockets disabled by the e2e harness '
    '(gateway_off)")\n')


class SUTError(Exception):
    """The SUT died, would not start, or did not become ready (the
    runner reports these as flakes)."""


def deep_merge(base, over):
    """base with over merged in (dicts recursively; anything else,
    lists included, replaced). Neither input is modified."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_template(path=DEFAULT_TEMPLATE):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def render_config(template, values):
    """Replace every string that is exactly "{{KEY}}" with values[KEY],
    mapping keys included (servers."{{GUILD_ID}}"). List entries whose
    placeholder has no value (missing or None) are dropped; any other
    unresolved placeholder value becomes None (an unresolved key stays
    as written)."""
    def render_key(k):
        ph = _placeholder(k)
        if ph and values.get(ph) is not None:
            return str(values[ph])
        return k

    def walk(node):
        if isinstance(node, dict):
            return {render_key(k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node
                    if not (_placeholder(v)
                            and values.get(_placeholder(v)) is None)]
        key = _placeholder(node)
        if key:
            val = values.get(key)
            return str(val) if val is not None else None
        return node
    return walk(template)


def _placeholder(node):
    if isinstance(node, str) and node.startswith("{{") \
            and node.endswith("}}"):
        return node[2:-2].strip()
    return None


def guild_of(cfg):
    """The test server id a rendered config names (engine.guild_ids),
    or None."""
    ids = ((cfg or {}).get("engine") or {}).get("guild_ids")
    return str(ids[0]) if ids else None


def base_config(guild_id, a_id, b_id, template=None):
    """The rendered base test config (token and persona paths are
    filled in by SUT)."""
    if template is None:
        template = load_template(DEFAULT_TEMPLATE)
    return render_config(template, {"GUILD_ID": guild_id, "A_ID": a_id,
                                    "B_ID": b_id})


def scenario_config(base, scenario, a_id, b_id, channel_id=None):
    """The config one scenario runs under: its `config:` overrides
    (placeholders {{GUILD_ID}}, {{A_ID}}, {{B_ID}} and {{CHANNEL_ID}},
    the scenario's own channel, rendered) deep-merged over base, and
    driver B left out of testing.treat_as_human when the scenario sets
    b_is_bot (unless the overrides set treat_as_human themselves)."""
    guild_id = guild_of(base)
    over = render_config(scenario.get("config") or {},
                         {"GUILD_ID": guild_id, "A_ID": a_id,
                          "B_ID": b_id, "CHANNEL_ID": channel_id})
    cfg = deep_merge(base, over)
    if "treat_as_human" not in (over.get("testing") or {}):
        humans = [str(a_id)]
        if not scenario.get("b_is_bot"):
            humans.append(str(b_id))
        cfg.setdefault("testing", {})["treat_as_human"] = humans
    return cfg


class SUT:
    """One monitor.py process tree under <run_dir>/pkg. Not thread-safe;
    the runner drives it from one thread."""

    def __init__(self, tree, run_dir, base_config, token=None,
                 python=sys.executable):
        self.tree = os.path.abspath(tree)
        self.run_dir = os.path.abspath(run_dir)
        self.pkg_dir = os.path.join(self.run_dir, "pkg")
        self.log_path = os.path.join(self.pkg_dir, LOG_NAME)
        self.token_file = os.path.join(self.run_dir, "sut_token")
        self.python = python
        self.proc = None
        self.gateway = True
        self._log_offset = 0
        os.makedirs(self.run_dir, exist_ok=True)
        if os.path.exists(self.pkg_dir):
            shutil.rmtree(self.pkg_dir)
        shutil.copytree(self.tree, self.pkg_dir, ignore=COPY_IGNORE)
        token = token if token is not None \
            else os.environ.get("E2E_SUT_TOKEN", "")
        fd = os.open(self.token_file,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token.strip() + "\n")
        os.chmod(self.token_file, 0o600)
        self.persona_file = self._persona()
        self._stub_dir = os.path.join(self.run_dir, "no-gateway")
        os.makedirs(os.path.join(self._stub_dir, "websockets"),
                    exist_ok=True)
        with open(os.path.join(self._stub_dir, "websockets",
                               "__init__.py"), "w") as f:
            f.write(STUB_WEBSOCKETS)
        self.base_config = copy.deepcopy(base_config)
        self.config = None
        self.write_config(self.base_config)

    def _persona(self):
        """The test persona (e2e/fixtures/persona.md in the copied tree,
        or a one-line stand-in when the tree has none)."""
        path = os.path.join(self.pkg_dir, "e2e", "fixtures", "persona.md")
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("You are muse-test, a brief, friendly test bot.\n")
        return path

    def effective_config(self, cfg):
        """cfg plus the fields the harness always controls."""
        cfg = deep_merge(cfg, {
            "token_from": self.token_file,
            "presence": {"enabled": "off"},
            "updates": {"notify": "off"},
            "persona": self.persona_file,
        })
        cfg.setdefault("engine", {})["rest_poll_seconds"] = \
            REST_POLL_SECONDS
        return cfg

    def write_config(self, cfg):
        """Write <pkg>/config.yaml (effective_config(cfg)). Takes effect
        on the next start."""
        cfg = self.effective_config(cfg)
        self.config = cfg
        with open(os.path.join(self.pkg_dir, "config.yaml"), "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

    # --- process control ---

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, gateway=True, config=None):
        """Start monitor.py (gateway=False: REST-only). config, when
        given, replaces config.yaml first. Does not wait for ready."""
        if self.alive():
            raise SUTError("monitor already running")
        if config is not None:
            self.write_config(config)
        self.gateway = gateway
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env["MUSE_DISCORD_CONFIG"] = os.path.join(self.pkg_dir,
                                                  "config.yaml")
        env["PYTHONUNBUFFERED"] = "1"
        # Never send usage stats from a test run (telemetry.py).
        env["MUSE_DISCORD_TELEMETRY"] = "off"
        env["MUSE_DISCORD_TELEMETRY_URL"] = ""   # "" = no endpoint at all
        if not gateway:
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in (self._stub_dir, env.get("PYTHONPATH")) if p)
        try:
            self._log_offset = os.path.getsize(self.log_path)
        except OSError:
            self._log_offset = 0
        log = open(self.log_path, "a")
        try:
            log.write(f"===== e2e: start monitor (gateway="
                      f"{'on' if gateway else 'off'}) "
                      f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
                      f" =====\n")
            log.flush()
            self.proc = subprocess.Popen(
                [self.python, os.path.join(self.pkg_dir, "monitor.py")],
                cwd=self.pkg_dir, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        finally:
            log.close()

    def stop(self, kill=False, timeout=15):
        """Stop the monitor: SIGTERM (then SIGKILL after timeout), or
        SIGKILL at once with kill=True. A no-op when it is not running."""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            sig = signal.SIGKILL if kill else signal.SIGTERM
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=10)
        self.proc = None

    def restart(self, gateway=None, config=None, kill=False):
        """stop() then start(); gateway None keeps the current mode."""
        self.stop(kill=kill)
        self.start(gateway=self.gateway if gateway is None else gateway,
                   config=config)

    def wait_ready(self, timeout=180, poll=0.5):
        """Block until this start's log shows the ready signal (see the
        module docstring). Raises SUTError when the monitor exits, logs
        a fatal marker, or times out."""
        want = [READY_IDENTITY, READY_DISCOVERY,
                READY_GATEWAY_ON if self.gateway else READY_GATEWAY_OFF]
        deadline = time.time() + timeout
        while True:
            text = self._log_since_start()
            for marker in FATAL_MARKERS:
                if marker in text:
                    raise SUTError(f"monitor failed to start: {marker}")
            if all(w in text for w in want):
                return True
            if self.proc is None or self.proc.poll() is not None:
                code = None if self.proc is None else self.proc.returncode
                raise SUTError(f"monitor exited before ready (code "
                               f"{code})")
            if time.time() >= deadline:
                missing = [w for w in want if w not in text]
                raise SUTError(f"monitor not ready after {timeout}s "
                               f"(missing: {', '.join(missing)})")
            time.sleep(poll)

    def _log_since_start(self):
        return self.log_since(self._log_offset)

    def log_size(self):
        """The current size of monitor.log in bytes (0 when missing): a
        mark for log_since()."""
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def log_since(self, offset, max_bytes=None):
        """monitor.log written after byte offset (all starts since),
        keeping only the last max_bytes when given."""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            return ""
        if max_bytes is not None and len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", "replace")

    # --- files ---

    def read_json(self, name):
        """Parsed <pkg>/<name>, or None when missing or unreadable."""
        try:
            with open(os.path.join(self.pkg_dir, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def read_yaml(self, name):
        """Parsed <pkg>/<name> (YAML), or None when missing or
        unreadable."""
        try:
            with open(os.path.join(self.pkg_dir, name)) as f:
                return yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            return None

    def log_tail(self, n=200):
        """The last n lines of monitor.log (all starts)."""
        try:
            with open(self.log_path, "rb") as f:
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

    def cleanup(self):
        """Stop the monitor and remove the token file."""
        try:
            self.stop()
        finally:
            try:
                os.unlink(self.token_file)
            except OSError:
                pass
