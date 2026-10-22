#!/usr/bin/env python3
"""muse-discord reminders and watches.

One concept for people ("reminders and watches": one list, one cancel),
three kinds inside:

    remind      at a time, or after a delay
    watch       a Discord watch: words or phrases in the messages the
                monitor already reads (gateway and poll)
    watch-web   a web watch: an outside-world condition ("the venue
                announces the date") a checker session of the owner's
                Muse checks on the web, every so often (see "Web
                watches" below; owners only)

This module is the data model, the outbox action's shape and meaning,
the caps, the matching and the store. It makes no Discord calls:
monitor.py does the I/O (who can read which channel, the queue, the
sends) and calls in here; bin/watches.py (the owner's CLI) uses the
Store. Standard library only.

Security properties (PROTOCOL.md, Reminders and watches):
- The requester is always the author of the queue item the action
  answers, from the monitor's own record of it, never a field the
  worker writes; the place is that item's channel. A reminder can only
  come back to that person, in that place.
- Watches only ever match in channels the requester can read (the
  monitor computes it from roles and overwrites, again at match time),
  never the requester's own messages or the bot's, at most once per
  watch per DEBOUNCE_SECS, and link the message instead of quoting it.
- Matching is case-insensitive literal words and phrases (escaped,
  bounded): no user-supplied regular expressions.
- A web watch's condition is stored as data and handed to the checker
  only inside a delimited data block; a check result counts only with
  that check's one-time nonce (128 bits, compared in constant time,
  spent on use, expiring after WEB_CHECK_SECS) for a watch still
  active, and checks are budgeted per day.
"""
import hmac
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone

VERSION = 1
KINDS = ("remind", "watch")       # an `add`'s kind (a web watch is a
                                  # watch with "check": "web")

# Limits (PROTOCOL.md, Reminders and watches).
REMIND_MIN_SECS = 60                    # at least a minute away
REMIND_MAX_SECS = 366 * 86400           # at most a year away
AFTER_MAX = 100 * REMIND_MAX_SECS       # a sane number (decide refuses
                                        # past a year, with its line)
WATCH_DEFAULT_SECS = 7 * 86400          # a watch runs a week by default
WATCH_MAX_SECS = 30 * 86400             # and 30 days at most
WATCH_MIN_SECS = 60
# Owners have no limits. Everyone else: one silent ceiling across all
# kinds (a backstop against trolls and bad parses, not a setting), and
# each fire counts as a reply toward limits.* where it fires (monitor).
PER_PERSON_MAX = 25
TOTAL_MAX = 2000      # everyone but the owners, a bound on the file
HELD_RETRY_SECS = 600   # a fire held by a reply cap is tried this often
HELD_KEEP_SECS = 7 * 86400
LATE_SECS = 3600      # a reminder this overdue fires once, marked late
DEBOUNCE_SECS = 15 * 60   # one notification per watch per 15 minutes
FIRE_KEEP_SECS = 24 * 3600  # a fire nobody answers is dropped after this
DONE_KEEP_SECS = 48 * 3600  # schedule outcomes, so one is applied once
FIRED_KEEP = 500
NOTE_MAX = 300
WORDS_MAX = 5
WORD_MIN, WORD_MAX = 2, 60
CHANGES_MAX = 5
LIST_MAX = 25         # items listed on a queue item
TZ_MAX_USERS = 5000
# Web watches.
WEB_EVERY_MIN = 1800            # checked at most every 30 minutes
WEB_EVERY_MAX = 7 * 86400
WEB_TOTAL_PER_DAY = 500         # checks a day in all: a safety budget,
                                # checked when issuing (web watches are
                                # the owners' only)
WEB_CHECK_SECS = 15 * 60        # a check's nonce lives this long
WEB_MAX_FAILS = 5               # unknown / no report in a row: it ends
WEB_REPEAT_SECS = 6 * 3600      # "every time": quiet this long after one
WEB_BACKOFF_MAX = 86400
WHAT_MAX = SUMMARY_MAX = 300
RESULTS = ("met", "not-met", "unknown", "refused")

ID_RE = re.compile(r"[1-9][0-9]{0,19}")
ITEM_ID_RE = re.compile(r"[rw][a-z0-9]{5}")
TZ_RE = re.compile(r"(UTC|[A-Za-z_]{1,32}(/[A-Za-z0-9_+\-]{1,32}){1,2})")
PLACE_RE = re.compile(r"here|server|server:([1-9][0-9]{0,19})"
                      r"|channel:([1-9][0-9]{0,19}):([1-9][0-9]{0,19})")
NONCE_RE = re.compile(r"[0-9a-f]{32,64}")
ADD_KEYS = {"op", "kind", "at", "after", "note", "match", "until"}
WEB_KEYS = {"op", "kind", "check", "what", "every", "until", "note",
            "repeat"}
MATCH_KEYS = {"words", "in", "every"}
NOT_ON_SCHEDULE_ITEMS = ("silent", "settings", "attachments")
_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

# The monitor's own reply when it does not do what the worker asked:
# all or nothing, so each says nothing changed.
LINES = {
    "not-allowed": "Only my owners can set reminders and watches here, "
                   "so nothing changed.",
    "bot": "I don't set reminders or watches for bots.",
    "cap-person": "You have {cap} reminders and watches going; cancel "
                  "one first.",
    "cap-total": "I can't set that: I'm keeping as many reminders and "
                 "watches as I can right now.",
    "time": "I couldn't set that: a reminder has to be between a minute "
            "and a year away, so nothing changed.",
    "watch-time": "I couldn't set that: a watch can run for 30 days at "
                  "most, so nothing changed.",
    "place": "I couldn't set that watch: {why} Nothing changed.",
    "cancel": "I couldn't find that among your reminders and watches, so "
              "nothing changed.",
    "tz": "I don't know that time zone, so nothing changed.",
    "channel": "I can only set reminders and watches where you asked, so "
               "nothing changed.",
    "unknown": "I couldn't set that, so nothing changed.",
    "web-not-allowed": "Only my owners can set web watches, so nothing "
                       "changed.",
}
# Ledger reasons: monitor.ledger_code maps the prefixes to codes.
REASON_CAP = "reminders: limit reached"          # -> watch-cap
REASON_NOT_ALLOWED = "reminders: not allowed"    # -> watch-not-allowed


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def parse_utc(value):
    """Epoch seconds for an ISO-8601 UTC time ("...Z" or "+00:00"), else
    None. Times in another offset, or with none, are refused: the
    worker converts, the protocol only takes UTC."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
            return None
        ts = dt.timestamp()
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    # Only times the protocol could ever take (decide checks the range).
    return ts if 0 < ts < 4102444800 else None   # before 2100


def iso(ts):
    """Epoch seconds as ISO-8601 UTC, to the second ("...Z")."""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def known_tz(name):
    """True when the IANA time zone exists here (zoneinfo), or when it
    cannot be checked (no zoneinfo / tzdata): the name is still just a
    label the worker reads back."""
    if not isinstance(name, str) or not TZ_RE.fullmatch(name):
        return False
    try:
        import zoneinfo
    except ImportError:
        return True
    try:
        zoneinfo.ZoneInfo(name)
        return True
    except Exception:
        try:
            return not zoneinfo.available_timezones()
        except Exception:
            return True


# ---------------------------------------------------------------------------
# The outbox action's shape (bin/outbox-append.py and the monitor)
# ---------------------------------------------------------------------------
def _is_id(v):
    return isinstance(v, str) and ID_RE.fullmatch(v) is not None


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _words_problem(words, where):
    if not isinstance(words, list) or not 1 <= len(words) <= WORDS_MAX:
        return f"{where}.words must be a list of 1 to {WORDS_MAX} words or " \
               f"phrases"
    for w in words:
        if not isinstance(w, str) or "\n" in w \
                or not WORD_MIN <= len(w.strip()) <= WORD_MAX:
            return f"{where}.words: each is one line of {WORD_MIN} to " \
                   f"{WORD_MAX} characters"
    return None


def _add_problems(c, where, keys):
    """Unknown fields and the note: what every add checks."""
    out = []
    extra = set(c) - keys
    if extra:
        out.append(f"{where} has unknown fields: "
                   f"{', '.join(sorted(map(str, extra)))} (the requester "
                   f"and place are always the item's own)")
    note = c.get("note")
    if note is not None and (not isinstance(note, str)
                             or len(note) > NOTE_MAX):
        out.append(f"{where}.note is text up to {NOTE_MAX} characters")
    return out


def _change_problems(c, where):
    if not isinstance(c, dict):
        return [f"{where} is not an object"]
    op = c.get("op")
    if op == "add" and c.get("check") is not None:
        return _web_problems(c, where)
    if op == "add":
        out = _add_problems(c, where, ADD_KEYS)
        kind = c.get("kind")
        if kind not in KINDS:
            return out + [f'{where}.kind must be "remind" or "watch"']
        if kind == "remind":
            if ("at" in c) == ("after" in c):
                out.append(f'{where}: a reminder takes "at" (ISO-8601 UTC) '
                           f'or "after" (seconds), one of them')
            elif "at" in c and parse_utc(c["at"]) is None:
                out.append(f'{where}.at must be an ISO-8601 UTC time such '
                           f'as "2026-10-21T21:36:00Z"')
            elif "after" in c and not (_int(c["after"])
                                       and 0 < c["after"] <= AFTER_MAX):
                out.append(f"{where}.after is a whole number of seconds "
                           f"(a reminder is at most a year away)")
            for k in ("match", "until"):
                if k in c:
                    out.append(f"{where}: a reminder takes no {k}")
            return out
        for k in ("at", "after"):
            if k in c:
                out.append(f"{where}: a watch takes no {k} (until, "
                           f"optional, ends it)")
        m = c.get("match")
        if not isinstance(m, dict) or "words" not in m or "in" not in m \
                or not set(m) <= MATCH_KEYS:
            return out + [f'{where}.match must be {{"words": [...], "in": '
                          f'"here" | "server" | "server:<id>" | '
                          f'"channel:<server id>:<channel id>", "every": '
                          f'true|false}}']
        p = _words_problem(m["words"], f"{where}.match")
        if p:
            out.append(p)
        if not isinstance(m["in"], str) or not PLACE_RE.fullmatch(m["in"]):
            out.append(f'{where}.match.in must be "here", "server", '
                       f'"server:<id>" or "channel:<server id>:<channel '
                       f'id>"')
        if "every" in m and not isinstance(m["every"], bool):
            out.append(f"{where}.match.every is true or false")
        if "until" in c and parse_utc(c["until"]) is None:
            out.append(f"{where}.until must be an ISO-8601 UTC time")
        return out
    if op == "cancel":
        if set(c) == {"op", "id"}:
            if isinstance(c["id"], str) and ITEM_ID_RE.fullmatch(c["id"]):
                return []
            return [f"{where}.id is a reminder or watch id from the item's "
                    f"watches list"]
        if set(c) == {"op", "all"} and c["all"] is True:
            return []
        return [f'{where} must be {{"op": "cancel", "id": "<id>"}} or '
                f'{{"op": "cancel", "all": true}}']
    if op == "tz":
        if set(c) != {"op", "tz"}:
            return [f'{where} must be {{"op": "tz", "tz": "<IANA name>"}} '
                    f'(null forgets it)']
        if c["tz"] is not None and not (isinstance(c["tz"], str)
                                        and TZ_RE.fullmatch(c["tz"])):
            return [f'{where}.tz is an IANA time zone such as '
                    f'"Europe/Berlin", or null']
        return []
    return [f'{where}.op must be "add", "cancel" or "tz"']


def _web_problems(c, where):
    out = _add_problems(c, where, WEB_KEYS)
    if c.get("kind") != "watch" or c.get("check") != "web":
        out.append(f'{where}: a web watch is "kind": "watch", "check": '
                   f'"web"')
    what = c.get("what")
    if not isinstance(what, str) or not 1 <= len(what.strip()) <= WHAT_MAX:
        out.append(f"{where}.what is the condition in the requester's "
                   f"words, up to {WHAT_MAX} characters")
    e = c.get("every")
    if not (_int(e) and WEB_EVERY_MIN <= e <= WEB_EVERY_MAX):
        out.append(f"{where}.every is the seconds between checks, "
                   f"{WEB_EVERY_MIN} to {WEB_EVERY_MAX}")
    if "until" in c and parse_utc(c["until"]) is None:
        out.append(f"{where}.until must be an ISO-8601 UTC time")
    if "repeat" in c and not isinstance(c["repeat"], bool):
        out.append(f"{where}.repeat is true or false")
    return out


def shape_problems(item):
    """What is wrong with an outbox item's `schedule` action, as
    sentences the worker can act on; [] when the shape is fine. Meaning
    (who asked, where, caps) is the monitor's: see Decider."""
    if not isinstance(item, dict) or "schedule" not in item:
        return []
    out = []
    if not _is_id(str(item.get("item") or "")):
        out.append('a schedule item needs "item" (the queue item\'s '
                   'message_id)')
    bad = [k for k in NOT_ON_SCHEDULE_ITEMS if k in item]
    if bad:
        out.append(f"a schedule item carries no {', '.join(bad)}: it is a "
                   f"normal reply (text or parts) plus the action")
    s = item["schedule"]
    if not isinstance(s, dict) or set(s) != {"v", "changes"}:
        return out + ['schedule must be {"v": 1, "changes": [...]}']
    if s["v"] != VERSION or isinstance(s["v"], bool):
        out.append("schedule.v must be 1")
    ch = s["changes"]
    if not isinstance(ch, list) or not 1 <= len(ch) <= CHANGES_MAX:
        return out + [f"schedule.changes must be a list of 1 to "
                      f"{CHANGES_MAX} changes"]
    for i, c in enumerate(ch):
        out += _change_problems(c, f"schedule.changes[{i}]")
    return out


# ---------------------------------------------------------------------------
# The store: watches.json, durable, monitor-internal (and bin/watches.py)
# ---------------------------------------------------------------------------
def empty():
    return {"v": VERSION, "items": {}, "fires": {}, "done": {}, "tz": {},
            "fired": {}, "web": {}, "held": {}}


def _clean(data):
    out = empty()
    if isinstance(data, dict):
        for k in out:
            if isinstance(data.get(k), dict):
                out[k] = data[k]
    return out


class Store:
    """watches.json. Reads are cached on the file's mtime and size (the
    monitor peeks at it every tick); every change is a read-modify-write
    under an flock on watches.lock, written atomically, so the owner's
    CLI and the monitor never lose each other's edits."""

    def __init__(self, path):
        self.path = path
        self.lock_path = os.path.join(os.path.dirname(path) or ".",
                                      "watches.lock")
        self._sig = None
        self._data = None

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _load(self):
        try:
            with open(self.path) as f:
                return _clean(json.load(f))
        except FileNotFoundError:
            return empty()
        except (OSError, ValueError) as e:
            # A damaged file is kept aside, never silently emptied.
            bad = f"{self.path}.bad-{int(time.time())}"
            with contextlib.suppress(OSError):
                os.replace(self.path, bad)
            print(f"watches.json unreadable ({e}); moved to "
                  f"{os.path.basename(bad)}", flush=True)
            return empty()

    def read(self):
        """The current data (do not modify it: use edit())."""
        sig = self._stat()
        if self._data is None or sig != self._sig:
            self._data = self._load()
            self._sig = sig
        return self._data

    @contextlib.contextmanager
    def edit(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self._write(data)
            self._data = data
            self._sig = self._stat()
        finally:
            os.close(fd)

    def _write(self, data):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".watches.json.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def new_id(data, kind):
    prefix = "r" if kind == "remind" else "w"
    while True:
        i = prefix + "".join(secrets.choice(_ALPHABET) for _ in range(5))
        if i not in data["items"]:
            return i


def active(data, now=None):
    """The live items (not past their expiry)."""
    now = time.time() if now is None else now
    return {i: r for i, r in data["items"].items() if isinstance(r, dict)
            and (r.get("kind") == "remind" or r.get("until", 0) > now)}


# ---------------------------------------------------------------------------
# Deciding a schedule action (all or nothing)
# ---------------------------------------------------------------------------
class Refused(Exception):
    def __init__(self, line, reason=None, detail=None):
        super().__init__(line)
        self.line = line
        self.reason = reason      # a ledger reason, or None (replied)
        self.detail = detail


class Request:
    """Who asked, where, and what the monitor knows about it. Built by
    the monitor from its own record of the queue item, never from the
    outbox item's fields. resolve(in_value) -> (scope dict, where text)
    or raises Refused (place checks need Discord: membership and
    channel permissions)."""

    def __init__(self, requester, name, owner, guild_id, channel_id, dm,
                 message_id, message_time, can_set, where, resolve,
                 can_web=False):
        self.requester = str(requester)
        self.name = name
        self.owner = bool(owner)
        self.guild_id = guild_id
        self.channel_id = str(channel_id)
        self.dm = bool(dm)
        self.message_id = str(message_id)
        self.message_time = message_time
        self.can_set = can_set
        self.where = where
        self.resolve = resolve
        self.can_web = can_web


def decide(data, req, changes, now=None):
    """Apply changes (a valid schedule.changes list) for req to data,
    all or nothing. Returns a list of what was done ([(op, id)]); raises
    Refused, leaving data untouched."""
    now = time.time() if now is None else now
    adds = [c for c in changes if c.get("op") == "add"]
    if not req.can_set and adds:
        raise Refused(LINES["not-allowed"], REASON_NOT_ALLOWED)
    if not (req.can_web and req.owner) \
            and any(c.get("check") == "web" for c in adds):
        raise Refused(LINES["web-not-allowed"], REASON_NOT_ALLOWED,
                      {"kind": "watch-web"})
    items = dict(data["items"])
    live = active(data, now)
    tz = dict(data["tz"])
    done = []
    for c in changes:
        op = c["op"]
        if op == "tz":
            if c["tz"] is None:
                tz.pop(req.requester, None)
            elif not known_tz(c["tz"]):
                raise Refused(LINES["tz"])
            else:
                tz[req.requester] = c["tz"]
            done.append(("tz", c["tz"]))
            continue
        if op == "cancel":
            if c.get("all"):
                ids = [i for i, r in items.items()
                       if r.get("requester") == req.requester and i in live]
            else:
                r = items.get(c["id"])
                if r is None or c["id"] not in live or not (
                        req.owner or r.get("requester") == req.requester):
                    raise Refused(LINES["cancel"])
                ids = [c["id"]]
            for i in ids:
                items.pop(i, None)
                live.pop(i, None)
                done.append(("cancel", i))
            continue
        kind = "watch-web" if c.get("check") == "web" else c["kind"]
        rec = {"kind": kind, "requester": req.requester, "name": req.name,
               "guild_id": None if req.dm else req.guild_id,
               "channel_id": req.channel_id, "dm": req.dm,
               "message_id": req.message_id, "created": now,
               "where": req.where,
               "note": (c.get("note") or "").strip()[:NOTE_MAX]}
        if kind == "remind":
            due = parse_utc(c["at"]) if "at" in c \
                else req.message_time + c["after"]
            delay = due - req.message_time
            if delay < REMIND_MIN_SECS or delay > REMIND_MAX_SECS \
                    or due - now > REMIND_MAX_SECS:
                raise Refused(LINES["time"])
            rec["due"] = due
        elif kind == "watch-web":
            until = parse_utc(c["until"]) if "until" in c \
                else now + WATCH_DEFAULT_SECS
            if until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
                raise Refused(LINES["watch-time"])
            rec.update({"until": until, "what": " ".join(c["what"].split()),
                        "every": c["every"], "repeat": bool(c.get("repeat")),
                        "owner": req.owner, "next_check": now,
                        "check": None, "fails": 0, "checks": 0})
        else:
            m = c["match"]
            until = parse_utc(c["until"]) if "until" in c \
                else now + WATCH_DEFAULT_SECS
            if until - now < WATCH_MIN_SECS or until - now > WATCH_MAX_SECS:
                raise Refused(LINES["watch-time"])
            scope, scope_name = req.resolve(m["in"])
            rec.update({"until": until, "scope": scope,
                        "scope_name": scope_name,
                        "words": [w.strip() for w in m["words"]],
                        "every": bool(m.get("every")),
                        "last_fired": 0, "fired": 0})
        if not req.owner:
            mine = sum(1 for r in live.values()
                       if r.get("requester") == req.requester)
            if mine >= PER_PERSON_MAX:
                raise Refused(LINES["cap-person"].format(cap=PER_PERSON_MAX),
                              REASON_CAP, {"kind": kind, "cap": PER_PERSON_MAX,
                                           "per": "person"})
            if len(live) >= TOTAL_MAX:
                raise Refused(LINES["cap-total"], REASON_CAP,
                              {"kind": kind, "cap": TOTAL_MAX,
                               "per": "total"})
        rid = new_id({"items": items}, kind)
        items[rid] = rec
        live[rid] = rec
        done.append(("add", rid))
    data["items"] = items
    if len(tz) > TZ_MAX_USERS:
        for k in list(tz)[:len(tz) - TZ_MAX_USERS]:
            del tz[k]
    data["tz"] = tz
    return done


def remember_done(data, qid, outcome, now=None):
    """Record a schedule outcome for queue item qid (applied once, ever:
    a retried send or a resend after a restart reads it back)."""
    now = time.time() if now is None else now
    d = data["done"]
    d[str(qid)] = dict(outcome, at=now)
    for k in [k for k, v in d.items()
              if now - (v or {}).get("at", 0) > DONE_KEEP_SECS]:
        del d[k]
    for k in list(d)[:-500]:
        del d[k]


# ---------------------------------------------------------------------------
# What a queue item carries: the requester's own list
# ---------------------------------------------------------------------------
def _entry(i, r, show_for=False):
    """One listed item. show_for: someone else's, listed in an owner's
    DM: who and when, but not its note or words, which that person's
    request chose (text from others never reaches an owner-DM wake)."""
    e = {"id": i, "kind": r.get("kind"), "set_in": r.get("where")}
    if not show_for:
        e["note"] = r.get("note") or ""
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", 0))
    elif r.get("kind") == "watch-web":
        e.update({"web": True, "every_secs": r.get("every"),
                  "repeat": bool(r.get("repeat")),
                  "until": iso(r.get("until", 0))})
        if not show_for:
            e["what"] = r.get("what")
    else:
        e.update({"in": r.get("scope_name"), "every": bool(r.get("every")),
                  "until": iso(r.get("until", 0))})
        if not show_for:
            e["words"] = r.get("words")
    if show_for:
        e["for"] = r.get("name") or r.get("requester")
        e["for_id"] = r.get("requester")
    return e


def card(data, uid, guild_id=None, dm=False, owner_dm=False, can_set=True,
         now=None, can_web=False):
    """The `watches` field of a queue item: the author's reminders and
    watches that may be shown where they asked (all of them in a DM,
    those set in this server in a server, with a count of the rest),
    their time zone if they told it, and whether they may set new ones
    (can_set; can_web for web watches).
    In an owner's own DM: everyone's, marked with whose."""
    now = time.time() if now is None else now
    uid = str(uid)
    live = sorted(active(data, now).items(),
                  key=lambda kv: kv[1].get("due") or kv[1].get("until", 0))
    if owner_dm:
        shown = [_entry(i, r, show_for=r.get("requester") != uid)
                 for i, r in live]
        hidden = 0
    else:
        mine = [(i, r) for i, r in live if r.get("requester") == uid]
        ok = [(i, r) for i, r in mine
              if dm or (not r.get("dm") and r.get("guild_id") == guild_id)]
        shown = [_entry(i, r) for i, r in ok]
        hidden = len(mine) - len(ok)
    out = {"can_set": bool(can_set), "can_web": bool(can_set and can_web),
           "items": shown[:LIST_MAX]}
    if hidden:
        out["elsewhere"] = hidden   # theirs, set in other places
    if len(shown) > LIST_MAX:
        out["more"] = len(shown) - LIST_MAX
    if data["tz"].get(uid):
        out["tz"] = data["tz"][uid]
    return out


# ---------------------------------------------------------------------------
# Due reminders, matches, fires
# ---------------------------------------------------------------------------
def due_reminders(data, now=None):
    now = time.time() if now is None else now
    return [(i, r) for i, r in data["items"].items()
            if isinstance(r, dict) and r.get("kind") == "remind"
            and r.get("due", 0) <= now]


def expired_watches(data, now=None):
    now = time.time() if now is None else now
    return [i for i, r in data["items"].items()
            if isinstance(r, dict) and r.get("kind") != "remind"
            and r.get("until", 0) <= now]


def next_due(data):
    """When the next reminder or expiry is due (epoch), or None."""
    ts = [r.get("due") if r.get("kind") == "remind" else r.get("until")
          for r in data["items"].values() if isinstance(r, dict)]
    ts = [t for t in ts if isinstance(t, (int, float))]
    return min(ts) if ts else None


_PATTERNS = {}


def matcher(words):
    """A compiled case-insensitive matcher for a watch's words: escaped
    literals between non-word boundaries (no user regex; linear time)."""
    key = tuple(words)
    pat = _PATTERNS.get(key)
    if pat is None:
        alts = "|".join(re.escape(w.casefold()) for w in
                        sorted(words, key=len, reverse=True))
        pat = re.compile(r"(?<!\w)(?:" + alts + r")(?!\w)")
        if len(_PATTERNS) > 2000:
            _PATTERNS.clear()
        _PATTERNS[key] = pat
    return pat


def match_text(words, text):
    """The watch's word or phrase found in text (casefolded), or None."""
    if not text or not words:
        return None
    m = matcher(words).search(text.casefold()[:8000])
    return m.group(0) if m else None


def watch_covers(rec, guild_id, channel_id, parent_id=None):
    """The message's place is inside the watch's scope."""
    sc = rec.get("scope") or {}
    if sc.get("guild_id") != guild_id:
        return False
    if sc.get("channel_id"):
        return channel_id == sc["channel_id"] or (
            parent_id is not None and parent_id == sc["channel_id"])
    return True


def group(entries):
    """Fire entries [(requester, channel_id, entry)] grouped into one
    fire per requester and place, in order: several reminders due at
    once (say, after downtime) reach a person as one message, never a
    burst."""
    out = {}
    for req, cid, e in entries:
        out.setdefault((req, cid), []).append(e)
    return out


def new_fire_id(now=None):
    """A queue-item id for a fire: a snowflake for now (it sorts and
    reads like a message id, and passes the outbox's id checks) whose
    low bits are random, so it never names a real message."""
    now = time.time() if now is None else now
    ms = int(now * 1000) - 1420070400000
    return str((ms << 22) | secrets.randbits(22) | 1)


def note_fired(data, fid, now=None):
    now = time.time() if now is None else now
    f = data["fired"]
    f[str(fid)] = now
    for k in [k for k, t in f.items() if now - t > 7 * 86400]:
        del f[k]
    for k in list(f)[:-FIRED_KEEP]:
        del f[k]


# ---------------------------------------------------------------------------
# Web watches
# ---------------------------------------------------------------------------
# The monitor issues each due check with a fresh one-time nonce and lists
# the live ones in web_watches_due.json; hooks/watch-due.sh wakes one
# checker session (watch-checker-prompt.template.md) with them as data;
# the checker reports each with bin/watch-report.py into
# web_watch_results.json, which the monitor drains (web_apply). A met
# result fires like a reminder (kind "watch-met"); not-met and unknown
# schedule the next check; unknown or no report backs off, and after
# WEB_MAX_FAILS in a row the watch ends and the requester is told once.
# The checker first screens each condition: one that is more than a
# plain condition (instructions, "also message...", a URL to post to) is
# reported "refused", which ends the watch (the requester is told once).
def _utc_day(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def _backoff(rec, now):
    rec["next_check"] = now + min(WEB_BACKOFF_MAX, rec.get("every", 3600)
                                  * 2 ** max(0, rec.get("fails", 0)))


def web_issue(data, now=None, owners=None):
    """Expire checks nobody reported on (a failure each: back off, and
    give up after WEB_MAX_FAILS), then issue the due ones, each with a
    fresh nonce, within today's budgets (over budget: tomorrow). A
    watch whose requester is not among owners (the owners now, when
    given) ends. Returns [(id, record)] of the watches that ended
    (removed)."""
    now = time.time() if now is None else now
    day = _utc_day(now)
    used = data.setdefault("web", {})
    if used.get("day") != day:
        used.clear()
        used.update({"day": day, "total": 0})
    gave_up = []
    for i, r in list(data["items"].items()):
        if not isinstance(r, dict) or r.get("kind") != "watch-web" \
                or r.get("until", 0) <= now:
            continue
        chk = r.get("check")
        if chk and chk.get("expires", 0) <= now:
            r["check"] = None
            r["fails"] = r.get("fails", 0) + 1
            if r["fails"] >= WEB_MAX_FAILS:
                gave_up.append((i, data["items"].pop(i)))
                continue
            _backoff(r, now)
            continue
        if not r.get("owner") or (owners is not None
                                  and str(r.get("requester")) not in owners):
            # Set before web watches were the owners' only, or by someone
            # no longer an owner: it ends (the requester is told, as for
            # one that gave up).
            gave_up.append((i, data["items"].pop(i)))
            continue
        if chk or r.get("next_check", 0) > now:
            continue
        if used["total"] >= WEB_TOTAL_PER_DAY:
            tomorrow = datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp() + 86400
            r["next_check"] = tomorrow + secrets.randbelow(600)
            continue
        used["total"] += 1
        r["check"] = {"nonce": secrets.token_hex(16), "issued": now,
                      "expires": now + WEB_CHECK_SECS}
        r["checks"] = r.get("checks", 0) + 1
    return gave_up


def web_due(data, now=None):
    """What web_watches_due.json lists: every live check, as data for the
    checker (the condition, the id, the nonce, when it expires), the
    hook's lease key, and `group`, an opaque tag per requester: the hook
    never hands two requesters' checks (and nonces) to one checker run,
    so one person's page cannot report on another's watch."""
    now = time.time() if now is None else now
    out = []
    for i, r in sorted(data["items"].items()):
        chk = r.get("check") if isinstance(r, dict) else None
        if r.get("kind") == "watch-web" and chk \
                and chk.get("expires", 0) > now:
            out.append({"id": i, "key": f"{i}:{int(chk['issued'])}",
                        "group": hashlib.sha256(
                            str(r.get("requester")).encode()).hexdigest()[
                            :16],
                        "nonce": chk["nonce"], "what": r.get("what"),
                        "expires": iso(chk["expires"])})
    return out


def report_problems(rep):
    """Shape of one check report (bin/watch-report.py, the monitor)."""
    if not isinstance(rep, dict):
        return ["not an object"]
    out = []
    if not (isinstance(rep.get("id"), str)
            and ITEM_ID_RE.fullmatch(rep["id"])):
        out.append("id is the check's id from the data block")
    if not (isinstance(rep.get("nonce"), str)
            and NONCE_RE.fullmatch(rep["nonce"])):
        out.append("nonce is the check's nonce from the data block")
    if rep.get("result") not in RESULTS:
        out.append("result is met, not-met, unknown or refused")
    sm = rep.get("summary")
    if not isinstance(sm, str) or len(sm) > SUMMARY_MAX \
            or any(ord(ch) < 32 and ch != "\n" for ch in sm):
        out.append(f"summary is plain text, up to {SUMMARY_MAX} "
                   f"characters")
    return out


def web_apply(data, rep, now=None):
    """Apply one check report: (result, id, record) for a report naming
    a live check of an active web watch with that check's nonce (spent
    now), else (None, id, why). met: the watch ends unless it repeats, then
    stays quiet WEB_REPEAT_SECS; not-met: the next check in `every`;
    unknown: a failure (back off; ends after WEB_MAX_FAILS, result
    "gave-up"); refused (the checker judged the condition to be more
    than a condition: instructions, a request to act): the watch ends."""
    now = time.time() if now is None else now
    if report_problems(rep):
        return None, None, "malformed"
    i = rep["id"]
    r = data["items"].get(i)
    chk = (r or {}).get("check") if isinstance(r, dict) else None
    if not r or r.get("kind") != "watch-web" or r.get("until", 0) <= now:
        return None, i, "no such active web watch"
    if not chk or chk.get("expires", 0) <= now:
        return None, i, "no live check (expired or already reported)"
    if not hmac.compare_digest(str(chk.get("nonce")).encode(),
                               rep["nonce"].encode()):
        return None, i, "wrong nonce"
    r["check"] = None
    result = rep["result"]
    if result == "met":
        r["fails"] = 0
        r["last_met"] = now
        if not r.get("repeat"):
            del data["items"][i]
        else:
            r["next_check"] = now + max(r.get("every", 0), WEB_REPEAT_SECS)
        return "met", i, dict(r)
    if result == "refused":
        return "refused", i, data["items"].pop(i)
    if result == "not-met":
        r["fails"] = 0
        r["next_check"] = now + r.get("every", 3600)
        return "not-met", i, r
    r["fails"] = r.get("fails", 0) + 1
    if r["fails"] >= WEB_MAX_FAILS:
        return "gave-up", i, data["items"].pop(i)
    _backoff(r, now)
    return "unknown", i, r


def web_work(data, now=None):
    """True when web_issue has something to do (a check to issue or to
    expire), so the monitor takes the lock only then."""
    now = time.time() if now is None else now
    for r in data["items"].values():
        if isinstance(r, dict) and r.get("kind") == "watch-web" \
                and r.get("until", 0) > now:
            chk = r.get("check")
            if (chk and chk.get("expires", 0) <= now) or (
                    not chk and r.get("next_check", 0) <= now):
                return True
    return False


def fire_entry(i, r, now, matched=None):
    """One entry of a fire's `reminder.items`."""
    e = {"id": i, "kind": r.get("kind"), "note": r.get("note") or "",
         "set_at": iso(r.get("created", now))}
    if r.get("kind") == "remind":
        e["due"] = iso(r.get("due", now))
        e["late"] = now - r.get("due", now) > LATE_SECS
    elif r.get("kind") == "watch-web":
        res = (matched or {}).get("result", "met")
        e.update({"what": r.get("what"), "result": res,
                  "ends": iso(r.get("until", now))
                  if r.get("repeat") and res == "met" else "now"})
        if (matched or {}).get("summary"):
            e["summary"] = matched["summary"]
    else:
        e.update({"words": r.get("words"), "in": r.get("scope_name")})
        if matched:
            e["matched"] = matched
        e["ends"] = "now" if not r.get("every") \
            else iso(r.get("until", now))
    return e
