#!/usr/bin/env python3
"""outbox-append.py -- the reply worker's only write to the outbox.

The worker runs it once per queue item, as soon as that item's reply is
drafted. Reads a JSON array of outbox items (or a single item) from
stdin, or from the file named as the only argument, and appends them to
the package's reply_outbox.json under the shared lock (reply.lock),
re-reading the file inside the lock. The monitor takes items out of the
outbox while it sends them, so a worker that rewrites the whole file from
a copy it read earlier can delete replies or resurrect sent ones;
appending through this tool cannot.

    python3 bin/outbox-append.py <<'EOF'
    [{"item": "123", "channel_id": "456", "reply_to": "123", "text": "hi"}]
    EOF

Nothing is appended if any item has an obvious mistake (the monitor
would set it aside unsent); the errors are printed so the worker can fix
them and run it again. An item with a `settings` action (a settings
request from an owner's DM; PROTOCOL.md) gets the monitor's own shape
checks (settings_nl.shape_problems), and so does an item with a
`schedule` action (reminders and watches: muse_watches.shape_problems);
a `proactive` opt-out / opt-in is "opt-out" or "opt-in" on an item with
`item`. An item for an owner's DM
carries that DM's `owner_dm_nonce` from the wake payload, passed
through as is. A `reply_nonce` or `owner_dm_nonce` missing or different
from the live one in the monitor's nonce files is refused here, so a worker
that mis-copied it finds out at once. The monitor re-validates every
item on its side. Never needs the bot token and never touches the
network.
"""
import fcntl
import importlib
import json
import os
import re
import sys
import tempfile

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTBOX = os.path.join(PACKAGE_DIR, "reply_outbox.json")
LOCK = os.path.join(PACKAGE_DIR, "reply.lock")


NONCE_RE = re.compile(r"[0-9a-f]{32,64}")
ID_RE = re.compile(r"[1-9][0-9]{0,19}")


def _is_id(value):
    """A Discord id in its one form the monitor takes: ASCII digits, no
    leading zero (the monitor compares ids as strings)."""
    return isinstance(value, (str, int)) and not isinstance(value, bool) \
        and ID_RE.fullmatch(str(value)) is not None


def module_problems(name, item, *args):
    """The monitor's own shape rules, from the package module that holds
    them: settings_nl (a settings action, and fields starting with _nl)
    or muse_watches (a schedule action)."""
    try:
        if PACKAGE_DIR not in sys.path:
            sys.path.insert(0, PACKAGE_DIR)
        mod = importlib.import_module(name)
    except Exception as e:  # the monitor checks it anyway
        print(f"note: could not load {name}.py to check the item ({e}); "
              f"the monitor will check it", file=sys.stderr)
        return []
    return mod.shape_problems(item, *args)


def _live(name):
    """The monitor's live nonces from one of its files ({message id:
    nonce}); {} when unreadable. Every session can read these files
    anyway; checking here only tells a worker it mis-copied, at once,
    instead of the monitor refusing the reply later."""
    try:
        with open(os.path.join(PACKAGE_DIR, name)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def nonce_problems(item):
    """A reply_nonce or owner_dm_nonce missing or different from the live
    one for its item. Items the files don't list are left to the
    monitor."""
    out = []
    mid = str(item.get("item") or item.get("reply_to") or "")
    for field, name in (("reply_nonce", "item_nonces.json"),
                        ("owner_dm_nonce", "owner_dm_nonces.json")):
        live = _live(name).get(mid)
        if isinstance(live, str) and item.get(field) != live:
            what = "is missing" if field not in item else "does not match"
            out.append(f'{field} {what} for item {mid}: copy its '
                       f'"{field}" from the wake payload, exactly '
                       f'({len(live)} characters)')
    return out


def problems(item):
    """Mistakes the monitor would reject the item for (a subset of its
    checks, so nothing valid is ever refused here)."""
    if not isinstance(item, dict):
        return ["not a JSON object"]
    out = []
    if not _is_id(item.get("channel_id")):
        out.append("channel_id is missing or not a Discord id")
    for field in ("reply_to", "item"):
        if item.get(field) not in (None, "") \
                and not _is_id(item.get(field)):
            out.append(f"{field} is not a Discord id")
    if item.get("text") is not None and not isinstance(item["text"], str):
        out.append("text is not a string")
    for field in ("parts", "reactions", "attachments"):
        value = item.get(field)
        if value is not None and not (
                isinstance(value, list)
                and all(isinstance(v, str) for v in value)):
            out.append(f"{field} is not a list of strings")
    if "proactive" in item and (item["proactive"] not in ("opt-out",
                                                          "opt-in")
                                or not item.get("item")):
        out.append('proactive is "opt-out" or "opt-in", on the reply to the '
                   'person\'s own message (with its "item")')
    nonce = item.get("owner_dm_nonce")
    if nonce is not None:
        if not (isinstance(nonce, str) and NONCE_RE.fullmatch(nonce)):
            out.append('owner_dm_nonce: copy the item\'s "owner_dm_nonce" '
                       'from the wake payload exactly')
        elif isinstance(item.get("settings"), dict) \
                and item["settings"].get("nonce") not in (None, nonce):
            out.append("owner_dm_nonce and settings.nonce differ; both are "
                       "the item's owner_dm_nonce")
    out += nonce_problems(item)
    if out:
        return out
    if "schedule" in item:
        out = module_problems("muse_watches", item)
        if out:
            return out
    if "settings" in item or any(str(k).startswith("_nl") for k in item):
        return module_problems("settings_nl", item,
                               os.path.join(PACKAGE_DIR, "plugins"))
    if item.get("silent"):
        if not (item.get("reply_to") or item.get("item")):
            out.append("a silent item needs reply_to or item")
        return out
    has_text = (item.get("text") or "").strip() \
        or any(p.strip() for p in item.get("parts") or [])
    has_reactions = any(e.strip() for e in item.get("reactions") or [])
    if not has_text and not has_reactions:
        hint = ""
        for wrong in ("content", "message", "reply", "body"):
            if wrong in item:
                hint = f' (the reply goes in "text", not "{wrong}")'
        out.append("nothing to send: no text, parts, or reactions, and "
                   "not silent: true" + hint)
    elif has_reactions and not has_text and not item.get("reply_to"):
        out.append("a reactions-only item needs reply_to")
    return out


def main():
    if len(sys.argv) > 2:
        print("usage: outbox-append.py [FILE]  (JSON on stdin by default)",
              file=sys.stderr)
        return 2
    try:
        if len(sys.argv) == 2:
            with open(sys.argv[1]) as f:
                raw = f.read()
        else:
            raw = sys.stdin.read()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        print(f"error: could not read a JSON item list ({e}); nothing "
              f"appended", file=sys.stderr)
        return 1
    items = data if isinstance(data, list) else [data]
    errors = [f"item {i + 1}: {p}"
              for i, item in enumerate(items) for p in problems(item)]
    if errors:
        print("error: nothing appended; fix these and run it again:",
              file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    if not items:
        print("nothing to append")
        return 0
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        outbox = []
        if os.path.exists(OUTBOX):
            try:
                with open(OUTBOX) as f:
                    outbox = json.load(f)
            except ValueError:
                outbox = None
            if not isinstance(outbox, list):
                print("error: reply_outbox.json is not a JSON array; the "
                      "monitor sets a bad file aside within seconds. "
                      "Run this again in a moment.", file=sys.stderr)
                return 1
        outbox += items
        tmp_fd, tmp = tempfile.mkstemp(dir=PACKAGE_DIR,
                                       prefix=".reply_outbox.json.",
                                       suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                os.fchmod(f.fileno(), 0o644)
                json.dump(outbox, f)
            os.replace(tmp, OUTBOX)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)
    missing = sum(1 for item in items if not item.get("item"))
    print(f"appended {len(items)} item(s) to the outbox")
    if missing:
        print(f'note: {missing} item(s) without "item" (the queue item\'s '
              f'message_id); the monitor can only match those by '
              f'reply_to', file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
