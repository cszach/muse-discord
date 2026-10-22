#!/usr/bin/env python3
"""muse-discord proactive: the bot starts a conversation on
its own now and then, like a person would, instead of only replying.

Off by default (proactive.enabled; the Friends preset turns it on for
its server). monitor.py loads this module (like telemetry.py) and calls
in at a few points; everything else is here:

- tick(state): the main loop. A heartbeat at jittered times
  (proactive.frequency: rarely about 1-2 a day, sometimes 4-6, often
  about hourly; moved out of quiet hours). Each heartbeat works out who
  and where the bot may reach out to right now (eligible(): owners by
  DM, people who DMed the bot first, quiet server channels where it is
  on) and, when anyone is eligible and the day's total allows, queues
  ONE item of kind "proactive" (a slot) for the worker: one wake, which
  usually ends in silence. The slot carries a nonce and the list of
  targets; it lives SLOT_SECS.
- outbox_item(state, item): an outbox item naming a slot. It may post
  only to one of that slot's targets, only with the slot's nonce, only
  while the slot is fresh and unspent, and only within the caps, in
  quiet-hours-free time, with the target still eligible. It never
  falls through to the ordinary send path.
- opt_action(state, item): "stop messaging me" (and "you can message me
  again"): an outbox item answering a person's own message carries
  "proactive": "opt-out" | "opt-in"; the person is that message's
  author from the monitor's own record, never a field the worker
  writes. An opt-out is recorded; an opt-in only asks (below).
- Asks: what a run could be steered into against someone (opting them
  back in; the time zone their quiet hours use, note_tz) is never
  taken from the outbox. The monitor DMs that person its own message
  with a Yes button (custom_id mdpro:yes:<token>), and only their own
  click records it (on_button on the interaction thread, apply_button
  in the main loop). A button lives ASK_SECS, works once, and a person
  gets at most ASKS_PER_DAY of them.
- note_item(mid, meta): every queued message (who wrote, where, when):
  whom the bot may DM first (only people who DMed it), and whether
  someone answered the last unprompted message.
- The live slot's nonce goes to proactive_nonce.json (mode 600; never
  owner_dm_nonces.json, so nothing takes it for an owner-DM nonce): the
  wake hook hands a proactive item out alone (never with anyone's
  message) and only that wake gets the nonce, as for an owner's DM.

The binding: like a reminder or watch fire, a slot is an item the
monitor starts (its own kind, an id from monitor.started_item_id), plus
the places its reply may go; monitor.bind_started binds both, before the
owner-DM gate. slot = {"id", "kind": "proactive", "nonce", "at",
"expires", "targets": {channel_id: {"type", "user_id"}}, "max_posts",
"posts", "spent"}. Unlike a fire's reply, a slot's post is sent here
(through monitor.send_fragmented, pinging at most its one person) and
never written to the mention ledger. The worker cannot add a target, so nothing it read (other
people's messages, its memory) can steer an unprompted message to
anyone else, and the caps bound the rest.

proactive.json (mode 600, monitor-internal): the next heartbeat, the
live slot, spent slot ids, the unprompted messages sent (for the caps),
who has messaged the bot, opt-outs and asks. One that cannot be read
is moved aside (proactive.json.bad-<time>), the owners are told, and no
heartbeat runs while such a file is there: the opt-outs in it are never
silently lost. Standard library only.
"""
import glob
import hmac
import json
import os
import random
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

VERSION = 1
KIND = "proactive"
FIELD = "proactive"                 # the slot's details on a queue item;
                                    # an outbox item's opt-out / opt-in
NONCE_FIELD = "owner_dm_nonce"      # the wake hook's one capability field
OPT_VALUES = ("opt-out", "opt-in")
SLOT_SECS = 15 * 60                 # a slot is spent or gone after this
MAX_PER_SLOT = 1                    # unprompted messages per heartbeat
ACTIVE_SECS = 30 * 60               # never into a conversation this fresh
TICK_SECS = 30
# Heartbeat gaps (seconds) by proactive.frequency, drawn uniformly.
INTERVALS = {"rarely": (12 * 3600, 24 * 3600),
             "sometimes": (4 * 3600, 6 * 3600),
             "often": (45 * 60, 75 * 60)}
PER_DAY = {"rarely": 2, "sometimes": 5, "often": 10}   # in all, a day
QUIET_SPREAD_SECS = 2 * 3600        # after quiet hours, a heartbeat lands
                                    # within this of their end
TARGETS_MAX = 25
DAY, WEEK = 86400, 7 * 86400
SENT_KEEP_SECS = 8 * DAY
SPENT_KEEP = 500
PEOPLE_MAX = 3000
FLUSH_SECS = 60
REACT_CHECK_SECS = 6 * 3600         # reactions looked up at most this often
SEND_RETRY_SECS = 90                # as the monitor's sends (nonce window)
TEXT_KINDS = ("mention", "dm")      # queued messages that count as talking
FORBIDDEN = ("reply_to", "reactions", "attachments", "settings",
             "schedule", FIELD)     # never on a slot's outbox item
ASK_KEY = "mdpro"                   # an ask's button: mdpro:yes:<token>
ASK_SECS = DAY                      # a button works this long, once
ASKS_PER_DAY = 3                    # buttons one person gets in a day
ASKS_MAX = 300                      # ask records kept in all
ASK_SEND_SECS = 10 * 60             # an ask not sent by then is dropped


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except (TypeError, ValueError):
        return 0.0


# -- time ---------------------------------------------------------------------
def zone(name):
    if not name or str(name).upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc


def parse_hours(text):
    """((start h, m), (end h, m)) of quiet hours, or None when off."""
    t = str(text or "").strip().lower()
    if t in ("", "off", "none"):
        return None
    try:
        a, b = t.split("-")
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return None
    if (sh, sm) == (eh, em):
        return None
    return (sh, sm), (eh, em)


def in_quiet(ts, hours, tz):
    """True when ts falls in quiet hours (local time in tz)."""
    if hours is None:
        return False
    local = datetime.fromtimestamp(ts, tz)
    m = local.hour * 60 + local.minute
    s = hours[0][0] * 60 + hours[0][1]
    e = hours[1][0] * 60 + hours[1][1]
    return s <= m < e if s < e else (m >= s or m < e)


def quiet_end(ts, hours, tz):
    """When the quiet hours ts is in end (epoch seconds)."""
    local = datetime.fromtimestamp(ts, tz)
    end = local.replace(hour=hours[1][0], minute=hours[1][1], second=0,
                        microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.timestamp()


class Verdict:
    """outbox_item's answer: reject (why it is set aside, or None),
    retry (keep it in the outbox for a later pass), spent (the slot id
    whose queue item can go: the slot is used up)."""

    def __init__(self, reject=None, retry=False, spent=None):
        self.reject, self.retry, self.spent = reject, retry, spent


class _G:
    """The monitor's globals, read live (tests stub them on the module)."""

    def __init__(self, g):
        self.__dict__["_g"] = g

    def __getattr__(self, name):
        return self._g[name]


class Proactive:
    def __init__(self, monitor_globals, clock=time.time, rng=None):
        self.m = _G(monitor_globals)
        self.clock = clock
        self.rng = rng or random.Random()
        self._data = None
        self._path = None
        self._dirty = False
        self._next_tick = 0.0
        self._flushed = 0.0
        self._nonce_written = None
        self._alert = None       # an owner notice owed (a bad store)
        self._readonly = False   # a bad store that could not be moved
        # The open asks, for the interaction thread (only the main loop
        # touches the store): {token: (user_id, at)}.
        self._lock = threading.Lock()
        self._live = {}

    # -- the store ------------------------------------------------------------
    def path(self):
        return self.m.PROACTIVE_FILE

    def data(self):
        """proactive.json, read once (re-read when the path changes)."""
        p = self.path()
        if self._data is None or self._path != p:
            self._path = p
            d = None
            try:
                with open(p) as f:
                    d = json.load(f)
            except FileNotFoundError:
                pass
            except Exception as e:   # any damage (deep nesting too)
                d = self._set_aside(p, f"{type(e).__name__}: {e}"[:200])
            if d is not None and not isinstance(d, dict):
                d = self._set_aside(p, "not a JSON object")
            d = d if isinstance(d, dict) else {}
            for k, empty in (("spent", {}), ("sent", []), ("people", {}),
                             ("opted_out", {}), ("asks", {})):
                if not isinstance(d.get(k), type(empty)):
                    d[k] = empty
            if not self._slot_ok(d.get("slot")):
                d["slot"] = None
            d["v"] = VERSION
            self._data = d
            self._sync_live()
        return self._data

    def _set_aside(self, p, why):
        """A store that does not parse: kept aside (never overwritten),
        the owners told once; no heartbeat while it is there."""
        bad = f"{p}.bad-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        try:
            os.replace(p, bad)
        except OSError:
            bad, self._readonly = p, True
        self._alert = (
            f"proactive.json could not be read ({why}), so it was kept as "
            f"{os.path.basename(bad)}. It holds who asked not to be "
            f"messaged first: until it is fixed and put back (or deleted), "
            f"the bot reaches out to nobody.")
        print(f"Proactive: {self._alert}", flush=True)
        return None

    @staticmethod
    def _slot_ok(s):
        return isinstance(s, dict) and isinstance(s.get("id"), str) \
            and isinstance(s.get("nonce"), str) \
            and isinstance(s.get("targets"), dict) \
            and isinstance(s.get("posts"), dict) \
            and isinstance(s.get("expires"), (int, float))

    def blocked(self):
        """A bad store is set aside (or could not be): fail closed."""
        p = self.path()
        return self._readonly or bool(glob.glob(glob.escape(p) + ".bad-*"))

    def save(self):
        if self._readonly:
            return
        try:
            self.m._write_private(self.path(), self.data())
            self._dirty = False
            self._flushed = self.clock()
        except OSError as e:
            print(f"Could not write proactive.json: {e}", flush=True)

    # -- settings ---------------------------------------------------------------
    def st(self):
        return self.m.settings()

    def every(self, key):
        return self.st().get(key, self.m.MS.Scope.everywhere())

    def tz_of(self, uid=None):
        """A person's own time zone when they confirmed it with their
        own button (note_tz), else proactive.timezone. Never the
        reminders' tz as such: a run can set that on anyone's message
        it holds."""
        if uid:
            p = self.data()["people"].get(str(uid)) or {}
            if isinstance(p.get("tz"), str) and p["tz"]:
                return p["tz"]
        return self.every("proactive.timezone")

    def quiet_now(self, now, uid=None):
        """Quiet hours now: in the person's own time zone (uid) when
        known, else in proactive.timezone."""
        return in_quiet(now, parse_hours(self.every("proactive.quiet_hours")),
                        zone(self.tz_of(uid)))

    def enabled_anywhere(self):
        st, MS = self.st(), self.m.MS
        if st.get("proactive.enabled", MS.Scope.dm()) \
                or st.get("proactive.enabled", MS.Scope("all_servers")):
            return True
        for cid, info in self.m.CHANNELS.items():
            if st.get("proactive.enabled",
                      self.m.scope_for(info.get("guild_id"), cid)):
                return True
        return False

    def schedule(self, now, freq=None):
        """The next heartbeat: a uniform gap for the frequency, moved out
        of quiet hours (to within QUIET_SPREAD_SECS after they end)."""
        freq = freq or self.every("proactive.frequency")
        lo, hi = INTERVALS.get(freq, INTERVALS["rarely"])
        t = now + self.rng.uniform(lo, hi)
        hours = parse_hours(self.every("proactive.quiet_hours"))
        tz = zone(self.every("proactive.timezone"))
        if in_quiet(t, hours, tz):
            t = quiet_end(t, hours, tz) + self.rng.uniform(
                0, min(lo, QUIET_SPREAD_SECS))
        return t

    # -- what the monitor tells us ---------------------------------------------
    def note_item(self, mid, meta):
        """A queued message (note_item_meta): who talked to the bot,
        where, when; a DM means they started a DM conversation."""
        try:
            if not isinstance(meta, dict) or meta.get("bot") \
                    or meta.get("kind") not in TEXT_KINDS:
                return
            uid = str(meta.get("author_id") or "")
            if not uid:
                return
            people = self.data()["people"]
            p = people.setdefault(uid, {})
            ts = snowflake_ts(mid) or self.clock()
            cid = str(meta.get("channel_id") or "")
            if ts >= p.get("last", 0):
                p.update(last=ts, last_mid=str(mid), last_cid=cid)
            seen = p.setdefault("seen", {})     # last message per place
            if cid and ts > seen.get(cid, 0):
                seen[cid] = ts
                for old in sorted(seen, key=seen.get)[:-10]:
                    del seen[old]
            if meta.get("dm") and meta.get("channel_id"):
                p["dm"] = str(meta["channel_id"])
            if meta.get("author"):
                p["name"] = str(meta["author"])[:100]
            if len(people) > PEOPLE_MAX:
                for old in sorted(people, key=lambda u: people[u].get(
                        "last", 0))[:len(people) - PEOPLE_MAX]:
                    del people[old]
            self._dirty = True
        except Exception as e:
            print(f"Proactive note error: {e}", flush=True)

    def hook_nonces(self):
        """{slot id: nonce} for the wake hook: the live, unspent slot
        only."""
        try:
            s = self.data().get("slot")
            if s and not s.get("spent") and s.get("expires", 0) > self.clock():
                return {s["id"]: s["nonce"]}
        except Exception:
            pass
        return {}

    def secret_nonces(self):
        """The slot nonce while any slot record holds it (spent too), for
        the monitor's outgoing secret filter."""
        try:
            s = self.data().get("slot")
            return {s["nonce"]} if self._slot_ok(s) else set()
        except Exception:
            return set()

    def write_nonces(self):
        """proactive_nonce.json (mode 600, the wake hook's): written when
        it changes."""
        body = self.hook_nonces()
        if body == self._nonce_written:
            return
        try:
            self.m._write_private(self.m.PROACTIVE_NONCE_FILE, body)
            self._nonce_written = body
        except OSError as e:
            print(f"Could not write proactive_nonce.json: {e}", flush=True)

    # -- eligibility ------------------------------------------------------------
    def sent_to(self, key, value, since):
        return [e for e in self.data()["sent"]
                if e.get(key) == value and e.get("at", 0) >= since]

    def history(self, state, cid):
        return ((state.get("history") or {}).get(cid) or [])

    def is_bot_msg(self, h):
        """This bot's own message."""
        return str(h.get("author_id")) in (str(self.m.BOT_USER_ID),
                                           str(self.m.BOT_ID))

    def human(self, h):
        """A person's message: not this bot, another bot or a webhook."""
        return not (h.get("bot") or h.get("webhook") or self.is_bot_msg(h))

    def reacted(self, e):
        """Someone reacted to that unprompted message: an answer too.
        Looked up once it matters, at most every REACT_CHECK_SECS (never
        during a REST backoff: these calls block the main loop), and
        remembered."""
        if e.get("answered"):
            return True
        now = self.clock()
        if not e.get("msgs") or self.m.rest_backing_off() \
                or now - e.get("checked", 0) < REACT_CHECK_SECS:
            return False
        e["checked"] = now
        self._dirty = True
        try:   # its last part: one call
            m = self.m.api_get(f"/channels/{e['channel_id']}/messages/"
                               f"{e['msgs'][-1]}")
        except Exception:
            return False
        for r in (m or {}).get("reactions") or []:
            if int(r.get("count") or 0) - (1 if r.get("me") else 0) > 0:
                e["answered"] = True
                return True
        return False

    def person_problem(self, state, uid, cid, now):
        """Why no unprompted message may go to person uid (in their DM
        channel cid) now, or None."""
        d = self.data()
        if uid in d["opted_out"]:
            return "opted out"
        st = self.st()
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        day = st.get("proactive.per_person_per_day",
                     self.m.MS.Scope.everywhere())
        week = st.get("proactive.per_person_per_week",
                      self.m.MS.Scope.everywhere())
        if len(self.sent_to("user_id", uid, now - DAY)) >= day:
            return "per person per day cap"
        if len(self.sent_to("user_id", uid, now - WEEK)) >= week:
            return "per person per week cap"
        mine = self.sent_to("user_id", uid, 0)
        p = d["people"].get(uid) or {}
        hist = self.history(state, cid)
        if mine:
            # Answered: they wrote in that same DM since (a message of
            # theirs elsewhere is not an answer), or reacted to it.
            e = max(mine, key=lambda x: x["at"])
            answered = (p.get("seen") or {}).get(cid, 0) > e["at"] or any(
                self.human(h) and snowflake_ts(h.get("id")) > e["at"]
                for h in hist) or self.reacted(e)
            if not answered:
                return "no answer to the last unprompted message yet"
        newest = max([snowflake_ts(h.get("id")) for h in hist]
                     + [p.get("last", 0)])
        if now - newest < ACTIVE_SECS:
            return "a conversation is going on"
        return None

    def channel_problem(self, state, cid, now):
        """Why no unprompted message may go to server channel cid now,
        or None (a channel the bot reads and may post in, not a thread,
        proactive on, replies not off or paused there, an owner in the
        server, quiet for ACTIVE_SECS, the bot not the last to speak,
        within the caps)."""
        m = self.m
        info = m.CHANNELS.get(cid)
        if not info or info.get("thread") or info.get("can_send") is not True:
            return "not a channel the bot can post in"
        gid = info.get("guild_id")
        if m.owners_absent(gid) or m.owner_check_pending(state, gid):
            return "none of the owners is in that server"
        here = self.st().resolve(m.scope_for(gid, cid))
        if not here["proactive.enabled"]:
            return "proactive is off there"
        if here["who.replies"] == "off":
            return "replies are off (or paused) there"
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        cap = self.every("proactive.per_channel_per_day")
        if len(self.sent_to("channel_id", cid, now - DAY)) >= cap:
            return "per channel per day cap"
        hist = self.history(state, cid)
        humans = [snowflake_ts(h.get("id")) for h in hist if self.human(h)]
        if not humans:
            return "no one is known to talk there"
        last_h = max(humans)
        own = [snowflake_ts(h.get("id")) for h in hist if self.is_bot_msg(h)]
        if own and max(own) > last_h:
            return "the bot spoke last there"
        if now - last_h < ACTIVE_SECS:
            return "a conversation is going on"
        mine = self.sent_to("channel_id", cid, 0)
        if mine:
            # Answered: a person wrote there since (other bots, feeds and
            # webhooks are not people), or someone reacted to it.
            e = max(mine, key=lambda x: x["at"])
            if last_h <= e["at"] and not self.reacted(e):
                return "no answer to the last unprompted message yet"
        return None

    def total_cap(self):
        return PER_DAY.get(self.every("proactive.frequency"), PER_DAY["rarely"])

    def sent_total(self, now):
        return len([e for e in self.data()["sent"]
                    if e.get("at", 0) >= now - DAY])

    def owner_dm_channel(self, uid):
        """An owner's DM channel: known, else opened (the same call
        notify_owners makes); None when Discord cannot say."""
        p = self.data()["people"].get(uid) or {}
        if p.get("dm") or p.get("owner_dm"):
            return p.get("dm") or p["owner_dm"]
        for cid, u in self.m.DM_CHANNEL_USER.items():
            if u == uid:
                return str(cid)
        if self.m.rest_backing_off():
            return None
        try:
            c = self.m.api_post("/users/@me/channels", {"recipient_id": uid})
        except Exception as e:
            print(f"Proactive: no DM channel for owner {uid} ({e}).",
                  flush=True)
            return None
        cid = str((c or {}).get("id") or "")
        if not self.m._is_snowflake(cid):
            return None
        # Its own key: "dm" means they DMed the bot first, and an owner who
        # never did must not become a "dm" target once no longer an owner.
        self.data()["people"].setdefault(uid, {})["owner_dm"] = cid
        self._dirty = True
        return cid

    def target_problem(self, state, t, now):
        """Why target t (a slot's binding entry) is not eligible now."""
        if self.quiet_now(now, t.get("user_id")):
            return "quiet hours"
        st, MS = self.st(), self.m.MS
        if t["type"] == "channel":
            return self.channel_problem(state, t["channel_id"], now)
        dm_here = st.resolve(MS.Scope.dm())
        if not dm_here["proactive.enabled"]:
            return "proactive is off for DMs"
        if dm_here["who.replies"] == "off":
            return "DMs are off (or paused)"
        uid = t["user_id"]
        owner = uid in self.m.OWNER_USER_IDS
        if t["type"] == "owner" and not owner:
            return "no longer an owner"
        if t["type"] == "dm":
            if owner:
                return "an owner now (owner check-ins instead)"
            p = self.data()["people"].get(uid) or {}
            if p.get("dm") != t["channel_id"]:
                return "never DMed the bot there"
            if self.m.dm_refusal({"id": uid, "username": p.get("name")
                                  or uid}) is not None:
                return "the DM settings no longer answer them"
        return self.person_problem(state, uid, t["channel_id"], now)

    def eligible(self, state, now):
        """[target] the bot may reach out to now (the slot's list)."""
        m, d = self.m, self.data()
        cands = []
        dm_here = self.st().resolve(m.MS.Scope.dm())
        if dm_here["proactive.enabled"] and dm_here["who.replies"] != "off":
            for uid in sorted(m.OWNER_USER_IDS):
                if uid in d["opted_out"]:
                    continue
                cid = self.owner_dm_channel(uid)
                if cid:
                    cands.append({"type": "owner", "user_id": uid,
                                  "channel_id": cid})
            for uid, p in sorted(d["people"].items()):
                if p.get("dm") and uid not in m.OWNER_USER_IDS \
                        and uid not in d["opted_out"]:
                    cands.append({"type": "dm", "user_id": uid,
                                  "channel_id": p["dm"]})
        for cid in sorted(m.CHANNELS):
            cands.append({"type": "channel", "user_id": None,
                          "channel_id": str(cid)})
        out = []
        for t in cands:
            try:
                why = self.target_problem(state, t, now)
            except Exception as e:
                why = f"error ({e})"
            if why is None:
                out.append(t)
        return out

    def pick_side(self, targets):
        """One slot holds private targets (owner and DMs) or public ones
        (channels), never both, so nothing private shares a run with a
        public room: alternately, else whichever side has any."""
        def shuffled(kind):   # past TARGETS_MAX, a different few each time
            ts = [t for t in targets if t["type"] == kind]
            return self.rng.sample(ts, len(ts))
        priv = [t for t in targets if t["type"] == "owner"] + shuffled("dm")
        pub = shuffled("channel")
        first, then = (pub, priv) if self.data().get("side") == "private" \
            else (priv, pub)
        return (first or then)[:TARGETS_MAX]

    # -- the heartbeat ----------------------------------------------------------
    def describe(self, state, t, now):
        """What the worker gets about one target: who or where, when they
        last talked and how that ended, the last unprompted message, and
        the settings card of the place. No message text."""
        m = self.m
        cid, uid = t["channel_id"], t.get("user_id")
        hist = self.history(state, cid)
        out = {"channel_id": cid, "type": t["type"]}
        mine = self.sent_to("channel_id", cid, 0)
        out["last_unprompted"] = iso(max([e["at"] for e in mine] or [0]))
        if t["type"] == "channel":
            info = m.CHANNELS.get(cid) or {}
            gid = info.get("guild_id")
            scope = m.scope_for(gid, cid)
            card = m.settings_card(scope, self.st().resolve(scope), None,
                                   False, cid)
            card.pop("speaker", None)
            humans = [h for h in hist if not self.is_bot_msg(h)]
            out.update(
                where=m.channel_display(info),
                last_message_at=iso(snowflake_ts(hist[-1]["id"]))
                if hist else None,
                recent_messages=len([h for h in humans if now - snowflake_ts(
                    h.get("id")) < DAY]),
                recent_people=len({h.get("author_id") for h in humans
                                   if now - snowflake_ts(h.get("id")) < DAY}))
        else:
            p = self.data()["people"].setdefault(uid, {})
            name = m._USER_NAMES.get(uid) or p.get("name") \
                or m.DM_CHANNELS.get(cid) or uid
            if name != uid and p.get("name") != name:
                p["name"] = name
                self._dirty = True
            scope = m.MS.Scope.dm()
            card = m.settings_card(scope, self.st().resolve(scope), uid,
                                   False, cid)
            card.pop("places", None)
            last_them = max([p.get("last", 0)] + [
                snowflake_ts(h.get("id")) for h in hist
                if str(h.get("author_id")) == uid])
            last_you = max([0] + [snowflake_ts(h.get("id")) for h in hist
                                  if self.is_bot_msg(h)])
            outcome = None
            if p.get("last_mid"):
                e = m._ledger_load()["entries"].get(p["last_mid"]) or {}
                outcome = e.get("code")
            tz = self.tz_of(uid)
            out.update(user_id=uid, name=name, owner=t["type"] == "owner",
                       local_time=datetime.fromtimestamp(now, zone(tz))
                       .strftime("%A %H:%M"), timezone=tz,
                       where=f"DM with {name}",
                       last_from_them=iso(last_them),
                       last_from_you=iso(last_you),
                       ended_with=None if not (last_them or last_you)
                       else ("you" if last_you > last_them else "them"),
                       their_last_message=outcome)
        out["settings"] = card
        return out

    def make_slot(self, state, targets, now):
        d = self.data()
        sid = self.m.started_item_id(now)   # the fires' id scheme
        slot = {"id": sid, "kind": KIND, "nonce": secrets.token_hex(16),
                "at": now, "expires": now + SLOT_SECS,
                "max_posts": MAX_PER_SLOT, "spent": False, "posts": {},
                "targets": {t["channel_id"]: {"type": t["type"],
                                              "user_id": t.get("user_id")}
                            for t in targets}}
        tzname = self.every("proactive.timezone")
        local = datetime.fromtimestamp(now, zone(tzname))
        item = {
            "message_id": sid, "kind": KIND, "channel_id": None,
            "channel_name": "(no one asked: your call)", "guild_id": None,
            "author": None, "author_id": None, "content": "",
            "timestamp": iso(now), "queued_at": now,
            "media": [], "documents": [], "unviewable": [], "history": [],
            "may_generate": False, "owner_command": False,
            "reply_to": None, "burst": False, "burst_fragments": [],
            "from_bot": False, "bot_turns": 0, "participants": [],
            "follow_up": False, "settings": None,
            FIELD: {"expires_at": iso(now + SLOT_SECS),
                    "max_messages": MAX_PER_SLOT,
                    "local_time": local.strftime("%A %H:%M"),
                    "timezone": tzname,
                    "targets": [self.describe(state, t, now)
                                for t in targets]},
        }
        d["slot"] = slot
        d["side"] = "public" if targets[0]["type"] == "channel" \
            else "private"
        self.save()                  # the slot (and its nonce) first,
        self.write_nonces()          # then the hook's nonce file,
        self.m.queue_edit(add=[item])  # then the queue
        print(f"Heartbeat: {len(targets)} place(s) or people it may reach "
              f"out to; queued slot {sid}.", flush=True)
        return item

    def end_slot(self, state, why):
        d = self.data()
        s = d.get("slot")
        if not s:
            return
        d["spent"][s["id"]] = self.clock()
        for old in sorted(d["spent"], key=d["spent"].get)[:-SPENT_KEEP]:
            del d["spent"][old]
        d["slot"] = None
        sid = s["id"]
        try:
            self.m.queue_edit(drop=lambda q: str(q.get("message_id")) == sid)
        except Exception as e:
            print(f"Proactive: could not drop slot {sid} ({e}).", flush=True)
        self.save()
        self.write_nonces()
        print(f"Slot {sid} ended ({why}).", flush=True)

    def tick(self, state, now=None):
        """Main loop: slot upkeep, then the heartbeat when it is due."""
        now = self.clock() if now is None else now
        self.send_asks(now)   # each pass: right after the reply it follows
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_SECS
        d = self.data()
        s = d.get("slot")
        if s and not self._slot_ok(s):
            print("Proactive: a damaged slot record was dropped.", flush=True)
            d["slot"] = s = None
        if s:
            sending = [p for p in s["posts"].values()
                       if p.get("status") == "sending"
                       and now - p.get("first", now) < SEND_RETRY_SECS + 60]
            if s.get("spent") and not sending:
                self.end_slot(state, "used")
            elif not s.get("spent") and now > s.get("expires", 0):
                self.end_slot(state, "expired unanswered")
        self.write_nonces()   # (after a restart, say; only when it changed)
        self.drop_orphans()
        if self._alert and self.m.notify_owners(self._alert):
            self._alert = None
        cut = now - SENT_KEEP_SECS
        if any(e.get("at", 0) < cut for e in d["sent"]):
            d["sent"] = [e for e in d["sent"] if e.get("at", 0) >= cut]
            self._dirty = True
        if not d.get("seeded"):
            # People who DMed the bot before this module kept count.
            for mid, meta in list((state.get("item_meta") or {}).items()):
                self.note_item(mid, meta)
            d["seeded"] = True
            self._dirty = True
        freq = self.every("proactive.frequency")
        if d.get("freq") != freq or not d.get("next"):
            d["freq"], d["next"] = freq, self.schedule(now, freq)
            self._dirty = True
        elif now >= d["next"]:
            d["next"] = self.schedule(now, freq)
            self._dirty = True
            if d.get("slot"):
                print("Heartbeat skipped: the last one is still open.",
                      flush=True)
            elif self.blocked():
                print("Heartbeat skipped: a bad proactive.json is set aside "
                      "(PROTOCOL.md, proactive.json).", flush=True)
            elif self.enabled_anywhere() \
                    and self.sent_total(now) < self.total_cap():
                # (Quiet hours are per target: a person's own time zone.)
                targets = self.pick_side(self.eligible(state, now))
                if targets:
                    self.make_slot(state, targets, now)
                    return
                print("Heartbeat: nobody and nowhere to reach out to now; "
                      "no wake.", flush=True)
        if self._dirty and now - self._flushed >= FLUSH_SECS:
            self.save()

    def drop_orphans(self):
        """A proactive queue item that is not the live slot (its record
        was lost, or it ended) leaves the queue: it would never get a
        nonce, and the hook would hold it back for good."""
        s = self.data().get("slot") or {}
        live = s.get("id") if not s.get("spent") else None
        try:
            if any(isinstance(q, dict) and q.get("kind") == KIND
                   and str(q.get("message_id")) != live
                   for q in self.m.load_queue()):
                self.m.queue_edit(drop=lambda q: q.get("kind") == KIND
                                  and str(q.get("message_id")) != live)
                print("Proactive: dropped a slot item with no live slot.",
                      flush=True)
        except (OSError, ValueError) as e:
            print(f"Proactive: queue unreadable ({e}).", flush=True)

    # -- the worker's answer to a slot -------------------------------------------
    def names_slot(self, item):
        """The slot id an outbox item names (item or reply_to; a live or
        spent slot), else None: such an item is bound here, by the slot's
        nonce, never by a reply_nonce (monitor.bound_elsewhere)."""
        if not isinstance(item, dict):
            return None
        d = self.data()
        s = d.get("slot") if self._slot_ok(d.get("slot")) else {}
        known = set(d["spent"]) | ({s["id"]} if s else set())
        names = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
        return next((n for n in names if n in known), None)

    def outbox_item(self, state, item):
        """None when the outbox item names no slot (it goes the usual way);
        else a Verdict. Posts only to the slot's targets, with its nonce,
        while it is fresh and unspent, within the caps. A damaged record
        never raises: an item naming a slot is then set aside."""
        d = self.data()
        s = d.get("slot") if self._slot_ok(d.get("slot")) else {}
        sid = self.names_slot(item)
        if sid is None:
            return None
        try:
            return self._decide(state, item, d, s, sid)
        except Exception as e:
            print(f"Proactive: slot {sid}: {type(e).__name__}: {e}",
                  flush=True)
            return Verdict(f"slot {sid}: could not be checked", spent=sid)

    def _decide(self, state, item, d, s, sid):
        now = self.clock()
        if str(item.get("item") or "") != sid:
            return Verdict(f"slot {sid}: item must be the slot's message_id")
        bad = [k for k in FORBIDDEN if k in item]
        if bad:
            return Verdict(f"slot {sid}: carries no {', '.join(bad)}")
        if not s or s["id"] != sid:
            return Verdict(f"slot {sid}: already used or expired")
        got = item.get(NONCE_FIELD)
        if not isinstance(got, str) or not hmac.compare_digest(
                s["nonce"].encode(), got.encode("utf-8", "replace")):
            # A forgery from another wake: the slot is untouched.
            return Verdict(f"slot {sid}: bad nonce")
        cid = str(item.get("channel_id") or "")
        post = s["posts"].get(cid)
        if post and post.get("status") == "sending":
            return self._send(state, s, cid, item, now)   # a retry
        if s.get("spent"):
            return Verdict(f"slot {sid}: already used", spent=sid)
        if self.blocked():
            s["spent"] = True
            return Verdict(f"slot {sid}: proactive.json is set aside",
                           spent=sid)
        if now > s.get("expires", 0):
            return Verdict(f"slot {sid}: expired", spent=sid)
        if item.get("silent"):
            s["spent"] = True
            self.save()
            print(f"Slot {sid}: the worker chose not to reach out.",
                  flush=True)
            return Verdict(spent=sid)
        t = s["targets"].get(cid)
        if t is None:
            s["spent"] = True
            self.save()
            return Verdict(f"slot {sid}: {cid} is not one of its targets",
                           spent=sid)
        t = dict(t, channel_id=cid)
        why = self.target_problem(state, t, now)
        if why is None and len(s["posts"]) >= s.get("max_posts",
                                                    MAX_PER_SLOT):
            why = "the slot's messages are used"
        # Clamped to Discord's limit after the rewrites, as replies are.
        parts = [x[:self.m.DISCORD_MAX_CHARS]
                 for x in self.m.normalize_parts(item)]
        if why is None and not parts:
            why = "nothing to send"
        if why is None and t["type"] != "owner" \
                and self.m.never_share_hit(state, {"channel_id": cid}, None,
                                           parts):
            why = "it contains a never_share phrase (dropped)"
        if why is not None:
            s["spent"] = True
            self.save()
            return Verdict(f"slot {sid}: not sent to {cid}: {why}",
                           spent=sid)
        s["posts"][cid] = {"status": "sending", "first": now, "n": 0,
                           "next": 0, "parts": parts}
        if len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            s["spent"] = True
        entry = {"at": now, "slot": sid, "channel_id": cid,
                 "user_id": t.get("user_id"), "type": t["type"]}
        d["sent"].append(entry)
        self.save()
        return self._send(state, s, cid, item, now)

    def _send(self, state, s, cid, item, now):
        """Send (or retry) the accepted post for cid: the parts fixed at
        acceptance (the outbox copy is worker-writable), pinging at most
        that one person."""
        sid, post = s["id"], s["posts"][cid]
        if post.get("next", 0) > now:
            return Verdict(retry=True, spent=sid)
        t = s["targets"][cid]
        mentions = {"parse": []}
        if t.get("user_id"):
            mentions["users"] = [t["user_id"]]
        parts = post["parts"]
        try:
            # The monitor's one send path (plain messages: no reply_to),
            # each part's Discord nonce seeded with the slot id.
            msgs = self.m.send_fragmented(cid, parts, None, (),
                                          mentions=mentions, nonce_key=sid)
        except Exception as e:
            code = getattr(e, "code", None)
            post["n"] = post.get("n", 0) + 1
            # A secret in it (the monitor's send guard): never retried,
            # the slot is used up, nothing counts toward the caps.
            blocked = isinstance(e, self.m.SecretBlocked)
            # A part may have landed: any part after the first (on this
            # or an earlier attempt), or the first one when the error
            # does not say it was refused (a timeout can land).
            part = getattr(e, "failed_part", None)
            if part != 0 or getattr(e, "sent_ids", None):
                post["landed"] = True
            refused = isinstance(code, int) and 400 <= code < 500
            maybe = not blocked and (post.get("landed") or not refused)
            if code == 403 or blocked \
                    or now - post["first"] >= SEND_RETRY_SECS:
                post["status"] = "failed"
                if maybe or code == 403:
                    # It counts, as a reply that may have landed does; a
                    # 403 (blocked, or no longer allowed there) too, so
                    # nobody is tried again before they write.
                    for x in self.data()["sent"]:
                        if x.get("slot") == sid and x.get("channel_id") == cid:
                            x["msgs"] = list(getattr(e, "sent_ids", None)
                                             or [])[-4:]
                else:
                    self.data()["sent"] = [
                        x for x in self.data()["sent"]
                        if not (x.get("slot") == sid
                                and x.get("channel_id") == cid)]
                self.save()
                print(f"Slot {sid}: could not post to {cid} ({e}); "
                      f"dropped{' (part of it may have landed)' if maybe else ''}.",
                      flush=True)
                return Verdict(spent=sid)
            post["next"] = now + min(30, 2 ** post["n"])
            self.save()
            print(f"Slot {sid}: post to {cid} failed (attempt "
                  f"{post['n']}; retrying, same nonce): {e}", flush=True)
            return Verdict(retry=True, spent=sid)
        post["status"] = "sent"
        post.pop("parts", None)
        for x in self.data()["sent"]:
            if x.get("slot") == sid and x.get("channel_id") == cid:
                x["msgs"] = msgs[-4:]     # for reactions (an answer too)
        self.save()
        print(f"Slot {sid}: reached out in {cid} ({t['type']}, "
              f"{len(parts)} part(s)): {parts[0][:60]}", flush=True)
        return Verdict(spent=sid)

    # -- "stop messaging me" ------------------------------------------------------
    def opt_action(self, state, item, index=None):
        """None when the outbox item carries no opt-out / opt-in, or it was
        recorded; else why it is refused. The person is the author of
        the message the item answers (item_meta), never anything the
        worker wrote. That message must be a person's own (a mention or
        a DM, not a bot's), still in the queue (index: queue_index()),
        and the item a visible reply under it (text, threaded under it
        or a burst fragment of it): the person always sees what was
        recorded. Opt-in only in the person's own DM."""
        if FIELD not in item:
            return None
        v = item.get(FIELD)
        if v not in OPT_VALUES:
            return f"{FIELD}: expected \"opt-out\" or \"opt-in\""
        qid = str(item.get("item") or "")
        index = index or {"frags": {}, "channels": {}}
        if qid not in index["channels"]:
            return f"{FIELD}: that message is no longer waiting for a reply"
        meta = self.m.queued_item_meta(state, qid, index)
        if not isinstance(meta, dict) or meta.get("bot") \
                or meta.get("kind") not in TEXT_KINDS:
            return (f"{FIELD}: goes on the reply to the person's own "
                    f"message (item: its message_id)")
        if str(item.get("channel_id")) != str(meta.get("channel_id")) \
                or index["channels"].get(qid) != str(meta.get("channel_id")):
            return f"{FIELD}: channel_id must be that message's channel"
        rt = str(item.get("reply_to") or "")
        if item.get("silent") or item.get("standalone") \
                or not self.m.normalize_parts(item) \
                or (rt != qid and index["frags"].get(rt) != qid):
            return (f"{FIELD}: goes on a reply with text, under that "
                    f"message (reply_to), so they see it")
        if v == "opt-in" and not (meta.get("dm") and meta.get("kind") == "dm"):
            return f"{FIELD}: opt-in only in the person's own DM"
        uid = str(meta.get("author_id") or "")
        if not uid:
            return f"{FIELD}: whose message it is is not known"
        d = self.data()
        if v == "opt-out":
            d["opted_out"][uid] = self.clock()
            self._close_asks(uid, "in")
            self.save()
            print(f"Proactive: {uid} opted out (item {qid}).", flush=True)
        elif uid in d["opted_out"]:
            # Never recorded from the outbox (one run may hold several
            # people's messages): the person's own button records it.
            self.ask(uid, str(meta["channel_id"]), "in")
        return None

    # -- asks: the person's own button ---------------------------------------
    def _sync_live(self):
        """The open asks, for on_button (the interaction thread)."""
        d, now = self._data or {}, self.clock()
        live = {t: (a.get("user_id"), a.get("at", 0))
                for t, a in (d.get("asks") or {}).items()
                if isinstance(a, dict) and a.get("sent")
                and not a.get("done") and now - a.get("at", 0) < ASK_SECS}
        with self._lock:
            self._live = live

    def _close_asks(self, uid, what):
        for a in self.data()["asks"].values():
            if a.get("user_id") == uid and a.get("what") == what:
                a["done"] = True
        self._sync_live()

    def ask(self, uid, cid, what, tz=None):
        """DM person uid (in their DM channel cid) the monitor's own
        question with a Yes button: what "in" (message them first
        again) or "tz" (use tz for their quiet hours). Sent by
        send_asks; at most ASKS_PER_DAY a person, one open per kind."""
        d, now = self.data(), self.clock()
        asks = d["asks"]
        for t in [t for t, a in asks.items() if not isinstance(a, dict)
                  or now - a.get("at", 0) >= ASK_SECS]:
            del asks[t]
        mine = [a for a in asks.values() if a.get("user_id") == uid]
        if any(a.get("what") == what and not a.get("done")
               and a.get("tz") == tz for a in mine):
            return            # that very question is open already
        if len(mine) >= ASKS_PER_DAY:
            print(f"Proactive: no new button for {uid} ({what}): "
                  f"{ASKS_PER_DAY} in a day already.", flush=True)
            return
        self._close_asks(uid, what)        # a newer question replaces it
        asks[secrets.token_hex(8)] = {"user_id": uid, "channel_id": cid,
                                      "what": what, "tz": tz, "at": now,
                                      "sent": False, "n": 0, "next": 0}
        for old in sorted(asks, key=lambda t: asks[t]["at"])[:-ASKS_MAX]:
            del asks[old]
        self.save()
        print(f"Proactive: asking {uid} with a button ({what}).", flush=True)

    def note_tz(self, uid, tz):
        """A schedule action set person uid's time zone (the reminders'):
        their quiet hours move only when they confirm it themselves, and
        only someone the bot may DM first is asked."""
        uid = str(uid)
        d = self.data()
        p = d["people"].get(uid) or {}
        if tz == p.get("tz") or uid in d["opted_out"]:
            return
        dm_here = self.st().resolve(self.m.MS.Scope.dm())
        if not dm_here["proactive.enabled"] or dm_here["who.replies"] == "off":
            return
        if uid in self.m.OWNER_USER_IDS:
            cid = self.owner_dm_channel(uid)
        elif p.get("dm") and self.m.dm_refusal(
                {"id": uid, "username": p.get("name") or uid}) is None:
            cid = p["dm"]
        else:
            return
        if cid:
            self.ask(uid, cid, "tz", tz)

    def ask_text(self, a):
        if a["what"] == "in":
            q = "Okay if I message you first again now and then?"
        else:
            tz = a.get("tz") or (f"the default time zone "
                                 f"({self.every('proactive.timezone')})")
            q = (f"Should I use {tz} for the night hours when I don't "
                 f"message you first?")
        return (f"{q} Tap **Yes** if so; otherwise nothing changes. "
                f"(Only you can tap it, for 24 hours.)")

    def send_asks(self, now):
        """Send the asks not sent yet (each pass of the main loop);
        retried for ASK_SEND_SECS, then dropped."""
        d = self._data if self._data is not None else self.data()
        owed = [(t, a) for t, a in d["asks"].items()
                if isinstance(a, dict) and not a.get("sent")
                and not a.get("done") and a.get("next", 0) <= now]
        if not owed or self.m.rest_backing_off():
            return
        for tok, a in owed:
            try:
                self.m.send_reply(
                    a["channel_id"], self.ask_text(a), None, nonce=tok,
                    components=[{"type": 1, "components": [
                        {"type": 2, "style": 1, "label": "Yes",
                         "custom_id": f"{ASK_KEY}:yes:{tok}"}]}],
                    mentions={"parse": []})
                a["sent"], a["at"] = True, now
            except Exception as e:
                a["n"] = a.get("n", 0) + 1
                a["next"] = now + min(30, 2 ** a["n"])
                if getattr(e, "code", None) == 403 \
                        or now - a["at"] >= ASK_SEND_SECS:
                    a["done"] = True
                print(f"Proactive: could not send the button to "
                      f"{a['user_id']} ({e}).", flush=True)
            self._dirty = True
        self._sync_live()
        self.save()

    def on_button(self, ix, rest, uid, record):
        """mdpro:yes:<token>, on the interaction thread: only the person
        asked, once, while it is open. record() hands it to the main
        loop (apply_button)."""
        verb, _, tok = str(rest).partition(":")
        with self._lock:
            rec = self._live.get(tok)
            if verb != "yes" or rec is None \
                    or self.clock() - rec[1] >= ASK_SECS:
                return None
            if rec[0] != str(uid):
                return {"type": 4, "data": {
                    "content": "That button is not for you.", "flags": 64,
                    "allowed_mentions": {"parse": []}}}
            del self._live[tok]
        record({"kind": "proactive_yes", "token": tok, "by": str(uid)})
        original = (ix.get("message") or {}).get("content") or ""
        line = "**Yes**: done."
        return {"type": 7, "data": {
            "content": original[:1990 - len(line)] + "\n\n" + line,
            "components": [], "allowed_mentions": {"parse": []}}}

    def apply_button(self, rec):
        """Main loop: the person's own Yes (on_button). Checked again
        here: the ask open, theirs."""
        d = self.data()
        tok = str(rec.get("token") or "")
        a = d["asks"].get(tok)
        if not isinstance(a, dict) or a.get("done") or not a.get("sent") \
                or a.get("user_id") != str(rec.get("by")) \
                or self.clock() - a.get("at", 0) >= ASK_SECS:
            return
        a["done"] = True
        uid = a["user_id"]
        if a["what"] == "in":
            d["opted_out"].pop(uid, None)
            print(f"Proactive: {uid} opted back in (their button).",
                  flush=True)
        else:
            p = d["people"].setdefault(uid, {})
            if a.get("tz"):
                p["tz"] = a["tz"]
            else:
                p.pop("tz", None)
            print(f"Proactive: {uid} confirmed their time zone.", flush=True)
        self._sync_live()
        self.save()

