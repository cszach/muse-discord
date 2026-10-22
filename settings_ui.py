#!/usr/bin/env python3
"""/settings in Discord: one screen with a few direct settings and a
Where select, as one pure function of an interaction and a Ctx (see
handle). monitor.py owns the plumbing (registering the commands,
routing "mdset:" interactions here, building the Ctx, answering
Discord); this module never touches the network, so it answers well
inside Discord's 3 seconds, and it never raises.

What each person gets:

- An owner gets the place they typed /settings in (a channel, or DMs in
  the bot's DM; the join DM's Configure button opens a whole server),
  with a Where select to switch to its server or everywhere: a
  plain-words line on how the bot behaves there, the few settings most
  owners change (when it replies, reply length, images & video, who
  can DM it, replies a person a day: whichever that place has), a
  style picker for the server (the Friends / Community / Work
  presets), Pause or Resume, "Why no reply?", Undo for the last
  change, and a reminder that anything else is one DM away
  (settings_nl.py: an owner can change any setting by DMing the bot in
  plain words).
- A server admin (Manage Server) gets "Quiet the bot" for their server:
  quieter only (muse_settings' admin rule), and Undo of admins' changes.
- Anyone else gets a polite refusal.

Pages are Components V2 messages (flags 64|32768: no content, no
embeds; at most 40 components), edited in place after the first. Every
write goes through muse_settings.apply as one undo group, after
muse_settings.assess (a risky outcome shows a confirm card first).

custom_id grammar ("mdset:3:" + colon-separated parts, <= 100 chars):

    place := x (everywhere) | d (DMs) | s<gid> | c<gid>.<cid>
    k     := a short code (CORE_CODES) | ~<plugin>.<key>

    pg:<place>:<page>        page: h home (admins: their panel), y why
    wh:<place>               Where select: a place token (here, its
                             server, everywhere)
    set:<place>:<k>          a setting's select: r ln med df pp (see
                             PLACE_CONTROLS; "=" follows the server)
    pre:<place>              style select (a server): apply a preset
    val:<place>:<k>:<v>      a button with an absolute value ("=": what
                             the place would follow without its own)
    ppl:<place>:ig           admins: add to the ignore list
    rm:<place>:ig            admins: take their additions off it
    pz:<place> | rs:<place>  pause / resume
    un:<place>:<grp>         undo the newest group there (+ channels),
                             if it is still <grp>
    ok:<place>:<tok>         apply what a confirm card stashed
    fx:<place>:<k>:<op>      a "Why no reply?" fix: = | v<value>
                             | -<id> | +<id>
    wy:d[:<id>]              DMs: check one person (mod:d:wy opens the
                             by-ID form, sub:d:wy is its submit)

Nothing in a custom_id is trusted: who may do what, and where, is
checked again on every interaction (server admins through
muse_settings' admin rule), and a stash token is bound to the user it
was made for. Ids from older designs ("mdset:2:..." and before) answer
"out of date" and change nothing, except mdset:open:server:<gid> (the
join DM's Configure button), which still opens that server.
"""
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Protocol

import muse_settings as ms

PREFIX = "mdset"
VERSION = "3"
COMMAND = "settings"
# The message context command; Discord may refuse "?" in its name, in
# which case the monitor registers the second.
WHY_COMMANDS = ("Why no reply?", "Why no reply")
EPHEMERAL = 1 << 6
COMPONENTS_V2 = 1 << 15
NEW_FLAGS = EPHEMERAL | COMPONENTS_V2
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
CUSTOM_ID_MAX = 100
MAX_COMPONENTS = 40
TEXT_BUDGET = 3600
STASH_TTL = 900
BLURPLE, GREY, AMBER, RED = 0x5865F2, 0x80848E, 0xF0B232, 0xED4245
WHY_ENTRIES = 5
# Whether a user select in the bot's DM searches people the DM does not
# include (verify on real clients; see e2e/README.md). While False, the
# DMs Why page in a DM offers Check by ID instead.
DM_USER_SELECT_SEARCHES = False
THREAD_TYPES = (10, 11, 12)
CATEGORY = 4

# Component types and button styles.
ROW, BUTTON, STRING_SELECT, TEXT_INPUT = 1, 2, 3, 4
USER_SELECT, MENTIONABLE_SELECT = 5, 7
SECTION, TEXT, SEPARATOR, CONTAINER, LABEL = 9, 10, 14, 17, 18
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4

REFUSAL_SERVER = ("Sorry, only the bot's owners can change its settings. "
                  "Server admins (Manage Server) can make it quieter here.")
REFUSAL_DM = ("Sorry, only the bot's owners can use /settings in DMs. "
              "Server admins: run it in your server.")
REFUSAL_WHY = ("Sorry, only the bot's owners and this server's admins can "
               "see why the bot didn't reply.")
OUT_OF_DATE = ("This settings page is out of date. Run /settings for a "
               "fresh one. Nothing was changed.")
NOT_THERE = ("I'm not in that server any more, so there is nothing to set "
             "there. Nothing was changed.")
EXPIRED = "This confirmation expired, so nothing was changed."
WRONG_USER = ("That button was made for someone else, so nothing was "
              "changed.")
APOLOGY = ("Sorry, something went wrong with /settings. Nothing was "
           "changed by this click; try again in a moment.")
ADMIN_ONLY_QUIETER = ("Server admins can make the bot quieter here, never "
                      "louder. Ask one of the bot's owners for that.")
OVERLAY_BROKEN = ("⚠ I can't read config.discord.yaml, so changes from "
                  "Discord are paused. Ask your Muse to fix that file.")
ROLES_ELSEWHERE = ("Roles only count in their own server, so the roles "
                   "you picked were left out.")
FIXED_NOTE = "-# Fixed. {who} must send again; refused messages stay " \
             "unanswered."
FOOTER_SERVER = ("-# Anything else? DM me what you'd like changed, e.g. "
                 "“only reply in #help” or “let Sam make images”.")
FOOTER_DM = ("-# Anything else? Just tell me here, e.g. “only reply in "
             "#help” or “let @Sam DM you”.")


class Ctx(Protocol):
    """What the monitor provides (monitor.SettingsCtx)."""
    settings: "ms.Settings"   # current, already reloaded
    owners: set
    bot_id: str
    now: float

    def guilds(self) -> list: ...
    # [{"id", "name", "member_count", "system_channel_id"}]
    def channels(self, guild_id) -> list: ...
    # [{"id", "name", "type", "parent_id", "readable", "can_send"}]
    def apply(self, changes, by, role, group=None, admin_gid=None): ...
    def undo(self, prefix, by, **kw): ...    # children, admin_gid, expect
    def reload(self) -> "ms.Settings": ...
    def names(self, some_id) -> str: ...      # servers and channels
    def display_name(self, user_id) -> str: ...
    def recent_outcomes(self, place, limit) -> list: ...
    def outcome(self, message_id) -> dict: ...
    def stash_put(self, user_id, payload, ttl=STASH_TTL) -> str: ...
    def stash_take(self, user_id, token): ...  # None: gone; raises
    #                                           PermissionError: not yours
    def notify_owners_admin_change(self, guild_id, user_id, key, old,
                                   new) -> None: ...
    def log(self, msg: str) -> None: ...


# ---------------------------------------------------------------------------
# Keys and places in custom ids
# ---------------------------------------------------------------------------
CORE_CODES = {
    "who.replies": "r", "who.dms_from": "df", "who.allow": "al",
    "who.ignore": "ig", "who.bots": "b", "who.bot_exchange_limit": "bx",
    "style.length": "ln", "style.notes": "n",
    "abilities.owner_commands": "oc", "abilities.reminders": "rw",
    "privacy.memory": "m",
    "privacy.suspicious": "su", "limits.per_day": "pd",
    "limits.per_person_per_day": "pp", "presence.status": "ps",
    "presence.kind": "pk", "updates.notify": "un",
}
CODE_KEYS = {v: k for k, v in CORE_CODES.items()}
_PLUGIN_CODE = re.compile(r"~([A-Za-z0-9_\-]{1,40})\.([A-Za-z0-9_\-]{1,40})")
_PLACE = re.compile(r"x|d|s(\d{1,25})|c(\d{1,25})\.(\d{1,25})")
_ID = re.compile(r"\d{1,25}")
_PART = re.compile(r"[^:]{1,100}")
PAGES = ("h", "y")


def kcode(key):
    if key in CORE_CODES:
        return CORE_CODES[key]
    if key.startswith("media."):
        return "~" + key  # the media plugin's code: ids stay valid
    if key.startswith("plugins."):
        code = "~" + key[len("plugins."):]
        if _PLUGIN_CODE.fullmatch(code):
            return code
    return None


def kdecode(code):
    if code in CODE_KEYS:
        return CODE_KEYS[code]
    if _PLUGIN_CODE.fullmatch(code or ""):
        return ms.moved_key("plugins." + code[1:])
    return None


class BadId(ValueError):
    pass


def cid(*parts):
    parts = [str(p) for p in parts]
    s = ":".join([PREFIX, VERSION] + parts)
    if len(s) > CUSTOM_ID_MAX or any(not _PART.fullmatch(p)
                                     for p in parts):
        raise BadId(s)
    return s


def ptok(place):
    if place.kind == "defaults":
        return "x"
    if place.kind == "dms":
        return "d"
    if place.kind == "server":
        return f"s{place.guild_id}"
    return f"c{place.guild_id}.{place.channel_id}"


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
clip = ms.clip


_MD = re.compile(r"([\\*_~|>#\[\]()`])")
# Bidi controls (embeddings, overrides, isolates): a name could reorder
# the words around it on the page.
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_SCHEME = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,30}):(?=//)")
_UNMD = re.compile(r"\\([\\*_~|>#\[\]()`])")
ZWSP = "​"


def esc(text):
    """A name (a server's, a channel's, a person's: anyone can choose
    them) made safe to put in a Text Display: markdown escaped, mentions
    and @everyone / @here neutralised, on one line. Labels and options
    are plain text in Discord; plain() undoes this for them."""
    t = _BIDI.sub("", " ".join(str(text).split()))
    t = _MD.sub(r"\\\1", t)
    t = re.sub(r"<(?=[@#])", "<" + ZWSP, t)
    # "https://" becomes "https:<ZWSP>//": a URL in a name never links.
    t = _SCHEME.sub(r"\1:" + ZWSP, t)
    return re.sub(r"@(?=(everyone|here)\b)", "@" + ZWSP, t)


def plain(text):
    """esc() undone, for text Discord shows as is (labels, options,
    placeholders, modal titles)."""
    t = _UNMD.sub(r"\1", str(text)).replace(ZWSP, "")
    return t[:-2] + "…" if t.endswith("\\…") else t


def td(text):
    return {"type": TEXT, "content": clip(text, 3900)}


def sep(divider=True):
    return {"type": SEPARATOR, "divider": divider, "spacing": 1}


def btn(label, custom_id, style=SECONDARY, disabled=False):
    b = {"type": BUTTON, "style": style, "label": clip(label, 80),
         "custom_id": custom_id}
    if disabled:
        b["disabled"] = True
    return b


def section(text, accessory):
    return {"type": SECTION, "components": [td(text)],
            "accessory": accessory}


def row(*components):
    return {"type": ROW, "components": [c for c in components if c]}


def container(children, colour=None):
    c = {"type": CONTAINER, "components": [x for x in children if x]}
    if colour is not None:
        c["accent_color"] = colour
    return c


def opt(label, value, description=None, default=False):
    o = {"label": clip(label, 100), "value": clip(value, 100)}
    if description:
        o["description"] = clip(description, 100)
    if default:
        o["default"] = True
    return o


def sel(custom_id, options, placeholder, min_values=1, max_values=1):
    return {"type": STRING_SELECT, "custom_id": custom_id,
            "options": options[:25], "placeholder": clip(placeholder, 150),
            "min_values": min_values, "max_values": max_values}


def picker(kind, custom_id, placeholder, min_values=0, max_values=25):
    return {"type": kind, "custom_id": custom_id,
            "placeholder": clip(placeholder, 150),
            "min_values": min_values, "max_values": max_values}


def message(R, comps):
    data = {"flags": COMPONENTS_V2 | (EPHEMERAL if R.new else 0),
            "components": finish_text(comps),
            "allowed_mentions": {"parse": []}}
    return {"type": 4 if R.new else 7, "data": data}


LIMITS = {"label": 80, "placeholder": 150}


def _all(comps):
    for c in comps or []:
        if isinstance(c, dict):
            yield c
            yield from _all(c.get("components"))
            for k in ("accessory", "component"):
                if isinstance(c.get(k), dict):
                    yield from _all([c[k]])


def text_size(comps):
    n = 0
    for c in _all(comps):
        for k in ("content", "label", "placeholder", "description"):
            if isinstance(c.get(k), str):
                n += len(c[k])
        for o in c.get("options") or []:
            n += len(o.get("label", "")) + len(o.get("description", ""))
    return n


def finish_text(comps):
    """Labels, options and placeholders as plain text (names unescaped),
    then the page within TEXT_BUDGET: option descriptions dropped, then
    long texts clipped."""
    for c in _all(comps):
        for k, n in LIMITS.items():
            if isinstance(c.get(k), str):
                c[k] = clip(plain(c[k]), n)
        if c.get("type") == LABEL and isinstance(c.get("description"), str):
            c["description"] = clip(plain(c["description"]), 100)
        for o in c.get("options") or []:
            o["label"] = clip(plain(o["label"]), 100)
            if "description" in o:
                o["description"] = clip(plain(o["description"]), 100)
    for c in sorted((c for c in _all(comps) if c.get("options")),
                    key=lambda c: -len(c["options"])):
        if text_size(comps) <= TEXT_BUDGET:
            return comps
        for o in c["options"]:
            o.pop("description", None)
    while text_size(comps) > TEXT_BUDGET:
        tds = [c for c in _all(comps) if c.get("type") == TEXT]
        big = max(tds, key=lambda c: len(c["content"]), default=None)
        if big is None or len(big["content"]) < 80:
            break
        over = text_size(comps) - TEXT_BUDGET
        big["content"] = clip(big["content"],
                              max(60, len(big["content"]) - over - 1))
    return comps


def card(text, colour=None, extra=()):
    return [container([td(text)] + list(extra), colour)]


def notice(text):
    """A new ephemeral message; the page it came from stays as it was."""
    return {"type": 4, "data": {"flags": NEW_FLAGS,
                                "components": card(text),
                                "allowed_mentions": {"parse": []}}}


def modal(custom_id, title, components):
    comps = finish_text(components[:5])
    for c in comps:
        if c.get("type") == LABEL:
            c["label"] = clip(plain(c["label"]), 45)
    return {"type": 9, "data": {"custom_id": custom_id,
                                "title": clip(plain(title), 45),
                                "components": comps}}


def labelled(text, component, description=None):
    c = {"type": LABEL, "label": clip(text, 45), "component": component}
    if description:
        c["description"] = clip(description, 100)
    return c


def text_input(custom_id, style, max_length, value=None, placeholder=None,
               required=False):
    t = {"type": TEXT_INPUT, "custom_id": custom_id, "style": style,
         "max_length": max_length, "required": required}
    if value:
        t["value"] = str(value)[:max_length]
    if placeholder:
        t["placeholder"] = clip(placeholder, 100)
    return t


def count_components(comps):
    n = 0
    for c in comps or []:
        if not isinstance(c, dict):
            continue
        n += 1
        n += count_components(c.get("components"))
        if isinstance(c.get("accessory"), dict):
            n += 1
        if isinstance(c.get("component"), dict):
            n += count_components([c["component"]])
    return n


# ---------------------------------------------------------------------------
# Who and where
# ---------------------------------------------------------------------------
def identify(ix, ctx):
    """(user id, "owner" | "admin" | None, the admin's server id)."""
    user = (ix.get("member") or {}).get("user") or ix.get("user") or {}
    uid = str(user.get("id") or "")
    if uid and uid in {str(o) for o in ctx.owners}:
        return uid, "owner", None
    gid = ix.get("guild_id")
    member = ix.get("member")
    if uid and gid and isinstance(member, dict):
        try:
            perms = int(member.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        if perms & (MANAGE_GUILD | ADMINISTRATOR):
            return uid, "admin", str(gid)
    return uid, None, None


def _harvest_names(ix):
    """Names the interaction itself carries (the clicking user, resolved
    users and members, a context command's message author)."""
    out = {}
    people = set()

    def user(u, member=None):
        if not isinstance(u, dict) or not u.get("id"):
            return
        n = ((member or {}).get("nick") or u.get("global_name")
             or u.get("username"))
        if n:
            out[str(u["id"])] = str(n)
            people.add(str(u["id"]))
    m = ix.get("member") or {}
    user(m.get("user"), m)
    user(ix.get("user"))
    res = (ix.get("data") or {}).get("resolved") or {}
    members = res.get("members") or {}
    for uid, u in (res.get("users") or {}).items():
        user(dict(u, id=uid), members.get(uid))
    for rid, r in (res.get("roles") or {}).items():
        if isinstance(r, dict) and r.get("name"):
            out[str(rid)] = "@" + str(r["name"])
            people.add(str(rid))
    for msg in (res.get("messages") or {}).values():
        if isinstance(msg, dict):
            user(msg.get("author"), msg.get("member"))
    for chid, ch in (res.get("channels") or {}).items():
        if isinstance(ch, dict) and ch.get("name"):
            out[str(chid)] = str(ch["name"])
    return out, people


class Req:
    """One interaction being answered."""

    def __init__(self, ix, ctx):
        self.ix, self.ctx = ix, ctx
        self.st = ctx.settings
        self.uid, self.role, self.admin_gid = identify(ix, ctx)
        self.guild_id = str(ix.get("guild_id") or "") or None
        self.data = ix.get("data") or {}
        self.new = False
        self.banner = None
        self.extra_names, self.extra_users = _harvest_names(ix)
        self.inf = ms._Info(ctx)
        self._chans = {}
        self.fixed = None       # whose refusal a Why fix just fixed
        self.check = None       # DMs: the person checked
        self.thread_note = False

    @property
    def owner(self):
        return self.role == "owner"

    @property
    def admin(self):
        return self.role == "admin"

    @property
    def now(self):
        try:
            return float(self.ctx.now)
        except (TypeError, ValueError, AttributeError):
            return time.time()

    def reload(self):
        fn = getattr(self.ctx, "reload", None)
        try:
            self.st = fn() if callable(fn) else ms.load(self.st.path)
        except Exception as e:  # keep showing the old values; it was saved
            self.ctx.log(f"settings_ui: reload after a write failed: {e}")

    # -- the directory ------------------------------------------------------
    def guilds(self):
        return self.inf.guilds()

    def channels(self, gid):
        gid = str(gid)
        if gid not in self._chans:
            self._chans[gid] = {str(c["id"]): c
                                for c in self.inf.channels(gid) or []}
        return self._chans[gid]

    def chan(self, gid, cid_):
        return self.channels(gid).get(str(cid_))

    def category_of(self, gid, cid_):
        return self.inf.category_of(gid, cid_)

    def members(self, gid):
        return self.inf.members(gid)

    def can_see(self, gid, cid_):
        """False when the bot cannot read the channel."""
        ch = self.channels(gid).get(str(cid_))
        return ch is None or ch.get("readable") is not False

    def look(self, some_id):
        """A name for a server, channel, role or person (None: unknown),
        escaped for a Text Display (see esc)."""
        n = self._look(some_id)
        return esc(n) if n else None

    def _look(self, some_id):
        i = str(some_id)
        if i in self.extra_names:
            return self.extra_names[i]
        g = self.guilds().get(i)
        if g and g.get("name"):
            return str(g["name"])
        for chans in self._chans.values():
            if i in chans and chans[i].get("name"):
                return str(chans[i]["name"])
        for name in ("names", "display_name"):
            fn = getattr(self.ctx, name, None)
            if callable(fn):
                try:
                    n = fn(i)
                except Exception:
                    n = None
                if n:
                    return str(n)
        return (self.st.labels or {}).get(i)

    def person(self, uid):
        """A person's (or role's) name, escaped, or None: only from the
        interaction's resolved data and the people the monitor has seen,
        never a server's or channel's name for a forged id."""
        i = str(uid)
        n = self.extra_names.get(i) if i in self.extra_users else None
        if not n:
            fn = getattr(self.ctx, "display_name", None)
            try:
                n = fn(i) if callable(fn) else None
            except Exception:
                n = None
        if n and str(n).startswith("@"):
            n = str(n)[1:]
        return esc(n) if n else None

    def typed(self):
        """(channel id, in a thread?) where the interaction happened, a
        thread counting as its parent; (None, False) outside a server."""
        if not self.guild_id:
            return None, False
        ch = self.ix.get("channel") or {}
        c = str(self.ix.get("channel_id") or ch.get("id") or "")
        if not _ID.fullmatch(c):
            return None, False
        if ch.get("type") in THREAD_TYPES and ch.get("parent_id"):
            return str(ch["parent_id"]), True
        return c, False

    def here(self):
        """The place the interaction happened: its channel (a thread
        counting as its parent), else its server, else DMs."""
        if not self.guild_id:
            return ms.Place.dms()
        here, _thread = self.typed()
        if not here or (self.chan(self.guild_id, here) or {}).get(
                "type") == CATEGORY:
            return ms.Place.server(self.guild_id)
        return ms.Place.channel(self.guild_id, here,
                                self.category_of(self.guild_id, here))

    def in_guild(self, gid):
        return self.guild_id is not None and self.guild_id == str(gid)

    def place(self, tok):
        """The Place a token names, or None (malformed, a category)."""
        m = _PLACE.fullmatch(tok or "")
        if not m:
            return None
        if tok == "x":
            return ms.Place.defaults()
        if tok == "d":
            return ms.Place.dms()
        if m[1]:
            return ms.Place.server(m[1])
        gid, c = m[2], m[3]
        if (self.chan(gid, c) or {}).get("type") == CATEGORY:
            return None
        return ms.Place.channel(gid, c, self.category_of(gid, c))

    def name(self, place):
        if place.kind == "defaults":
            return "everywhere"
        return ms.place_name(place, self.look)

    def gname(self, gid):
        return self.look(gid) or "this server"


# ---------------------------------------------------------------------------
# Values at a place
# ---------------------------------------------------------------------------
class Val:
    """One setting at one place: path, set here?, value here, source,
    value in force, value without this layer."""

    def __init__(self, R, place, key):
        st = R.st
        self.key = key
        self.place = place
        self.spec = st.spec(key)
        self.path = ms.place_path(st, place, key)
        self.set, self.here, self.src = st.layer(self.path, key)
        self.value = ms.place_value(st, place, key)
        self.inherited = st.value_without(
            ms._scope_for(place, key, self.path), key, self.path)


def value(R, place, key):
    return ms.place_value(R.st, place, key)


def short_date(at):
    try:
        d = datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ")
        return f"{d:%b} {d.day}"
    except ValueError:
        return "earlier"


def ago(R, when):
    """"4 min ago", "3 h ago", "Oct 19" for a unix time or ISO stamp."""
    if isinstance(when, str):
        try:
            when = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return "earlier"
    try:
        secs = max(0, R.now - float(when))
    except (TypeError, ValueError):
        return "earlier"
    if secs < 60:
        return f"{int(secs)} s ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    d = datetime.fromtimestamp(float(when), timezone.utc)
    return f"{d:%b} {d.day}"


def snowflake_time(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except (TypeError, ValueError):
        return None


def who_text(R, by):
    """"you", a mention, or a name such as muse.ai."""
    by = str(by or "")
    if by and by == R.uid:
        return "you"
    if _ID.fullmatch(by):
        return f"<@{by}>"
    return esc(by) if by else "someone"


# ---------------------------------------------------------------------------
# How the bot behaves at a place, in plain words
# ---------------------------------------------------------------------------
REPLIES_WORDS = {"conversational": "I chat freely",
                 "when-addressed": "I reply only when @mentioned",
                 "off": "I'm off"}
LENGTH_WORDS = {"brief": "short replies", "detailed": "detailed replies"}
MEMORY_WORDS = {"everything": "may bring up anything I remember about "
                               "people",
                "public-only": "shares only public facts about people",
                "nothing": "keeps personal things out of it"}
DM_WORDS = {"owners": "Only you can DM me",
            "shared-servers": "Anyone in your servers can DM me",
            "anyone": "Anyone can DM me", "nobody": "Nobody can DM me"}


def paused_at(R, place):
    """The place whose pause keeps the bot quiet at place (place itself,
    its server, or "everywhere"), or None."""
    st = R.st
    servers_off = st.layer(("all_servers",), "who.replies")[:2] == (
        True, "off")
    if place.kind == "dms":
        if ms.is_paused(st, ms.Place.defaults()):
            return ms.Place.defaults()
        return place if ms.is_paused(st, place) else None
    if place.kind == "defaults":
        return place if servers_off else None
    if place.kind == "channel" and ms.is_paused(st, place):
        return place
    server = ms.Place.server(place.guild_id)
    if ms.is_paused(st, server):
        return server
    return ms.Place.defaults() if servers_off else None


def pause_words(R, place, at):
    """"I'm paused here (you, Oct 19)." and the like."""
    v = Val(R, at, "who.replies") if at.kind != "defaults" else None
    who = ""
    if v is not None and v.src and v.src[0] == "discord":
        who = f" ({who_text(R, v.src[1])}, {short_date(v.src[2])})"
    if at.kind == "defaults":
        where = "everywhere"
    elif at == place:
        where = "here"
    else:
        where = f"in all of {R.gname(at.guild_id)}"
    return f"**I'm paused {where}**{who}. I won't reply to anyone"


def status_text(R, place):
    """The page's head: what the bot does at place, in one line."""
    if place.kind == "dms":
        access = ms.dm_access(R.st)
        if access == "list":
            n = len([p for p in value(R, place, "who.dms_from") or []
                     if p not in R.ctx.owners])
            first = (f"You and {n} {'person' if n == 1 else 'people'} you "
                     f"picked can DM me")
        else:
            first = DM_WORDS.get(access, "Only you can DM me")
    else:
        rep = value(R, place, "who.replies")
        first = REPLIES_WORDS.get(rep, "I chat freely")
        if place.kind == "defaults":
            first += " in servers"
    at = paused_at(R, place)
    if at is not None:
        return pause_words(R, place, at) + "."
    parts = [f"**{first}**"]
    if place.kind == "defaults":
        dm = DM_WORDS.get(ms.dm_access(R.st)) \
            or "You and people you picked can DM me"
        parts.append(dm[0].lower() + dm[1:])
    ln = LENGTH_WORDS.get(value(R, place, "style.length"))
    if ln:
        parts.append(ln)
    parts.append(MEMORY_WORDS.get(value(R, place, "privacy.memory"),
                                  MEMORY_WORDS["public-only"]))
    if value(R, place, "media.enabled"):
        parts.append(media_words(R, place))
    cap = value(R, place, "limits.per_person_per_day")
    if isinstance(cap, int):
        parts.append(f"up to {cap:,} replies a person a day")
    return " · ".join(parts)


def media_words(R, place):
    sp = place if place.kind != "channel" \
        else ms.Place.server(place.guild_id)
    who = value(R, sp, "media.who")
    if who == "everyone":
        return "images & video for everyone"
    if isinstance(who, list) and [p for p in who if p not in R.ctx.owners]:
        return "images & video for you and people you picked"
    return "images & video for you"


# ---------------------------------------------------------------------------
# Changes in plain words
# ---------------------------------------------------------------------------
UI_LABELS = {
    "who.replies": "Replies", "who.ignore": "Ignore list",
    "who.allow": "Only answers", "who.bots": "Other bots",
    "who.bot_exchange_limit": "Replies in a row to other bots",
    "who.dms_from": "Who can DM me", "style.length": "Reply length",
    "style.notes": "Instructions", "privacy.memory": "What I may share",
    "privacy.suspicious": "Suspicious messages",
    "limits.per_day": "Replies a day",
    "limits.per_person_per_day": "Replies a person a day",
    "media.enabled": "Images & video",
    "media.who": "Who can ask for images & video",
    "media.per_day": "Images & video a day",
}
UI_VALUES = {("who.replies", "conversational"): "chat freely",
             ("who.replies", "when-addressed"): "only when @mentioned",
             ("who.replies", "off"): "off"}


def ui_label(key):
    return UI_LABELS.get(key) or ms.label(key)


def ui_value(R, key, v):
    if isinstance(v, str) and (key, v) in UI_VALUES:
        return UI_VALUES[(key, v)]
    return ms.value_label(key, v, R.look)


def ui_where(R, place, path=None):
    if place.kind == "defaults":
        if path == ("dms",):
            return "In DMs"
        if path == ("all_servers",):
            return "In every server"
        return "Everywhere"
    if place.kind == "server":
        return f"In all of {R.name(place)}"
    return f"In {R.name(place)}"


def say_change(R, place, key, old, new, path=None):
    """"In #general, Replies: chat freely → off"."""
    return (f"{ui_where(R, place, path)}, {ui_label(key)}: "
            f"{ui_value(R, key, old)} → {ui_value(R, key, new)}")


def say_undo(R, plan):
    """What an Undo of plan would make, as "In #general, Replies back to
    chat freely"."""
    return "; ".join(f"{ui_where(R, ch.place, ch.path)}, {ui_label(ch.key)} "
                     f"back to {ui_value(R, ch.key, new)}"
                     for ch, new in ms.undo_results(R.st, plan))


# ---------------------------------------------------------------------------
# Banner and last-change slot
# ---------------------------------------------------------------------------
def banner_done(R, text, undo_place=None, group=None):
    text = clip(f"✓ **Done.** {text}", 1500)
    if undo_place is not None and group:
        try:
            return [section(text, btn("Undo", cid("un", ptok(undo_place),
                                                  group)))]
        except BadId:   # no Undo without its group (the stale check)
            pass
    return [td(text)]


def banner_error(text):
    return [td(clip(f"**Not saved:** {text}", 1500))]


def undo_scope(R, place):
    """(place an Undo works on, include channels?, admin's server): a
    channel's page undoes its server's newest change (the server's or
    any channel's), since its style applies to the whole server."""
    if R.admin:
        return ms.Place.server(R.admin_gid), True, R.admin_gid
    if place.kind == "channel":
        return ms.Place.server(place.guild_id), True, None
    return place, place.kind == "server", None


def last_author(R, plan):
    """(who, when) of the change an Undo would revert."""
    if not plan:
        return None, None
    ks = ms.format_key(ms.change_path(R.st, plan[0]), plan[0].key)
    for h in reversed((R.st.overlay_doc or {}).get("history") or []):
        if isinstance(h, dict) and h.get("key") == ks and "to" in h \
                and not h.get("undone"):
            return h.get("by"), h.get("at")
    return None, None


def changed_names(plan):
    """"Replies", "Replies and Reply length", "Replies, … and 2 more":
    what a group changed, by name only."""
    names = list(dict.fromkeys(ui_label(ch.key) for ch in plan))
    if len(names) > 2:
        return f"{names[0]}, {names[1]} and {len(names) - 2} more"
    return " and ".join(names)


def last_change_slot(R, place):
    """SEC "-# Last change: Replies · you, 4 min ago" [Undo], or []:
    which settings the newest change touched, not its values."""
    if R.banner or R.st.overlay_error or R.role is None:
        return []
    up, children, admin_gid = undo_scope(R, place)
    try:
        _text, gid = ms.undo_preview(R.st, up, include_children=children,
                                     names=R.look, admin_gid=admin_gid)
        if not gid:
            return []
        plan = ms.undo_plan(R.st, up, children, admin_gid)
    except Exception as e:
        R.ctx.log(f"settings_ui: last change: {e}")
        return []
    by, at = last_author(R, plan)
    when = f" · {who_text(R, by)}, {ago(R, at)}" if by else ""
    head = "Last change by admins" if R.admin else "Last change"
    text = clip(f"-# {head}: {changed_names(plan)}{when}", 300)
    try:
        b = btn("Undo", cid("un", ptok(up), gid))
    except BadId:   # no Undo without its group (the stale check)
        return [td(text)]
    return [section(text, b)]


WRITE_VERBS = ("set", "pre", "val", "ppl", "rm", "un", "pz", "rs", "fx", "ok")


def _verb(c):
    parts = str(c.get("custom_id") or "").split(":")
    return parts[2] if len(parts) > 2 and parts[0] == PREFIX else None


def read_only(comps):
    """The overlay can't be read: write controls removed, a warning on
    top (reading stays)."""
    out = [td(OVERLAY_BROKEN)]
    for c in comps:
        out.append(_strip(c))
    return [c for c in out if c]


def _strip(c):
    t = c.get("type")
    if t == SECTION:
        if _verb(c.get("accessory") or {}) in WRITE_VERBS:
            return c["components"][0]
        return c
    if t == ROW:
        kept = [x for x in c["components"] if _verb(x) not in WRITE_VERBS]
        return dict(c, components=kept) if kept else None
    if t == CONTAINER:
        kept = [x for x in (_strip(y) for y in c["components"]) if x]
        return dict(c, components=kept)
    return c


def finish(R, comps):
    comps = list(R.banner or []) + list(comps)
    if R.st.overlay_error:
        comps = read_only(comps)
    return comps


# ---------------------------------------------------------------------------
# The owner's page
# ---------------------------------------------------------------------------
PRESET_HELP = {
    "friends": "Chats freely, no daily caps, may use what it remembers, "
               "reaches out now and then",
    "community": "Only when @mentioned, public facts only, 20 replies a "
                 "person a day",
    "work": "Only when @mentioned, nothing personal, ignores other bots",
}


def plugin_keys(R, place):
    """The on/off keys of the plugins installed and offered at place
    (plugins.<id>.enabled; never media, which is a core setting)."""
    try:
        keys = ms.place_keys(R.st, place, "basics")
    except Exception:
        return []
    out = []
    for k in keys:
        if not (k.startswith("plugins.") and ms._is_enabled_key(k)):
            continue
        if kcode(k) is None:    # an id no custom_id can carry
            if k not in _UNNAMEABLE:
                _UNNAMEABLE.add(k)
                R.ctx.log(f"settings_ui: plugin key {k} can't be shown "
                          f"(its id must be 1-40 of A-Z a-z 0-9 _ -)")
            continue
        out.append(k)
    return out


_UNNAMEABLE = set()     # plugin keys already logged as skipped


def on_off_name(key):
    """"images & video", or a plugin's name ("Example")."""
    if key == "media.enabled":
        return "images & video"
    parts = key.split(".")
    return esc(parts[1].replace("_", " ").capitalize()) \
        if len(parts) == 3 else esc(key)


def plugins_line(R, place):
    """"-# Plugins here: Example (on). DM me to switch them." or None:
    plugins have no controls of their own on these pages."""
    keys = plugin_keys(R, place)
    if not keys:
        return None
    parts = [f"{on_off_name(k)} ({'on' if value(R, place, k) else 'off'})"
             for k in keys]
    return td("-# Plugins here: " + ", ".join(parts) + ". DM me to switch "
              "them or change their settings.")


def style_select(R, gid):
    server = ms.Place.server(gid)
    cur = ms.matching_preset(R.st, server)
    opts = [opt(title, name, PRESET_HELP.get(name), default=cur == title)
            for name, (title, _v) in ms.PRESETS.items()]
    if cur is None:
        # Not a style to apply: what the select shows while none
        # matches (picking it changes nothing; see v_pre).
        opts.append(opt("Customized", CUSTOMIZED, "Change the settings "
                        "above, or DM me what you want.", default=True))
    return sel(cid("pre", f"s{gid}"), opts, "Pick a style…")


CUSTOMIZED = "custom"


def pause_buttons(R, place):
    """Pause / Resume (and Turn on, when it is off here without a
    pause) for place."""
    at = paused_at(R, place)
    if at is not None:
        if at.kind == "defaults":
            label = "Resume everywhere"
        elif at == place:
            label = "Resume here" if place.kind != "dms" else "Resume DMs"
        else:
            label = "Resume server"
        return [btn(label, cid("rs", ptok(at)), SUCCESS)]
    if place.kind in ("channel", "server") \
            and value(R, place, "who.replies") == "off":
        server = ms.Place.server(place.guild_id)
        to = value(R, server, "who.replies") if place.kind == "channel" \
            else "off"
        return [btn("Turn on here", cid(
            "val", ptok(place), "r",
            to if to != "off" else "when-addressed"), SUCCESS)]
    label = {"dms": "Pause DMs", "channel": "Pause here",
             "server": "Pause server",
             "defaults": "Pause everywhere"}[place.kind]
    return [btn(label, cid("pz", ptok(place)), DANGER)]


# -- Where ---------------------------------------------------------------------
def where_options(R, place):
    """The places the Where select offers: here (the channel /settings
    was typed in, or DMs), its server, everywhere; a server opened from
    the join notice in a DM stays on the list while it is shown."""
    here = R.here()
    out = [here]
    if here.kind == "channel":
        out.append(ms.Place.server(here.guild_id))
    if place.kind == "server" and place not in out:
        out.insert(0, place)
    out.append(ms.Place.defaults())
    return out


def where_select(R, place):
    here = R.here()
    opts = []
    for p in where_options(R, place):
        if p.kind == "channel":
            label, d = f"{R.name(p)} (here)", "Just this channel"
        elif p.kind == "server":
            label = f"All of {R.gname(p.guild_id)}" + (
                " (here)" if p == here else "")
            d = "Every channel that has no setting of its own"
        elif p.kind == "dms":
            label, d = "DMs (here)", "Direct messages with me"
        else:
            label, d = "Everywhere", ("Every server and DM that has no "
                                      "setting of its own")
        opts.append(opt(label, ptok(p), d, default=p == place))
    return sel(cid("wh", ptok(place)), opts, "Where…")


# -- The settings on the page --------------------------------------------------
# The settings each kind of place shows, in order (custom_id codes).
PLACE_CONTROLS = {"channel": ("r", "med"),
                  "server": ("r", "ln", "med", "pp"),
                  "dms": ("df", "ln", "med"),
                  "defaults": ("r", "df", "ln", "med", "pp")}
CAP_CHOICES = (5, 10, 20, 50, 100)
# When I reply's options, by value (and "Off", which is Pause).
REPLY_CHOICES = {"conversational": "Chat freely",
                 "when-addressed": "Only when @mentioned", "off": "Off"}


def follow_option(R, place, key, now):
    """The "=" option of a channel's select, in Discord's own words (its
    notification settings): "Use server default", or "Use category
    default" when the channel's category sets key; now is the label of
    the value that gives."""
    cat = place.category_id
    by_cat = cat and R.st.layer(("servers", place.guild_id, "channels",
                                 cat), key)[0]
    return opt("Use category default" if by_cat else "Use server default",
               "=", f"Currently: {now}")


def only_you(R):
    return "Only you" if len(owners_set(R)) <= 1 else "Only the owners"


def ctl_replies(R, place):
    v = Val(R, place, "who.replies")
    opts = []
    if place.kind == "channel":
        inh = REPLY_CHOICES.get(v.inherited, "Off")
        o = follow_option(R, place, "who.replies", inh)
        if not v.set:
            o["default"] = True
        opts.append(o)
    cur = v.here if place.kind == "channel" else v.value
    for x, label, d in (
            ("conversational", "Chat freely", "Answers @mentions, replies "
             "to me, and follow-ups for 2 minutes"),
            ("when-addressed", "Only when @mentioned", "Answers @mentions "
             "and replies to my messages")):
        opts.append(opt(label, x, d, default=(v.set or place.kind !=
                                               "channel") and cur == x))
    title = "When I reply" + (" in servers" if place.kind == "defaults"
                              else "")
    ph = "Paused · pick one to turn me on" if v.value == "off" \
        else "When I reply…"
    return title, sel(cid("set", ptok(place), "r"), opts, ph)


def ctl_length(R, place):
    cur = value(R, place, "style.length")
    opts = [opt(label, x, d, default=cur == x) for x, label, d in (
        ("brief", "Short", "A line or two"),
        ("normal", "Normal", "As long as the message needs"),
        ("detailed", "Detailed", "Longer, fuller replies"))]
    return "Reply length", sel(cid("set", ptok(place), "ln"), opts,
                               "Reply length…")


def media_now(R, place):
    """"off", "owners", "everyone" or "list" in force at a server or
    everywhere."""
    if not value(R, place, "media.enabled"):
        return "off"
    who = value(R, place, "media.who")
    if isinstance(who, list):
        others = [p for p in who if p not in R.ctx.owners]
        return "list" if others else "owners"
    return who if who in ("owners", "everyone") else "owners"


def ctl_media(R, place):
    opts = []
    if place.kind in ("channel", "dms"):
        v = Val(R, place, "media.enabled")
        if place.kind == "channel":
            inh = "On" if v.inherited else "Off"
            o = follow_option(R, place, "media.enabled", inh)
            if not v.set:
                o["default"] = True
            opts.append(o)
        on = bool(v.here if v.set else v.value)
        pick = v.set or place.kind == "dms"
        opts.append(opt("On", "on", "For the people allowed in the server"
                        if place.kind == "channel" else
                        "For the people allowed everywhere",
                        default=pick and on))
        opts.append(opt("Off", "off", default=pick and not on))
    else:
        cur = media_now(R, place)
        opts.append(opt("Off", "off", "No images or video",
                        default=cur == "off"))
        opts.append(opt(only_you(R), "owners", "Only the bot's owners can "
                        "ask", default=cur == "owners"))
        opts.append(opt("Everyone", "everyone", "Anyone can ask; this can "
                        "cost real money", default=cur == "everyone"))
        if cur == "list":
            opts.append(opt("You and people you picked", "list",
                            "DM me to change who", default=True))
    return "Images & video", sel(cid("set", ptok(place), "med"), opts,
                                 "Images & video…")


def ctl_dms(R, place):
    access = ms.dm_access(R.st)
    opts = [opt(only_you(R), "owners", "Strangers' DMs go unanswered",
                default=access == "owners"),
            opt("Anyone in your servers", "shared-servers",
                "Anyone who shares a server with me",
                default=access == "shared-servers"),
            opt("Anyone", "anyone", "Anyone on Discord, strangers too",
                default=access == "anyone"),
            opt("Nobody", "nobody", "No DM gets an answer, not even yours",
                default=access == "nobody")]
    if access == "list":
        n = len(dm_people(R))
        opts.insert(1, opt(f"You and {n} {'person' if n == 1 else 'people'}"
                           f" you picked", "list", "DM me to change who",
                           default=True))
    return "Who can DM me", sel(cid("set", ptok(place), "df"), opts,
                                "Who can DM me…")


def ctl_cap(R, place):
    cur = value(R, place, "limits.per_person_per_day")
    opts = [opt("No limit", "none", "Owners are never limited",
                default=cur is None)]
    choices = list(CAP_CHOICES)
    if isinstance(cur, int) and cur not in choices:
        choices = sorted(choices + [cur])
    for n in choices:
        opts.append(opt(f"{n:,} a day", str(n),
                        default=cur == n))
    title = "Replies a person a day" + (
        "\n-# Counted across every server and DM together"
        if place.kind == "defaults" else "")
    return title, sel(cid("set", ptok(place), "pp"), opts,
                      "Replies a person a day…")


CONTROLS = {"r": ctl_replies, "ln": ctl_length, "med": ctl_media,
            "df": ctl_dms, "pp": ctl_cap}


def controls_for(R, place):
    """[(title, select)] for the settings place shows."""
    out = []
    for code in PLACE_CONTROLS.get(place.kind, ()):
        out.append(CONTROLS[code](R, place))
    return out


def head_lines(R, place):
    lines = []
    if place.kind == "channel":
        gid = place.guild_id
        lines.append(f"## {R.name(place)}")
        lines.append(status_text(R, place))
        sub = [f"In {R.gname(gid)}"]
        if R.thread_note:
            sub.append(f"threads follow {R.name(place)}")
        if not R.can_see(gid, place.channel_id):
            sub.append(f"I can't see {R.name(place)}, so I never reply "
                       f"there")
        elif value(R, place, "who.replies") == "off" \
                and paused_at(R, place) is None:
            sub.append("off for this channel or its category")
        lines.append("-# " + " · ".join(sub))
    elif place.kind == "server":
        gid = place.guild_id
        n = R.members(gid)
        lines.append(f"## All of {R.gname(gid)}")
        lines.append(status_text(R, place))
        lines.append("-# Every channel without its own setting" + (
            f" · {n:,} members" if n is not None else ""))
    elif place.kind == "dms":
        lines.append("## DMs with me")
        lines.append(status_text(R, place))
    else:
        lines.append("## Everywhere")
        lines.append(status_text(R, place))
        lines.append("-# Every server and DM follows these unless it has "
                     "its own setting.")
    return lines


def page_home(R, place):
    """The screen: how the bot behaves at place, where (here, its server,
    everywhere), a few settings, the server's style, pause, why, the
    last change, and the DM footer."""
    inner = [td("\n".join(head_lines(R, place))),
             td("-# Applies to"), row(where_select(R, place)), sep()]
    for title, s in controls_for(R, place):
        head, _, note = title.partition("\n")
        inner.append(td(f"**{head}**" + (f"\n{note}" if note else "")))
        inner.append(row(s))
    inner.append(plugins_line(R, place))
    if place.kind in ("channel", "server"):
        gid = place.guild_id
        inner.append(sep())
        inner.append(td(f"**Style for all of {R.gname(gid)}**\n"
                        f"-# Applies to the whole server."))
        inner.append(row(style_select(R, gid)))
    inner += last_change_slot(R, place)
    paused = paused_at(R, place) is not None
    buttons = pause_buttons(R, place)
    if place.kind != "defaults":
        buttons.append(btn("Why no reply?",
                           cid("pg", ptok(place), "y")))
    comps = [container(inner, GREY if paused else BLURPLE),
             row(*buttons), td(FOOTER_DM if not R.guild_id
                               else FOOTER_SERVER)]
    return finish(R, comps)


# ---------------------------------------------------------------------------
# Why didn't it reply?
# ---------------------------------------------------------------------------
REFUSED = ("skipped", "quarantined", "enqueue_failed")


def msg_link(gid, channel_id, mid):
    return (f"https://discord.com/channels/{gid or '@me'}/{channel_id}/"
            f"{mid}")


def _setter(R, sc, key):
    """The Place whose layer decides key at scope sc (None: Defaults,
    a category or the built-in value: nothing the UI can fix there)."""
    _v, rows = R.st.trace(sc, key)
    for _name, path, s, _val, _src in reversed(rows):
        if not s:
            continue
        if path[0] in ("builtin", "builtin_all_servers", "alias"):
            return None, path
        return ms.place_of_path(path), path
    return None, None


def _outcome_scope(R, o):
    gid = o.get("guild_id")
    ch = str(o.get("parent_id") or o.get("channel_id") or "")
    if not gid:
        return ms.Scope.dm(), None
    if (R.chan(gid, ch) or {}).get("type") == CATEGORY:
        return ms.Scope.server(gid), None
    place = ms.Place.channel(gid, ch, R.category_of(gid, ch)) \
        if _ID.fullmatch(ch) else ms.Place.server(gid)
    return place.scope("who.replies"), place


def _may_fix(R, changes):
    """The viewer may make these changes (an admin only through the
    admin rule; a fix button is shown only then)."""
    if R.owner:
        return True
    if not R.admin:
        return False
    try:
        ms.admin_changes(R.st, R.admin_gid, changes, R.ctx)
        return True
    except Exception:
        return False


def is_category(R, place):
    return place is not None and place.kind == "channel" and (
        R.chan(place.guild_id, place.channel_id) or {}).get(
        "type") == CATEGORY


def setter_name(R, place):
    if place is None:
        return "every server"
    if is_category(R, place):
        return f"the {R.look(place.channel_id) or 'its'} category"
    if place.kind == "server":
        return f"all of {R.name(place)}"
    return R.name(place)


def _fix_button(R, label, place, key, op):
    """A Why fix button (fx), shown only when the viewer may make the
    changes it makes (fix_changes)."""
    if place is None or is_category(R, place):
        return None
    try:
        if not _may_fix(R, fix_changes(R, place, key, op) or []):
            return None
        return btn(label, cid("fx", ptok(place), kcode(key), op))
    except (BadId, ms.SettingsError):
        return None


DM_ME = " DM me to change it."


def explain_outcome(R, o):
    """(copy, fix button or None, still holds?) for one refused message,
    re-checked against the settings in force now."""
    code = o.get("code") or "other"
    aid = str(o.get("author_id") or "")
    who_m = f"<@{aid}>" if _ID.fullmatch(aid) else (
        f"@{esc(o['author'])}" if o.get("author") else "They")
    short = R.person(aid) if aid else None
    short = short or (esc(o["author"]) if o.get("author") else "them")
    sc, place = _outcome_scope(R, o)
    where = R.name(place) if place else "DMs"
    detail = o.get("detail") if isinstance(o.get("detail"), dict) else {}
    tip = DM_ME if R.owner else ""
    if code in ("replies-off", "dm-off"):
        cur = R.st.get("who.replies", sc)
        if cur != "off":
            return (f"I was off in {where} then.\n-# Not any more: "
                    f"{REPLIES_WORDS.get(cur, 'I reply')} there now."), \
                None, False
        setter, path = _setter(R, sc, "who.replies")
        if setter is None or setter.kind == "defaults":
            return ("I'm paused in every server." if path and path[0] ==
                    "all_servers" else f"I'm off in {where}."), None, True
        s, _v, src = R.st.layer(path, "who.replies")
        by = f" (by {who_text(R, src[1])})" if src and src[0] == \
            "discord" else ""
        return (f"I'm off in {setter_name(R, setter)}{by}.",
                _fix_button(R, "Turn me back on", setter, "who.replies",
                            "="), True)
    if code == "bots-ignore":
        cur = R.st.get("who.bots", sc)
        if cur != "ignore":
            return ("I ignored other bots then.\n-# Not any more: I answer "
                    "bots that address me now."), None, False
        setter, _p = _setter(R, sc, "who.bots")
        return (f"I ignore other bots in {setter_name(R, setter)}.",
                _fix_button(R, "Answer bots that @mention me", setter,
                            "who.bots", "vwhen-addressed"), True)
    if code in ("not-allowed", "dm-not-allowed") and _ID.fullmatch(aid):
        ign = R.st.get("who.ignore", sc) or []
        if aid in ign:
            setter, path = _setter(R, sc, "who.ignore")
            added = ""
            if path:
                _s, _v, src = R.st.layer(path, "who.ignore")
                entry = R.st.overlay.get((path, "who.ignore"))
                if src and src[0] == "discord":
                    role = ", an admin" if ms.entry_role(entry) == "admin" \
                        else ""
                    added = (f" (added by {who_text(R, src[1])}{role}, "
                             f"{short_date(src[2])})")
                elif src and src[0] == "file":
                    added = " (in config.yaml)"
            fix = _fix_button(R, f"Stop ignoring {plain(short)}", setter,
                              "who.ignore", f"-{aid}")
            return (f"I ignore {who_m} in {setter_name(R, setter)}"
                    f"{added}.", fix, True)
        allow = R.st.get("who.allow", sc)
        if isinstance(allow, list) and aid not in allow \
                and aid not in R.ctx.owners:
            setter, _p = _setter(R, sc, "who.allow")
            fix = _fix_button(R, f"Let {plain(short)} talk to me", setter,
                              "who.allow", f"+{aid}")
            return (f"In {setter_name(R, setter)} I only answer people on "
                    f"a list, and {who_m} isn't on it.", fix, True)
        if code == "dm-not-allowed":
            return person_check_text(R, aid, short)[0], None, True
        return (f"{who_m} wasn't allowed then.\n-# Not any more: they can "
                f"get replies now."), None, False
    if code in ("not-allowed", "dm-not-allowed"):
        return "They weren't on the list of people I answer.", None, True
    if code == "cap":
        key = detail.get("key")
        n = detail.get("cap")
        if key == "limits.per_person_per_day" and isinstance(n, int):
            return (f"{who_m} hit the daily limit of {n:,} replies a "
                    f"person.{tip}"), None, True
        if key == "limits.per_day" and isinstance(n, int):
            return (f"I hit the daily limit of {n:,} replies in total."
                    f"{tip}"), None, True
        return f"I hit a daily reply limit.{tip}", None, True
    if code == "watch-cap":
        n, per = detail.get("cap"), detail.get("per")
        if per == "person" and isinstance(n, int):
            return (f"{who_m} asked for a reminder or watch, but already "
                    f"had {n:,} going, the most I keep for one person. I "
                    f"told them to cancel one first."), None, True
        if per == "budget":
            return ("I didn't set the web watch they asked for: checking "
                    "it that often would go past the web checks I do a "
                    "day. I told them so."), None, True
        return ("I didn't set the reminder or watch they asked for: I "
                "already keep as many as I can. I told them so."), \
            None, True
    if code == "watch-held" and detail.get("why"):
        return (f"A reminder or watch for {who_m} came due, but I couldn't "
                f"deliver it where they asked ({esc(str(detail['why']))}). "
                f"I keep trying for a day."), None, True
    if code == "watch-held":
        key, n = detail.get("key"), detail.get("cap")
        which = (f"the daily limit of {n:,} replies a person"
                 if key == "limits.per_person_per_day" and isinstance(n, int)
                 else f"the daily limit of {n:,} replies in total"
                 if isinstance(n, int) else "a daily reply limit")
        return (f"A reminder or watch for {who_m} came due, but they had "
                f"hit {which} (each one counts as a reply). It is held, "
                f"and goes out once the limit allows.{tip}"), None, True
    if code == "watch-not-allowed":
        cur = R.st.get("abilities.reminders", sc)
        if cur == "everyone":
            return ("Only owners could set reminders and watches then.\n-# "
                    "Not any more: everyone can now."), None, False
        return (f"Only some people can set reminders and watches in "
                f"{where}, and {who_m} isn't one of them. I told them "
                f"so.{tip}"), None, True
    if code == "bot-exchange":
        lim = detail.get("limit")
        return ("I'd already answered other bots "
                + (f"{lim} times" if isinstance(lim, int) else "too often")
                + f" in a row in {where}, so I stopped.{tip}"), None, True
    if code == "suspicious-ignored":
        cur = R.st.get("privacy.suspicious", sc)
        if cur != "ignore":
            return ("It looked like a prompt-injection attempt, so I "
                    "dropped it.\n-# Not any more: I hold those for your "
                    "review now."), None, False
        setter, _p = _setter(R, sc, "privacy.suspicious")
        return ("It looked like a prompt-injection attempt, so I dropped "
                "it.", _fix_button(R, "Hold these for my review", setter,
                                   "privacy.suspicious", "vhold-for-review"),
                True)
    if R.admin and code in ("quarantined", "never-share"):
        return "I held it for my owners to look at.", None, True
    text = {
        "quarantined": "I held it for your review; look for my DM.",
        "predates-join": "It was sent before I joined.",
        "ownerless": "None of my owners is in this server, so I answer "
                     "nobody here. An owner must turn replies on here.",
        "no-send": f"I can't send messages in {where}; fix it in "
                   f"Discord's channel permissions.",
        "internal": "I hit an internal error; your Muse can find it in "
                    "monitor.log.",
        "worker-skipped": "I read it and chose not to answer.",
        "send-failed": "Sending my reply failed; your Muse can find why "
                       "in monitor.log.",
        "never-share": "I held my reply: it contained a phrase you never "
                       "share.",
    }.get(code, "I didn't answer it.")
    return text, None, True


def page_why(R, place):
    """The last refused messages that addressed the bot here."""
    if place.kind == "dms":
        return page_why_dms(R)
    tok = ptok(place)
    where = R.name(place)
    inner = [td(f"## Why didn't it reply? · {where}\n-# The last "
                f"{WHY_ENTRIES} messages here that addressed me and got no "
                f"answer, newest first. Ordinary chatter isn't recorded."),
             sep()]
    try:
        got = R.ctx.recent_outcomes(place, 50) or []
    except Exception as e:
        R.ctx.log(f"settings_ui: recent_outcomes: {e}")
        got = []
    shown = 0
    fixes = set()
    most = WHY_ENTRIES - (1 if R.banner else 0)
    for o in got:
        if not isinstance(o, dict) or o.get("status") not in REFUSED:
            continue
        if shown >= most:
            break
        if (o.get("guild_id") or place.guild_id) != place.guild_id:
            continue
        o = dict(o, guild_id=place.guild_id)
        shown += 1
        aid = str(o.get("author_id") or "")
        who_m = f"<@{aid}>" if _ID.fullmatch(aid) else (
            f"@{esc(o['author'])}" if o.get("author") else "Someone")
        mid = str(o.get("message_id") or "")
        link = msg_link(o.get("guild_id"), o.get("channel_id"), mid)
        when = ago(R, o.get("at") or snowflake_time(mid))
        text, fix, holds = explain_outcome(R, o)
        if not aid:
            fix = None   # an older entry: no fix
        if fix is not None:
            # Two refusals with the same cause have the same fix: one
            # button (the newest entry's); a custom_id may appear only
            # once in a message, or Discord refuses the whole page.
            if fix["custom_id"] in fixes:
                fix = None
            else:
                fixes.add(fix["custom_id"])
        body = f"**{who_m}** · [message]({link}) · {when}\n" + text
        if R.fixed and R.fixed == aid and not holds:
            body += "\n" + FIXED_NOTE.format(who=who_m)
        inner.append(section(body, fix) if fix else td(body))
    if not shown:
        inner.append(td(f"Nothing was refused here lately. If someone "
                        f"@mentioned me and got no answer, check that I "
                        f"can see and send messages in {where}."))
    return finish(R, [container(inner, BLURPLE),
                      row(btn("Back", cid("pg", tok, "h")),
                          btn("Refresh", cid("pg", tok, "y")))])


def person_check_text(R, uid, short):
    """(copy, changes that would let them DM it or None)."""
    st = R.st
    access = ms.dm_access(st)
    who_m = f"<@{uid}>"
    if uid in R.ctx.owners:
        if access == "nobody":
            return (f"{who_m} is an owner, but nobody can DM me right "
                    f"now, owners included."), None
        return f"{who_m} is an owner and can always DM me.", None
    people = dm_people(R)
    let = ms.dms_from_changes(st, "list", people + [uid])
    if access == "nobody":
        return f"{short} can't DM me: nobody can right now.", let
    if access == "owners":
        return f"{short} can't DM me: only you can.", let
    if access == "list" and uid not in people:
        return (f"{short} can't DM me: they aren't one of the people you "
                f"picked."), let
    r = st.resolve(ms.Scope.dm())
    if uid in (r["who.ignore"] or []):
        return f"{short} can't DM me: I ignore them in DMs.", None
    allow = r["who.allow"]
    if isinstance(allow, list) and uid not in allow:
        return (f"{short} can't DM me: I only answer a list of people in "
                f"DMs, and they aren't on it."), None
    if access == "shared-servers":
        return f"{short} can DM me if we share a server.", None
    return f"{short} can DM me.", None


def dm_people(R):
    v = value(R, ms.Place.dms(), "who.dms_from")
    return [p for p in v if p not in R.ctx.owners] \
        if isinstance(v, list) else []


def page_why_dms(R):
    """DMs: strangers' DMs aren't recorded, so check a person."""
    inner = [td("## Why didn't it reply? · DMs\n-# DMs from people who "
                "may not DM me aren't recorded. Check a person instead."),
             sep()]
    if R.guild_id or DM_USER_SELECT_SEARCHES:
        inner.append(row(picker(USER_SELECT, cid("wy", "d"),
                                "Check a person…", 1, 1)))
    else:
        inner.append(row(btn("Check someone by ID", cid("mod", "d", "wy"),
                             PRIMARY)))
    if R.check:
        uid = R.check
        short = R.person(uid) or "They"
        text, let = person_check_text(R, uid, short)
        b = None
        if let and R.owner:
            try:
                b = btn(f"Let {plain(R.person(uid) or 'them')} DM me",
                        cid("fx", "d", "df", f"+{uid}"), PRIMARY)
            except BadId:
                b = None
        inner.append(section(text, b) if b else td(text))
    return finish(R, [container(inner, BLURPLE),
                      row(btn("Back", cid("pg", "d", "h")),
                          btn("Refresh", cid("pg", "d", "y")))])


def why_card(R):
    """The message context command's card for one message."""
    data = R.data
    tid = str(data.get("target_id") or "")
    msg = ((data.get("resolved") or {}).get("messages") or {}).get(tid) \
        or {}
    gid = R.guild_id
    if not (R.owner or (R.admin and gid == R.admin_gid)):
        return notice(REFUSAL_WHY)
    ch = str(msg.get("channel_id") or R.ix.get("channel_id") or "")
    here, _thread = R.typed()
    author = msg.get("author") or {}
    aid = str(author.get("id") or "")
    chan = None
    if gid:
        chan = here or ch
        place = ms.Place.channel(gid, chan, R.category_of(gid, chan))
    else:
        place = ms.Place.dms()
    where = R.name(place)
    when = ago(R, snowflake_time(tid))
    link = msg_link(gid, ch, tid)
    who_m = f"<@{aid}>" if aid else "Someone"
    inner = [td(f"## Why didn't it reply?\n-# {who_m}'s [message]({link}) "
                f"in {where}, {when}"), sep()]
    o = None
    fn = getattr(R.ctx, "outcome", None)
    try:
        if callable(fn):
            o = fn(tid)
    except Exception as e:
        R.ctx.log(f"settings_ui: outcome: {e}")
    if isinstance(o, dict):
        # Where the message is comes from the interaction, never from the
        # ledger: an entry for another server (a forged or stale id), or a
        # server message seen from a DM, counts as not recorded.
        if (o.get("guild_id") or None) not in (None, gid) \
                or (not gid and o.get("guild_id")):
            o = None
        elif gid:
            o = dict(o, guild_id=gid, channel_id=ch, parent_id=chan)
        else:
            o = dict(o, guild_id=None, parent_id=None)
    tail = None
    if aid and aid == str(R.ctx.bot_id):
        inner.append(td("That's my own message."))
    elif o and o.get("status") == "replied":
        inner.append(td("I replied."))
    elif o and o.get("status") == "queued":
        inner.append(td(f"I'm still working on it (since "
                        f"{ago(R, o.get('at'))})."))
    elif o and o.get("status") in REFUSED:
        o = dict(o, author_id=aid or o.get("author_id"))
        text, fix, holds = explain_outcome(R, o)
        inner.append(section(text, fix) if fix else td(text))
        if fix:
            short = R.person(aid) or "them"
            tail = (f"-# Fixing this doesn't answer this message; ask "
                    f"{short} to send it again.")
    else:
        inner.append(td(not_recorded(R, msg, place)))
    if tail:
        inner.append(td(tail))
    return [container(inner, BLURPLE),
            row(btn("Open settings", cid("pg", ptok(place), "h")))]


def not_recorded(R, msg, place):
    bot = str(R.ctx.bot_id)
    mentions = [str((m or {}).get("id")) for m in msg.get("mentions") or []]
    ref = (msg.get("referenced_message") or {}).get("author") or {}
    addressed = bot in mentions or str(ref.get("id") or "") == bot
    if place.kind == "dms":
        return ("Not recorded: DMs from people who may not DM me aren't "
                "kept. Use Why no reply? in /settings to check "
                "them.")
    where = R.name(place)
    if addressed:
        return ("Not recorded: I have no record of it, maybe because it's "
                "older than what I keep.")
    rep = value(R, place, "who.replies")
    how = {"off": f"I'm off in {where}",
           "when-addressed": f"in {where} I reply only when @mentioned",
           "conversational": f"in {where} I reply to @mentions, replies "
                             f"and follow-ups within 2 minutes"
           }.get(rep, "")
    return (f"Not recorded: it didn't @mention me or reply to me, and "
            f"{how}.")


# ---------------------------------------------------------------------------
# Server admins: "Quiet the bot"
# ---------------------------------------------------------------------------
QUIETER = ("when-addressed", "off")
QUIET_WORDS = {"when-addressed": "Only when @mentioned", "off": "Off"}


def quieter_values(cur):
    r = ("off", "when-addressed", "conversational")
    if cur not in r:
        return []
    return [v for v in QUIETER if r.index(v) < r.index(cur)]


def admin_ignore(R, gid):
    """(the owners' own ignore list at the server's layer, the admins'
    additions) for a server: an admin's entry holds only those two (the
    engine adds what the owners set elsewhere)."""
    path = ("servers", str(gid))
    s_, pin, _src = ms.owners_view(R.st, gid).layer(path, "who.ignore")
    pin = list(pin) if s_ and isinstance(pin, list) else []
    e = R.st.overlay.get((path, "who.ignore"))
    own = e.get("_norm") if isinstance(e, dict) \
        and ms.entry_role(e) == "admin" else None
    adds = [p for p in (own if isinstance(own, list) else [])
            if p not in pin]
    return pin, adds


def quiet_row(R, place, where):
    """"**In #general:** I chat freely" and its quieter buttons."""
    cur = value(R, place, "who.replies")
    words = REPLIES_WORDS.get(cur, "I chat freely")
    bs = [btn(QUIET_WORDS[x], cid("val", ptok(place), "r", x),
              DANGER if x == "off" else SECONDARY)
          for x in quieter_values(cur)]
    return [td(f"**{where}:** {words}"), row(*bs) if bs else None]


def admin_turned_off(R, gid, key):
    """An admin's entry turned key off for the whole server, and the
    owners have it on there: an admin may turn it back on."""
    e = R.st.overlay.get((("servers", str(gid)), key))
    if ms.entry_role(e) != "admin" or e.get("_norm", e.get("value")) \
            not in (False, "off"):
        return False
    try:
        return bool(ms.owners_view(R.st, gid).get(key, ms.Scope.server(
            gid)))
    except Exception:
        return False


def page_quiet(R, gid):
    """The admin panel for one server (here = where it was typed)."""
    server = ms.Place.server(gid)
    gname = R.gname(gid)
    here = R.here() if R.in_guild(gid) else server
    inner = [td(f"## Quiet the bot in {gname}\n-# You're an admin here, so "
                f"you can make me quieter, never louder. My owners see "
                f"every change."), sep()]
    if here.kind == "channel":
        inner += quiet_row(R, here, f"In {R.name(here)}")
    inner += quiet_row(R, server, f"In all of {gname}")
    inner.append(sep())
    ign = value(R, server, "who.ignore") or []
    _pin, adds = admin_ignore(R, gid)
    parts = []
    for p in ign[:10]:
        m = f"<@&{p[5:]}>" if str(p).startswith("role:") else f"<@{p}>"
        parts.append(m + (" (an admin)" if p in adds else " (the owners)"))
    if len(ign) > 10:
        parts.append(f"and {len(ign) - 10} more")
    inner.append(td("**People I ignore here:** "
                    + (", ".join(parts) if parts else "nobody")))
    inner.append(row(picker(MENTIONABLE_SELECT, cid("ppl", f"s{gid}", "ig"),
                            "Ignore someone…", 1, 25)))
    if adds:
        inner.append(row(sel(cid("rm", f"s{gid}", "ig"), [
            opt((R.person(p[5:]) if str(p).startswith("role:")
                 else R.person(p)) or "Someone an admin added", p)
            for p in adds[:25]], "Stop ignoring…", 1,
            min(25, len(adds)))))
    offs, items = [], []
    for key in ["media.enabled"] + plugin_keys(R, server):
        name = on_off_name(key)
        if value(R, server, key):
            items.append(f"{name} on")
            offs.append(btn(f"Turn off {plain(name)}",
                            cid("val", f"s{gid}", kcode(key), "off"),
                            DANGER))
        elif admin_turned_off(R, gid, key):
            # "=": back to the owners' value (the engine's admin rule).
            items.append(f"{name} off (an admin turned it off)")
            offs.append(btn(f"Turn {plain(name)} back on",
                            cid("val", f"s{gid}", kcode(key), "="),
                            SUCCESS))
    if offs:
        inner.append(td(f"**In {gname}:** " + ", ".join(items)))
        inner.append(row(*offs[:5]))
    inner += last_change_slot(R, server)
    return finish(R, [container(inner, AMBER),
                      row(btn("Why no reply?",
                              cid("pg", ptok(here), "y")))])


# ---------------------------------------------------------------------------
# Confirm card
# ---------------------------------------------------------------------------
def page_confirm(R, origin, lines, warnings, tok, undo=False):
    """A risky outcome; Apply or Cancel (back to the origin page)."""
    place, page = origin
    head = "## Check before undoing" if undo else "## Check before applying"
    body = "\n".join([head] + [clip(x, 300) for x in lines[:8]])
    inner = [td(body), td("**What this means**\n" + "\n".join(
        f"- {clip(w, 400)}" for w in warnings[:6]))]
    return [container(inner, RED),
            row(btn("Undo" if undo else "Apply",
                    cid("ok", ptok(place), tok), DANGER),
                btn("Cancel", cid("pg", ptok(place), page)))]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(R, place, page):
    """The components of one page (owners; server admins get the panel,
    or Why)."""
    if page == "y" and place.kind in ("channel", "server", "dms"):
        return page_why(R, place)
    if R.admin:
        return page_quiet(R, R.admin_gid)
    return page_home(R, place)


def show(R, place, page, banner=None):
    if banner is not None:
        R.banner = banner
    return message(R, render(R, place, page))


def shown(R, fallback):
    """(place, page) the clicked message showed, from its own controls:
    the Why page has a Back (pg:<place>:h); the home page a Why button
    (pg:<place>:y). (fallback, "h") when it can't tell."""
    found = {}
    for c in _all((R.ix.get("message") or {}).get("components")):
        parts = str(c.get("custom_id") or "").split(":")
        if len(parts) == 5 and parts[:3] == [PREFIX, VERSION, "pg"] \
                and parts[4] in PAGES:
            p = R.place(parts[3])
            if p is not None:
                found.setdefault(parts[4], p)
    if "h" in found:
        return found["h"], "y"
    if "y" in found:
        return found["y"], "h"
    return fallback, "h"


# ---------------------------------------------------------------------------
# Writes: every change goes through do_changes (or do_undo), which re-checks
# who may do it, runs assess (owners: a risky outcome shows a confirm card
# first) and writes one undo group with muse_settings.apply.
# ---------------------------------------------------------------------------
def plain_error(e):
    text = str(e).strip()
    return text[0].upper() + text[1:] if text else "That couldn't be saved."


def written_place(res, fallback):
    """The place whose Undo sees the group just written."""
    places = {p.change.place for p in res.written}
    if len(places) == 1:
        return next(iter(places))
    if all(p.kind in ("defaults", "dms") for p in places):
        return ms.Place.defaults()
    servers = {p.guild_id for p in places}
    if len(servers) == 1 and None not in servers:
        return ms.Place.server(next(iter(servers)))
    return fallback


def do_changes(R, origin, changes, confirmed=False, named=(), notes=(),
               headline=None):
    """Apply changes (one group) and show the origin page with a banner,
    or a confirm card, or the error in the banner. headline: the
    banner's words instead of each change (listed small below it)."""
    place, page = origin
    if changes and not confirmed and R.owner:
        changes = [c for c in changes if not ui_noop(R, c)]
        if not changes:
            return show(R, place, page,
                        [td("**No change:** it's already like that.")])
    if not changes:
        return show(R, place, page, [td("**No change.**")])
    if R.st.overlay_error:
        return show(R, place, page,
                    banner_error(OVERLAY_BROKEN.lstrip("⚠ ")))
    if R.owner:
        try:
            a = ms.assess(R.st, changes, R.ctx, R.look, named=named)
        except ms.SettingsError as e:
            return show(R, place, page, banner_error(plain_error(e)))
        if a.risky and not confirmed:
            try:
                planned = ms.plan_changes(R.st, changes)
            except ms.SettingsError as e:
                return show(R, place, page, banner_error(plain_error(e)))
            tok = R.ctx.stash_put(R.uid, {
                "kind": "apply",
                "changes": [dict(c.to_dict(), revert=c.revert)
                            for c in changes],
                "named": list(named), "notes": list(notes),
                "headline": headline,
                "origin": [ptok(place), page]}, STASH_TTL)
            return message(R, page_confirm(
                R, origin, [say_change(R, p.change.place, p.key, p.old,
                                       p.new, p.path)
                            for p in planned if not p.noop]
                or ["No change"], a.warnings, tok))
    try:
        if R.admin:
            res = R.ctx.apply(changes, R.uid, "admin",
                              admin_gid=R.admin_gid)
        elif R.owner:
            res = R.ctx.apply(changes, R.uid, "owner")
        else:
            return notice(REFUSAL_SERVER)
    except ms.SettingsError as e:
        if R.admin:
            return notice(ADMIN_ONLY_QUIETER)
        return show(R, place, page, banner_error(plain_error(e)))
    R.reload()
    if not res.changed:
        return show(R, place, page,
                    [td("**No change:** it's already like that.")])
    if R.admin:
        for p in res.written:
            try:
                R.ctx.notify_owners_admin_change(
                    R.admin_gid, R.uid, p.keystr, p.old, p.new)
            except Exception as e:
                R.ctx.log(f"settings_ui: admin notice: {e}")
    if headline:
        text = headline + "\n-# " + " · ".join(
            f"{ui_label(p.key)}: {ui_value(R, p.key, p.old)} → "
            f"{ui_value(R, p.key, p.new)}" for p in res.written)
    else:
        text = "; ".join(say_change(R, p.change.place, p.key, p.old,
                                    p.new, p.path) for p in res.written)
    if notes:
        text += "\n" + "\n".join(notes)
    up = ms.Place.server(R.admin_gid) if R.admin \
        else written_place(res, place)
    return show(R, place, page, banner_done(R, text, up, res.group))


def ui_noop(R, change):
    """A change that would change nothing as this page sees it: the
    place sets nothing and the value equals what it follows (the engine
    cannot tell a channel with no category from an unknown one, so it
    writes such a value; the page knows)."""
    if change.path is not None or change.revert:
        return False
    try:
        v = Val(R, change.place, change.key)
    except ms.SettingsError:
        return False
    if v.set:
        if change.value == "inherit":
            return False
        ok, norm = ms.normalize(v.spec, change.value)
        return ok and norm == v.here and ms.entry_role(
            R.st.overlay.get((v.path, change.key))) != "admin"
    if change.value == "inherit":
        return True
    ok, norm = ms.normalize(v.spec, change.value)
    if not ok:
        return False
    if isinstance(norm, list) and isinstance(v.inherited, list):
        return sorted(norm) == sorted(v.inherited)
    return norm == v.inherited


def do_undo(R, place, expect=None, origin=None, confirmed=False):
    origin = origin or (place, "h")
    if R.st.overlay_error:
        return show(R, *origin, banner_error(OVERLAY_BROKEN.lstrip("⚠ ")))
    if R.admin:
        if place.kind not in ("server", "channel") \
                or place.guild_id != R.admin_gid:
            return notice(ADMIN_ONLY_QUIETER)
        up, children, admin_gid = ms.Place.server(R.admin_gid), True, \
            R.admin_gid
    else:
        up, children, admin_gid = place, place.kind == "server", None
    try:
        text, gid = ms.undo_preview(R.st, up, include_children=children,
                                    names=R.look, admin_gid=admin_gid)
        plan = ms.undo_plan(R.st, up, children, admin_gid)
    except ms.SettingsError as e:
        return show(R, *origin, banner_error(plain_error(e)))
    if not gid:
        return show(R, *origin, [td("Nothing to undo here.")])
    if not expect or expect != gid:
        text = clip(f"Something changed since. Undo now would make: "
                    f"{say_undo(R, plan)}", 900)
        try:
            R.banner = [section(text, btn("Undo that",
                                          cid("un", ptok(up), gid)))]
        except BadId:   # no Undo without its group (the stale check)
            R.banner = [td(text)]
        return show(R, *origin)
    if R.owner and not confirmed:
        try:
            a = ms.assess(R.st, plan, R.ctx, R.look)
        except ms.SettingsError:
            a = None
        if a is not None and a.risky:
            tok = R.ctx.stash_put(R.uid, {
                "kind": "undo", "place": ptok(up), "expect": gid,
                "origin": [ptok(origin[0]), origin[1]]}, STASH_TTL)
            lines = ["Undo would make: " + clip(say_undo(R, plan), 600)]
            return message(R, page_confirm(R, origin, lines, a.warnings,
                                           tok, undo=True))
    text = say_undo(R, plan)
    try:
        kw = {"children": children, "expect": gid}
        if admin_gid:
            kw["admin_gid"] = admin_gid
        keys = R.ctx.undo(up, R.uid, **kw)
    except ms.SettingsError as e:
        if R.admin:
            return notice(ADMIN_ONLY_QUIETER)
        return show(R, *origin, banner_error(plain_error(e)))
    if not keys:
        return show(R, *origin, [td("Nothing to undo here.")])
    if R.admin:
        before = R.st
        R.reload()
        for ch in plan:
            try:
                sc = ms._scope_for(ch.place, ch.key, ch.path)
                R.ctx.notify_owners_admin_change(
                    R.admin_gid, R.uid,
                    ms.format_key(ms.change_path(before, ch), ch.key),
                    before.get(ch.key, sc), R.st.get(ch.key, sc))
            except Exception as e:
                R.ctx.log(f"settings_ui: admin notice: {e}")
    R.reload()
    return show(R, *origin, [td(clip(f"↩ **Undone.** {text}", 900))])


# ---------------------------------------------------------------------------
# Building changes from a click
# ---------------------------------------------------------------------------
def people_from(R, values, place):
    """([ids and "role:<id>"], roles dropped?) from a user / mentionable
    select's values. Only ids Discord resolved count (a forged value is
    dropped), and a role only for the server the interaction is in: a
    role id means nothing in another server."""
    res = R.data.get("resolved") or {}
    users = {str(u) for u in (res.get("users") or {})} | \
        {str(u) for u in (res.get("members") or {})}
    roles = {str(r) for r in (res.get("roles") or {})}
    own = place.guild_id is not None and R.in_guild(place.guild_id)
    out, dropped = [], False
    for v in values:
        if v in roles:
            if own:
                out.append(f"role:{v}")
            else:
                dropped = True
        elif v in users and _ID.fullmatch(v):
            out.append(v)
    return list(dict.fromkeys(out)), dropped


def list_change(R, place, key, new):
    """A Change setting a people list, or a reset when it matches what
    the place would inherit anyway."""
    v = Val(R, place, key)
    if key == "who.allow" and not new:
        return ms.Change(place, key, "inherit")
    if key == "who.ignore" and not new and not v.inherited:
        return ms.Change(place, key, "inherit")
    if isinstance(v.inherited, list) and sorted(v.inherited) == sorted(new):
        return ms.Change(place, key, "inherit")
    return ms.Change(place, key, new)


def owners_set(R):
    return {str(o) for o in R.ctx.owners}


def fix_changes(R, place, key, op):
    """The changes a Why fix makes (None: malformed)."""
    if op == "=":
        return [ms.Change(place, key, "inherit")]
    if op.startswith("v") and len(op) > 1:
        return [ms.Change(place, key, op[1:])]
    if op[:1] in "+-" and _ID.fullmatch(op[1:]):
        uid = op[1:]
        if key == "who.dms_from":
            if place.kind != "dms" or op[0] != "+":
                return None
            return ms.dms_from_changes(R.st, "list", dm_people(R) + [uid])
        spec = R.st.spec(key)
        if spec is None or spec.type != "people":
            return None
        if R.admin and key == "who.ignore" and op[0] == "-" \
                and place.kind == "server":
            pin, adds = admin_ignore(R, place.guild_id)
            left = pin + [p for p in adds if p != uid]
            return [ms.Change(place, key, left if left != pin or not adds
                              else "inherit")]
        cur = value(R, place, key)
        cur = list(cur) if isinstance(cur, list) else []
        new = cur + [uid] if op[0] == "+" else [p for p in cur if p != uid]
        return [list_change(R, place, key, list(dict.fromkeys(new)))]
    return None


def parse_ids(text):
    return list(dict.fromkeys(re.findall(r"\d{15,25}", str(text or ""))))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle(interaction, ctx):
    """The full interaction callback body ({"type": 4|7|9, "data": ...})
    for /settings, the "Why no reply?" message command, or a component /
    modal submit whose custom_id starts with "mdset:". Never raises: an
    error is logged and answered with an ephemeral apology. No network."""
    try:
        return _handle(interaction, ctx)
    except Exception as e:
        try:
            ctx.log(f"settings_ui: {type(e).__name__}: {e}\n"
                    + traceback.format_exc())
        except Exception:
            pass
        return notice(APOLOGY)


def not_there(R, gid):
    """The bot is not in server gid (when the server list is known)."""
    gs = R.guilds()
    return bool(gs) and str(gid) not in gs


def refusal(R):
    return notice(REFUSAL_SERVER if R.guild_id else REFUSAL_DM)


def _handle(ix, ctx):
    R = Req(ix, ctx)
    data = R.data
    t = ix.get("type")
    if t == 2:
        R.new = True
        if data.get("type") == 3 or data.get("name") in WHY_COMMANDS:
            if data.get("name") not in WHY_COMMANDS:
                return notice(OUT_OF_DATE)
            got = why_card(R)
            return got if isinstance(got, dict) else message(R, got)
        if data.get("name") != COMMAND:
            return notice(OUT_OF_DATE)
        return on_command(R)
    if t not in (3, 5):
        return notice(OUT_OF_DATE)
    custom = str(data.get("custom_id") or "")
    parts = custom.split(":")
    if parts[0] != PREFIX or len(parts) < 2 or len(custom) > CUSTOM_ID_MAX:
        return notice(OUT_OF_DATE)
    if parts[1] == "open":
        # The join DM's Configure button (kept: join DMs are durable).
        if len(parts) != 4 or parts[2] != "server" \
                or not _ID.fullmatch(parts[3]):
            return notice(OUT_OF_DATE)
        if not R.owner:
            return refusal(R)
        if not_there(R, parts[3]):
            return notice(NOT_THERE)
        R.new = True
        return show(R, ms.Place.server(parts[3]), "h")
    if parts[1] != VERSION or len(parts) < 4:
        return notice(OUT_OF_DATE)
    if R.role is None:
        return refusal(R)
    verb, args = parts[2], parts[3:]
    place = R.place(args[0])
    if place is None:
        return notice(OUT_OF_DATE)
    if R.admin and not admin_may(R, verb, place, args):
        return notice(ADMIN_ONLY_QUIETER)
    fn = VERBS.get(verb)
    if fn is None:
        return notice(OUT_OF_DATE)
    values = [str(v) for v in data.get("values") or []]
    return fn(R, place, args[1:], values)


def admin_may(R, verb, place, args):
    """What a server admin may even try (the engine's admin rule checks
    every write again): their own server's panel, Why, and quieter
    writes there."""
    if place.kind not in ("server", "channel") \
            or place.guild_id != R.admin_gid:
        return False
    if place.kind == "channel" and R.chan(place.guild_id,
                                          place.channel_id) is None:
        return False
    if verb == "pg":
        return len(args) == 2 and args[1] in PAGES
    return verb in ("val", "ppl", "rm", "un", "fx", "pz", "ok")


def on_command(R):
    """/settings: owners get the place they typed it in; server admins
    the "Quiet the bot" panel; anyone else a refusal."""
    if R.role is None:
        return refusal(R)
    if R.admin:
        return message(R, page_quiet(R, R.admin_gid))
    R.thread_note = R.typed()[1]
    return show(R, R.here(), "h")


def gone(R, place):
    """The page to show instead when place no longer exists (a deleted
    channel, a server the bot left), else None."""
    if place.kind == "channel":
        chans = R.channels(place.guild_id)
        if chans and place.channel_id not in chans \
                and place.channel_id not in R.st.channels_listed(
                    place.guild_id):
            return show(R, ms.Place.server(place.guild_id), "h",
                        [td("That channel no longer exists.")])
    if place.kind in ("server", "channel"):
        gs = R.guilds()
        if gs and place.guild_id not in gs \
                and place.guild_id not in R.st.servers_listed():
            return show(R, R.here(), "h",
                        [td("I'm not in that server any more.")])
    return None


def _key(R, code):
    key = kdecode(code)
    if key is None or R.st.spec(key) is None:
        return None
    return key


def v_pg(R, place, args, values):
    if len(args) != 1 or args[0] not in PAGES:
        return notice(OUT_OF_DATE)
    return gone(R, place) or show(R, place, args[0])


def v_wh(R, place, args, values):
    """The Where select: here, its server, or everywhere."""
    if not R.owner:
        return refusal(R)
    if args or len(values) != 1:
        return notice(OUT_OF_DATE)
    target = R.place(values[0])
    if target is None or target not in where_options(R, place):
        return notice(OUT_OF_DATE)
    if target.guild_id and not_there(R, target.guild_id):
        return notice(NOT_THERE)
    return gone(R, target) or show(R, target, "h")


def set_changes(R, place, code, v):
    """The changes one of the page's selects makes, None (not a value
    it offers), or a str (nothing to change: the banner's words)."""
    kind = place.kind
    if code == "r":
        if v == "=" and kind == "channel":
            return [ms.Change(place, "who.replies", "inherit")]
        if v in ("conversational", "when-addressed"):
            return [ms.Change(place, "who.replies", v)]
        return None
    if code == "ln":
        if v in ("brief", "normal", "detailed"):
            return [ms.Change(place, "style.length", v)]
        return None
    if code == "med":
        if kind in ("channel", "dms"):
            if v == "=" and kind == "channel":
                return [ms.Change(place, "media.enabled", "inherit")]
            if v in ("on", "off"):
                return [ms.Change(place, "media.enabled", v)]
            return None
        if v == "off":
            return [ms.Change(place, "media.enabled", "off")]
        if v in ("owners", "everyone"):
            return [ms.Change(place, "media.enabled", "on"),
                    ms.Change(place, "media.who", v)]
        if v == "list":
            return "You already picked who can. DM me to change who."
        return None
    if code == "df":
        if v in ("owners", "shared-servers", "anyone", "nobody"):
            return ms.dms_from_changes(R.st, v)
        if v == "list":
            return "You already picked who can DM me. DM me to change who."
        return None
    if code == "pp":
        if v == "none":
            return [ms.Change(place, "limits.per_person_per_day", "none")]
        if _ID.fullmatch(v):
            ok, _n = ms.normalize(R.st.spec("limits.per_person_per_day"), v)
            return [ms.Change(place, "limits.per_person_per_day",
                              int(v))] if ok else None
        return None
    return None


def v_set(R, place, args, values):
    """One of the page's settings selects."""
    if not R.owner:
        return refusal(R)
    if len(args) != 1 or len(values) != 1 \
            or args[0] not in PLACE_CONTROLS.get(place.kind, ()):
        return notice(OUT_OF_DATE)
    changes = set_changes(R, place, args[0], values[0])
    if changes is None:
        return notice(OUT_OF_DATE)
    origin = shown(R, place)
    if isinstance(changes, str):
        return show(R, *origin, [td(f"**No change:** {changes}")])
    return do_changes(R, origin, changes)


# The keys an owner's val button may set (the page's own controls).
OWNER_VAL_KEYS = ("who.replies", "media.enabled")


def v_val(R, place, args, values):
    if len(args) != 2:
        return notice(OUT_OF_DATE)
    key = _key(R, args[0])
    if key is None or (R.owner and key not in OWNER_VAL_KEYS):
        return notice(OUT_OF_DATE)
    write = "inherit" if args[1] == "=" else args[1]
    if write != "inherit":
        ok, _norm = ms.normalize(R.st.spec(key), write)
        if not ok:
            return notice(OUT_OF_DATE)
    return do_changes(R, shown(R, place), [ms.Change(place, key, write)])


def v_pre(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if args or place.kind != "server" or not values:
        return notice(OUT_OF_DATE)
    if values[0] == CUSTOMIZED:     # the "Customized" option: nothing
        return show(R, *shown(R, place))
    if values[0] not in ms.PRESETS:
        return notice(OUT_OF_DATE)
    name = values[0]
    try:
        changes = ms.preset_changes(R.st, name, place, R.ctx, big="skip")
    except ms.SettingsError as e:
        return show(R, *shown(R, place), banner_error(plain_error(e)))
    title = ms.PRESETS[name][0]
    notes = []
    members = R.members(place.guild_id)
    if title == "Friends" and members is not None \
            and members > ms.BIG_FOR_MEMORY:
        gname = R.gname(place.guild_id)
        notes.append(f"-# I still keep to public facts: {gname} has more "
                     f"than {ms.BIG_FOR_MEMORY} members. DM me if you're "
                     f"sure.")
    if not changes:
        if notes:   # Friends, held back only by the big-server rule
            R.banner = [td(f"**No change:** {R.gname(place.guild_id)} has "
                           f"more than {ms.BIG_FOR_MEMORY} members, too "
                           f"many for Friends' sharing of private facts, "
                           f"so I keep to public facts there. Everything "
                           f"else already matches Friends.")]
        else:
            R.banner = [td(f"**No change:** {R.gname(place.guild_id)} "
                           f"already has the {title} style.")]
        return show(R, *shown(R, place))
    return do_changes(R, shown(R, place), changes, notes=notes,
                      headline=f"{R.gname(place.guild_id)} now has the "
                               f"{title} style.")


def v_ppl(R, place, args, values):
    """Admins: add people or roles to the server's ignore list."""
    if not R.admin or args != ["ig"] or place.kind != "server":
        return notice(ADMIN_ONLY_QUIETER if R.admin else OUT_OF_DATE)
    picked, dropped = people_from(R, values, place)
    if not picked:
        return notice(OUT_OF_DATE)
    owners = owners_set(R)
    kept = [p for p in picked if p not in owners]
    notes = ["Owners can't be ignored."] if len(kept) < len(picked) else []
    if dropped:
        notes.append(ROLES_ELSEWHERE)
    pin, adds = admin_ignore(R, place.guild_id)
    new = list(dict.fromkeys(pin + adds + kept))
    return do_changes(R, (place, "h"), [ms.Change(place, "who.ignore", new)],
                      notes=notes)


def v_rm(R, place, args, values):
    """Admins: take their own additions off the ignore list."""
    if not R.admin or args != ["ig"] or place.kind != "server" \
            or not values:
        return notice(ADMIN_ONLY_QUIETER if R.admin else OUT_OF_DATE)
    pin, adds = admin_ignore(R, place.guild_id)
    if set(values) - set(adds):
        return notice(ADMIN_ONLY_QUIETER)
    left = pin + [p for p in adds if p not in set(values)]
    return do_changes(R, (place, "h"), [ms.Change(
        place, "who.ignore", left if left != pin or not adds
        else "inherit")])


def v_mod(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if args != ["wy"] or place.kind != "dms":
        return notice(OUT_OF_DATE)
    return modal(cid("sub", "d", "wy"), "Check a person", [
        td("In Discord: Settings › Advanced › Developer Mode, then "
           "right-click a person › Copy User ID."),
        labelled("User ID", text_input("ids", 1, 600, None, None, True),
                 "One ID or @mention.")])


def v_sub(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if R.ix.get("type") != 5 or args != ["wy"] or place.kind != "dms":
        return notice(OUT_OF_DATE)
    comps = []
    for c in _all(R.data.get("components") or []):
        if c.get("custom_id") == "ids":
            comps.append(str(c.get("value") or ""))
    ids = parse_ids(" ".join(comps))
    if not ids:
        R.banner = banner_error("No user ID found. Copy one with Developer "
                                "Mode on.")
    else:
        R.check = ids[0]
    return show(R, place, "y")


def v_ok(R, place, args, values):
    if len(args) != 1:
        return notice(OUT_OF_DATE)
    try:
        payload = R.ctx.stash_take(R.uid, args[0])
    except PermissionError:
        return notice(WRONG_USER)
    if not R.owner:
        return refusal(R) if not R.admin else notice(ADMIN_ONLY_QUIETER)
    if not isinstance(payload, dict) or payload.get("kind") not in (
            "apply", "undo"):
        return message(R, [container([td(EXPIRED)], BLURPLE),
                           row(btn("Open settings",
                                   cid("pg", ptok(place), "h")))])
    otok, opage = (payload.get("origin") or [ptok(place), "h"])[:2]
    oplace = R.place(otok) or place
    opage = opage if opage in PAGES else "h"
    if payload["kind"] == "undo":
        up = R.place(payload.get("place") or "")
        if up is None:
            return notice(OUT_OF_DATE)
        return do_undo(R, up, payload.get("expect"), (oplace, opage),
                       confirmed=True)
    try:
        changes = []
        for d in payload.get("changes") or []:
            c = ms.Change.from_dict(d, R.ctx)
            c.revert = bool(d.get("revert"))
            changes.append(c)
    except ms.SettingsError:
        return notice(OUT_OF_DATE)
    return do_changes(R, (oplace, opage), changes, confirmed=True,
                      named=payload.get("named") or (),
                      notes=payload.get("notes") or (),
                      headline=payload.get("headline"))


def v_un(R, place, args, values):
    # un:<place>:<group>: the group the button was made for, always; an
    # Undo without it would skip the stale check.
    if len(args) != 1 or not args[0]:
        return notice(OUT_OF_DATE)
    return do_undo(R, place, args[0], shown(R, place))


def v_pz(R, place, args, values, resume=False):
    if args:
        return notice(OUT_OF_DATE)
    origin = shown(R, place)
    if resume:
        changes = ms.resume_changes(R.st, place)
        if not changes:
            return show(R, *origin,
                        [td("**No change:** it isn't paused there.")])
    else:
        changes = ms.pause_changes(place)
    return do_changes(R, origin, changes)


def v_rs(R, place, args, values):
    if R.admin:
        return notice(ADMIN_ONLY_QUIETER)
    return v_pz(R, place, args, values, resume=True)


def v_fx(R, place, args, values):
    if len(args) != 2:
        return notice(OUT_OF_DATE)
    key = _key(R, args[0])
    if key is None:
        return notice(OUT_OF_DATE)
    if R.admin and key == "who.ignore" and args[1].startswith("-") \
            and args[1][1:] not in admin_ignore(R, place.guild_id)[1]:
        return notice(ADMIN_ONLY_QUIETER)
    try:
        changes = fix_changes(R, place, key, args[1])
    except ms.SettingsError:    # not a setting of that place
        changes = None
    if not changes:
        return notice(OUT_OF_DATE)
    if args[1][:1] in "+-":
        R.fixed = args[1][1:]
    if place.kind == "dms" and key == "who.dms_from":
        R.check = args[1][1:]
        return do_changes(R, (ms.Place.dms(), "y"), changes,
                          named=[args[1][1:]])
    origin, page = shown(R, place)
    return do_changes(R, (origin, "y"), changes)


def v_wy(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if place.kind != "dms" or len(args) > 1:
        return notice(OUT_OF_DATE)
    uid = args[0] if args else (values[0] if values else None)
    if not uid or not _ID.fullmatch(uid):
        return notice(OUT_OF_DATE)
    R.check = uid
    return show(R, place, "y")


VERBS = {"pg": v_pg, "wh": v_wh, "set": v_set, "val": v_val, "pre": v_pre,
         "ppl": v_ppl, "rm": v_rm, "mod": v_mod, "sub": v_sub, "ok": v_ok,
         "un": v_un, "pz": v_pz, "rs": v_rs, "fx": v_fx, "wy": v_wy}
