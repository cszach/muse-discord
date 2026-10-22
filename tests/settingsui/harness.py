"""The /settings fixtures and fake Ctx shared by the settingsui suites
and the crawler (crawl.py): an owner, a server admin (Manage Server in
Muse Lounge) and a member; Muse Lounge (1,240 members), Study Group (9)
and Pixel Plaza (48,213) plus 60 more servers; a Staff category; 80
channels in Muse Lounge; 20-digit ids; a ledger with every reason; a
config.yaml value the overlay overrides; a hidden "answer" value; and
(on request) an unreadable overlay. Writes go through muse_settings
into a temp dir, as the monitor's SettingsCtx does.
MUT_DIR = the package tree under test."""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR as MUT, scratch  # noqa: E402

sys.path.insert(0, MUT)
import muse_settings as MS  # noqa: E402
import settings_ui as UI  # noqa: E402,F401 (re-exported)

PLUGINS = os.path.join(MUT, "plugins")
# The custom_id prefix the pages render now ("mdset:<version>:").
P = f"{UI.PREFIX}:{UI.VERSION}:"

# 20-digit snowflakes (the longest real ids).
OWNER = "11100000000000000001"
OWNER2 = "11100000000000000002"
ADMIN = "22200000000000000001"      # Riley: Manage Server in Muse Lounge
MEMBER = "33300000000000000001"
SAM = "44400000000000000001"
KAI = "44400000000000000002"
ANA = "44400000000000000003"
SPAM = "44400000000000000004"
BOB = "44400000000000000005"
OTHERBOT = "44400000000000000009"
BOT = "99900000000000000001"
ROLE = "55500000000000000001"
G1 = "60000000000000000001"     # Muse Lounge
G2 = "60000000000000000002"     # Study Group
G3 = "60000000000000000003"     # Pixel Plaza
CAT = "70000000000000000001"    # Staff (category) in Muse Lounge
GENERAL = "70000000000000000002"    # in Staff
MUSECHAT = "70000000000000000003"
WELCOME = "70000000000000000004"    # Muse Lounge's system channel
LOBBY = "70000000000000000005"      # the bot can't post there
HIDDEN = "70000000000000000006"     # the bot can't read it
THREAD = "70000000000000000007"     # a thread in #muse-chat
HOMEWORK = "70000000000000000101"   # Study Group
ART = "70000000000000000201"        # Pixel Plaza
MANAGE = str(1 << 5)
FIXED_AT = "2026-10-19T10:00:00Z"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp()

NAMES = {OWNER: "Zach", OWNER2: "Mia", ADMIN: "Riley", MEMBER: "Lee",
         SAM: "Sam", KAI: "Kai", ANA: "Ana", SPAM: "spam1", BOB: "Bob",
         OTHERBOT: "OtherBot", ROLE: "@Mods"}

GUILDS = [
    {"id": G1, "name": "Muse Lounge", "member_count": 1240,
     "system_channel_id": WELCOME},
    {"id": G2, "name": "Study Group", "member_count": 9,
     "system_channel_id": None},
    {"id": G3, "name": "Pixel Plaza", "member_count": 48213,
     "system_channel_id": ART},
] + [{"id": f"6100000000000000{i:04d}", "name": f"Server {i:02d}",
      "member_count": 100 + i, "system_channel_id": None}
     for i in range(1, 61)]


def _chan(cid, name, type_=0, parent=None, readable=True, can_send=True):
    return {"id": cid, "name": name, "type": type_, "parent_id": parent,
            "readable": readable, "can_send": can_send}


CHANNELS = {
    G1: [_chan(CAT, "Staff", 4),
         _chan(GENERAL, "general", 0, CAT),
         _chan(MUSECHAT, "muse-chat"),
         _chan(WELCOME, "welcome"),
         _chan(LOBBY, "lobby", can_send=False),
         _chan(HIDDEN, "hidden", readable=False, can_send=None)]
    + [_chan(f"7100000000000000{i:04d}", f"room-{i:03d}")
       for i in range(1, 75)],
    G2: [_chan(HOMEWORK, "homework")],
    G3: [_chan(ART, "art")],
}

BASE = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
everywhere:
  privacy:
    never_share: ["phrase one", "phrase two", "phrase three"]
servers:
  "{G1}":
    name: Muse Lounge
    who:
      replies: when-addressed
      ignore: ["{BOB}"]
    style:
      notes: "Friendly community. Playful is fine."
    channels:
      "{CAT}":
        replies: "off"
  "{G2}":
    name: Study Group
    privacy:
      suspicious: answer
"""


HOSTILE_MD = ("**b** __u__ @everyone @here <@&55500000000000000001> "
              "[x](https://evil.example) ||s|| `c` # h https://e.x/a "
              "\u202e ")


def go_hostile():
    """The hostile fixtures (review B): 100-character names full of
    markdown, links and mentions, 200 servers, 280 channels in Muse
    Lounge, hostile display names. Changes this module's fixtures in
    place: call it only in a process of its own (test_crawl_hostile)."""
    def long(base, n=100):
        return ((base + " " + HOSTILE_MD) * 3)[:n]
    GUILDS[:] = [dict(g, name=long(g["name"])) for g in GUILDS] + [
        {"id": f"6200000000000000{i:04d}", "name": long(f"Extra {i}"),
         "member_count": 10, "system_channel_id": None}
        for i in range(1, 138)]
    for chans in CHANNELS.values():
        for c in chans:
            c["name"] = long(c["name"])
    CHANNELS[G1] += [_chan(f"7200000000000000{i:04d}", long(f"zz-{i:03d}"))
                     for i in range(1, 201)]
    for k in list(NAMES):
        NAMES[k] = ("**" + NAMES[k] + "** @everyone [x](https://e.x)")[:32]


def sf(ts, seq=1):
    return str(((int(ts * 1000) - 1420070400000) << 22) + seq)


def _outcome(n, mins, code, status="skipped", channel=MUSECHAT, guild=G1,
             author=SAM, detail=None, parent=None):
    at = NOW - mins * 60
    return {"message_id": sf(at, n), "channel_id": channel,
            "guild_id": guild, "parent_id": parent, "author_id": author,
            "author": (NAMES.get(author) or "someone").lower()
            if author else "olduser",
            "status": status, "code": code, "detail": detail or {},
            "at": at, "reason": code}


OUTCOMES = [
    _outcome(1, 4, "not-allowed", author=SPAM),
    _outcome(2, 60, "cap", author=ANA,
             detail={"key": "limits.per_person_per_day",
                     "counter": f"server:{G1}", "cap": 5}),
    _outcome(3, 180, "replies-off", channel=GENERAL, author=KAI),
    _outcome(4, 200, "bot-exchange", author=OTHERBOT,
             detail={"limit": 4}),
    _outcome(5, 220, "suspicious-ignored", author=SAM),
    _outcome(6, 240, "quarantined", status="quarantined", author=SAM),
    _outcome(7, 260, "predates-join", author=SAM),
    _outcome(8, 280, "no-send", channel=LOBBY, author=SAM),
    _outcome(9, 300, "internal", status="enqueue_failed", author=SAM),
    _outcome(10, 320, "worker-skipped", author=SAM),
    _outcome(11, 340, "send-failed", author=SAM),
    _outcome(12, 360, "bots-ignore", author=OTHERBOT),
    _outcome(13, 380, "other", author=None),
    _outcome(14, 2, "replied", status="replied", author=KAI),
    _outcome(15, 1, "queued", status="queued", author=ANA),
    _outcome(16, 30, "not-allowed", author=SAM, channel=THREAD,
             parent=MUSECHAT),
    _outcome(17, 10, "not-allowed", author=KAI, guild=G3, channel=ART),
    # The same cause twice (one fix each would be a duplicate custom_id).
    _outcome(18, 6, "not-allowed", author=SPAM),
    _outcome(19, 190, "replies-off", channel=GENERAL, author=SAM),
    _outcome(20, 365, "bots-ignore", author=OTHERBOT),
]


def undo_id(ctx, place, admin_gid=None):
    """The Undo custom_id a page renders for place now (with its group),
    or None when there is nothing to undo."""
    _t, gid = MS.undo_preview(ctx.reload(), place,
                              include_children=place.kind == "server",
                              admin_gid=admin_gid)
    tok = {"defaults": "x", "dms": "d"}.get(place.kind) or (
        f"s{place.guild_id}" if place.kind == "server"
        else f"c{place.guild_id}.{place.channel_id}")
    return f"{P}un:{tok}:{gid}" if gid else None


def install_determinism():
    """Fixed timestamps and group ids, so runs are repeatable."""
    counter = {"n": 0}

    def gid(prefix="g"):
        counter["n"] += 1
        return f"{prefix}{counter['n']:010x}"
    MS._now = lambda: FIXED_AT
    MS.new_group_id = gid
    return counter


class FakeCtx:
    """What monitor.SettingsCtx gives settings_ui.handle."""

    def __init__(self, text=BASE, owners=(OWNER,), seed=True, guilds=None,
                 channels=None, outcomes=None, broken_overlay=False):
        d = scratch("muse-ui-")
        self.dir = d
        self.cfg = os.path.join(d, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write(text.replace(f'owners: ["{OWNER}"]', "owners: ["
                                 + ", ".join(f'"{o}"' for o in owners)
                                 + "]"))
        self.owners = set(owners)
        self.bot_id = BOT
        self.now = NOW
        self._guilds = GUILDS if guilds is None else guilds
        self._channels = CHANNELS if channels is None else channels
        self.outcomes = list(OUTCOMES if outcomes is None else outcomes)
        self.applied, self.undos, self.notified, self.logs = [], [], [], []
        self.stash = {}
        self._tok = 0
        self.names_extra = dict(NAMES)
        if seed and text == BASE:
            self._seed()
        if broken_overlay:
            with open(self.overlay_path, "w") as f:
                f.write("values: [this is: not: an overlay\n")
        self.refresh()

    # -- fixture state ------------------------------------------------------
    @property
    def overlay_path(self):
        return MS.overlay_path_for(self.cfg)

    def _seed(self):
        """An owner's Discord change over a config.yaml value, and an
        admin's addition to Never answer."""
        MS.apply(self.cfg, [MS.Change(MS.Place.server(G1), "who.replies",
                                      "conversational")], OWNER,
                 plugins_dir=PLUGINS, role="owner")
        MS.apply(self.cfg, [MS.Change(MS.Place.server(G1), "who.ignore",
                                      [BOB, SPAM])], ADMIN,
                 plugins_dir=PLUGINS, role="admin", admin_gid=G1,
                 info=self)

    def refresh(self):
        self.settings = MS.load(self.cfg, PLUGINS)

    def reload(self):
        self.refresh()
        return self.settings

    def snapshot(self):
        try:
            with open(self.overlay_path) as f:
                ov = f.read()
        except OSError:
            ov = None
        return ov, dict(self.stash), self._tok

    def restore(self, snap):
        ov, stash, tok = snap
        if ov is None:
            if os.path.exists(self.overlay_path):
                os.remove(self.overlay_path)
        else:
            with open(self.overlay_path, "w") as f:
                f.write(ov)
        self.stash, self._tok = dict(stash), tok
        self.refresh()

    # -- the Ctx ------------------------------------------------------------
    # Copies (the fixture dicts are flat), so a caller cannot change them.
    def guilds(self):
        return [dict(g) for g in self._guilds]

    def channels(self, gid):
        return [dict(c) for c in self._channels.get(str(gid), [])]

    def apply(self, changes, by, role, group=None, admin_gid=None):
        self.applied.append((list(changes), by, role, admin_gid))
        return MS.apply(self.cfg, changes, by, group, PLUGINS, role=role,
                        admin_gid=admin_gid, info=self)

    def undo(self, prefix, by, **kw):
        if kw.get("admin_gid"):
            kw.setdefault("info", self)
        out = MS.undo_changes(self.cfg, prefix, by, PLUGINS, **kw)
        self.undos.append((prefix, by, kw, out))
        return out

    def names(self, some_id):
        some_id = str(some_id)
        for g in self._guilds:
            if g["id"] == some_id:
                return g["name"]
        for chans in self._channels.values():
            for c in chans:
                if c["id"] == some_id:
                    return c["name"]
        return (self.settings.labels or {}).get(some_id)

    def display_name(self, uid):
        return self.names_extra.get(str(uid))

    def recent_outcomes(self, place, limit=20):
        out = []
        for o in self.outcomes:
            if place.kind == "dms":
                ok = not o.get("guild_id")
            elif place.kind == "server":
                ok = o.get("guild_id") == place.guild_id
            elif place.kind == "channel":
                ok = (o.get("parent_id") or o.get("channel_id")) \
                    == place.channel_id
            else:
                ok = True
            if ok:
                out.append(dict(o))
        out.sort(key=lambda o: -o["at"])
        return out[:limit]

    def outcome(self, mid):
        for o in self.outcomes:
            if o["message_id"] == str(mid):
                return dict(o)
        return None

    def stash_put(self, uid, payload, ttl=900):
        self._tok += 1
        tok = f"t{self._tok:09d}"
        self.stash[tok] = (str(uid), json.loads(json.dumps(payload)),
                           self.now + ttl)
        return tok

    def stash_take(self, uid, tok):
        e = self.stash.get(tok)
        if e is None or e[2] < self.now:
            self.stash.pop(tok, None)
            return None
        if e[0] != str(uid):
            raise PermissionError("not yours")
        del self.stash[tok]
        return e[1]

    def notify_owners_admin_change(self, *a):
        self.notified.append(a)

    def log(self, msg):
        self.logs.append(msg)

    # -- reading back -------------------------------------------------------
    def value(self, key, place):
        return MS.place_value(MS.load(self.cfg, PLUGINS), place, key)

    def overlay(self):
        return MS._read_overlay_doc(self.overlay_path)


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------
def _who(ix, user, guild, perms, channel, ctype=0, parent=None):
    if guild:
        ix["guild_id"] = guild
        ix["member"] = {"user": {"id": user, "username": NAMES.get(
            user, "user").lower()}, "permissions": perms or "0"}
        ix["channel_id"] = channel
        ix["channel"] = {"id": channel, "type": ctype,
                         "parent_id": parent}
    else:
        ix["user"] = {"id": user, "username": NAMES.get(user,
                                                        "user").lower()}
        ix["channel_id"] = "40000000000000000077"
        ix["channel"] = {"id": "40000000000000000077", "type": 1}
    return ix


def command(user, guild=None, perms=None, channel=MUSECHAT, ctype=0,
            parent=None):
    return _who({"type": 2, "id": "1", "token": "t",
                 "data": {"name": "settings", "type": 1}},
                user, guild, perms, channel, ctype, parent)


def context_command(user, target, guild=G1, perms=None, channel=MUSECHAT,
                    author=SAM, mentions=(BOT,), name="Why no reply?"):
    msg = {"id": target, "channel_id": channel,
           "author": {"id": author, "username": NAMES.get(author, "x")},
           "mentions": [{"id": m} for m in mentions], "content": "hey"}
    return _who({"type": 2, "id": "1", "token": "t",
                 "data": {"name": name, "type": 3, "target_id": target,
                          "resolved": {"messages": {target: msg}}}},
                user, guild, perms, channel)


def component(user, custom_id, values=None, guild=None, perms=None,
              channel=MUSECHAT, resolved=None, message=None, ctype=0,
              parent=None):
    data = {"custom_id": custom_id, "component_type": 2}
    if values is not None:
        data["values"] = list(values)
        data["component_type"] = 3
    if resolved:
        data["resolved"] = resolved
    return _who({"type": 3, "id": "1", "token": "t", "data": data,
                 "message": message or {"id": "9", "flags": 64 | 32768}},
                user, guild, perms, channel, ctype, parent)


def submit(user, custom_id, fields, guild=None, perms=None,
           channel=MUSECHAT):
    comps = []
    for name, value in fields.items():
        inner = {"type": 3, "custom_id": name, "values": list(value)} \
            if isinstance(value, list) else \
            {"type": 4, "custom_id": name, "value": value}
        comps.append({"type": 18, "component": inner})
    return _who({"type": 5, "id": "1", "token": "t",
                 "data": {"custom_id": custom_id, "components": comps},
                 "message": {"id": "9", "flags": 64 | 32768}},
                user, guild, perms, channel)


def resolved_for(kind, values):
    """Resolved data a user / mentionable / channel select carries."""
    if kind == 8:
        return {"channels": {v: {"id": v, "type": 0} for v in values}}
    users = {v: {"id": v, "username": NAMES.get(v, "u")} for v in values
             if v != ROLE}
    roles = {v: {"id": v, "name": "Mods"} for v in values if v == ROLE}
    out = {}
    if users:
        out["users"] = users
    if roles:
        out["roles"] = roles
    return out


# ---------------------------------------------------------------------------
# Reading responses
# ---------------------------------------------------------------------------
def walk(comps):
    for c in comps or []:
        if not isinstance(c, dict):
            continue
        yield c
        yield from walk(c.get("components"))
        if isinstance(c.get("accessory"), dict):
            yield c["accessory"]
        if isinstance(c.get("component"), dict):
            yield from walk([c["component"]])


def controls(resp):
    return [c for c in walk(resp.get("data", {}).get("components"))
            if c.get("custom_id", "").startswith("mdset:")]


def texts(resp):
    return [c["content"] for c in walk(resp.get("data", {}).get(
        "components")) if c.get("type") == 10]


def text(resp):
    return "\n".join(texts(resp))


def find(resp, prefix=None, label=None):
    """The first control whose custom_id starts with prefix (or whose
    label is label)."""
    for c in controls(resp):
        if prefix and not c["custom_id"].startswith(prefix):
            continue
        if label and c.get("label") != label:
            continue
        return c
    return None
