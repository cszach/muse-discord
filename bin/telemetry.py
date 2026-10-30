#!/usr/bin/env python3
"""muse-discord usage stats (anonymous, opt-out; see telemetry.py).

    bin/telemetry.py show
        Print exactly the report(s) that would be sent next, as JSON on
        stdout (notes on stderr). With no closed day waiting, today's
        counts so far (sent only after the day closes). The monitor
        writes its counts to telemetry.json every few minutes.
    bin/telemetry.py status
        On or off (and why), where reports go, the last send (and why
        it failed), and how many days are waiting.
    bin/telemetry.py send
        Ask the running monitor to send the waiting days now (within a
        minute) instead of at its next scheduled try.
    bin/telemetry.py off | on
        Turn usage stats off or on (telemetry.usage_stats, written like
        `bin/settings.py set`; the owners get the usual change notice).
        Days it was off are never sent. The environment variable
        MUSE_DISCORD_TELEMETRY=off also turns it off.
    bin/telemetry.py reset-id
        Replace the random install id with a new one.

--config PATH picks the config file (default: $MUSE_DISCORD_CONFIG, else
config.yaml in the package directory).
"""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [PKG, os.path.join(PKG, "bin")]
import setup_common  # noqa: E402,F401  (re-runs under .venv if needed)
import muse_settings as MS  # noqa: E402
import telemetry as T  # noqa: E402

PLUGINS = os.path.join(PKG, "plugins")
DATA = os.path.join(PKG, "telemetry.json")
ID_FILE = os.path.join(PKG, "telemetry_id")
SEND_NOW = os.path.join(PKG, T.SEND_NOW_FILE)


def _cfg(args):
    return args.config or os.environ.get("MUSE_DISCORD_CONFIG") \
        or os.path.join(PKG, "config.yaml")


def _settings(args):
    try:
        return MS.load(_cfg(args), PLUGINS)
    except MS.SettingsError as e:
        print(f"settings: {e}", file=sys.stderr)
        return None


def _why_off(st):
    if T.env_off():
        return f"off ({T.ENV_SWITCH}={os.environ.get(T.ENV_SWITCH)})"
    if st is not None and not T.enabled(st):
        return f"off ({T.SETTING}: off)"
    return None


def cmd_show(args):
    st = _settings(args)
    d = T.load_file(DATA)
    pending = [p for p in d.get("pending") or [] if isinstance(p, dict)
               and p.get("day")]
    why = _why_off(st)
    install_id = T.read_install_id(ID_FILE, create=False) \
        or "(made at the first send)"
    if why:
        print(f"Usage stats are {why}: nothing is sent.", file=sys.stderr)
    elif not T.endpoint():
        print("No endpoint is configured (telemetry.TELEMETRY_URL): "
              "nothing is sent.", file=sys.stderr)
    if pending and not why:
        print(f"{len(pending)} closed day(s) waiting; the next send posts "
              f"each of these:", file=sys.stderr)
        out = [T.build_payload(p, install_id) for p in pending]
    else:
        today = d.get("today") if isinstance(d.get("today"), dict) \
            else T.new_day(T.utc_day())
        rec = T.new_day(today.get("day") or T.utc_day())
        rec.update(today)
        gauges = dict(rec.get("gauges") or {})
        gauges.setdefault("version", T.installed_version(PKG))
        rec["gauges"] = gauges
        print(f"Today ({rec['day']}) so far; sent only after the day "
              f"closes:", file=sys.stderr)
        out = [T.build_payload(rec, install_id)]
    print(json.dumps(out, indent=2, sort_keys=False))
    return 0


def cmd_status(args):
    st = _settings(args)
    d = T.load_file(DATA)
    why = _why_off(st)
    print(f"Usage stats: {why or 'on'}")
    print(f"Endpoint:    {T.endpoint() or '(none configured: nothing is sent)'}")
    last = d.get("last_send")
    if isinstance(last, dict):
        retry = d.get("retry_after")
        if last.get("ok"):
            then = ""
        elif isinstance(retry, (int, float)) and abs(retry) < 1e11 \
                and d.get("last_attempt") == T.utc_day():
            at = datetime.fromtimestamp(retry, timezone.utc)
            then = f"; the rest is retried after {at:%H:%M} UTC"
        else:
            then = "; the rest is retried the next day"
        print(f"Last send:   {last.get('day')}: {last.get('sent', 0)} "
              f"day(s) sent" + (f", {last['refused']} refused"
                                if last.get("refused") else "") + then)
        if last.get("error"):
            print(f"Failed with: {last['error']}")
    else:
        print("Last send:   never")
    pending = [p.get("day") for p in d.get("pending") or []
               if isinstance(p, dict)]
    print(f"Waiting:     {len(pending)} day(s)"
          + (f" ({', '.join(pending)})" if pending else ""))
    after = d.get("send_after")
    if pending and not why and T.endpoint() \
            and isinstance(after, (int, float)) \
            and d.get("last_attempt") != T.utc_day():
        at = datetime.fromtimestamp(after, timezone.utc)
        print(f"Next send:   after {at:%Y-%m-%d %H:%M} UTC")
    print(f"Install id:  {T.read_install_id(ID_FILE, create=False) or '(none yet)'}")
    if not os.path.exists(DATA):
        print("(No counts yet: the monitor writes telemetry.json once it "
              "runs.)")
    return 0


def cmd_switch(args, value):
    cfg = _cfg(args)
    try:
        st = MS.load(cfg, PLUGINS)
        place = MS.parse_place("defaults")
        changes = [MS.Change(place, T.SETTING, value)]
        rows = MS.plan_changes(st, changes)
        if all(r.noop for r in rows):
            print(f"Usage stats are already {value}.")
        else:
            MS.apply(cfg, changes, args.by,
                     group=f"cli-{secrets.token_hex(4)}",
                     plugins_dir=PLUGINS, role="owner")
            print(f"Usage stats are now {value}"
                  + (" (days it was off are never sent)." if value == "off"
                     else "."))
    except MS.SettingsError as e:
        print(f"settings: {e}", file=sys.stderr)
        return 1
    if value == "on" and T.env_off():
        print(f"Note: {T.ENV_SWITCH}={os.environ.get(T.ENV_SWITCH)} in this "
              f"environment still turns them off where it is set.")
    return 0


def cmd_send(args):
    why = _why_off(_settings(args))
    pending = [p for p in T.load_file(DATA).get("pending") or []
               if isinstance(p, dict)]
    if why:
        print(f"Usage stats are {why}: nothing is sent.")
        return 1
    if not T.endpoint():
        print("No endpoint is configured: nothing is sent.")
        return 1
    version = T.installed_version(PKG)
    if not T.is_release(version):
        print(f"This copy ({version or 'unknown version'}) is not a "
              f"release: only releases send.")
        return 1
    if not pending:
        print("Nothing is waiting to be sent.")
        return 0
    with open(SEND_NOW, "w"):
        pass
    print(f"Asked the monitor to send {len(pending)} waiting day(s) now; "
          f"check `bin/telemetry.py status` in a minute.")
    return 0


def cmd_reset_id(args):
    T.reset_install_id(ID_FILE)
    print("New install id made; the next report uses it.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bin/telemetry.py",
        description="muse-discord anonymous usage stats",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--config")
    ap.add_argument("--by", default="bin/telemetry.py",
                    help="who asked, for off/on (a name)")
    ap.add_argument("command",
                    choices=("show", "status", "send", "off", "on",
                             "reset-id"))
    args = ap.parse_args(argv)
    if args.command == "show":
        return cmd_show(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "send":
        return cmd_send(args)
    if args.command in ("off", "on"):
        return cmd_switch(args, args.command)
    return cmd_reset_id(args)


if __name__ == "__main__":
    sys.exit(main())
