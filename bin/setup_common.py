"""setup_common.py -- shared by bin/connect-discord.py and bin/doctor.py.

The few Discord facts setup depends on, one small REST helper (stdlib
urllib, no third-party packages), and the token lookup. Not a command.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The package's .venv counts only once bin/install.sh has checked the
# packages import in it (it then writes this marker).
VENV_PYTHON = os.path.join(PKG_DIR, ".venv", "bin", "python3")
VENV_OK = os.path.join(PKG_DIR, ".venv", ".muse-ok")


def venv_python():
    """.venv/bin/python3 when bin/install.sh set it up, else None."""
    if os.access(VENV_PYTHON, os.X_OK) and os.path.exists(VENV_OK):
        return VENV_PYTHON
    return None


# Run under that venv when this Python lacks the packages, so
# `python3 bin/<script>` always works.
try:
    import yaml  # noqa: F401
except ImportError:
    _venv = venv_python()
    # Not running in .venv yet (its python3 may be a symlink to this
    # very binary, so compare prefixes): once there, never again.
    if _venv and os.path.realpath(sys.prefix) \
            != os.path.realpath(os.path.join(PKG_DIR, ".venv")):
        os.execv(_venv, [_venv] + sys.argv)

API = "https://discord.com/api/v10"
# Tests point the scripts at a local fake with this variable. Only a
# loopback http URL is accepted, so it can never send the token to
# another host.
API_ENV = "MUSE_DISCORD_API_BASE"
TIMEOUT = 20

# What the bot asks for in a server: View Channel, Send Messages, Send
# Messages in Threads, Read Message History, Attach Files, Embed Links,
# Add Reactions, Pin Messages. No administrator, no moderation.
PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 38) | (1 << 16) | (1 << 15) \
    | (1 << 14) | (1 << 6) | (1 << 51)
assert PERMISSIONS == 2252074691710016
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
ADMINISTRATOR = 1 << 3

# Application flags (docs: Application, "Application Flags"). Only the
# *_LIMITED intent flags can be changed through the API.
MESSAGE_CONTENT = 1 << 18
MESSAGE_CONTENT_LIMITED = 1 << 19
EDITABLE_FLAGS = (1 << 13) | (1 << 15) | MESSAGE_CONTENT_LIMITED

COMMUNITY_LINE = ("Want to meet other Muse owners and their Muses? Join "
                  "the Muse Agent server: https://discord.gg/2wwcHbq47N")


class ApiError(Exception):
    """A Discord REST error: status is the HTTP status (0 when Discord
    could not be reached), code Discord's JSON error code, if any."""

    def __init__(self, status, code=None, message=""):
        super().__init__(f"HTTP {status}: {message}" if status
                         else f"cannot reach Discord: {message}")
        self.status = status
        self.code = code
        self.message = message


def api_base():
    base = os.environ.get(API_ENV)
    if not base:
        return API
    u = urllib.parse.urlparse(base)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost",
                                                "::1"):
        sys.exit(f"{API_ENV} must be a loopback http URL; ignoring it is "
                 f"not safe, so stopping.")
    return base.rstrip("/")


def call(method, path, token, body=None):
    """One REST call as the bot. Returns the parsed JSON (None for an
    empty body). Raises ApiError. Never puts the token in an error."""
    data = None
    headers = {"Authorization": f"Bot {token}",
               "User-Agent": "DiscordBot (https://github.com/cszach/"
                             "muse-discord, setup)"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(api_base() + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}")
        except ValueError:
            err = {}
        raise ApiError(e.code, err.get("code"),
                       str(err.get("message") or e.reason)) from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(0, None, str(getattr(e, "reason", e))) from None
    return json.loads(raw) if raw else None


def invite_url(app_id):
    """The link that adds the bot to a server (integration_type=0: a
    server install, never a user install)."""
    q = urllib.parse.urlencode({"client_id": app_id, "scope": "bot",
                                "permissions": str(PERMISSIONS),
                                "integration_type": "0"})
    return f"https://discord.com/oauth2/authorize?{q}"


PLUGINS_DIR = os.path.join(PKG_DIR, "plugins")


def settings_module():
    if PKG_DIR not in sys.path:
        sys.path.insert(0, PKG_DIR)
    import muse_settings
    return muse_settings


def load_settings(config_path):
    """config.yaml through muse_settings, as the bot loads it. Raises
    muse_settings.SettingsError."""
    return settings_module().load(config_path, PLUGINS_DIR)


def token_path(where):
    """A token_from file path as the bot resolves it: ~ expanded, and a
    relative path taken from the package dir (the bot's working dir)."""
    return os.path.join(PKG_DIR, os.path.expanduser(where))


def read_token(st):
    """The bot token from config.yaml's token_from (an env var or a
    file), or None. Never printed."""
    for kind, where in st.token_sources():
        if kind == "env":
            v = os.environ.get(where, "").strip()
        else:
            try:
                with open(token_path(where)) as f:
                    v = f.read().strip()
            except OSError:
                v = ""
        if v:
            return v
    return None


def token_file(st):
    files = [p for k, p in st.token_sources() if k == "file"]
    return token_path(files[0]) if files else None
