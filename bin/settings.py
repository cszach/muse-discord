#!/usr/bin/env python3
"""muse-discord settings tool.

    bin/settings.py validate
        Check config.yaml (and config.discord.yaml, the Discord-side
        changes). Exit status 1, with every problem listed, when the bot
        would refuse to start.
    bin/settings.py show [--dms | --server ID [--channel ID]
                          [--category ID]] [--explain]
        What the bot does in one place: every setting's resolved value
        and where it comes from (built-in, config.yaml, a Discord change,
        or inherited from a wider scope). No flags = the everywhere
        scope. --explain lists every layer for each setting.
    bin/settings.py fold [--dry-run]
        Copy the Discord-side changes into config.yaml and clear them
        (server admins' changes stay where they are).
        Rewrites config.yaml (its comments are lost) after saving a
        timestamped backup next to it. Does not prompt.
    bin/settings.py migrate [--dry-run] [--state PATH]
        Convert an old-layout config.yaml to the new layout. Resolves the
        old and the new file for every known scope and refuses if any
        behavior would change (except the announced new default,
        who.replies: conversational). Saves a timestamped backup first;
        comments in the old file are not carried over. Does not prompt.

Changing settings (the muse.ai path; writes config.discord.yaml, as
/settings does, and prints each change in plain words):

    bin/settings.py set KEY VALUE --place PLACE
    bin/settings.py reset KEY --place PLACE
    bin/settings.py undo --place PLACE
    bin/settings.py preset friends|community|work --place server:ID
    bin/settings.py pause --place PLACE
    bin/settings.py resume --place PLACE
    bin/settings.py explain [--place PLACE] [KEY ...]
    bin/settings.py places [--find TEXT] [--server ID]

`places` lists the places with their ids (from directory.json, which
the running monitor keeps up to date): defaults, dms, each server and
its channels. --find shows only the servers and channels whose name
contains TEXT; --server only that server.

PLACE is defaults (every server and DM without its own setting), dms,
server:<server id> or channel:<server id>:<channel id>. KEY is a
setting's key (who.replies) or its name ("When it answers"); VALUE a
value or its name (when-addressed, "Only when @mentioned", none, on,
a list of user ids separated by commas). Undo reverts the newest
change at that place with everything applied together with it (a
server's Undo includes its channels). Risky changes (judged by what
they lead to: strangers' DMs, your memory in a big server, Pause
everywhere, ...) are printed with a warning and NOT made unless --yes
is given: only add --yes after the owner explicitly said yes (exit
status 3 = needs --yes). --by NAME records who asked (default
muse.ai); it is a name, never a user id (ids are for changes made in
Discord): every change made here is sent to the owners in a silent DM
with an Undo button. --category ID names a channel's category.

--config PATH picks the file (default: $MUSE_DISCORD_CONFIG, else
config.yaml in the package directory).
"""
import argparse
import json
import os
import secrets
import sys
import time

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [PKG, os.path.join(PKG, "bin")]
import setup_common  # noqa: E402,F401  (re-runs under .venv if needed)
import muse_settings as MS  # noqa: E402

PLUGINS = os.path.join(PKG, "plugins")


def _cfg_path(args):
    return args.config or os.environ.get("MUSE_DISCORD_CONFIG") \
        or os.path.join(PKG, "config.yaml")


def _state(cfg):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(cfg)),
                               "state.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _known_guilds(cfg):
    known = _state(cfg).get("known_guilds")
    return known if isinstance(known, dict) else {}


DIRECTORY_STALE_SECS = 3600
CARD_TYPES = (0, 5, 15, 16)   # text, announcement, forum, media channels


def _directory(cfg):
    """directory.json (written by the running monitor: its servers, with
    member counts, and their channels), or None."""
    for d in (os.path.dirname(os.path.abspath(cfg)), PKG):
        try:
            with open(os.path.join(d, "directory.json")) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("guilds"), list):
            return data
    return None


def _info(cfg):
    """What the monitor last saw: server and channel names and member
    counts (directory.json, else state.json), for the risk checks and
    the sentences."""
    state = _state(cfg)
    known = state.get("known_guilds")
    counts = state.get("guild_members")
    known = known if isinstance(known, dict) else {}
    counts = counts if isinstance(counts, dict) else {}
    guilds = {str(g): {"id": str(g), "name": n,
                       "member_count": counts.get(g)}
              for g, n in known.items()}
    channels = {}
    d = _directory(cfg)
    if d is not None:
        for g in d.get("guilds") or []:
            if isinstance(g, dict) and g.get("id"):
                gid = str(g["id"])
                guilds[gid] = {"id": gid, "name": g.get("name") or gid,
                               "member_count": g.get("member_count")
                               if g.get("member_count") is not None
                               else counts.get(gid)}
        chans = d.get("channels")
        channels = chans if isinstance(chans, dict) else {}
    return {"guilds": list(guilds.values()), "channels": channels}


def _fmt(spec, value):
    v = MS.display(spec, value)
    if isinstance(v, str):
        return json.dumps(v) if v == "" or "\n" in v else v
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) if isinstance(x, str) else
                               str(x) for x in v) + "]"
    return str(v)


def cmd_validate(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    print(f"{cfg}: valid ({st.layout} layout).")
    if st.notice:
        print(st.notice)
        for line in st.mapping:
            print(f"  {line}")
    n = len(st.overlay)
    if n or st.retire:
        print(f"{MS.OVERLAY_NAME}: {n} Discord change(s) in effect.")
    for keystr, _entry, reason in st.retire:
        print(f"  {keystr}: no longer applies ({reason}); the monitor will "
              f"move it to history and tell the owners.")
    if getattr(st, "overlay_error", None):
        print(st.overlay_error)
    return 0


def cmd_show(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    known = _known_guilds(cfg)
    if args.dms:
        scope = MS.Scope.dm()
    elif args.server:
        name = known.get(args.server) or st.labels.get(args.server)
        scope = MS.Scope.server(args.server, name, args.channel,
                                args.category)
    else:
        if args.channel:
            print("--channel needs --server", file=sys.stderr)
            return 2
        scope = MS.Scope.everywhere()
    print(f"Settings for: {scope.label()}"
          + ("  (config.yaml uses the old layout)" if st.layout == "old"
             else ""))
    if scope.kind == "everywhere":
        print(f"  owners                   {', '.join(st.owners) or '(none)'}")
        print(f"  persona                  {st.persona}")
        print("  token_from               "
              + ", ".join(f"env:{w}" if k == "env" else w
                          for k, w in st.token_sources()))
        for key in ("presence.status", "presence.kind", "updates.notify",
                    "telemetry.usage_stats"):
            print(f"  {key:<24} {_fmt(MS.TOP[key], st.top(key))}")
        if not st.presence_enabled:
            print("  presence.enabled         off")
    kinds = {MS._scope_kind(p) for p, _n in st.chain(scope)
             if p[0] not in ("builtin", "builtin_all_servers")}
    if scope.kind == "server":
        kinds |= {"server", "channel"}
    for key, value, source, rows in st.explain(scope):
        spec = st.spec(key)
        if spec is None or not kinds & set(spec.scopes):
            continue
        if spec.hidden and source.startswith("built-in"):
            continue
        print(f"  {key:<24} {_fmt(spec, value):<18} [{source}]")
        if args.explain:
            for name, path, is_set, v, src in rows:
                shown = _fmt(spec, v) if is_set else "(not set)"
                where = MS._source_text(src) if is_set else ""
                print(f"      {name:<28} {shown}"
                      + (f"  ({where})" if where else ""))
    return 0


def cmd_fold(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    keys = sorted(MS.format_key(p, k) for (p, k), e in st.overlay.items()
                  if MS.entry_role(e) != "admin")
    if not keys:
        print("Nothing to fold: no Discord changes in effect (server "
              "admins' changes stay in Discord).")
        return 0
    if args.dry_run:
        print("Would copy into config.yaml (and clear from "
              f"{MS.OVERLAY_NAME}):")
        for k in keys:
            print(f"  {k}")
        return 0
    bak, folded = MS.fold(cfg, PLUGINS)
    print(f"Backed up config.yaml to {bak}.")
    print(f"Copied {len(folded)} Discord change(s) into config.yaml and "
          f"cleared them from {MS.OVERLAY_NAME}:")
    for k in folded:
        print(f"  {k}")
    print("config.yaml was rewritten: its comments are gone (the backup "
          "has them).")
    return 0


def cmd_migrate(args):
    cfg = _cfg_path(args)
    bak, text, lines, notes = MS.migrate(cfg, PLUGINS, args.state,
                                         write=not args.dry_run)
    print("What moved where:")
    for line in lines or ["(nothing set; the defaults carry over)"]:
        print(f"  {line}")
    print("Behavior check: every known scope resolves the same, except:")
    for n in notes:
        print(f"  {n}")
    if args.dry_run:
        print("\nThe new config.yaml would be:\n")
        print(text)
        return 0
    print(f"\nBacked up the old config.yaml to {bak}.")
    print("Wrote config.yaml in the new layout. Comments in the old file "
          "are not carried over (the backup has them).")
    return 0


class NeedsYes(Exception):
    pass


def _place(args, info):
    place = MS.parse_place(args.place or "defaults", info)
    if getattr(args, "category", None):
        if place.kind != "channel":
            raise MS.SettingsError("--category goes with a channel place")
        cat = str(args.category).strip()
        if not cat.isdigit() or len(cat) > 25 or cat == place.channel_id:
            raise MS.SettingsError(f"--category {cat}: expected the "
                                   f"channel's category id (digits)")
        place.category_id = cat
    return place


def _run(args, st, cfg, info, changes, by):
    """Print the changes and their risks; make them unless they are risky
    and --yes is missing."""
    names = MS.names_from(info, st)
    rows = MS.plan_changes(st, changes)
    todo = [r for r in rows if not r.noop]
    for r in rows:
        if r.noop:
            print("No change: " + r.describe(names) + ".")
    if not todo:
        return 0
    risk = MS.assess(st, [r.change for r in todo], info)
    for r in todo:
        print(r.describe(names))
    for n in risk.notes:
        print("Note: " + n)
    for w in risk.warnings:
        print("Warning: " + w)
    if risk.risky and not args.yes:
        raise NeedsYes()
    MS.apply(cfg, [r.change for r in todo], by,
             group=f"cli-{secrets.token_hex(4)}", plugins_dir=PLUGINS,
             role="owner")
    print("Done.")
    return 0


def cmd_set(args, reset=False):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    info = _info(cfg)
    place = _place(args, info)
    key = MS.key_from_text(st, args.key)
    if reset:
        changes = [MS.Change(place, key, "inherit")]
    elif key == "who.dms_from" and place.kind in ("dms", "defaults"):
        raw = args.value.strip()
        if raw.lower() in ("nobody", "off"):
            changes = MS.dms_from_changes(st, "nobody")
        else:
            value = MS.parse_value(st, key, raw)
            changes = MS.dms_from_changes(
                st, "list" if isinstance(value, list) else value,
                value if isinstance(value, list) else None)
    else:
        changes = [MS.Change(place, key, MS.parse_value(st, key,
                                                        args.value))]
    return _run(args, st, cfg, info, changes, args.by)


def cmd_reset(args):
    return cmd_set(args, reset=True)


def cmd_preset(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    info = _info(cfg)
    place = _place(args, info)
    changes = MS.preset_changes(st, args.name, place, info)
    if not changes:
        print(f"No change: {MS.place_name(place, MS.names_from(info, st))} "
              f"already matches {args.name.capitalize()}.")
        return 0
    return _run(args, st, cfg, info, changes, args.by)


def cmd_pause(args, resume=False):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    if resume:
        changes = MS.resume_changes(st, place)
        if not changes:
            print(f"No change: {MS.place_name(place, names)} is not paused "
                  f"there.")
            return 0
    else:
        changes = MS.pause_changes(place)
    return _run(args, st, cfg, info, changes, args.by)


def cmd_resume(args):
    return cmd_pause(args, resume=True)


def cmd_undo(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    children = place.kind == "server"
    text, ident = MS.undo_preview(st, place, children, names)
    if text is None:
        print(f"Nothing to undo in {MS.place_name(place, names)}.")
        return 0
    print(text)
    risk = MS.assess(st, MS.undo_plan(st, place, children), info)
    for w in risk.warnings:
        print("Warning: " + w)
    if risk.risky and not args.yes:
        raise NeedsYes()
    MS.undo_changes(cfg, place, args.by, PLUGINS, children=children,
                    role="owner", expect=ident)
    print("Done.")
    return 0


def cmd_explain(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    info = _info(cfg)
    place = _place(args, info)
    names = MS.names_from(info, st)
    keys = [MS.key_from_text(st, k) for k in args.keys] or None
    print(f"{MS.place_name(place, names)}:")
    if MS.is_paused(st, place):
        print("  Paused here.")
    if place.kind == "server" and not keys:
        print(f"  Matches: {MS.matching_preset(st, place) or 'Custom'}")
    for line in MS.explain_lines(st, place, keys, names):
        print(f"  {line}")
    return 0


def cmd_places(args):
    cfg = _cfg_path(args)
    d = _directory(cfg)
    if d is None:
        print("No directory.json yet: the monitor writes it once it is "
              "running. Start the monitor, then try again.",
              file=sys.stderr)
        return 1
    age = time.time() - (d.get("updated") or 0)
    if age > DIRECTORY_STALE_SECS:
        print(f"Note: directory.json is {int(age // 3600)} hour(s) old; "
              f"start the monitor for fresh names.")
    find = (args.find or "").strip().lower()
    chans = d.get("channels") if isinstance(d.get("channels"), dict) \
        else {}
    lines = [] if find or args.server else ["defaults", "dms"]
    for g in d.get("guilds") or []:
        if not isinstance(g, dict) or not g.get("id"):
            continue
        gid, name = str(g["id"]), str(g.get("name") or g["id"])
        if args.server and gid != str(args.server):
            continue
        n = g.get("member_count")
        size = f"{n:,} members" if isinstance(n, int) else "size unknown"
        rows = [c for c in chans.get(gid) or [] if isinstance(c, dict)
                and c.get("type") in CARD_TYPES]
        hit = not find or find in name.lower()
        rows = [c for c in rows
                if hit or find in str(c.get("name") or "").lower()]
        if not hit and not rows:
            continue
        lines.append(f"server:{gid}  {name} ({size})")
        lines += [f"  channel:{gid}:{c['id']}  #{c.get('name') or c['id']}"
                  for c in rows]
    if not lines:
        print(f"Nothing matches {args.find or args.server!r}.")
        return 1
    print("\n".join(lines))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="settings.py", description="muse-discord settings tool")
    ap.add_argument("--config", help="config.yaml to use")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="check config.yaml")
    sh = sub.add_parser("show", help="resolved settings for one scope")
    g = sh.add_mutually_exclusive_group()
    g.add_argument("--dms", action="store_true")
    g.add_argument("--server")
    sh.add_argument("--channel", help="a channel id (with --server)")
    sh.add_argument("--category", help="that channel's category id")
    sh.add_argument("--explain", action="store_true")
    fo = sub.add_parser("fold", help="copy Discord changes into "
                        "config.yaml")
    fo.add_argument("--dry-run", action="store_true")
    mi = sub.add_parser("migrate", help="convert an old-layout config.yaml")
    mi.add_argument("--dry-run", action="store_true")
    mi.add_argument("--state", help="state.json (server names; default: "
                    "next to config.yaml)")

    def changer(name, help_, *pos):
        p = sub.add_parser(name, help=help_)
        for a in pos:
            p.add_argument(*a[0], **a[1])
        p.add_argument("--place", required=name not in ("explain",),
                       help="defaults, dms, server:ID or channel:ID:ID")
        p.add_argument("--category", help="the channel's category id")
        if name != "explain":
            p.add_argument("--by", default="muse.ai",
                           help="who asked (default: muse.ai)")
            p.add_argument("--yes", action="store_true",
                           help="make a risky change (only after the "
                                "owner said yes)")
        return p
    changer("set", "change a setting at a place",
            (["key"], {}), (["value"], {}))
    changer("reset", "make a place follow its wider place again",
            (["key"], {}))
    changer("undo", "undo the newest change at a place")
    changer("preset", "apply Friends, Community or Work to a server",
            (["name"], {"choices": ["friends", "community", "work"],
                        "type": str.lower}))
    changer("pause", "stop answering at a place")
    changer("resume", "undo a pause at a place")
    changer("explain", "what the bot does at a place, in plain words",
            (["keys"], {"nargs": "*"}))
    pl = sub.add_parser("places", help="the places (servers, channels) "
                        "with their ids")
    pl.add_argument("--find", help="only names containing this text")
    pl.add_argument("--server", help="only this server id")
    args = ap.parse_args(argv)
    by = getattr(args, "by", None)
    if by is not None and (not str(by).strip()
                           or MS._id_str(str(by)) is not None):
        print(f"--by {by!r}: give a name (like muse.ai), not a user id; "
              f"user ids are for changes made in Discord.",
              file=sys.stderr)
        return 2
    try:
        return {"validate": cmd_validate, "show": cmd_show,
                "fold": cmd_fold, "migrate": cmd_migrate, "set": cmd_set,
                "reset": cmd_reset, "undo": cmd_undo, "preset": cmd_preset,
                "pause": cmd_pause, "resume": cmd_resume,
                "explain": cmd_explain, "places": cmd_places}[args.cmd](args)
    except NeedsYes:
        print("Not changed: this is risky. Tell the owner the warning; run "
              "it again with --yes only after they say yes.")
        return 3
    except MS.SettingsError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
