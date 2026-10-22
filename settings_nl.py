#!/usr/bin/env python3
"""settings_nl.py: plain-language settings (an owner DMs the bot "only
answer in #general when I @ you").

The worker never writes config. It attaches a `settings` action to its
one outbox item for an owner's DM (PROTOCOL.md, "Settings requests"),
and the monitor decides everything here: whether the item may change
settings at all (from its own records of the message, never from what
the worker wrote), what the changes mean, whether they need the owner's
Apply first (judged by outcome, muse_settings.assess), and the reply,
which the monitor writes itself.

Pure functions over a small ctx, like settings_ui.py: no network, no
monitor globals. monitor.py gives a ctx with:

    reload()              the current muse_settings.Settings (fresh)
    owners                set of owner user ids (now)
    info()                {"guilds": [{id, name, member_count}],
                           "channels": {gid: [{id, name, type,
                           parent_id}]}} (discovery's directory)
    thread_parent(cid)    the parent channel id of a thread, else None
    dm_channel_user(cid)  the user a resolved DM channel belongs to
    person_names()        {user id: name} the monitor has seen
    apply(changes, by, role="owner", group=...)  muse_settings.apply
    undo_group(group, by, undo_id)         muse_settings.undo_group
    undo_changes(place, by, children, expect, undo_id)
    now                   time.time() when the ctx was made
    save(state)           persist state.json now
    live                  a Live (the interaction thread's copy of the
                          pending confirms and undo buttons)
    channel_label(cid)    "#general in Work" / "a DM with me": a name
                          the monitor knows (never an outbox field)
    write_lock()          the overlay write lock (re-entrant), held
                          across a decision and its write
    journal_put(tok, e) / journal_drop(tok) / journal_all()
                          a small file the interaction thread writes
                          before an Apply, so a crash before the main
                          loop records the Undo button loses nothing

Every owner-DM queue item gets a capability nonce (nl_nonces, never in
reply_queue.json; the wake hook passes it only to a wake that holds
nothing but owner DMs, as owner_dm_nonce). A settings action must echo
it as settings.nonce: a worker woken for anything else cannot forge an
owner's request even if it reads the queue. A missing or wrong nonce,
or a channel that is not the item's own, is refused without settling
the item, so the real reply still goes out. (The monitor's
owner_dm_gate checks the nonce on every outbox item for an owner DM,
settings or not, before this module sees it; the nonce is spent when
the item settles.)

State (state.json, main loop only): nl_outcomes {item: outcome},
nl_pending {token: confirm}, nl_undo {token: undo button},
nl_alert_at (the last non-owner alert), nl_notices (CLI change notices
waiting for their batch). Button clicks (custom_id mdnl:ok|no|undo:
<token>) are answered on the interaction thread from Live, and hand
their state changes to the main loop through record() (apply_record).
"""
import hashlib
import hmac
import json
import re
import secrets
import threading

import muse_settings as MS

VERSION = 1
MAX_CHANGES = 10
MAX_IDS = 25
TEXT_MAX = 300
CONFIRM_TTL = 600            # an Apply / Cancel card, seconds
UNDO_TTL = 24 * 3600         # an Undo button
OUTCOME_TTL = 48 * 3600      # nl_outcomes entries (one action per item)
MAX_AGE = 24 * 3600          # older requests are refused
CONFIRM_AGE = 3600           # older requests always need Apply
ALERT_EVERY = 3600           # owner alerts about non-owner attempts
NOTICE_BATCH_SECS = 60       # CLI change notices batch this long
NOTICE_TRIES = 3
SUPPRESS_NOTIFICATIONS = 4096
EPOCH_MS = 1420070400000
CHANNEL_TYPES = (0, 5, 15, 16)   # text, announcement, forum, media
CARD_CHANNELS_PER_SERVER = 60
CARD_ENTRIES = 400

OPS = {
    # op: (required keys, optional keys) besides "op"
    "set": (("place", "key", "value"), ()),
    "reset": (("place", "key"), ()),
    "add": (("place", "key", "value"), ()),
    "remove": (("place", "key", "value"), ()),
    "undo": ((), ("place",)),
    "preset": (("place", "name"), ()),
    "pause": (("place",), ()),
    "resume": (("place",), ()),
}
GROUP_OPS = ("undo", "preset", "pause", "resume")
NOT_ON_SETTINGS_ITEMS = ("parts", "attachments", "reactions", "standalone",
                         "silent")
PLACE_RE = re.compile(r"(defaults|dms|server:\d{1,25}|channel:\d{1,25}:"
                      r"\d{1,25})")
_USER_RE = re.compile(r"\d{1,25}")
NONCE_RE = re.compile(r"[0-9a-f]{32,64}")
_PERSON_RE = re.compile(r"(role:)?\d{1,25}")

# Reply copy (the monitor's own words; the worker's text is only ever a
# short lead-in before the applied or confirm block).
NOT_DM = "I only change settings in a DM with me, or with /settings."
FORWARDED = ("I don't change settings from a forwarded message. Tell me in "
             "your own words.")
TOO_OLD = ("That request is over a day old, so I left it. Ask me again if "
           "you still want it.")
ALREADY = ("I already acted on that request, so I left it. Ask me again if "
           "you want something else.")
UNKNOWN = ("I couldn't match that request to a message of yours, so nothing "
           "changed. Ask me again.")
INVALID = "I couldn't change that, so nothing changed: "
NOTHING = "Nothing to change: "
INACTIVE = "That control is no longer active. Nothing changed."
OTHER_OWNER = "This was another owner's request."
EXPIRED = "Expired after 10 minutes. Nothing changed."
CANCELLED = "Cancelled. Nothing changed."
NOT_CURRENT = ("Those settings changed since, so I didn't undo this. Use "
               "/settings or tell me what you want.")
LOCKOUT = ("After this I won't read your DMs, so you can't undo it by "
           "asking me. The buttons and /settings still work.")
STALE = "Something changed since I asked, so here it is again:"
ALERT = ("I refused a settings change attached to a message from {who} in "
         "{where}. Nothing changed. That usually means the message tried "
         "to talk your Muse into it.")
HIDDEN = ("{label} can only be changed in config.yaml; ask your Muse in its "
          "main chat.")


# ---------------------------------------------------------------------------
# Shape (outbox-append.py and the monitor's _outbox_problem)
# ---------------------------------------------------------------------------
def known_spec(key, plugins_dir=None):
    """The Spec of a settings key from the tables and the plugin
    manifests alone (config.yaml is not read), else None."""
    if not isinstance(key, str):
        return None
    key = MS.moved_key(key)  # an older worker's plugins.media.*
    manifests = {}
    if key.startswith("plugins."):
        try:
            manifests = MS.load_manifests(plugins_dir)
        except (MS.SettingsError, OSError):
            return None
    return MS.spec_of(key, manifests)


def _list_key(key, spec):
    return spec is not None and (spec.type == "people" or spec.ids)


def _change_problems(c, where, plugins_dir):
    if not isinstance(c, dict):
        return [f"{where} is not an object"]
    op = c.get("op")
    if op not in OPS:
        return [f'{where}.op must be one of {", ".join(OPS)}']
    need, opt = OPS[op]
    out = []
    for k in need:
        if k not in c:
            out.append(f'{where} ({op}) needs "{k}"')
    extra = sorted(set(c) - set(need) - set(opt) - {"op"})
    if extra:
        allowed = ", ".join(("op",) + need + opt)
        out.append(f"{where} ({op}) takes only {allowed}; drop "
                   f"{', '.join(extra)}")
    if out:
        return out
    if "place" in c and not (isinstance(c["place"], str)
                             and PLACE_RE.fullmatch(c["place"])):
        out.append(f'{where}.place {json.dumps(c["place"])}: use "defaults", '
                   f'"dms", "server:<server id>" or "channel:<server id>:'
                   f'<channel id>"')
    if op == "preset":
        if not (isinstance(c.get("place"), str)
                and c["place"].startswith("server:")):
            out.append(f'{where}: a preset goes on a whole server '
                       f'("server:<id>")')
        if str(c.get("name")).lower() not in MS.PRESETS:
            out.append(f'{where}.name: "friends", "community" or "work"')
    if "key" not in c:
        return out
    key = c["key"]
    spec = known_spec(key, plugins_dir)
    if spec is None:
        out.append(f"{where}.key {json.dumps(key)} is not a setting (e.g. "
                   f"who.replies, style.length, media.enabled)")
        return out
    if op == "set":
        ok, why = MS.normalize(spec, c["value"])
        if not ok:
            out.append(f"{where}.value for {key}: {why}; valid: "
                       f"{spec.valid_text()}")
    elif op in ("add", "remove"):
        v = c["value"]
        if key == "style.notes":
            if op != "add":
                out.append(f'{where}: style.notes takes "add" (one line '
                           f'to append) or "set"/"reset", not "remove"')
            elif not isinstance(v, str) or not v.strip() \
                    or len(v) > MS.NOTES_MAX:
                out.append(f"{where}.value: one line of text (up to "
                           f"{MS.NOTES_MAX} characters) to append")
        elif not _list_key(key, spec):
            out.append(f'{where}: "{op}" works on lists of people '
                       f'(who.allow, who.ignore, who.dms_from, '
                       f'media.who); use "set" for {key}')
        else:
            pat = _USER_RE if spec.ids else _PERSON_RE
            if not (isinstance(v, list) and 1 <= len(v) <= MAX_IDS
                    and all(isinstance(x, str) and pat.fullmatch(x)
                            for x in v)):
                what = '"<user id>"' if spec.ids \
                    else '"<user id>" or "role:<role id>"'
                out.append(f"{where}.value: a list of 1 to {MAX_IDS} "
                           f"{what} strings")
    return out


def shape_problems(item, plugins_dir=None):
    """What is wrong with an outbox item's `settings` action (and the
    item around it), as plain sentences the worker can act on; [] when
    the shape is fine. Meaning (places, who asked) is checked later, by
    the monitor."""
    if not isinstance(item, dict):
        return ["not a JSON object"]
    out = []
    nl = [k for k in item if str(k).startswith("_nl")]
    if nl:
        out.append(f"fields starting with _nl are the monitor's own: "
                   f"{', '.join(sorted(map(str, nl)))}")
    if "settings" not in item:
        return out
    if item.get("item") in (None, ""):
        out.append('a settings item needs "item" (the DM\'s message_id)')
    bad = [k for k in NOT_ON_SETTINGS_ITEMS if k in item]
    if bad:
        out.append(f"a settings item carries no {', '.join(bad)}; only "
                   f"item, channel_id, channel_name, author, reply_to, "
                   f"text, reply_nonce, owner_dm_nonce and settings")
    text = item.get("text")
    if text is not None and (not isinstance(text, str)
                             or len(text) > TEXT_MAX):
        out.append(f"text on a settings item is a short lead-in (up to "
                   f"{TEXT_MAX} characters) or nothing")
    s = item["settings"]
    if not isinstance(s, dict) or not {"v", "changes"} <= set(s) \
            or not set(s) <= {"v", "changes", "nonce"}:
        out.append('settings must be {"v": 1, "nonce": "<the item\'s '
                   'owner_dm_nonce>", "changes": [...]}')
        return out
    if s["v"] != VERSION or isinstance(s["v"], bool):
        out.append("settings.v must be 1")
    if not (isinstance(s.get("nonce"), str)
            and NONCE_RE.fullmatch(s["nonce"])):
        out.append('settings.nonce: copy the item\'s "owner_dm_nonce" from '
                   'the wake payload (owner DMs only)')
    ch = s["changes"]
    if not isinstance(ch, list) or not 1 <= len(ch) <= MAX_CHANGES:
        out.append(f"settings.changes must be a list of 1 to {MAX_CHANGES} "
                   f"changes")
        return out
    ops = [c.get("op") for c in ch if isinstance(c, dict)]
    group = [o for o in ops if o in GROUP_OPS]
    if group and len(ch) > 1:
        out.append(f'"{group[0]}" goes alone in settings.changes; send the '
                   f'other changes in a later reply')
    for i, c in enumerate(ch):
        out += _change_problems(c, f"settings.changes[{i}]", plugins_dir)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def snowflake_secs(mid):
    try:
        return ((int(mid) >> 22) + EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return None


def new_token():
    return secrets.token_hex(6)


def names_for(ctx, inf, st):
    """{id: name}: servers by name, channels as "#general in Work" (so a
    sentence never shows a bare "#general"), people the monitor saw."""
    out = {str(k): str(v) for k, v in (st.labels or {}).items()}
    try:
        out.update({str(k): str(v) for k, v in
                    (ctx.person_names() or {}).items()})
    except Exception:
        pass
    for gid, g in inf.guilds().items():
        gname = str(g.get("name") or gid)
        out[gid] = gname
        for c in inf.channels(gid) or []:
            if c.get("name"):
                out[str(c["id"])] = f"#{c['name']} in {gname}"
    return out


def group_written(st, group):
    """True when config.discord.yaml's history has any line of group
    (an applied change, or the undo lines of an undo)."""
    if not group:
        return False
    return any(isinstance(h, dict) and h.get("group") == group
               for h in (st.overlay_doc or {}).get("history") or [])


def buttons(*specs):
    """One action row of buttons: (label, style, custom_id)."""
    return [{"type": 1, "components": [
        {"type": 2, "style": style, "label": label, "custom_id": cid}
        for label, style, cid in specs]}]


def undo_button(tok, label="Undo"):
    return buttons((label, 2, f"mdnl:undo:{tok}"))


def confirm_buttons(tok):
    return buttons(("Apply", 4, f"mdnl:ok:{tok}"),
                   ("Cancel", 2, f"mdnl:no:{tok}"))


def _clip(text):
    return MS.clip(text, 1990)


def _block(head, lines):
    if len(lines) == 1:
        return f"{head} {lines[0]}."
    return head + "\n" + "\n".join(f"- {x}" for x in lines)


def undo_lines(st, plan, names):
    """["#general in Work, When it answers → back to Chatty", ...]."""
    return [x[3:] if x.startswith("In ") else x
            for x in MS.undo_lines(st, plan, names)]


def locks_out(st, changes):
    """The outcome stops the owners' own DM path (DMs replies off)."""
    try:
        after = MS.simulate(st, changes)
    except MS.SettingsError:
        return False
    dm = MS.Scope.dm()
    return st.get("who.replies", dm) != "off" \
        and after.get("who.replies", dm) == "off"


def _fingerprint(lines, warnings, lockout):
    raw = json.dumps([lines, warnings, bool(lockout)], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Meaning: places, keys, list arithmetic
# ---------------------------------------------------------------------------
def check_place(ctx, st, inf, place, names):
    """Raise SettingsError (plain words) unless place is one the bot
    knows: a server it is in (or config.yaml lists), a channel of that
    server (not a thread, not a category)."""
    if place.kind in ("defaults", "dms"):
        return
    gid = place.guild_id
    if gid not in inf.guilds() and gid not in st.servers_listed():
        raise MS.SettingsError(f"I don't know a server with id {gid}.")
    gname = names.get(gid) or f"server {gid}"
    if place.kind == "server":
        return
    cid = place.channel_id
    if ctx.thread_parent(cid):
        raise MS.SettingsError(
            f"channel {cid} is a thread; set it on its parent channel "
            f"(threads follow it).")
    ch = inf.channel(gid, cid)
    if ch is None:
        for other in inf.guilds():
            if other != gid and inf.channel(other, cid) is not None:
                raise MS.SettingsError(
                    f"{names.get(cid) or 'that channel'} is not in "
                    f"{gname}.")
        if inf.channels(gid) is None and cid in st.channels_listed(gid):
            return   # no channel list yet, but config.yaml names it
        raise MS.SettingsError(f"I don't know a channel with id {cid} in "
                               f"{gname}.")
    if ch.get("type") == MS.CATEGORY_TYPE:
        raise MS.SettingsError(f"{ch.get('name') or cid} is a category; "
                               f"categories can only be set in "
                               f"config.yaml. Pick a channel, or the "
                               f"server.")
    if ch.get("type") not in CHANNEL_TYPES:
        raise MS.SettingsError(f"{names.get(cid) or cid} is not a text "
                               f"channel I answer in.")


def check_key(st, place, key, names):
    offered = set(MS.place_keys(st, place, "basics")) \
        | set(MS.place_keys(st, place, "more"))
    if key in offered:
        return
    if place.kind == "defaults" and key in MS.TOP_DISCORD_EDITABLE \
            and key not in MS.HIDDEN_KEYS:
        return   # bot-wide, not on a /settings page (usage stats)
    spec = st.spec(key)
    if spec is None:
        raise MS.SettingsError(f"{key} is not a setting here.")
    if key in MS.HIDDEN_KEYS or key in MS.place_keys(st, place, "hidden") \
            or spec.hidden or not spec.discord_editable:
        raise MS.SettingsError(HIDDEN.format(label=MS.label(key)))
    raise MS.SettingsError(f"{MS.label(key)} isn't set for "
                           f"{MS.place_name(place, names)}.")


def _list_change(st, place, key, op, ids):
    """[Change] for adding / removing people on a list setting."""
    cur = MS.place_value(st, place, key)
    label = MS.label(key)
    if key == "who.dms_from":
        if cur in ("anyone", "shared-servers"):
            if op == "add":   # they can DM it already: nothing to change
                return [MS.Change(place, key, cur)]
            raise MS.SettingsError(
                f"{label} is {MS.value_label(key, cur)}, so there is no "
                f"list to take them off; say who should be able to DM it.")
        cur = [] if cur == "owners" else list(cur)
    elif not isinstance(cur, list):
        if op == "remove":
            if cur == "owners":
                return [MS.Change(place, key, cur)]
            raise MS.SettingsError(
                f"{label} is {MS.value_label(key, cur)}, so there is no "
                f"list to take them off.")
        # The list replaces the keyword: keep the owners on a list that
        # replaced "owners" (Who can ask for images and video).
        cur = sorted(st.owners) if cur == "owners" else []
    if op == "add":
        new = cur + [i for i in ids if i not in cur]
    else:
        new = [i for i in cur if i not in ids]
    if key == "who.allow" and not new:
        raise MS.SettingsError(
            f"that would leave {label} with nobody on it; to answer "
            f"everyone again, set it to everyone.")
    if key == "who.dms_from" and place.kind in ("dms", "defaults"):
        return MS.dms_from_changes(st, "list" if new else "owners",
                                   new or None)
    return [MS.Change(place, key, new)]


def build_change(ctx, st, inf, c, names):
    """[Change] for one set / reset / add / remove change (raises
    SettingsError in plain words)."""
    place = MS.parse_place(c["place"], inf)
    check_place(ctx, st, inf, place, names)
    key = MS.moved_key(c["key"])
    check_key(st, place, key, names)
    op = c["op"]
    if op == "reset":
        return [MS.Change(place, key, "inherit")]
    if op == "set":
        v = c["value"]
        if key == "who.dms_from" and place.kind in ("dms", "defaults"):
            ok, norm = MS.normalize(st.spec(key), v)
            if ok:
                if isinstance(norm, list):
                    return MS.dms_from_changes(st, "list", norm)
                return MS.dms_from_changes(st, norm)
        return [MS.Change(place, key, v)]
    if key == "style.notes":
        path = MS.place_path(st, place, key)
        s, cur, _src = st.layer(path, key)
        base = cur if s and cur else ""
        text = str(c["value"]).strip()
        new = f"{base}\n{text}" if base else text
        if len(new) > MS.NOTES_MAX:
            raise MS.SettingsError(
                f"{MS.label(key)} for {MS.place_name(place, names)} would "
                f"be {len(new)} characters with that line; the limit is "
                f"{MS.NOTES_MAX}.")
        return [MS.Change(place, key, new)]
    return _list_change(st, place, key, op, list(c["value"]))


# ---------------------------------------------------------------------------
# Plans: what an action would do (shared by the DM path and the buttons)
# ---------------------------------------------------------------------------
class Plan:
    """What one action does. kind: "apply" (changes), "undo_group"
    (group), "undo_place" (place, ident). lines: the sentences; noops:
    the "(already so)" ones; nothing: no change at all (its text)."""

    def __init__(self, kind):
        self.kind = kind
        self.changes = []
        self.lines = []
        self.noops = []
        self.warnings = []
        self.notes = []
        self.lockout = False
        self.group = None
        self.place = None
        self.children = False
        self.ident = None
        self.nothing = None
        self.free_text = False   # sets text the Muse follows or shows

    @property
    def fingerprint(self):
        return _fingerprint(self.lines, self.warnings, self.lockout)

    def assess(self, st, inf, people):
        a = MS.assess(st, self.changes, inf.src, people)
        self.warnings, self.notes = list(a.warnings), list(a.notes)
        self.lockout = locks_out(st, self.changes)


def plan_apply(st, inf, changes, names, people):
    p = Plan("apply")
    rows = MS.plan_changes(st, changes)
    todo = [r for r in rows if not r.noop]
    p.noops = [r.describe(names) for r in rows if r.noop]
    if not todo:
        p.nothing = NOTHING + (p.noops[0] + "." if len(p.noops) == 1 else
                               "\n" + "\n".join(f"- {x}" for x in p.noops))
        return p
    p.changes = [r.change for r in todo]
    p.lines = [r.describe(names) for r in todo]
    p.assess(st, inf, people)
    return p


def plan_undo_group(st, inf, group, names, people):
    p = Plan("undo_group")
    p.group = group
    if not MS.group_current(st, group):
        p.nothing = NOT_CURRENT
        return p
    p.changes = MS.group_undo_plan(st, group)
    p.lines = undo_lines(st, p.changes, names)
    p.assess(st, inf, people)
    return p


def plan_undo_place(st, inf, place, names, people):
    p = Plan("undo_place")
    p.place = place
    p.children = place.kind == "server"
    text, ident = MS.undo_preview(st, place, p.children, names)
    if text is None:
        p.nothing = (f"Nothing to undo in "
                     f"{MS.place_name(place, names)}.")
        return p
    p.ident = ident
    p.changes = MS.undo_plan(st, place, p.children)
    p.lines = undo_lines(st, p.changes, names)
    p.assess(st, inf, people)
    return p


def latest_own_group(st, uid):
    """The newest group this owner made by message (nl-*) or that the
    owners' Muse made with bin/settings.py (cli-*), still current."""
    seen = set()
    for h in reversed((st.overlay_doc or {}).get("history") or []):
        if not isinstance(h, dict) or h.get("undo") or "to" not in h:
            continue
        g = str(h.get("group") or "")
        if not g or g in seen:
            continue
        seen.add(g)
        mine = g.startswith("nl-") and not g.startswith("nl-undo-") \
            and str(h.get("by")) == str(uid)
        if (mine or g.startswith("cli-")) and MS.group_current(st, g):
            return g
    return None


def plan_action(ctx, st, inf, action, uid, names, people):
    """The Plan for an outbox action (raises SettingsError with every
    reason, joined, when any change fails: all or nothing)."""
    changes = action["changes"]
    op = changes[0]["op"]
    if op == "undo":
        if "place" in changes[0]:
            place = MS.parse_place(changes[0]["place"], inf)
            check_place(ctx, st, inf, place, names)
            return plan_undo_place(st, inf, place, names, people)
        group = latest_own_group(st, uid)
        if group is None:
            p = Plan("undo_group")
            p.nothing = ("There is no change of yours I can undo (only "
                         "ones still in effect). /settings shows what is "
                         "set.")
            return p
        return plan_undo_group(st, inf, group, names, people)
    if op in ("preset", "pause", "resume"):
        place = MS.parse_place(changes[0]["place"], inf)
        check_place(ctx, st, inf, place, names)
        if op == "preset":
            built = MS.preset_changes(st, changes[0]["name"], place, inf.src)
            if not built:
                p = Plan("apply")
                p.nothing = (f"{NOTHING}{MS.place_name(place, names)} "
                             f"already matches "
                             f"{str(changes[0]['name']).capitalize()}.")
                return p
        elif op == "pause":
            check_key(st, place, "who.replies", names)
            built = MS.pause_changes(place)
        else:
            built = MS.resume_changes(st, place)
            if not built:
                p = Plan("apply")
                p.nothing = (f"{NOTHING}{MS.place_name(place, names)} "
                             f"isn't paused.")
                return p
        return plan_apply(st, inf, built, names, people)
    built, errors = [], []
    for c in changes:
        try:
            built += build_change(ctx, st, inf, c, names)
        except MS.SettingsError as e:
            errors.append(str(e).rstrip("."))
    if errors:
        raise MS.SettingsError("; ".join(errors))
    p = plan_apply(st, inf, built, names, people)
    # Free text (Instructions, the status line) always
    # needs Apply: words in it steer the Muse or show publicly, and an
    # owner-DM run may have read other people's text.
    p.free_text = any(
        c["op"] in ("set", "add")
        and (st.spec(c["key"]) is not None
             and st.spec(c["key"]).type == "text")
        for c in changes)
    return p


def change_dicts(changes):
    out = []
    for ch in changes:
        d = {"place": MS.format_place(ch.place), "key": ch.key,
             "value": ch.value}
        if ch.path is not None:
            d["path"] = list(ch.path)
        out.append(d)
    return out


def changes_from(dicts, inf):
    return [MS.Change(MS.parse_place(d["place"], inf), d["key"], d["value"],
                      path=tuple(d["path"]) if d.get("path") else None)
            for d in dicts]


def replan(ctx, st, inf, rec, names, people):
    """The Plan for a stored confirm, against the settings now."""
    kind = rec["kind"]
    if kind == "undo_group":
        return plan_undo_group(st, inf, rec["group"], names, people)
    if kind == "undo_place":
        place = MS.parse_place(rec["place"], inf)
        p = plan_undo_place(st, inf, place, names, people)
        if p.nothing is None and p.ident != rec.get("ident"):
            p.lines = p.lines + ["(something else is newest there now)"]
        return p
    return plan_apply(st, inf, changes_from(rec["changes"], inf), names,
                      people)


def card_text(plan, lead=None, why=()):
    """The confirm card: what it would do, the warnings, Apply first."""
    head = "Undoing this needs your OK first:" if plan.kind != "apply" \
        else "This one needs your OK first:"
    parts = [lead.strip()] if lead and lead.strip() else []
    parts.append(head + "\n" + "\n".join(f"- {x}" for x in plan.lines))
    heads = list(plan.warnings) + list(why)
    if heads:
        parts.append("Heads up: " + " ".join(heads))
    if plan.lockout:
        parts.append(LOCKOUT)
    parts.append("This expires in 10 minutes.")
    return _clip("\n".join(parts))


def done_text(plan, head="Done:", lead=None):
    lines = list(plan.lines) + [f"{x} (already so)" for x in plan.noops]
    parts = [lead.strip()] if lead and lead.strip() else []
    parts.append(_block(head, lines))
    parts += [f"Note: {n}" for n in plan.notes]
    return _clip("\n".join(parts))


# ---------------------------------------------------------------------------
# Live: the interaction thread's view of pending confirms and undo buttons
# ---------------------------------------------------------------------------
class Live:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.undo = {}

    def load(self, state):
        with self.lock:
            self.pending = dict(state.get("nl_pending") or {})
            self.undo = dict(state.get("nl_undo") or {})

    def add(self, kind, tok, rec):
        with self.lock:
            getattr(self, kind)[tok] = rec


def _add(ctx, state, kind, tok, rec):
    """A pending confirm or an undo button, in state and Live."""
    state.setdefault("nl_" + kind, {})[tok] = rec
    ctx.live.add(kind, tok, rec)


def recover(ctx, state):
    """Startup: Apply clicks the interaction thread wrote down but the
    main loop never recorded (the monitor stopped in between). Applied
    (its group is in the overlay's history): the card is used and its
    Undo button is kept. Not applied: the card stays as it was."""
    done = []
    st = None
    for tok, e in (ctx.journal_all() or {}).items():
        st = st or ctx.reload()
        if isinstance(e, dict) and group_written(st, e.get("group")):
            (state.get("nl_pending") or {}).pop(tok, None)
            if e.get("undo") and isinstance(e.get("urec"), dict):
                state.setdefault("nl_undo", {})[e["undo"]] = e["urec"]
        done.append(tok)
    for tok in done:
        ctx.journal_drop(tok)
    return len(done)


def apply_record(state, rec):
    """Main loop: what a button click changed (record() from on_button)."""
    for tok in rec.get("drop_pending") or []:
        (state.get("nl_pending") or {}).pop(tok, None)
    for tok in rec.get("drop_undo") or []:
        (state.get("nl_undo") or {}).pop(tok, None)
    for tok, r in (rec.get("pending") or {}).items():
        state.setdefault("nl_pending", {})[tok] = r
    for tok, r in (rec.get("undo") or {}).items():
        state.setdefault("nl_undo", {})[tok] = r


# ---------------------------------------------------------------------------
# The outbox path
# ---------------------------------------------------------------------------
def _outcome(status, reason, text=None, components=None, channel_id=None,
             reply_to=None, code=None, at=0.0):
    return {"status": status, "reason": reason, "text": text,
            "components": components, "channel_id": channel_id,
            "reply_to": reply_to, "code": code, "at": at, "sent": False,
            "phase": "ready"}


def _reply_target(state, item, iid):
    """The message to thread under: the outbox reply_to when it is the
    item or one of its burst fragments, else the item."""
    rt = str(item.get("reply_to") or "")
    if rt and (rt == iid or (state.get("reply_aliases") or {}).get(rt)
               == iid):
        return rt
    return iid


class Result:
    """What process() decided: the outcome record (text None: settle
    silently), log lines, and an owner alert to send (or None). rec None
    with reject set: refused WITHOUT settling the item (a forged or
    misdirected action): the item stays open for its real reply."""

    def __init__(self, rec, logs=(), alert=None, reject=None):
        self.rec, self.logs, self.alert = rec, list(logs), alert
        self.reject = reject


def _where(ctx, item, meta=None):
    cid = (meta or {}).get("channel_id") or item.get("channel_id")
    try:
        return ctx.channel_label(str(cid or ""))
    except Exception:
        return f"channel {cid}"


def _raw(item):
    s = item.get("settings")
    if isinstance(s, dict) and "nonce" in s:
        s = dict(s, nonce="(redacted)")   # never a capability in a log
    return json.dumps(s, sort_keys=True)[:300]


def _refuse(ctx, state, item, iid, code, text, status, channel_id,
            reply_to, now, alert=None, meta=None):
    rec = _outcome(status, f"settings refused: {code}", text, None,
                   channel_id, reply_to, code, now)
    state.setdefault("nl_outcomes", {})[iid] = rec
    return Result(rec, [f"Refused a settings action on item {iid} "
                        f"({_where(ctx, item, meta)}): {code}; action: "
                        f"{_raw(item)}"], alert)


def _drop(ctx, item, iid, code, meta=None):
    """Refused, and the item is NOT settled or recorded: whoever wrote
    this could not prove it answers the owner's DM, so the real reply
    must still be able to go out."""
    return Result(None, [f"Refused a settings action on item {iid} "
                         f"({_where(ctx, item, meta)}): {code}; the item "
                         f"stays open; action: {_raw(item)}"],
                  reject=f"settings refused: {code}")


def _alert_text(ctx, meta, item):
    who = f"<@{meta.get('author_id')}>" if meta.get("author_id") \
        else "someone"
    return ALERT.format(who=who, where=_where(ctx, item, meta))


def process(ctx, state, item):
    """Decide one outbox item carrying a `settings` action (its shape was
    checked already). Writes state (nl_outcomes, nl_pending, nl_undo)
    and, for a safe change, the overlay. The returned outcome record is
    what the monitor sends (and resends on a retry: never applied
    twice)."""
    iid = str(item["item"])
    now = ctx.now
    outs = state.setdefault("nl_outcomes", {})
    rec = outs.get(iid)
    if rec is not None and rec.get("phase") == "applying":
        # The monitor stopped between recording and applying: applied
        # when the group is in the overlay's history, else start over.
        if group_written(ctx.reload(), rec.get("group")):
            rec["phase"] = "ready"
            ctx.save(state)
        else:
            outs.pop(iid, None)
            if rec.get("nonce") and iid not in (state.get("nl_nonces")
                                                or {}):
                # Not applied: the capability holds (an older monitor
                # spent it on use).
                state.setdefault("nl_nonces", {})[iid] = {
                    "nonce": rec["nonce"], "at": now}
            if rec.get("undo_token"):
                (state.get("nl_undo") or {}).pop(rec["undo_token"], None)
                with ctx.live.lock:
                    ctx.live.undo.pop(rec["undo_token"], None)
            rec = None
    if rec is not None:
        return Result(rec)

    meta = (state.get("item_meta") or {}).get(iid)
    cid = str(item.get("channel_id") or "")
    owners = {str(o) for o in ctx.owners}
    if meta is None:
        uid = ctx.dm_channel_user(cid)
        if uid and str(uid) in owners:
            return _refuse(ctx, state, item, iid, "unknown item", UNKNOWN,
                           "replied", cid, None, now)
        return _refuse(ctx, state, item, iid, "unknown item", None,
                       "skipped", None, None, now)
    uid = str(meta.get("author_id") or "")
    mcid = str(meta.get("channel_id") or "")
    if uid not in owners or meta.get("bot"):
        alert = None
        last = state.get("nl_alert_at") or 0
        if now - last >= ALERT_EVERY:
            state["nl_alert_at"] = now
            alert = _alert_text(ctx, meta, item)
        r = _refuse(ctx, state, item, iid, "not an owner", None, "skipped",
                    None, None, now, alert, meta)
        if alert is None:
            r.logs[0] += " (owners were alerted within the hour)"
        return r
    if cid != mcid:
        return _drop(ctx, item, iid, "wrong channel", meta)
    target = _reply_target(state, item, iid)
    if not meta.get("dm") or meta.get("kind") != "dm":
        return _refuse(ctx, state, item, iid, "not a DM", NOT_DM, "replied",
                       mcid, target, now, meta=meta)
    if str(ctx.dm_channel_user(cid) or "") != uid:
        return _drop(ctx, item, iid, "wrong channel", meta)
    # The capability: only the wake that was handed this owner DM knows
    # its nonce (never in reply_queue.json). The monitor checked it
    # already (owner_dm_gate); checked again here so this module stands
    # on its own. Spent when the item settles (one action per item:
    # nl_outcomes), not here, so a failed send's retry still holds it.
    want = ((state.get("nl_nonces") or {}).get(iid) or {}).get("nonce")
    got = (item.get("settings") or {}).get("nonce")
    if not want or not isinstance(got, str) \
            or not hmac.compare_digest(str(want).encode(),
                                       got.encode("utf-8", "replace")):
        return _drop(ctx, item, iid, "bad nonce", meta)
    if meta.get("forwarded"):
        return _refuse(ctx, state, item, iid, "forwarded", FORWARDED,
                       "replied", cid, target, now, meta=meta)
    with ctx.write_lock():
        st = ctx.reload()
        if group_written(st, f"nl-{iid}"):
            return _refuse(ctx, state, item, iid, "already done", ALREADY,
                           "replied", cid, target, now, meta=meta)
        sent_at = snowflake_secs(iid) or now
        age = now - sent_at
        if age > MAX_AGE:
            return _refuse(ctx, state, item, iid, "too old", TOO_OLD,
                           "replied", cid, target, now, meta=meta)
        return _decide(ctx, state, item, iid, meta, uid, cid, target, st,
                       age, now)


def _failed(e):
    """A reason for an unexpected failure, short and without paths."""
    if isinstance(e, MS.SettingsError):
        return str(e).rstrip(".")
    return f"something went wrong saving it ({type(e).__name__})"


def _decide(ctx, state, item, iid, meta, uid, cid, target, st, age, now):
    inf = MS._Info(ctx.info())
    names = names_for(ctx, inf, st)
    people = {k: v for k, v in (ctx.person_names() or {}).items()}
    who = people.get(uid) or uid
    lead = (item.get("text") or "").strip().replace(
        "@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    try:
        plan = plan_action(ctx, st, inf, item["settings"], uid, names,
                           people)
    except Exception as e:
        text = INVALID + _failed(e) + "."
        rec = _outcome("replied", "settings: refused (invalid)",
                       _clip(text), None, cid, target, "invalid", now)
        state.setdefault("nl_outcomes", {})[iid] = rec
        return Result(rec, [f"Settings (DM) from {who}: nothing changed "
                            f"(invalid: {e})"])
    if plan.nothing is not None:
        rec = _outcome("replied", "settings: nothing to change",
                       _clip(plan.nothing), None, cid, target, "nothing",
                       now)
        state.setdefault("nl_outcomes", {})[iid] = rec
        return Result(rec, [f"Settings (DM) from {who}: nothing to change"])
    why = []
    if age > CONFIRM_AGE:
        why.append("Your request is over an hour old.")
    if meta.get("has_inputs"):
        why.append("Your message came with attachments or answered "
                   "someone else's message, so I want your OK.")
    if plan.free_text:
        why.append("This sets text I will follow or show, so I want your "
                   "OK on the exact words.")
    group = f"nl-{iid}"
    if plan.warnings or why or plan.lockout:
        tok = new_token()
        pend = {"owner": uid, "item": iid, "kind": plan.kind,
                "group": group, "fp": plan.fingerprint, "at": now,
                "channel_id": cid, "why": why}
        if plan.kind == "apply":
            pend["changes"] = change_dicts(plan.changes)
        elif plan.kind == "undo_group":
            pend["group"] = plan.group
            pend["undo_id"] = group
        else:
            pend["place"] = MS.format_place(plan.place)
            pend["ident"] = plan.ident
            pend["undo_id"] = group
        _add(ctx, state, "pending", tok, pend)
        rec = _outcome("replied", "settings: confirm asked",
                       card_text(plan, None, why), confirm_buttons(tok),
                       cid, target, "confirm", now)
        rec["pending"] = tok
        state.setdefault("nl_outcomes", {})[iid] = rec
        return Result(rec, [f"Settings (DM) from {who}: confirm asked (mdnl "
                            f"{tok}), {len(plan.lines)} change(s), "
                            f"{len(plan.warnings)} warning(s)"])
    # Applied now. An undo (by message) is never undoable itself: no
    # button.
    apply = plan.kind == "apply"
    tok = new_token() if apply else None
    rec = _outcome("replied", "settings: applied",
                   done_text(plan, "Done:" if apply else "Undone:", lead),
                   undo_button(tok) if apply else None, cid, target,
                   "applied", now)
    rec.update(phase="applying", group=group,
               nonce=(item.get("settings") or {}).get("nonce"))
    state.setdefault("nl_outcomes", {})[iid] = rec
    if apply:
        rec["undo_token"] = tok
        _add(ctx, state, "undo", tok, {"owner": uid, "group": group,
                                       "at": now, "channel_id": cid})
    ctx.save(state)
    try:
        if apply:
            ctx.apply(plan.changes, uid, role="owner", group=group)
        else:
            _run_undo(ctx, plan, uid, group)
    except Exception as e:
        # Unless written after all (the error came after the write).
        if not group_written(ctx.reload(), group):
            if tok:
                (state.get("nl_undo") or {}).pop(tok, None)
                with ctx.live.lock:
                    ctx.live.undo.pop(tok, None)
            rec.update(_outcome("replied", "settings: refused (invalid)",
                                _clip(INVALID + _failed(e) + "."), None,
                                cid, target, "invalid", now))
            ctx.save(state)
            return Result(rec, [f"Settings (DM) from {who}: nothing "
                                f"changed ({_failed(e)})"])
    rec["phase"] = "ready"
    ctx.save(state)
    did = f"applied {group}" if apply \
        else f"undid {plan.group or plan.ident}"
    return Result(rec, [f"Settings (DM) from {who}: {did}: "
                        + "; ".join(plan.lines)])


def _run_undo(ctx, plan, uid, undo_id):
    if plan.kind == "undo_group":
        ctx.undo_group(plan.group, uid, undo_id)
    else:
        ctx.undo_changes(plan.place, uid, plan.children, plan.ident,
                         undo_id)


def sent(state, iid, message_id=None):
    """The outcome for iid went out (message_id: the message, for an
    expiring confirm card to be edited later)."""
    rec = (state.get("nl_outcomes") or {}).get(str(iid))
    if rec is None:
        return
    rec["sent"] = True
    tok = rec.get("pending")
    if tok and message_id:
        p = (state.get("nl_pending") or {}).get(tok)
        if p is not None:
            p["message_id"] = str(message_id)


# ---------------------------------------------------------------------------
# Buttons (interaction thread)
# ---------------------------------------------------------------------------
def _update(text, components=None):
    return {"type": 7, "data": {"content": _clip(text),
                                "components": components or [],
                                "allowed_mentions": {"parse": []}}}


def _ephemeral(text):
    return {"type": 4, "data": {"content": text, "flags": 64,
                                "allowed_mentions": {"parse": []}}}


def on_button(ctx, ix, rest, uid, record):
    """mdnl:<verb>:<token>: ok (Apply), no (Cancel), undo. Returns the
    interaction response; record() hands state changes to the main
    loop."""
    verb, _, tok = str(rest).partition(":")
    live = ctx.live
    uid = str(uid)
    now = ctx.now
    if verb in ("ok", "no"):
        with live.lock:
            rec = live.pending.get(tok)
            if rec is not None and str(rec.get("owner")) != uid:
                return _ephemeral(OTHER_OWNER)
            if rec is not None:
                live.pending.pop(tok, None)
        if rec is None:
            return _ephemeral(INACTIVE)
        if now - rec.get("at", 0) >= CONFIRM_TTL:
            record({"kind": "nl", "drop_pending": [tok]})
            ctx.log(f"Settings (DM): mdnl {tok} expired; nothing changed")
            return _update(EXPIRED)
        if verb == "no":
            record({"kind": "nl", "drop_pending": [tok]})
            ctx.log(f"Settings (DM): {uid} cancelled mdnl {tok}; nothing "
                    f"changed")
            return _update(CANCELLED)
        with ctx.write_lock():
            return _confirmed(ctx, tok, rec, uid, record, now)
    if verb == "undo":
        with live.lock:
            rec = live.undo.get(tok)
            if rec is not None and rec.get("owner") \
                    and str(rec["owner"]) != uid:
                return _ephemeral(OTHER_OWNER)
            if rec is not None:
                live.undo.pop(tok, None)
        if rec is None or now - rec.get("at", 0) >= UNDO_TTL:
            if rec is not None:
                record({"kind": "nl", "drop_undo": [tok]})
            return _ephemeral(INACTIVE)
        with ctx.write_lock():
            return _undo_clicked(ctx, ix, tok, rec, uid, record, now)
    return _ephemeral(INACTIVE)


def _fresh_card(ctx, rec, plan, uid, record, now, drop):
    tok = new_token()
    new = dict(rec, fp=plan.fingerprint, at=now, owner=uid)
    if plan.kind == "apply":
        new["changes"] = change_dicts(plan.changes)
    if plan.kind == "undo_place":
        new["ident"] = plan.ident
    ctx.live.add("pending", tok, new)
    record({"kind": "nl", "pending": {tok: new}, **drop})
    ctx.log(f"Settings (DM): {uid} chose Apply, but settings changed "
            f"since; asked again (mdnl {tok})")
    return _update(STALE + "\n" + card_text(plan, None, rec.get("why")),
                   confirm_buttons(tok))


def _confirmed(ctx, tok, rec, uid, record, now):
    st = ctx.reload()
    inf = MS._Info(ctx.info())
    names = names_for(ctx, inf, st)
    people = ctx.person_names() or {}
    try:
        plan = replan(ctx, st, inf, rec, names, people)
    except Exception as e:
        record({"kind": "nl", "drop_pending": [tok]})
        return _update(INVALID + _failed(e) + ".")
    if plan.nothing is not None:
        record({"kind": "nl", "drop_pending": [tok]})
        return _update(plan.nothing)
    if plan.fingerprint != rec.get("fp"):
        return _fresh_card(ctx, rec, plan, uid, record, now,
                           {"drop_pending": [tok]})
    utok = new_token()
    urec = {"owner": uid, "group": rec.get("group"), "at": now,
            "channel_id": rec.get("channel_id")}
    if plan.kind == "apply":
        # Written down before the overlay write: a crash before the main
        # loop records the Undo button is recovered at startup.
        ctx.journal_put(tok, {"group": rec["group"], "undo": utok,
                              "urec": urec, "at": now})
    try:
        if plan.kind == "apply":
            ctx.apply(plan.changes, uid, role="owner", group=rec["group"])
        else:
            _run_undo(ctx, plan, uid, rec.get("undo_id"))
    except Exception as e:
        if plan.kind == "apply" and not group_written(ctx.reload(),
                                                      rec["group"]):
            ctx.journal_drop(tok)
        record({"kind": "nl", "drop_pending": [tok]})
        return _update(INVALID + _failed(e) + ".")
    if plan.kind != "apply":
        record({"kind": "nl", "drop_pending": [tok]})
        ctx.log(f"Settings (DM): {uid} chose Apply for mdnl {tok}; undid "
                f"{plan.group or plan.ident}")
        return _update(done_text(plan, "Undone:"))
    ctx.live.add("undo", utok, urec)
    record({"kind": "nl", "drop_pending": [tok], "undo": {utok: urec},
            "journal": tok})
    ctx.log(f"Settings (DM): {uid} chose Apply for mdnl {tok}; applied "
            f"{rec['group']}")
    return _update(done_text(plan, "Applied:"), undo_button(utok))


def _undo_clicked(ctx, ix, tok, rec, uid, record, now):
    st = ctx.reload()
    inf = MS._Info(ctx.info())
    names = names_for(ctx, inf, st)
    people = ctx.person_names() or {}
    group = rec["group"]
    plan = plan_undo_group(st, inf, group, names, people)
    original = ((ix or {}).get("message") or {}).get("content") or ""
    if plan.nothing is not None:
        record({"kind": "nl", "drop_undo": [tok]})
        return _update((original + "\n\n" if original else "") + NOT_CURRENT)
    if plan.warnings or plan.lockout:
        ptok = new_token()
        pend = {"owner": uid, "item": None, "kind": "undo_group",
                "group": group, "undo_id": f"nl-undo-{tok}",
                "fp": plan.fingerprint, "at": now,
                "channel_id": rec.get("channel_id"), "why": []}
        ctx.live.add("pending", ptok, pend)
        record({"kind": "nl", "drop_undo": [tok], "pending": {ptok: pend}})
        ctx.log(f"Settings (DM): {uid} asked to undo {group}; confirm "
                f"asked (mdnl {ptok})")
        return _update(card_text(plan), confirm_buttons(ptok))
    try:
        ctx.undo_group(group, uid, f"nl-undo-{tok}")
    except Exception as e:
        record({"kind": "nl", "drop_undo": [tok]})
        return _update((original + "\n\n" if original else "")
                       + f"Nothing was undone: {_failed(e)}.")
    record({"kind": "nl", "drop_undo": [tok]})
    ctx.log(f"Settings (DM): {uid} undid {group}: " + "; ".join(plan.lines))
    return _update(done_text(plan, "Undone:"))


# ---------------------------------------------------------------------------
# Main-loop upkeep: expiry, pruning, notices for changes made elsewhere
# ---------------------------------------------------------------------------
def expire(ctx, state):
    """Drop what has run out. Returns [(channel_id, message_id, text)]:
    expired confirm cards to edit (best effort), plus log lines."""
    now = ctx.now
    edits, logs = [], []
    pend = state.get("nl_pending") or {}
    for tok, rec in list(pend.items()):
        if now - rec.get("at", 0) < CONFIRM_TTL:
            continue
        del pend[tok]
        with ctx.live.lock:
            live = ctx.live.pending.pop(tok, None)
        if live is not None:
            logs.append(f"Settings (DM): mdnl {tok} expired; nothing "
                        f"changed")
            if rec.get("message_id") and rec.get("channel_id"):
                edits.append((rec["channel_id"], rec["message_id"],
                              EXPIRED))
    und = state.get("nl_undo") or {}
    for tok, rec in list(und.items()):
        if now - rec.get("at", 0) >= UNDO_TTL:
            del und[tok]
            with ctx.live.lock:
                ctx.live.undo.pop(tok, None)
    outs = state.get("nl_outcomes") or {}
    for iid, rec in list(outs.items()):
        if now - rec.get("at", 0) >= OUTCOME_TTL:
            del outs[iid]
    return edits, logs


def _layer_effective(st, place, key, path, v):
    """What key resolves to at place when its layer (path) holds v (a
    history line's from / to; "inherit": the layer sets nothing)."""
    spec = st.spec(key)
    if v == "inherit" or v is None:
        if path == ("top",):
            return MS.TOP[key].default if key in MS.TOP else None
        sc = MS._scope_for(place, key, path)
        return st.value_without(sc, key, path) if sc is not None else None
    ok, norm = MS.normalize(spec, v) if spec is not None else (False, v)
    return norm if ok else v


def _cli_line(h):
    """A history line the owners are told about: any line of a cli-*
    group (bin/settings.py, whatever its --by), or any line written by a
    name rather than a user id. Never retired or folded lines."""
    if not isinstance(h, dict) or "to" not in h or h.get("retired") \
            or h.get("folded"):
        return False
    g = str(h.get("group") or "")
    if g.startswith("cli-"):
        return True
    by = str(h.get("by") or "")
    return bool(by) and MS._id_str(by) is None and not g.startswith("nl-")


def new_cli_groups(st, state, names=None):
    """Change groups made with bin/settings.py since the last call, from
    config.discord.yaml's history and the position saved in state.json
    (nl_history_mark), so changes made while the monitor was down are
    noticed at its start. The first time, only the position is saved
    (no flood of old changes). [{group, by, lines, undoable}], oldest
    first."""
    new, state["nl_history_mark"] = MS.history_since(
        st.overlay_doc, state.get("nl_history_mark"))
    groups, order = {}, []
    for n, h in enumerate(new):
        if not _cli_line(h):
            continue
        gid = h.get("group") or ("line", n)   # never a group id
        if gid not in groups:
            groups[gid] = {"group": h.get("group"),
                           "by": str(h.get("by") or "?"), "lines": [],
                           "undoable": bool(h.get("group"))}
            order.append(gid)
        g = groups[gid]
        if h.get("undo"):
            g["undoable"] = False
        try:
            path, key = MS.parse_key(h.get("key"))
        except ValueError:
            continue
        place = MS.place_of_path(path)
        if place is None:
            continue
        try:
            old = _layer_effective(st, place, key, path, h.get("from"))
            nw = _layer_effective(st, place, key, path, h.get("to"))
        except Exception:
            old, nw = h.get("from"), h.get("to")
        g["lines"].append(MS.describe_change(place, key, old, nw, names,
                                             path))
    return [groups[g] for g in order]


def notice_text(groups):
    by = sorted({g["by"] for g in groups})
    head = f"Changed by your Muse ({', '.join(by)}):"
    if len(groups) == 1:
        lines = [f"- {x}" for x in groups[0]["lines"]]
        return _clip(head + "\n" + "\n".join(lines))
    # Several groups: a numbered list whose numbers match the Undo
    # buttons; a group of more than one change nests its lines.
    lines = []
    for i, g in enumerate(groups):
        if len(g["lines"]) == 1:
            lines.append(f"{i + 1}. {g['lines'][0]}")
        else:
            lines.append(f"{i + 1}. {len(g['lines'])} changes at once:")
            lines += [f"   - {x}" for x in g["lines"]]
    return _clip(head + "\n" + "\n".join(lines))


def notice_buttons(groups):
    with_tok = [(i, g) for i, g in enumerate(groups) if g.get("token")][:5]
    if not with_tok:
        return None
    if len(groups) == 1:
        return undo_button(groups[0]["token"])
    return buttons(*[(f"Undo {i + 1}", 2, f"mdnl:undo:{g['token']}")
                     for i, g in with_tok])


def queue_notices(state, groups, now):
    if groups:
        q = state.setdefault("nl_notices", {"first": now, "groups": [],
                                            "tries": 0})
        if not q.get("groups"):
            q["first"] = now
        q["groups"] += groups


def due_notice(ctx, state, force=False):
    """The batched CLI-change notice to send now, as (text, components,
    groups), or None. Undo tokens are made here (any owner may click)."""
    q = state.get("nl_notices")
    if not q or not q.get("groups"):
        return None
    if not force and ctx.now - q.get("first", 0) < NOTICE_BATCH_SECS:
        return None
    groups = q["groups"]
    for g in groups:
        if g.get("undoable") and g.get("group") and not g.get("token"):
            g["token"] = new_token()
            _add(ctx, state, "undo", g["token"],
                 {"owner": None, "group": g["group"], "at": ctx.now,
                  "channel_id": None})
    return notice_text(groups), notice_buttons(groups), groups


def notice_done(state, delivered):
    """After a notice send: drop it when delivered (or after
    NOTICE_TRIES failures). Returns True when it was dropped."""
    q = state.get("nl_notices") or {}
    q["tries"] = q.get("tries", 0) + 1
    if delivered or q["tries"] >= NOTICE_TRIES:
        state.pop("nl_notices", None)
        return True
    return False


# ---------------------------------------------------------------------------
# The owner-DM settings card: the places the worker may name
# ---------------------------------------------------------------------------
def card_places(info):
    """{"servers": [{"id", "name", "members", "channels": [[id, name],
    ...]}], "more": bool} for an owner-DM card: text, announcement,
    forum and media channels only, at most CARD_CHANNELS_PER_SERVER per
    server and CARD_ENTRIES in all (more: true past that)."""
    inf = MS._Info(info)
    servers, n, more = [], 0, False
    for gid, g in sorted(inf.guilds().items(),
                         key=lambda kv: (str(kv[1].get("name") or "")
                                         .lower(), kv[0])):
        chans = [c for c in inf.channels(gid) or []
                 if c.get("type") in CHANNEL_TYPES]
        if len(chans) > CARD_CHANNELS_PER_SERVER:
            chans, more = chans[:CARD_CHANNELS_PER_SERVER], True
        room = CARD_ENTRIES - n
        if len(chans) > room:
            chans, more = chans[:max(0, room)], True
        n += len(chans)
        servers.append({"id": gid, "name": g.get("name") or gid,
                        "members": inf.members(gid),
                        "channels": [[str(c["id"]), str(c.get("name")
                                                        or c["id"])]
                                     for c in chans]})
    return {"servers": servers, "more": more}
