#!/usr/bin/env python3
"""telemetry.py: anonymous, opt-out daily usage counts.

The monitor counts a few things in memory (messages seen, replies sent,
refusals by reason, ...), keeps them in telemetry.json rolled up per UTC
day, and at most once a day, a few hours after the day closed, posts each
closed day as one small JSON report to the maintainer's Cloudflare
Worker (telemetry/worker/). Nothing else, and nowhere else.

Never in a report: message content, names, any Discord id, tokens, file
paths, error messages, IP addresses (the Worker stores none), or any
time finer than the day. Member counts are rounded down to a power of 2.
README.md, "Privacy and usage stats", lists every field; build_payload
below is the one place a report is made, and `bin/telemetry.py show`
prints exactly what would be sent next.

Off when any of these holds (then nothing is sent, and the days it was
off are never sent later):
  - telemetry.usage_stats: off (config.yaml, a DM to the bot, or
    `bin/telemetry.py off` / `bin/settings.py set telemetry.usage_stats
    off --place defaults`);
  - the environment variable MUSE_DISCORD_TELEMETRY=off (the offline
    tests and the e2e harness set it);
Nothing is sent either while no endpoint is configured (TELEMETRY_URL
below, or MUSE_DISCORD_TELEMETRY_URL); the counts are still kept, so
`show` works.

The monitor must never block, crash or slow down because of this: every
Recorder method swallows its own errors, the send runs on a daemon
thread with a short timeout, and a failed send is tried again the next
day (unsent days older than KEEP_DAYS are dropped).

Standard library only.
"""
import json
import math
import os
import platform
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

SCHEMA = 1
# The maintainer's Worker (telemetry/worker). Empty = nothing is ever
# sent (counts are still kept locally).
TELEMETRY_URL = "https://muse-telemetry.clono.workers.dev"
REPORT_PATH = "/v1/report"
ENV_SWITCH = "MUSE_DISCORD_TELEMETRY"       # "off" turns it off
ENV_URL = "MUSE_DISCORD_TELEMETRY_URL"      # overrides TELEMETRY_URL
SETTING = "telemetry.usage_stats"

KEEP_DAYS = 7               # unsent closed days kept at most
FLUSH_SECS = 300            # counters reach telemetry.json this often
TICK_SECS = 30              # tick() does its work at most this often
JITTER_SECS = 3 * 3600      # the daily send waits up to this into the day
TIMEOUT_SECS = 10           # per request
LATENCY_MAX_SECS = 3600     # longer latencies count as this
MAX_CRASH_NAMES = 20
MAX_WAKES_BYTES = 1 << 20   # a wakes file past this is just dropped
REFUSAL_CODES = (
    "replies-off", "dm-off", "bots-ignore", "not-allowed", "dm-not-allowed",
    "cap", "bot-exchange", "suspicious-ignored", "predates-join", "no-send",
    "internal", "worker-skipped", "send-failed", "never-share", "other")
SOURCES = ("settings", "dm", "cli")
PRESETS = ("friends", "community", "work")
COUNTERS = ("messages_seen", "queued", "replies_sent", "quarantined",
            "wakes", "media_sent", "restarts")
OS_FAMILIES = ("linux", "darwin", "windows", "freebsd", "openbsd")
FEATURES = {
    # name: allowed values (anything else is reported as "other")
    "server_replies": ("off", "when-addressed", "conversational"),
    "dm_replies": ("off", "when-addressed", "conversational"),
    "dms_from": ("owners", "shared-servers", "anyone", "list", "nobody"),
    "memory": ("everything", "public-only", "nothing"),
    "suspicious": ("hold-for-review", "ignore", "answer"),
    "bots": ("ignore", "when-addressed"),
    "length": ("brief", "normal", "detailed"),
    "daily_cap": ("on", "off"),
    "per_person_cap": ("on", "off"),
    "presence": ("on", "off"),
    "update_notices": ("on", "off"),
    "media": ("on", "off"),
}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}"
                      r"-[0-9a-f]{12}$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SEMVER_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------
def utc_day(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,
                                  timezone.utc).strftime("%Y-%m-%d")


def day_start(day):
    """The epoch second a UTC day ("YYYY-MM-DD") starts."""
    return datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp()


def env_off(environ=None):
    v = (os.environ if environ is None else environ).get(ENV_SWITCH, "")
    return v.strip().lower() in ("off", "0", "false", "no", "disabled")


def endpoint(environ=None):
    """The report URL. MUSE_DISCORD_TELEMETRY_URL, when set at all,
    replaces TELEMETRY_URL; set to "" it means no endpoint (the tests
    rely on this, so they can never reach the real Worker)."""
    env = os.environ if environ is None else environ
    url = (env[ENV_URL] if ENV_URL in env else TELEMETRY_URL or "").strip()
    return url.rstrip("/") if url.startswith(("https://", "http://")) \
        else ""


def enabled(st, environ=None):
    """On unless the env var or the setting turns it off. st: a
    muse_settings.Settings (None: the setting's default, on)."""
    if env_off(environ):
        return False
    if st is None:
        return True
    try:
        return bool(st.top(SETTING))
    except Exception:
        return True


def members_bucket(total):
    """Total members rounded down to a power of 2 (0 stays 0)."""
    try:
        n = int(total)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return 1 << int(math.floor(math.log2(n)))


def percentile(hist, q):
    """The q-th percentile (0-1) of a {seconds: count} histogram, in
    whole seconds; None when it is empty."""
    pairs = sorted((int(k), int(v)) for k, v in (hist or {}).items()
                   if int(v) > 0)
    total = sum(v for _k, v in pairs)
    if not total:
        return None
    rank = max(1, math.ceil(q * total))
    seen = 0
    for k, v in pairs:
        seen += v
        if seen >= rank:
            return k
    return pairs[-1][0]


def os_family():
    s = (platform.system() or "").lower()
    return s if s in OS_FAMILIES else "other"


def python_minor():
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def installed_version(pkg_dir, timeout=5):
    """The newest vX.Y.Z release tag HEAD contains (as watchdog.sh
    decides it), "unreleased" when there is none, "unknown" when git
    cannot tell."""
    try:
        out = subprocess.run(
            ["git", "-C", pkg_dir, "tag", "--merged", "HEAD", "--list",
             "v*"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if out.returncode != 0:
        return "unknown"
    tags = [t for t in out.stdout.split() if _SEMVER_RE.match(t)]
    if not tags:
        return "unreleased"
    return max(tags, key=lambda t: tuple(int(x) for x in
                                         _SEMVER_RE.match(t).groups()))


def build_info(pkg_dir, version, timeout=5):
    """(ahead, modified) for a release version: commits HEAD is past that
    tag, and whether tracked code (*.py, *.sh) has uncommitted edits.
    persona.md and the config are the owner's to edit, so they don't
    count. (0, False) when git cannot tell or there is no release."""
    if not (version and _SEMVER_RE.match(version)):
        return 0, False
    git = ["git", "-C", pkg_dir]
    try:
        n = subprocess.run(git + ["rev-list", "--count", f"{version}..HEAD"],
                           capture_output=True, text=True, timeout=timeout)
        st = subprocess.run(git + ["status", "--porcelain",
                                   "--untracked-files=no", "--",
                                   "*.py", "*.sh"],
                            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 0, False
    ahead = int(n.stdout.strip()) if n.returncode == 0 \
        and n.stdout.strip().isdigit() else 0
    return min(ahead, 100_000), st.returncode == 0 and bool(st.stdout.strip())


def new_day(day):
    return {"day": day, **{c: 0 for c in COUNTERS}, "refusals": {},
            "latency": {}, "settings_changes": {}, "presets": {},
            "crashes": {}, "gauges": {}}


def _bump(d, key, n=1):
    d[key] = int(d.get(key, 0)) + n


def features(st, ms):
    """Bot-wide defaults in force, as enums only (FEATURES)."""
    out = {}
    if st is None or ms is None:
        return out

    def get(key, scope):
        try:
            return st.get(key, scope)
        except Exception:
            return None

    def onoff(v):
        return "on" if v else "off"

    E, D, A = ms.Scope.everywhere(), ms.Scope.dm(), ms.Scope("all_servers")
    dms_from = get("who.dms_from", D)
    raw = {
        "server_replies": get("who.replies", A),
        "dm_replies": get("who.replies", D),
        "dms_from": "list" if isinstance(dms_from, list) else dms_from,
        "memory": get("privacy.memory", E),
        "suspicious": get("privacy.suspicious", E),
        "bots": get("who.bots", E),
        "length": get("style.length", E),
        "daily_cap": onoff(get("limits.per_day", E) is not None),
        "per_person_cap": onoff(
            get("limits.per_person_per_day", E) is not None),
        "media": onoff(get("media.enabled", A) or get("media.enabled", E)),
    }
    try:
        raw["presence"] = onoff(st.presence_enabled)
        raw["update_notices"] = onoff(st.updates_notify)
    except Exception:
        pass
    for name, allowed in FEATURES.items():
        v = raw.get(name)
        if v is None:
            continue
        out[name] = v if isinstance(v, str) and v in allowed else "other"
    return out


def _is_id(v):
    return isinstance(v, str) and v.isdigit() and 1 <= len(v) <= 25


def change_source(h):
    """"settings" (/settings: an owner or server admin by user id), "dm"
    (an owner's plain-language DM), or "cli" (bin/settings.py, the
    owners' Muse, bin/telemetry.py)."""
    g = str(h.get("group") or "")
    if g.startswith("nl-"):
        return "dm"
    if g.startswith("cli-"):
        return "cli"
    return "settings" if _is_id(str(h.get("by") or "")) else "cli"


def preset_of(lines, st, ms):
    """The preset (friends | community | work) one history group applied,
    else None: at least two changes, all at one server, each the value
    that preset gives that key."""
    if len(lines) < 2 or ms is None:
        return None
    server, picks = None, []
    for h in lines:
        parts = str(h.get("key") or "").split(".")
        if len(parts) < 4 or parts[0] != "servers" or parts[2] == "channels":
            return None
        if server not in (None, parts[1]):
            return None
        server = parts[1]
        picks.append((".".join(parts[2:]), h.get("to")))
    presets = getattr(ms, "PRESETS", {}) or {}
    for name, (_title, values) in presets.items():
        if name not in PRESETS:
            continue
        ok = True
        for key, to in picks:
            if key not in values:
                ok = False
                break
            want = values[key]
            if isinstance(want, tuple):
                want = want[1]
            try:
                spec = st.spec(key)
                a = ms.normalize(spec, to)
                b = ms.normalize(spec, want)
                same = a[0] and b[0] and a[1] == b[1]
            except Exception:
                same = to == want
            if not same:
                ok = False
                break
        if ok:
            return name
    return None


def count_history(rec, lines, st, ms):
    """Add settings changes (by source) and presets applied to rec."""
    groups = {}
    for h in lines:
        if "to" not in h or h.get("retired") or h.get("folded"):
            continue
        _bump(rec.setdefault("settings_changes", {}), change_source(h))
        if h.get("group") and not h.get("undo"):
            groups.setdefault(h["group"], []).append(h)
    for g in groups.values():
        name = preset_of(g, st, ms)
        if name:
            _bump(rec.setdefault("presets", {}), name)


def build_payload(rec, install_id):
    """The report for one closed day: every field, and nothing else (the
    Worker refuses anything it does not know)."""
    g = rec.get("gauges") or {}
    refusals = {k: int(v) for k, v in (rec.get("refusals") or {}).items()
                if k in REFUSAL_CODES and int(v) > 0}
    crashes = {k: int(v) for k, v in (rec.get("crashes") or {}).items()
               if _NAME_RE.match(str(k)) and int(v) > 0}
    lat = rec.get("latency") or {}
    version = str(g.get("version") or "unknown")
    release = bool(_SEMVER_RE.match(version))
    if not release and version != "unreleased":
        version = "unknown"
    os_ = g.get("os") or os_family()
    feats = {k: v for k, v in (g.get("features") or {}).items()
             if k in FEATURES and (v in FEATURES[k] or v == "other")}
    return {
        "schema": SCHEMA,
        "install_id": install_id,
        "day": rec["day"],
        "version": version,
        # Past the release, or edited: lets reports from main or a
        # customized copy be told apart from the release itself.
        "ahead": max(0, min(int(g.get("ahead") or 0), 100_000))
        if release else 0,
        "modified": release and bool(g.get("modified")),
        "python": str(g.get("python") or python_minor())[:8],
        "os": os_ if os_ in OS_FAMILIES else "other",
        "servers": max(0, int(g.get("servers") or 0)),
        "members_bucket": members_bucket(g.get("members") or 0),
        "messages_seen": int(rec.get("messages_seen", 0)),
        "queued": int(rec.get("queued", 0)),
        "replies_sent": int(rec.get("replies_sent", 0)),
        "quarantined": int(rec.get("quarantined", 0)),
        "refusals": refusals,
        "wakes": int(rec.get("wakes", 0)),
        "latency_p50": percentile(lat, 0.5),
        "latency_p90": percentile(lat, 0.9),
        "settings_changes": {k: int(v) for k, v in
                             (rec.get("settings_changes") or {}).items()
                             if k in SOURCES and int(v) > 0},
        "presets_applied": {k: int(v) for k, v in
                            (rec.get("presets") or {}).items()
                            if k in PRESETS and int(v) > 0},
        "media_sent": int(rec.get("media_sent", 0)),
        "restarts": int(rec.get("restarts", 0)),
        "crashes": crashes,
        "features": feats,
    }


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def atomic_write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(obj, f, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_file(path):
    """telemetry.json as a dict (a missing or unreadable file: empty)."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_install_id(path, create=True):
    """The random install id (uuid4) in path; a new one when the file is
    missing or holds anything else (create=False: None instead)."""
    try:
        with open(path) as f:
            v = f.read().strip()
        if _UUID_RE.match(v):
            return v
    except OSError:
        pass
    if not create:
        return None
    return reset_install_id(path)


def reset_install_id(path):
    v = str(uuid.uuid4())
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(v + "\n")
    os.replace(tmp, path)
    return v


def drain_wakes(path):
    """How many wakes hooks/mention-watch.sh noted in path (one line
    each) since the last call; the file is taken away first, so a wake
    noted meanwhile lands in a new file."""
    taken = path + ".taking"
    try:
        os.replace(path, taken)
    except OSError:
        return 0
    try:
        if os.path.getsize(taken) > MAX_WAKES_BYTES:
            return 0
        with open(taken, "rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0
    finally:
        try:
            os.unlink(taken)
        except OSError:
            pass


def post_json(url, obj, timeout=TIMEOUT_SECS):
    """POST obj as JSON; the HTTP status (an HTTPError's too). Raises
    for anything else (no connection, a timeout)."""
    body = json.dumps(obj, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"muse-discord-telemetry/{SCHEMA}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


# ---------------------------------------------------------------------------
# The monitor's recorder
# ---------------------------------------------------------------------------
def _safe(fn):
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception as e:
            self._errors += 1
            if self._errors <= 3:
                print(f"Telemetry: {type(e).__name__} (ignored)",
                      flush=True)
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Recorder:
    """The monitor's counters. paths() gives (telemetry.json, the install
    id file, the wakes file), read on every use so tests can move them.
    Every method is safe to call from any thread and never raises."""

    def __init__(self, paths, ms=None, pkg_dir=None, clock=time.time,
                 post=post_json, environ=None):
        self.paths = paths
        self.ms = ms
        self.pkg_dir = pkg_dir
        self.clock = clock
        self.post = post
        self.environ = environ
        self.lock = threading.Lock()
        self.data = None          # telemetry.json, loaded on first use
        self.version = None
        self.ahead, self.modified = 0, False
        self._errors = 0
        self._last_tick = 0.0
        self._last_flush = 0.0
        self._flushed_day = None
        self._hooked = False
        self._sending = None      # the send thread
        self._result = None       # (day attempted, [sent days], [dropped])

    # -- state -------------------------------------------------------------
    def _load(self):
        if self.data is not None:
            return self.data
        d = load_file(self.paths()[0])
        today = utc_day(self.clock())
        rec = d.get("today")
        if not isinstance(rec, dict) or not _DAY_RE.match(
                str(rec.get("day") or "")):
            rec = new_day(today)
        base = new_day(rec["day"])
        base.update(rec)
        d["today"] = base
        pend = d.get("pending")
        d["pending"] = [p for p in pend if isinstance(p, dict)
                        and _DAY_RE.match(str(p.get("day") or ""))] \
            if isinstance(pend, list) else []
        self.data = d
        self._roll(today)
        return d

    def _today(self):
        d = self._load()
        today = utc_day(self.clock())
        if d["today"]["day"] != today:
            self._roll(today)
        return d["today"]

    def _roll(self, today):
        """Close the stored day when it is not today: queued for sending
        unless telemetry was off at any point that day."""
        d = self.data
        rec = d["today"]
        if rec["day"] >= today:
            return
        if not rec.get("off"):
            d["pending"] = [p for p in d["pending"]
                            if p["day"] != rec["day"]] + [rec]
        d["pending"] = sorted(d["pending"], key=lambda p: p["day"])
        d["pending"] = [p for p in d["pending"]
                        if day_start(p["day"]) >= day_start(today)
                        - KEEP_DAYS * 86400][-KEEP_DAYS:]
        d["today"] = new_day(today)

    def flush(self):
        """Write telemetry.json now (atomically)."""
        with self.lock:
            self._flush_locked()

    def _flush_locked(self):
        if self.data is None:
            return
        atomic_write_json(self.paths()[0], self.data)
        self._last_flush = self.clock()

    # -- counting (any thread) ------------------------------------------------
    @_safe
    def start(self):
        """At monitor start: one restart, the version (found on a
        thread: git can be slow), and thread crashes counted."""
        with self.lock:
            _bump(self._today(), "restarts")
        threading.Thread(target=self._find_version, daemon=True,
                         name="telemetry-version").start()
        if self._hooked:
            return
        self._hooked = True
        prev = threading.excepthook

        def hook(args):
            if args.exc_type is not None and not issubclass(
                    args.exc_type, (SystemExit, KeyboardInterrupt)):
                self.crashed(args.exc_type, flush=False)
            prev(args)

        threading.excepthook = hook

    def _find_version(self):
        try:
            self.version = installed_version(self.pkg_dir or ".")
            self.ahead, self.modified = build_info(self.pkg_dir or ".",
                                                   self.version)
        except Exception:
            self.version = "unknown"

    @_safe
    def count(self, name, n=1):
        if name not in COUNTERS:
            return
        with self.lock:
            _bump(self._today(), name, n)

    @_safe
    def ledger(self, code, before=None):
        """A mention-ledger outcome (monitor.ledger_code) for one message
        or burst: replied, queued, quarantined, or why it was skipped.
        before: the burst's ledger entries before this write; when every
        one already had this outcome, it is not counted again."""
        if before and all(isinstance(e, dict) and e.get("code") == code
                          for e in before):
            return
        with self.lock:
            rec = self._today()
            if code == "replied":
                _bump(rec, "replies_sent")
            elif code in ("queued", "quarantined"):
                _bump(rec, code)
            else:
                _bump(rec["refusals"], code if code in REFUSAL_CODES
                      else "other")

    @_safe
    def latency(self, secs):
        """Seconds from a message to the reply that answered it."""
        s = min(LATENCY_MAX_SECS, max(0, int(round(float(secs)))))
        with self.lock:
            _bump(self._today()["latency"], str(s))

    @_safe
    def crashed(self, exc, flush=True):
        """An exception that stopped the monitor or one of its threads:
        its class name only (never its message)."""
        if exc is None or isinstance(exc, (SystemExit, KeyboardInterrupt)):
            return
        cls = exc if isinstance(exc, type) else type(exc)
        if issubclass(cls, (SystemExit, KeyboardInterrupt)):
            return
        name = cls.__name__ if _NAME_RE.match(cls.__name__) else "Other"
        with self.lock:
            crashes = self._today()["crashes"]
            if name not in crashes \
                    and len(crashes) >= MAX_CRASH_NAMES - 1:
                name = "Other"
            _bump(crashes, name)
            if flush:
                self._flush_locked()

    # -- the main loop ----------------------------------------------------------
    @_safe
    def tick(self, settings=None, guilds=None, member_counts=None,
             force=False):
        """Main loop, every iteration (cheap: does its work at most every
        TICK_SECS). settings: a callable giving the current Settings;
        guilds: the servers the bot is in; member_counts: {id: count}."""
        now = self.clock()
        if not force and now - self._last_tick < TICK_SECS:
            return
        self._last_tick = now
        st = settings() if callable(settings) else settings
        on = enabled(st, self.environ)
        wakes = drain_wakes(self.paths()[2])
        with self.lock:
            d = self._load()
            rec = self._today()
            if wakes:
                _bump(rec, "wakes", wakes)
            if not on:
                rec["off"] = True
                d["pending"] = []   # days of an install that is off now
            if st is not None and self.ms is not None:
                lines, d["history_mark"] = self.ms.history_since(
                    getattr(st, "overlay_doc", None), d.get("history_mark"))
                count_history(rec, lines, st, self.ms)
            counts = [v for v in (member_counts or {}).values()
                      if isinstance(v, int) and v > 0]
            rec["gauges"] = {
                "servers": len(guilds or ()),
                "members": sum(counts),
                "features": features(st, self.ms),
                "version": self.version or rec.get("gauges", {}).get(
                    "version"),
                "ahead": self.ahead, "modified": self.modified,
                "python": python_minor(), "os": os_family()}
            self._take_result(d, now)
            if on and self.released() and endpoint(self.environ) \
                    and d["pending"] and self._sending is None \
                    and self._send_due(d, now):
                self._start_send(d, now)
            if force or now - self._last_flush >= FLUSH_SECS \
                    or self._flushed_day != rec["day"]:
                self._flushed_day = rec["day"]
                self._flush_locked()

    def released(self):
        """Only a release (a vX.Y.Z tag) sends: a dev
        checkout, an unreleased main or an unknown version never does."""
        return bool(self.version and _SEMVER_RE.match(self.version))

    def _send_due(self, d, now):
        """One attempt per UTC day, after a random time within the first
        JITTER_SECS of it (drawn once per day)."""
        today = utc_day(now)
        if d.get("last_attempt") == today:
            return False
        sched = d.get("send_after")
        if not isinstance(sched, (int, float)) \
                or utc_day(sched) != today:
            sched = day_start(today) + random.uniform(0, JITTER_SECS)
            d["send_after"] = sched
        return now >= sched

    def _start_send(self, d, now):
        d["last_attempt"] = utc_day(now)
        install_id = read_install_id(self.paths()[1])
        payloads = [build_payload(p, install_id) for p in d["pending"]]
        url = endpoint(self.environ) + REPORT_PATH
        attempt = utc_day(now)

        def run():
            sent, dropped = [], []
            try:
                for p in payloads:
                    try:
                        status = self.post(url, p)
                    except Exception:
                        break       # offline / timeout: try tomorrow
                    if 200 <= status < 300:
                        sent.append(p["day"])
                    elif 400 <= status < 500 and status != 429:
                        dropped.append(p["day"])  # refused: never again
                    else:
                        break
            finally:
                with self.lock:
                    self._result = (attempt, sent, dropped)

        t = threading.Thread(target=run, daemon=True, name="telemetry-send")
        self._sending = t
        t.start()

    def _take_result(self, d, now):
        if self._result is None:
            return
        attempt, sent, dropped = self._result
        self._result = None
        self._sending = None
        gone = set(sent) | set(dropped)
        d["pending"] = [p for p in d["pending"] if p["day"] not in gone]
        d["last_send"] = {"day": attempt, "sent": len(sent),
                          "refused": len(dropped),
                          "ok": not d["pending"]}
        self._flush_locked()
