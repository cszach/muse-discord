#!/usr/bin/env python3
"""muse-discord presence client.

Keeps a Discord gateway (websocket) session open so the bot shows as
ONLINE instead of offline. REST API calls alone don't create presence;
only an active gateway session does.

- Identifies with intents=0 (no message events; detection happens in
  monitor.py, not here).
- Shows ONE status from config (presence.status / presence.kind; old
  layout presence.status_text / status_kind). There is deliberately no
  rotation, scheduling, or profile/banner management.
- Follows settings changes live: while connected it checks config.yaml
  and config.discord.yaml (the /settings overlay) every few seconds (a
  stat; the files are re-read only when they changed) and, when the
  status changes, sends a Presence Update (op 3) over the open session:
  no reconnect, at most one update per PRESENCE_MIN_INTERVAL seconds
  (rapid changes collapse into the latest one). Every identify and
  resume uses the current settings. An edit that fails validation keeps
  the last good status (logged once).
- Sends heartbeats on Discord's interval; auto-reconnects with backoff
  and resumes the session when Discord allows it. Exits instead when
  Discord rejects the token (printing "TOKEN REJECTED" for the
  watchdog) or closes with a code reconnecting cannot fix.
- If presence.enabled is off in config, exits immediately, or, when
  it is turned off while running, goes offline and exits (the watchdog
  checks the same flag and won't try to restart it; turning it back on
  is the watchdog's job).

Settings are read through muse_settings.py (both config layouts).
Token resolution matches monitor.py: argv[1], else token_from in order
(an env:NAME variable when set, a readable file). The token is never
printed or written anywhere.

Usage: python3 presence.py [bot-token]
"""
import asyncio
import json
import os
import sys

import websockets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
GATEWAY_QUERY = "/?v=10&encoding=json"
# Close codes reconnecting cannot fix (same contract as monitor.py):
# 4004 = the token was rejected: print the "TOKEN REJECTED" marker the
# watchdog alerts on, and exit. 4010-4014 = shard / API version / intents
# problems: exit too, so the watchdog's restart attempt reports them
# instead of this process reconnecting forever. 4007 / 4009 = the
# session cannot be resumed: identify fresh.
EXIT_TOKEN_REJECTED = 3
TOKEN_REJECTED_CODE = 4004
FATAL_CODES = (4010, 4011, 4012, 4013, 4014)
FRESH_SESSION_CODES = (4007, 4009)
ACTIVITY_TYPES = {"playing": 0, "streaming": 1, "listening": 2,
                  "watching": 3, "custom": 4, "competing": 5}
# Live settings: how often (seconds) to check the config files for a
# change while connected, and the least time between two Presence
# Updates (Discord limits gateway commands; this stays far under it).
SETTINGS_POLL_SECONDS = 2.0
PRESENCE_MIN_INTERVAL = 5.0


def open_settings():
    try:
        import yaml  # noqa: F401 (muse_settings needs it)
    except ImportError:
        print("PyYAML is required: pip install -r requirements.txt",
              file=sys.stderr)
        sys.exit(1)
    sys.path.insert(0, BASE_DIR)
    import muse_settings
    path = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        BASE_DIR, "config.yaml")
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        path = sys.argv[2]
    try:
        return muse_settings.Watcher(path, os.path.join(BASE_DIR, "plugins"))
    except muse_settings.SettingsError as e:
        print(f"CONFIG ERROR: config.yaml is invalid; not starting.\n{e}",
              flush=True)
        sys.exit(1)


# WATCHER re-reads config.yaml / config.discord.yaml when either changes;
# SETTINGS is its current (last valid) settings.
WATCHER = open_settings()
SETTINGS = WATCHER.current


async def refresh_settings():
    """Pick up a change to either settings file (a stat when nothing
    changed; the re-read runs off the event loop so heartbeats never
    wait on it). An invalid edit keeps the previous settings and is
    logged once. True when the settings changed."""
    global SETTINGS
    changed, err = await asyncio.to_thread(WATCHER.poll)
    if err:
        print(f"Config re-read failed; keeping the last good status:\n"
              f"{err}", flush=True)
    if changed:
        SETTINGS = WATCHER.current
    return changed


def _load_token():
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        return sys.argv[1]
    for kind, where in SETTINGS.token_sources():
        if kind == "env":
            if where and os.environ.get(where):
                return os.environ[where].strip()
            continue
        try:
            with open(os.path.expanduser(where)) as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


TOKEN = _load_token()


def presence_payload():
    """Online, with the one static activity from config (if any)."""
    text = str(SETTINGS.presence_status or "").strip()
    atype = ACTIVITY_TYPES.get(
        str(SETTINGS.presence_kind or "watching").lower(), 3)
    acts = [] if not text else [
        {"name": "Custom Status", "type": 4, "state": text} if atype == 4
        else {"name": text, "type": atype}]
    return {"status": "online", "activities": acts, "afk": False,
            "since": None}


def describe(payload):
    """One-line summary of a presence payload, for the log."""
    for a in payload.get("activities") or ():
        kind = next((k for k, v in ACTIVITY_TYPES.items()
                     if v == a.get("type")), "?")
        return f'{kind} "{a.get("state") or a.get("name")}"'
    return "no status"


class PresenceSync:
    """The presence Discord has for the session and when it was last
    changed, so a settings change becomes one op 3, rate limited."""

    def __init__(self):
        self.sent = None      # the payload the session shows
        self.last = None      # loop time of the last op 3
        self.live = False     # READY / RESUMED seen on this connection
        self.stop = False     # presence.enabled turned off

    def identified(self, payload):
        self.sent = payload

    async def sync(self, ws):
        """Send the current presence if it differs from the session's
        and the rate limit allows; otherwise a later call sends the
        latest one (so a burst of changes becomes a single update)."""
        if not self.live:
            return
        want = presence_payload()
        if want == self.sent:
            return
        now = asyncio.get_running_loop().time()
        if self.last is not None and now - self.last < PRESENCE_MIN_INTERVAL:
            return
        await ws.send(json.dumps({"op": 3, "d": want}))
        self.sent = want
        self.last = now
        print(f"Status changed: now {describe(want)}", flush=True)

    async def watch(self, ws):
        """While connected: follow the settings files; go offline and
        stop when presence.enabled is turned off."""
        while True:
            await asyncio.sleep(SETTINGS_POLL_SECONDS)
            try:
                await refresh_settings()
            except Exception as e:  # keep the last good settings
                print(f"Config re-read failed: {e}", flush=True)
                continue
            if not SETTINGS.presence_enabled:
                print("presence.enabled is now off in config; going "
                      "offline and exiting.", flush=True)
                self.stop = True
                await ws.close()
                return
            try:
                await self.sync(ws)
            except websockets.ConnectionClosed:
                return  # the receive loop reconnects; resume re-syncs


def _close_code(exc):
    """The close code of a websockets ConnectionClosed, across versions
    (newer ones carry it on .rcvd; .code is the deprecated spelling)."""
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        return rcvd.code
    return getattr(exc, "code", None)


async def run():
    """Hold the gateway session until presence.enabled is turned off
    (returns) or the token / gateway is refused (exits)."""
    backoff = 5
    session_id = None
    seq = None
    resume_url = None
    pres = PresenceSync()
    while True:
        hb_task = None
        watch_task = None
        pres.live = False
        await refresh_settings()  # (re)identify / resume with the latest
        if not SETTINGS.presence_enabled:
            print("presence.enabled is false in config; not starting.",
                  flush=True)
            return
        resuming = bool(session_id and seq is not None)
        # A resume goes to the session's own resume_gateway_url (from
        # READY); the default URL is for fresh identifies.
        url = GATEWAY
        if resuming and resume_url:
            url = resume_url.rstrip("/") + GATEWAY_QUERY
        try:
            async with websockets.connect(
                    url, max_size=10 * 1024 * 1024) as ws:
                hello = json.loads(await ws.recv())
                if hello.get("op") != 10:
                    # Raise rather than loop straight back: the backoff
                    # below must apply.
                    raise RuntimeError(f"unexpected first payload: {hello}")
                hb_interval = hello["d"]["heartbeat_interval"] / 1000.0

                if resuming:
                    await ws.send(json.dumps({"op": 6, "d": {
                        "token": TOKEN, "session_id": session_id,
                        "seq": seq}}))
                else:
                    payload = presence_payload()
                    await ws.send(json.dumps({"op": 2, "d": {
                        "token": TOKEN,
                        "intents": 0,
                        "properties": {"os": "linux",
                                       "browser": "muse-discord",
                                       "device": "muse-discord"},
                        "presence": payload,
                    }}))
                    pres.identified(payload)
                backoff = 5  # reset on successful connect

                async def heartbeat_loop():
                    while True:
                        await asyncio.sleep(hb_interval)
                        await ws.send(json.dumps({"op": 1, "d": seq}))

                hb_task = asyncio.create_task(heartbeat_loop())
                watch_task = asyncio.create_task(pres.watch(ws))
                async for raw in ws:
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if op == 0:  # dispatch
                        seq = msg.get("s", seq)
                        if msg.get("t") == "READY":
                            session_id = msg["d"].get("session_id")
                            resume_url = msg["d"].get("resume_gateway_url")
                            user = msg["d"]["user"]
                            # The watchdog's health check greps
                            # presence.log for "READY".
                            print(f"READY - {user.get('username')} is "
                                  f"online (session {session_id})",
                                  flush=True)
                            pres.live = True
                        elif msg.get("t") == "RESUMED":
                            print("Session resumed", flush=True)
                            # A change made while disconnected.
                            pres.live = True
                            await pres.sync(ws)
                    elif op == 1:  # heartbeat requested now
                        await ws.send(json.dumps({"op": 1, "d": seq}))
                    elif op == 7:  # server asks us to reconnect
                        print("Discord requested reconnect", flush=True)
                        break
                    elif op == 9:  # invalid session
                        # d is true when the session can still be
                        # resumed; otherwise re-identify fresh.
                        if not msg.get("d"):
                            print("Invalid session, will re-identify",
                                  flush=True)
                            session_id = None
                            seq = None
                        await asyncio.sleep(5)
                        break
                    # op 11 = heartbeat ACK, nothing to do
        except websockets.ConnectionClosed as e:
            code = _close_code(e)
            if code == TOKEN_REJECTED_CODE:
                print("TOKEN REJECTED: Discord refused the bot token "
                      "(gateway close code 4004); it was probably reset in "
                      "the developer portal. Exiting.", flush=True)
                sys.exit(EXIT_TOKEN_REJECTED)
            if code in FATAL_CODES:
                print(f"GATEWAY DISABLED (code {code}): Discord refused "
                      f"this connection for a reason reconnecting cannot "
                      f"fix. Exiting.", flush=True)
                sys.exit(1)
            if code in FRESH_SESSION_CODES:
                session_id = None
                seq = None
            print(f"Gateway closed (code {code})", flush=True)
        except Exception as e:
            print(f"Gateway error: {e}", flush=True)
        finally:
            for task in (hb_task, watch_task):
                if task:
                    task.cancel()
        if pres.stop:
            return
        print(f"Reconnecting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    if not SETTINGS.presence_enabled:
        print("presence.enabled is false in config; not starting.",
              flush=True)
        sys.exit(0)
    if not TOKEN:
        sys.exit("usage: presence.py <bot-token> (or point token_from in "
                 "config.yaml at the token)")
    asyncio.run(run())
