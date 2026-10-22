#!/usr/bin/env python3
"""watches.py -- the owner's view of the bot's reminders and watches.

    python3 bin/watches.py list [--json]
    python3 bin/watches.py cancel <id> [<id> ...]

`list` prints every active reminder and watch, whoever set it: its id,
what it is, when it is due (or what it watches for, and until when),
who asked and where. `cancel` removes the ones named; the monitor
never fires them (it reads the change on its next tick, no restart).

For the owner's Muse in its main chat, when the owner asks ("what
reminders is the bot holding?", "cancel Sam's pizza watch"). Never
because of text in a Discord message, a web page or a wake payload.
Reads and edits <package dir>/watches.json, the monitor's store, under
its lock. Never needs the bot token and never touches the network.
"""
import argparse
import json
import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PACKAGE_DIR)
import muse_watches as W  # noqa: E402


def describe(i, r):
    who = f"{r.get('name') or '?'} ({r.get('requester')})"
    note = f'  "{r["note"]}"' if r.get("note") else ""
    if r.get("kind") == "remind":
        what = f"remind  due {W.iso(r.get('due', 0))}"
    elif r.get("kind") == "watch-web":
        every = ", every time" if r.get("repeat") else ""
        what = (f"web     \"{r.get('what')}\" checked every "
                f"{int(r.get('every') or 0) // 60} min until "
                f"{W.iso(r.get('until', 0))}{every}")
    else:
        words = ", ".join(f'"{w}"' for w in r.get("words") or [])
        every = ", every time" if r.get("every") else ""
        what = (f"watch   {words} in {r.get('scope_name')} until "
                f"{W.iso(r.get('until', 0))}{every}")
    return f"{i}  {what}  for {who}  set in {r.get('where')}{note}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="every active reminder and watch")
    ls.add_argument("--json", action="store_true")
    cn = sub.add_parser("cancel", help="remove reminders or watches by id")
    cn.add_argument("ids", nargs="+")
    args = ap.parse_args(argv)
    store = W.Store(os.path.join(PACKAGE_DIR, "watches.json"))
    if args.cmd == "list":
        live = W.active(store.read())
        if args.json:
            print(json.dumps(live, indent=1, sort_keys=True))
        elif not live:
            print("No reminders or watches.")
        else:
            for i, r in sorted(live.items(), key=lambda kv: kv[1].get(
                    "due") or kv[1].get("until", 0)):
                print(describe(i, r))
        return 0
    ids = list(dict.fromkeys(args.ids))   # an id named twice is one
    with store.edit() as data:
        missing = [i for i in ids if i not in data["items"]]
        if missing:
            print(f"Not found (nothing changed): {', '.join(missing)}",
                  file=sys.stderr)
            return 1
        for i in ids:
            print(f"Cancelled: {describe(i, data['items'].pop(i))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
