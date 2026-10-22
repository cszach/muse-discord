"""discord_rest.py -- the e2e harness's minimal Discord REST client.

The driver bots (playing users) and the harness itself talk to Discord
through this module only: stdlib urllib, no gateway. It is NOT the
product's client; monitor.py has its own.

    bot = Bot(token)
    me = bot.me()
    msg = bot.send(channel_id, "hello", reply_to=None, files=["cat.png"])
    msgs = bot.history(channel_id)          # every message, oldest first
    bot.delete_message(channel_id, msg["id"])
    th = bot.start_thread(channel_id, msg["id"], "side chat")
    bot.send(th["id"], "in the thread")     # a thread is a channel
    ch = bot.create_channel(guild_id, "e2e-123-mention_burst")
    bot.delete_channel(ch["id"])

A 429 is waited out (Retry-After) and retried; a 5xx or a network error
is retried a few times with growing gaps. Anything else raises
DiscordError. Error messages never contain the token.
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/cszach/muse-discord, e2e)"
# Discord's epoch (2015-01-01) in ms, for snowflake <-> time.
DISCORD_EPOCH_MS = 1420070400000
MAX_429_RETRIES = 8
MAX_5XX_RETRIES = 4
TIMEOUT = 30


class DiscordError(Exception):
    """A Discord REST failure. status is the HTTP status (None for a
    network error); transient is True for failures that are Discord's or
    the network's fault (5xx, timeouts, retries exhausted), which the
    runner reports as a flake rather than a test failure."""

    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def snowflake_time(snowflake):
    """Epoch seconds encoded in a Discord id."""
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000.0


def _multipart(payload, files):
    """Encode payload_json plus files[n] as multipart/form-data."""
    boundary = "e2e" + uuid.uuid4().hex
    out = []

    def part(headers, body):
        out.append(f"--{boundary}\r\n".encode())
        for h in headers:
            out.append(f"{h}\r\n".encode())
        out.append(b"\r\n")
        out.append(body)
        out.append(b"\r\n")

    part(['Content-Disposition: form-data; name="payload_json"',
          "Content-Type: application/json"],
         json.dumps(payload).encode())
    for i, path in enumerate(files):
        name = os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        part([f'Content-Disposition: form-data; name="files[{i}]"; '
              f'filename="{name}"',
              "Content-Type: application/octet-stream"], data)
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class Bot:
    """One bot account's REST session."""

    def __init__(self, token, api=API, sleep=time.sleep):
        self._token = token
        self.api = api.rstrip("/")
        self._sleep = sleep  # injectable for offline tests

    def __repr__(self):
        return "<discord_rest.Bot>"  # never the token

    # --- transport ---

    def _open(self, req):
        """urlopen, isolated so tests can replace it."""
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    def request(self, method, path, body=None, files=None):
        """One API call, with 429 / 5xx retries. Returns parsed JSON, or
        None for an empty (204) response."""
        url = self.api + path
        n429 = n5xx = 0
        while True:
            if files:
                data, ctype = _multipart(body or {}, files)
            elif body is not None:
                data, ctype = json.dumps(body).encode(), "application/json"
            else:
                data, ctype = None, None
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bot {self._token}")
            req.add_header("User-Agent", USER_AGENT)
            if ctype:
                req.add_header("Content-Type", ctype)
            try:
                with self._open(req) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    raw = e.read()
                except Exception:
                    raw = b""
                finally:
                    e.close()
                if status == 429 and n429 < MAX_429_RETRIES:
                    n429 += 1
                    self._sleep(_retry_after(e, raw))
                    continue
                if status >= 500 and n5xx < MAX_5XX_RETRIES:
                    n5xx += 1
                    self._sleep(2 * n5xx)
                    continue
                detail = raw[:300].decode("utf-8", "replace")
                raise DiscordError(
                    f"{method} {path} -> HTTP {status}: {detail}",
                    status=status,
                    transient=status >= 500 or status == 429) from None
            except (urllib.error.URLError, OSError) as e:
                if n5xx < MAX_5XX_RETRIES:
                    n5xx += 1
                    self._sleep(2 * n5xx)
                    continue
                raise DiscordError(f"{method} {path} -> network error: "
                                   f"{e}", transient=True) from None

    # --- API ---

    def me(self):
        """The bot's own user object ({id, username, ...})."""
        return self.request("GET", "/users/@me")

    def send(self, channel_id, content, reply_to=None, files=None):
        """Post a message; returns the created message object. reply_to
        makes it a Discord reply. files are local paths. Only explicit
        <@id> user mentions ping: a reply does not ping (or list in
        `mentions`) the replied-to author, so a reply to the SUT
        exercises its reply path, not its mention path."""
        body = {"content": content,
                "allowed_mentions": {"parse": ["users"],
                                     "replied_user": False}}
        if reply_to:
            body["message_reference"] = {"message_id": str(reply_to),
                                         "fail_if_not_exists": False}
        return self.request("POST", f"/channels/{channel_id}/messages",
                            body=body, files=list(files or []))

    def typing(self, channel_id):
        """Show "typing..." in the channel, as a person does before they
        post (the SUT holds a burst open while its author types)."""
        return self.request("POST", f"/channels/{channel_id}/typing")

    def history(self, channel_id, after=None):
        """Every message in the channel newer than `after` (an id; None =
        all), oldest first. Paginates 100 at a time."""
        cursor = str(after or 0)
        out = {}
        while True:
            page = self.request(
                "GET",
                f"/channels/{channel_id}/messages?after={cursor}&limit=100")
            if not page:
                break
            for m in page:
                out[m["id"]] = m
            cursor = max((m["id"] for m in page), key=int)
            if len(page) < 100:
                break
        return sorted(out.values(), key=lambda m: int(m["id"]))

    def delete_message(self, channel_id, message_id):
        """Delete a message (a bot may always delete its own). One that
        is already gone is fine."""
        try:
            return self.request(
                "DELETE", f"/channels/{channel_id}/messages/{message_id}")
        except DiscordError as e:
            if e.status == 404:
                return None
            raise

    def start_thread(self, channel_id, message_id, name):
        """Start a public thread on a message; returns the thread (a
        channel object whose id also works with send / history). Needs
        Create Public Threads in the channel (and Send Messages in
        Threads to post in it): a 403 means the bot's role lacks it."""
        return self.request(
            "POST", f"/channels/{channel_id}/messages/{message_id}/threads",
            body={"name": str(name)[:100], "auto_archive_duration": 60})

    def create_channel(self, guild_id, name):
        """Create a text channel; returns the channel object."""
        return self.request("POST", f"/guilds/{guild_id}/channels",
                            body={"name": name[:100], "type": 0})

    def delete_channel(self, channel_id):
        """Delete a channel. A channel that is already gone is fine."""
        try:
            return self.request("DELETE", f"/channels/{channel_id}")
        except DiscordError as e:
            if e.status == 404:
                return None
            raise

    def guild_channels(self, guild_id):
        """Every channel in the guild (for stale-channel cleanup)."""
        return self.request("GET", f"/guilds/{guild_id}/channels") or []


def _retry_after(err, raw):
    """Seconds to wait on a 429: the JSON retry_after, else the header,
    else 2; capped so a bad value cannot hang the run."""
    secs = None
    try:
        secs = float(json.loads(raw or b"{}").get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        pass
    if secs is None:
        try:
            secs = float(err.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            secs = 2.0
    return min(max(secs, 0.1), 60.0) + 0.1
