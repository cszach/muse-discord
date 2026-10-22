#!/usr/bin/env python3
"""fake_worker.py -- the tier-1 e2e worker: scripted, no LLM.

    python3 e2e/fake_worker.py --pkg <package dir> [--interval 1]

Polls <pkg>/reply_queue.json (and item_nonces.json, for each item's
reply_nonce, playing the wake hook) and answers every queue item exactly
once with one outbox item, written through <pkg>/bin/outbox-append.py like
a real worker (PROTOCOL.md): one append call per item, as
worker-prompt.template.md has the real worker do. The reply threads
under the item's LAST burst fragment (the trigger when there are none)
and says `ack <n>`, n = the number of messages in the item (trigger +
fragments), unless the item's text (any fragment) carries a directive:

  [[silent]]     a `silent` outbox item: deliberately no reply
  [[long]]       a 3500-character reply (the monitor must cap/split it)
  [[everyone]]   the reply contains @everyone and <@&<guild id>>
                 (allowed_mentions must stop both pings)
  [[mention_a]]  the reply mentions driver A as <@id> (driver A is the
                 config's first owner; --a-id overrides)
  [[slow]]       wait 8 s before writing the outbox item
  [[say:<text>]] the reply goes on with " <text>" after `ack <n>` (the
                 never_share scenario plants a listed phrase this way)
  [[remind:<seconds> <note>]]
                 the reply carries a schedule action (PROTOCOL.md,
                 Reminders and watches) asking for a reminder that
                 many seconds after the message
  [[watch:<word>]]
                 the same action also adds a Discord watch for <word>
                 in the item's channel

The reply to a schedule action still says `ack <n>`; a fire the
monitor queues later (kind "reminder") is answered `reminder <k>`, k =
the number of reminders and watches it carries. Directives combine
([[slow]] [[long]] is a slow long reply); [[silent]] wins over the
rest.

Every queue item it handles is also recorded in
<pkg>/e2e_worker_items.json (its ids, kind, follow_up, from_bot,
owner_command and settings card; the newest 200), so the harness can
check what the monitor queued (run.py copies it into the transcript;
the queue_item check reads it). Runs until SIGTERM. Never reads the
token.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

LONG_CHARS = 3500
SLOW_SECS = 8.0
WORKER_ITEMS = "e2e_worker_items.json"
WORKER_ITEMS_KEEP = 200
FLAGS = ("silent", "long", "everyone", "mention_a", "slow")
SAY_RE = re.compile(r"\[\[say:([^\]]*)\]\]")
REMIND_RE = re.compile(r"\[\[remind:(\d+)(?: ([^\]]*))?\]\]")
WATCH_RE = re.compile(r"\[\[watch:([^\]]+)\]\]")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def driver_a_id(pkg):
    """Driver A is the test config's (only) owner: `owners: [id]`, or
    owners.user_ids in the old layout."""
    try:
        import yaml
        with open(os.path.join(pkg, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        owners = cfg.get("owners") or []
        if isinstance(owners, dict):
            owners = owners.get("user_ids") or []
        return str(owners[0]) if owners else None
    except Exception:
        return None


def item_texts(item):
    """The trigger's text plus every fragment's (content already joins
    them for bursts; fragments are checked too in case it is cut)."""
    texts = [item.get("content") or ""]
    texts += [f.get("content") or "" for f in item.get("burst_fragments")
              or []]
    return "\n".join(texts)


def directives(item):
    text = item_texts(item)
    found = {d for d in FLAGS if f"[[{d}]]" in text}
    if SAY_RE.search(text):
        found.add("say")
    if REMIND_RE.search(text):
        found.add("remind")
    if WATCH_RE.search(text):
        found.add("watch")
    return found


def say_text(item):
    """The text of every distinct [[say:<text>]] directive, in order,
    joined (item_texts repeats a burst's fragments)."""
    out = []
    for t in SAY_RE.findall(item_texts(item)):
        if t.strip() and t.strip() not in out:
            out.append(t.strip())
    return " ".join(out)


def item_record(item):
    """What e2e_worker_items.json keeps about one queue item."""
    return {"message_id": item.get("message_id"),
            "fragment_ids": [f.get("message_id") for f in
                             item.get("burst_fragments") or []
                             if isinstance(f, dict)],
            "channel_id": item.get("channel_id"),
            "kind": item.get("kind"),
            "follow_up": item.get("follow_up"),
            "from_bot": item.get("from_bot"),
            "owner_command": item.get("owner_command"),
            "settings": item.get("settings")}


def record_items(pkg, items):
    """Append records to <pkg>/e2e_worker_items.json (atomic replace,
    newest WORKER_ITEMS_KEEP kept)."""
    if not items:
        return
    path = os.path.join(pkg, WORKER_ITEMS)
    data = load_json(path, [])
    if not isinstance(data, list):
        data = []
    data = (data + [item_record(i) for i in items])[-WORKER_ITEMS_KEEP:]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def build_reply(item, a_id=None):
    """(outbox item, delay seconds) answering one queue item."""
    frags = item.get("burst_fragments") or []
    n = 1 + len(frags)
    last = frags[-1]["message_id"] if frags else item.get("message_id")
    flags = directives(item)
    out = {"item": item.get("message_id"),
           "channel_id": item.get("channel_id"),
           "channel_name": item.get("channel_name"),
           "author": item.get("author"),
           "reply_to": last}
    if item.get("owner_dm_nonce"):
        # An owner-DM-only wake's payload: every item for the DM echoes it.
        out["owner_dm_nonce"] = item["owner_dm_nonce"]
    if item.get("reply_nonce"):
        # Every outbox item echoes its item's reply_nonce.
        out["reply_nonce"] = item["reply_nonce"]
    delay = SLOW_SECS if "slow" in flags else 0.0
    if "silent" in flags:
        out["silent"] = True
        return out, delay
    text = f"ack {n}"
    if item.get("kind") == "reminder":
        due = (item.get("reminder") or {}).get("items") or []
        text = f"reminder {len(due)}"
    if "mention_a" in flags and a_id:
        text += f" <@{a_id}>"
    if "say" in flags and say_text(item):
        text += " " + say_text(item)
    if "everyone" in flags:
        text += f" @everyone <@&{item.get('guild_id')}>"
    if "long" in flags:
        filler = " lorem ipsum dolor sit amet"
        while len(text) < LONG_CHARS:
            text += filler
        text = text[:LONG_CHARS]
    out["text"] = text
    changes = []
    if "remind" in flags:
        secs, note = REMIND_RE.search(item_texts(item)).groups()
        changes.append({"op": "add", "kind": "remind", "after": int(secs),
                        "note": (note or "").strip()})
    if "watch" in flags:
        word = WATCH_RE.search(item_texts(item)).group(1).strip()
        changes.append({"op": "add", "kind": "watch", "note": word,
                        "match": {"words": [word], "in": "here"}})
    if changes:
        out["schedule"] = {"v": 1, "changes": changes}
    return out, delay


def append_outbox(pkg, items):
    """Append through the package's own tool (locks, validates)."""
    tool = os.path.join(pkg, "bin", "outbox-append.py")
    p = subprocess.run([sys.executable, tool], input=json.dumps(items),
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        print(f"outbox-append failed ({p.returncode}): {p.stderr.strip()}",
              flush=True)
    return p.returncode == 0


class Worker:
    def __init__(self, pkg, a_id=None, clock=time.time):
        self.pkg = os.path.abspath(pkg)
        self.a_id = a_id
        self.clock = clock
        self.answered = set()   # queue message_ids handled (or scheduled)
        self.scheduled = []     # [(due, outbox item)] for [[slow]]
        # Items already answered in the outbox (a restarted worker) are
        # never answered again.
        for name in ("reply_outbox.json", "reply_outbox.processing.json"):
            for o in load_json(os.path.join(self.pkg, name), []) or []:
                if isinstance(o, dict) and o.get("item"):
                    self.answered.add(str(o["item"]))

    def tick(self):
        """One poll: schedule new items, write what is due. Returns the
        number of outbox items written."""
        if self.a_id is None:
            self.a_id = driver_a_id(self.pkg)
        queue = load_json(os.path.join(self.pkg, "reply_queue.json"), [])
        # Playing the wake hook too: each item's reply_nonce, from the
        # monitor's file (a real worker gets it in its wake payload).
        nonces = load_json(os.path.join(self.pkg, "item_nonces.json"), {})
        nonces = nonces if isinstance(nonces, dict) else {}
        now = self.clock()
        seen = []
        for item in queue if isinstance(queue, list) else []:
            mid = str(item.get("message_id") or "")
            if not mid or mid in self.answered:
                continue
            if nonces.get(mid):
                item = dict(item, reply_nonce=nonces[mid])
            elif os.path.exists(os.path.join(self.pkg, "item_nonces.json")):
                continue   # minted within seconds: the next tick
            self.answered.add(mid)
            seen.append(item)
            if item.get("kind") == "proactive":
                # A heartbeat slot: its nonce is never in the queue file
                # this worker reads, so it lets the slot expire.
                continue
            out, delay = build_reply(item, self.a_id)
            print(f"item {mid}: {sorted(directives(item)) or 'ack'}"
                  f"{f' (in {delay:.0f}s)' if delay else ''}", flush=True)
            self.scheduled.append((now + delay, out))
        try:
            record_items(self.pkg, seen)
        except OSError as e:
            print(f"could not record items: {e}", flush=True)
        written, kept = 0, []
        for t, o in self.scheduled:
            if t <= now and append_outbox(self.pkg, [o]):
                written += 1
            else:
                kept.append((t, o))  # not due yet, or retried next tick
        self.scheduled = kept
        return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pkg", required=True, help="the SUT package dir")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--a-id", default=None,
                    help="driver A's user id (default: first owner)")
    args = ap.parse_args(argv)
    stop = {"now": False}

    def on_term(signum, frame):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    w = Worker(args.pkg, a_id=args.a_id)
    print(f"fake worker polling {w.pkg}", flush=True)
    while not stop["now"]:
        try:
            w.tick()
        except Exception as e:  # keep answering; the run checks results
            print(f"fake worker error: {e}", flush=True)
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
