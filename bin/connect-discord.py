#!/usr/bin/env python3
"""connect-discord.py -- connect a new Discord app to this install.

SETUP.md step 3. The Muse writes the token the owner pasted in chat to
a file in ~/.config/muse-discord/incoming/ (made mode 700 by
bin/install.sh) with its file-writing tool, never a shell command, and
names that file:

    python3 bin/connect-discord.py --name "Pip" \
        --token-file ~/.config/muse-discord/incoming/token

The file is read, then deleted. A token typed into a shell command (an
argument, or a heredoc in `bash -c '...'`) shows in the process list, so
never do that. --saved reuses the token already saved (to re-check after
a portal step); with neither, the token is read from stdin.

It then:

- checks the paste is a bot token, and if not, says what was pasted
  instead (the Application ID, the Public Key, the Client Secret, a
  link) and what to copy;
- asks Discord who the bot is (GET /users/@me) and which app it
  belongs to (GET /applications/@me): the application id and the owner
  (the app's owner, or its team's owner) come from there, so nobody
  types an id;
- turns on the Message Content intent (PATCH /applications/@me flags,
  GATEWAY_MESSAGE_CONTENT_LIMITED), then reads the app back to check;
- saves the token to the token file (mode 600, written atomically),
  writes config.yaml with the owner filled in and checks it the way the
  bot will (an existing valid config.yaml is kept, or backed up before
  the owner is added; an invalid one stops everything before anything
  is written), and, while persona.md is still the shipped placeholder,
  writes the default persona there: the bot is the Muse itself, by
  name;
- prints the invite link.

    python3 bin/connect-discord.py --wait [SECONDS]

waits (default 600 s) until the bot is in a server, then says which
server, and how many channels it can read and post in. It uses the
saved token.

Exit status: 0 done; 2 the paste is not a bot token or Discord refused
it (nothing written); 3 Discord could not be reached (nothing written);
4 connected and saved, but the owner has a step to do in the Developer
Portal (printed); 5 --wait timed out; 6 config.yaml is invalid (nothing
written: fix it first) or could not be written. --json prints one JSON
object instead of sentences ("problem" is the kind, "owner_steps" the
portal steps). The token is never printed, logged, or written anywhere but the
token file.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import setup_common as C  # noqa: E402

PKG = C.PKG_DIR
DEFAULT_TOKEN_FILE = "~/.config/muse-discord/bot_token"
# persona.md as shipped (sha256): only that placeholder is replaced by
# the default persona below; a persona the owner wrote is never touched.
SHIPPED_PERSONAS = {
    "5798df97daabe50c99d3fc4e6b122aa135f3abd26ca00cefefce4c502d99a1f8",
}
DEFAULT_PERSONA = """\
# Persona

<!-- The bot's personality. By default the bot is simply your Muse,
     written here by setup (bin/connect-discord.py). To shape it, DM the
     bot in plain words ("keep replies short", "no swearing in the work
     server"), or ask your Muse in its main chat to edit this file. The
     worker re-reads it on every wake. -->

Your name is {name}.

You are your owner's Muse, the same one they talk with in your main
chat. Be yourself here: the same personality, voice and humor. In
their Discord servers you are also talking with other people, so:

- Answer what you're asked, directly; say so briefly when you don't
  know something.
- Keep to Discord's register: short to medium messages, light humor
  when it lands naturally, never forced.
- Stay calm and friendly if someone is rude.
- You are a guest in these communities: be a good community member
  first, a bot second.
"""

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{17,40}\.[A-Za-z0-9_-]{4,10}"
                      r"\.[A-Za-z0-9_-]{25,}")

AGAIN = ("In the Developer Portal, open your app, click **Bot** in the "
         "left menu, then **Reset Token**, then **Copy**, and paste it "
         "here.")
WRONG_PASTE = {
    "empty": "Nothing came through.",
    "id": "That's the Application ID (just a number), not the token.",
    "public_key": "That's the Public Key from the General Information "
                  "page, not the token.",
    "client_secret": "That's the Client Secret from the OAuth2 page, not "
                     "the bot's token.",
    "url": "That's a link, not the token.",
    "user_token": "That looks like a token for a personal Discord account, "
                  "not a bot. Don't share that one with anyone.",
    "unknown": "That doesn't look like a bot token.",
    "rejected": "Discord didn't accept that token. Either it was reset "
                "after you copied it, or it's your own account's token "
                "rather than the bot's (never share that one).",
}

OWNER_STEPS = {
    "intent": ("Turn on Message Content: in the Developer Portal, open "
               "your app and click **Bot** in the left menu. Scroll down "
               "to **Privileged Gateway Intents**, switch on **Message "
               "Content Intent**, then click **Save Changes** in the bar "
               "that appears at the bottom."),
    "code_grant": ("On the **Bot** page, switch off **Requires OAuth2 "
                   "Code Grant**, then click **Save Changes** at the "
                   "bottom. (With it on, the invite link fails.)"),
    "interactions_url": ("On the **General Information** page, empty the "
                         "**Interactions Endpoint URL** box, then click "
                         "**Save Changes** at the bottom. (With it set, "
                         "the bot's buttons and /settings go elsewhere.)"),
}

# ---------------------------------------------------------------------------
# What was pasted
# ---------------------------------------------------------------------------

def clean_paste(raw):
    s = (raw or "").strip().strip("`'\"").strip()
    if s.lower().startswith("bot "):
        s = s[4:].strip()
    return s


def classify(s):
    """"token" for something shaped like a bot token, else the kind of
    wrong paste (a WRONG_PASTE key)."""
    if not s:
        return "empty"
    if re.match(r"(?i)https?://|discord(app)?\.com/|www\.", s):
        return "url"
    if re.fullmatch(r"\d{15,21}", s):
        return "id"
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        return "public_key"
    if re.fullmatch(r"[A-Za-z0-9_-]{32}", s):
        return "client_secret"
    if s.startswith("mfa."):
        return "user_token"
    if TOKEN_RE.fullmatch(s):
        head = s.split(".", 1)[0]
        try:
            uid = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
        except (ValueError, TypeError):
            return "unknown"
        if re.fullmatch(rb"\d{15,21}", uid):
            return "token"
    return "unknown"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, as_json):
        self.as_json = as_json
        self.data = {"ok": True, "lines": [], "owner_steps": []}

    def line(self, text):
        self.data["lines"].append(text)
        if not self.as_json:
            print(text, flush=True)

    def step(self, kind):
        self.data["owner_steps"].append({"kind": kind,
                                         "text": OWNER_STEPS[kind]})
        if not self.as_json:
            print(f"OWNER STEP: {OWNER_STEPS[kind]}", flush=True)

    def fail(self, kind, text, code):
        self.data.update(ok=False, problem=kind)
        self.line(f"PROBLEM: {text}")
        self.finish()
        return code

    def finish(self):
        if self.as_json:
            print(json.dumps(self.data, indent=1))


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def write_private(path, text):
    """Write text to path, mode 600 from the start, atomically."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bot_token.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fresh_config_text(owner_id):
    with open(os.path.join(PKG, "config.example.yaml")) as f:
        text = f.read()
    new, n = re.subn(r"^owners: \[\]", f'owners: ["{owner_id}"]', text,
                     count=1, flags=re.M)
    if n != 1:
        raise RuntimeError("config.example.yaml has no 'owners: []' line")
    return new


def write_config(cfg, owner_id, rep):
    """config.yaml with owner_id among the owners. An existing valid file
    that already lists them is left alone; anything else is backed up
    before it is replaced. Returns the loaded settings."""
    MS = C.settings_module()
    text = None
    if os.path.exists(cfg):
        try:
            st = C.load_settings(cfg)
        except MS.SettingsError:
            st = None
        if st is None:  # checked before anything was written
            raise MS.SettingsError("config.yaml is not valid")
        if owner_id in st.owners:
            rep.line("OK: Kept the existing config.yaml (it already "
                     "lists the app's owner).")
            return MS, st
        bak = MS.backup(cfg)
        import yaml
        with open(cfg) as f:
            raw = yaml.safe_load(f) or {}
        before = [str(o) for o in raw.get("owners") or []]
        raw["owners"] = before + [owner_id]
        text = MS.dump_config(raw)
        rep.line(f"NOTE: config.yaml listed {', '.join(before) or 'no owner'}"
                 f"; added Discord user {owner_id} (this app's owner) as an "
                 f"owner too. Tell the owner, and remove whoever shouldn't "
                 f"be there. The old file is saved as "
                 f"{os.path.basename(bak)}.")
    if text is None:
        text = fresh_config_text(owner_id)
        if not os.path.exists(cfg):
            rep.line(f"OK: Wrote config.yaml (owner: Discord user "
                     f"{owner_id}); everything else is the default.")
    # Check it exactly as the bot will, before it replaces anything.
    MS.load(cfg, C.PLUGINS_DIR, text=text)
    MS.atomic_write_text(cfg, text)
    return MS, C.load_settings(cfg)


def name_persona(st, name, rep):
    """Write the default persona (the Muse itself, by name) while
    persona.md is still the shipped placeholder."""
    path = os.path.join(PKG, os.path.expanduser(st.persona or "persona.md"))
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raw = None
    if raw is not None and hashlib.sha256(raw).hexdigest() \
            not in SHIPPED_PERSONAS:
        rep.line("OK: Left persona.md as it is (it is already yours).")
        return
    safe = " ".join(name.split())[:80]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(DEFAULT_PERSONA.format(name=safe))
    os.replace(tmp, path)
    rep.line(f"OK: persona.md: the bot is {safe}, being itself.")


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------

def app_owner(app):
    """(owner user id, their username or None, owned by a team?)."""
    team = app.get("team")
    if isinstance(team, dict) and team.get("owner_user_id"):
        uid = str(team["owner_user_id"])
        name = next((((m.get("user") or {}).get("username"))
                     for m in team.get("members") or []
                     if str((m.get("user") or {}).get("id")) == uid), None)
        return uid, name, True
    owner = app.get("owner") or {}
    return str(owner.get("id") or ""), owner.get("username"), False


def read_token_file(path):
    """The token from a file the Muse wrote, then delete it. Only a file
    the user owns, in a place only they can read (the file mode 600, or
    its directory mode 700), never a symlink. Returns (token, problem)."""
    path = os.path.expanduser(path)
    try:
        st = os.lstat(path)
        dst = os.stat(os.path.dirname(os.path.abspath(path)))
    except OSError as e:
        return None, f"Can't read {path}: {e.strerror}."
    uid = os.getuid()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
        return None, f"{path} isn't a plain file of this account."
    private_file = not st.st_mode & 0o077
    private_dir = dst.st_uid == uid and not dst.st_mode & 0o077
    if not (private_file or private_dir):
        return None, (f"{path} is readable by other accounts; write the "
                      f"token into ~/.config/muse-discord/incoming/ "
                      f"(bash bin/install.sh makes it) instead.")
    with open(path) as f:
        token = f.read()
    os.unlink(path)
    return token, None


def get_token(args, rep):
    """The pasted token, from --token-file, --saved or stdin. Returns
    (token, exit code when it failed)."""
    if args.saved:
        try:
            token = C.read_token(C.load_settings(args.config))
        except C.settings_module().SettingsError:
            token = None
        if not token:
            return None, rep.fail("no_token", "No saved token yet.", 2)
        return token, None
    if args.token_file:
        saved = None
        try:
            saved = C.token_file(C.load_settings(args.config))
        except Exception:
            pass
        if saved and os.path.realpath(os.path.expanduser(args.token_file)) \
                == os.path.realpath(saved):
            return None, rep.fail("usage", "That is the saved token file; "
                                  "use --saved to reuse it.", 2)
        token, problem = read_token_file(args.token_file)
        if problem:
            return None, rep.fail("token_file", problem, 2)
        return token, None
    return sys.stdin.read(), None


def config_problem(cfg):
    """Why an existing config.yaml can't be used as is, or None."""
    if not os.path.exists(cfg):
        return None
    MS = C.settings_module()
    try:
        C.load_settings(cfg)
    except MS.SettingsError as e:
        return str(e)
    return None


def connect(args, rep):
    token, code = get_token(args, rep)
    if code is not None:
        return code
    token = clean_paste(token)
    kind = classify(token)
    if kind != "token":
        return rep.fail(kind, f"{WRONG_PASTE[kind]} {AGAIN}", 2)
    bad = config_problem(args.config)
    if bad:
        return rep.fail("config", f"config.yaml has a problem, so nothing "
                        f"was changed. Fix config.yaml first (python3 "
                        f"bin/settings.py validate names each problem), "
                        f"then run this again. {bad}", 6)
    try:
        me = C.call("GET", "/users/@me", token)
        if not (me or {}).get("bot"):
            return rep.fail("user_token",
                            f"{WRONG_PASTE['user_token']} {AGAIN}", 2)
        app = C.call("GET", "/applications/@me", token) or {}
    except C.ApiError as e:
        if e.status == 401:
            return rep.fail("rejected", f"{WRONG_PASTE['rejected']} "
                            f"{AGAIN}", 2)
        return rep.fail("unreachable", f"Could not reach Discord ({e}). "
                        f"Try again in a minute.", 3)
    app_id = str(app.get("id") or "")
    owner_id, owner_name, team = app_owner(app)
    if not app_id or not owner_id:
        return rep.fail("unreachable", "Discord did not say which app this "
                        "is. Try again in a minute.", 3)
    bot_name = me.get("username") or "the bot"
    name = args.name or bot_name
    rep.data.update(app_id=app_id, owner_id=owner_id, bot_name=bot_name)
    rep.line(f"OK: This is the bot {bot_name} (app {app_id}); its owner "
             f"is Discord user {owner_id}"
             + (f" (@{owner_name})." if owner_name else "."))
    if team:
        rep.data["team"] = True
        rep.line("NOTE: This app belongs to a Discord team, so the owner "
                 "set here is the team's owner, "
                 + (f"@{owner_name}" if owner_name else f"user {owner_id}")
                 + ". Ask the owner whether that is their Discord account; "
                 "if not, change owners in config.yaml.")
    if args.name and args.name.casefold() != bot_name.casefold():
        rep.line(f"NOTE: The app is named {bot_name!r}, not {args.name!r}. "
                 f"The bot still calls itself {args.name}; to change the "
                 f"name people see, rename it on the Bot page.")

    # Message Content: GATEWAY_MESSAGE_CONTENT_LIMITED (an unverified
    # app's form of the intent) is one of the few flags the API lets an
    # app set itself. Either form counts as on.
    flags = int(app.get("flags") or 0)
    intent_ok = bool(flags & (C.MESSAGE_CONTENT | C.MESSAGE_CONTENT_LIMITED))
    if not intent_ok:
        try:
            try:
                C.call("PATCH", "/applications/@me", token,
                       {"flags": flags | C.MESSAGE_CONTENT_LIMITED})
            except C.ApiError as e:
                if e.status != 400:
                    raise
                # Some flags in the app's value may not be ours to send
                # back: send only the editable ones.
                C.call("PATCH", "/applications/@me", token,
                       {"flags": (flags & C.EDITABLE_FLAGS)
                        | C.MESSAGE_CONTENT_LIMITED})
            again = C.call("GET", "/applications/@me", token) or {}
            intent_ok = bool(int(again.get("flags") or 0)
                             & (C.MESSAGE_CONTENT
                                | C.MESSAGE_CONTENT_LIMITED))
        except C.ApiError as e:
            rep.data["intent_error"] = str(e)
    rep.data["message_content"] = intent_ok
    if intent_ok:
        rep.line("OK: Message Content is on.")
    else:
        rep.step("intent")
    if app.get("bot_require_code_grant"):
        rep.step("code_grant")
    if app.get("interactions_endpoint_url"):
        rep.step("interactions_url")

    # Save: token, config, persona.
    cfg = args.config
    MS = C.settings_module()
    path = None
    if os.path.exists(cfg):
        try:
            path = C.token_file(C.load_settings(cfg))
        except MS.SettingsError:
            path = None
    path = path or os.path.expanduser(DEFAULT_TOKEN_FILE)
    write_private(path, token + "\n")
    rep.line(f"OK: Saved the token in {path} (only this account can read "
             f"it).")
    try:
        MS, st = write_config(cfg, owner_id, rep)
    except (MS.SettingsError, RuntimeError, OSError) as e:
        return rep.fail("config", f"Could not write config.yaml (the token "
                        f"is saved): {e}", 6)
    if C.token_file(st) != path:
        rep.line(f"NOTE: config.yaml reads the token from "
                 f"{st.token_sources()}, not {path}.")
    name_persona(st, name, rep)

    url = C.invite_url(app_id)
    rep.data["invite_url"] = url
    rep.line(f"INVITE: {url}")
    code = 4 if rep.data["owner_steps"] else 0
    rep.finish()
    return code


# ---------------------------------------------------------------------------
# Wait for the invite
# ---------------------------------------------------------------------------

def channel_perms(base, everyone_id, member_roles, bot_id, overwrites):
    """Discord's permission overwrite rules, for one channel."""
    if base & C.ADMINISTRATOR:
        return -1
    p = base
    ow = {o.get("id"): o for o in overwrites or []}
    if everyone_id in ow:
        p &= ~int(ow[everyone_id].get("deny") or 0)
        p |= int(ow[everyone_id].get("allow") or 0)
    deny = allow = 0
    for rid in member_roles:
        if rid in ow:
            deny |= int(ow[rid].get("deny") or 0)
            allow |= int(ow[rid].get("allow") or 0)
    p = (p & ~deny) | allow
    if bot_id in ow:
        p &= ~int(ow[bot_id].get("deny") or 0)
        p |= int(ow[bot_id].get("allow") or 0)
    return p


def describe_guild(token, bot_id, g):
    gid = g["id"]
    roles = {r["id"]: int(r.get("permissions") or 0)
             for r in C.call("GET", f"/guilds/{gid}/roles", token) or []}
    member = C.call("GET", f"/guilds/{gid}/members/{bot_id}", token) or {}
    mine = list(member.get("roles") or [])
    base = roles.get(gid, 0)
    for rid in mine:
        base |= roles.get(rid, 0)
    read, post, read_only = 0, 0, []
    for ch in C.call("GET", f"/guilds/{gid}/channels", token) or []:
        if ch.get("type") not in (0, 5, 15):  # text, announcement, forum
            continue
        p = channel_perms(base, gid, mine, bot_id,
                          ch.get("permission_overwrites"))
        if p != -1 and not p & C.VIEW_CHANNEL:
            continue
        read += 1
        if p == -1 or p & C.SEND_MESSAGES:
            post += 1
        else:
            read_only.append("#" + str(ch.get("name")))
    return {"id": gid, "name": g.get("name"), "can_read": read,
            "can_post": post, "read_only": read_only}


def wait(args, rep):
    MS = C.settings_module()
    try:
        token = C.read_token(C.load_settings(args.config))
    except MS.SettingsError as e:
        return rep.fail("config", f"config.yaml: {e}", 6)
    if not token:
        return rep.fail("no_token", "No saved token yet: run this script "
                        "with the token first.", 2)
    deadline = time.time() + args.wait
    try:
        bot_id = C.call("GET", "/users/@me", token)["id"]
        while True:
            guilds = C.call("GET", "/users/@me/guilds", token) or []
            if guilds or time.time() >= deadline:
                break
            time.sleep(args.poll)
        if not guilds:
            return rep.fail("timeout", "The bot hasn't joined a server yet. "
                            "Ask the owner to open the invite link, pick "
                            "their server and click Authorize.", 5)
        found = [describe_guild(token, bot_id, g) for g in guilds[:10]]
    except C.ApiError as e:
        code = 2 if e.status == 401 else 3
        return rep.fail("rejected" if code == 2 else "unreachable",
                        f"Discord: {e}", code)
    rep.data["servers"] = found
    for g in found:
        if not g["can_read"]:
            rep.line(f"PROBLEM: The bot is in {g['name']} but can't see "
                     f"any channels there. In Discord: Server Settings, "
                     f"Roles, the bot's role: switch on View Channels and "
                     f"Send Messages.")
            continue
        text = (f"OK: The bot is in {g['name']}. It can read "
                f"{g['can_read']} channels and post in {g['can_post']} of "
                f"them.")
        if g["read_only"]:
            text += (" It can read but not post in "
                     + ", ".join(g["read_only"][:10]) + ".")
        rep.line(text)
    rep.finish()
    return 0 if any(g["can_post"] for g in found) else 4


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Connect a Discord app to muse-discord (token on "
                    "stdin), or --wait for the bot to join a server.")
    ap.add_argument("--name", help="the bot's name (your Muse's name)")
    ap.add_argument("--token-file", metavar="PATH",
                    help="read the pasted token from this file (in "
                         "~/.config/muse-discord/incoming/), then delete it")
    ap.add_argument("--saved", action="store_true",
                    help="use the token already saved (re-check after a "
                         "portal step)")
    ap.add_argument("--wait", nargs="?", type=float, const=600.0,
                    metavar="SECONDS",
                    help="wait for the bot to be in a server (default "
                         "600 s), then report what it can see")
    ap.add_argument("--poll", type=float, default=5.0,
                    help=argparse.SUPPRESS)
    ap.add_argument("--config", default=os.environ.get(
        "MUSE_DISCORD_CONFIG") or os.path.join(PKG, "config.yaml"))
    ap.add_argument("--json", action="store_true",
                    help="one JSON object instead of sentences")
    args = ap.parse_args(argv)
    rep = Report(args.json)
    if args.wait is not None:
        return wait(args, rep)
    if not (args.token_file or args.saved) and sys.stdin.isatty():
        print("Use --token-file PATH (see SETUP.md step 3).",
              file=sys.stderr)
        return 2
    return connect(args, rep)


if __name__ == "__main__":
    sys.exit(main())
