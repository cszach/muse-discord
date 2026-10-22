#!/usr/bin/env python3
"""
e2e scenario loader, validator, and placeholder rendering.

A scenario is one YAML file in e2e/scenarios/ describing a short
conversation two driver bots (a, b) play against the bot under test
(the SUT), and what the resulting channel transcript must show. The
schema lives in the e2e spec and e2e/README.md; this module enforces it
so a typo in a scenario fails loudly at load time (and in --dry-run and
the offline unit tests) instead of half-way through a live run.

API:
    load_all(dir, tier=None) -> [scenario dict, ...]   (validated)
    validate(scenario, source=None, fixtures_dir=None)  (raises)
    render(text, ids) -> str
    fixture_path(name) -> absolute path of a file in e2e/fixtures/

Every problem raises ScenarioError with the file and the field at
fault, e.g. "mention_burst.yaml: steps[2].send.reply_to: unknown label
'q9' (defined so far: q1)".

Threads and labels. A `thread` step starts a thread on a channel
message and names it with a label; `send.in: <thread label>` posts in
that thread. Message labels and thread labels share one namespace (a
label is defined once, whatever it names). A reply stays where it is
sent: `send.reply_to` must name a message in the same channel or
thread as the new message (last_sut: the SUT's newest message there).
Checks name message labels only, wherever the message was posted.

The `settings` step's key, scope and value are checked against the
muse_settings.py next to e2e/ (the code under test) when it is there:
an unknown key, a scope that cannot set it, or an invalid value fails
at load time instead of mid-run.
"""
import importlib.util
import os
import re

import yaml

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIOS_DIR = os.path.join(E2E_DIR, "scenarios")
FIXTURES_DIR = os.path.join(E2E_DIR, "fixtures")

ID_RE = re.compile(r"^[a-z0-9_]+$")
LABEL_RE = re.compile(r"^[a-z0-9_]+$")
DRIVERS = ("a", "b")
STEPS = ("send", "wait", "wait_for", "action", "converse", "delete",
         "thread", "settings")
ACTIONS = ("kill_monitor", "start_monitor", "gateway_off")
LEDGER_STATUSES = ("queued", "replied", "quarantined", "skipped")
# send.reply_to may name the SUT's newest message instead of a label.
LAST_SUT = "last_sut"
# settings step scopes: this scenario's channel, the test server, and
# the two global scopes.
SETTINGS_SCOPES = ("channel", "server", "everywhere", "dms")
SETTINGS_OPS = ("set", "reset", "undo")
_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)+$")
THREAD_NAME_MAX = 100

TOP_KEYS = {"id", "tier", "title", "b_is_bot", "config", "steps",
            "expect", "judge"}
# Placeholders a scenario's `config:` overrides may use (sut.py renders
# them, in keys and values).
CONFIG_PLACEHOLDERS = ("GUILD_ID", "A_ID", "B_ID", "CHANNEL_ID")
_CONFIG_PH_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
# queue_item: the item fields the check can compare.
QUEUE_ITEM_FIELDS = ("follow_up", "kind", "from_bot", "owner_command")
# Placeholders render() understands. Anything else shaped like one is a
# typo that would go out to Discord verbatim.
MENTION_PLACEHOLDERS = ("SUT", "A", "B")
NAME_PLACEHOLDERS = ("SUT_NAME",)
_MENTION_PH_RE = re.compile(r"<@!?([A-Za-z_]+)>")
_NAME_PH_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


class ScenarioError(Exception):
    """A scenario file is unreadable or breaks the schema."""


def render(text, ids):
    """Fill placeholders: <@SUT>, <@A>, <@B> -> <@id>; {SUT_NAME} -> the
    SUT bot's username. ids = {"SUT": id, "A": id, "B": id,
    "SUT_NAME": name}."""
    for key in MENTION_PLACEHOLDERS:
        text = text.replace(f"<@{key}>", f"<@{ids[key]}>")
    return text.replace("{SUT_NAME}", str(ids["SUT_NAME"]))


def fixture_path(name):
    """Absolute path of a fixture file referenced by send.attach."""
    return os.path.join(FIXTURES_DIR, name)


# ---------------------------------------------------------------------------
# Validation helpers. Each raises ScenarioError(f"{where}: {reason}"),
# where `where` is "<file>: <field path>".
# ---------------------------------------------------------------------------

def _fail(where, reason):
    raise ScenarioError(f"{where}: {reason}")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _need_map(v, where):
    if not isinstance(v, dict):
        _fail(where, f"expected a mapping, got {type(v).__name__}")
    return v


def _only_keys(d, allowed, where, required=()):
    extra = sorted(set(d) - set(allowed))
    if extra:
        _fail(where, f"unknown field(s) {', '.join(map(str, extra))} "
                     f"(allowed: {', '.join(sorted(allowed))})")
    missing = [k for k in required if k not in d]
    if missing:
        _fail(where, f"missing required field(s) {', '.join(missing)}")


def _positive(v, where, integer=False, allow_zero=False):
    ok = _is_int(v) if integer else _is_num(v)
    if not ok or v < 0 or (v == 0 and not allow_zero):
        kind = "integer" if integer else "number"
        bound = ">= 0" if allow_zero else "> 0"
        _fail(where, f"expected a {kind} {bound}, got {v!r}")


def _check_text(text, where):
    if not isinstance(text, str):
        _fail(where, f"expected a string, got {type(text).__name__}")
    for m in _MENTION_PH_RE.finditer(text):
        if m.group(1) not in MENTION_PLACEHOLDERS:
            _fail(where, f"unknown mention placeholder {m.group(0)!r} "
                         f"(use <@SUT>, <@A> or <@B>)")
    for m in _NAME_PH_RE.finditer(text):
        if m.group(1) not in NAME_PLACEHOLDERS:
            _fail(where, f"unknown placeholder {m.group(0)!r} "
                         f"(only {{SUT_NAME}})")


def _label_ref(v, labels, where, allow_last_sut=False):
    if allow_last_sut and v == LAST_SUT:
        return
    if not isinstance(v, str) or v not in labels:
        known = ", ".join(labels) or "none"
        extra = f" or '{LAST_SUT}'" if allow_last_sut else ""
        _fail(where, f"unknown label {v!r}{extra} (defined so far: "
                     f"{known})")


def _count_spec(v, where):
    """{eq: n} | {min: n} | {max: n} | {min: n, max: m}."""
    _need_map(v, where)
    _only_keys(v, ("eq", "min", "max"), where)
    if not v:
        _fail(where, "needs eq, min or max")
    if "eq" in v and len(v) > 1:
        _fail(where, "eq cannot be combined with min/max")
    for k, n in v.items():
        _positive(n, f"{where}.{k}", integer=True, allow_zero=True)
    if "min" in v and "max" in v and v["min"] > v["max"]:
        _fail(where, f"min {v['min']} > max {v['max']}")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _new_label(label, where, labels, threads):
    """A fresh label: well formed, not reserved, not yet defined as a
    message or a thread label."""
    if not isinstance(label, str) or not LABEL_RE.match(label):
        _fail(where, f"expected [a-z0-9_]+, got {label!r}")
    if label == LAST_SUT:
        _fail(where, f"'{LAST_SUT}' is reserved")
    if label in labels or label in threads:
        what = "thread" if label in threads else "message"
        _fail(where, f"label {label!r} defined twice (already a {what} "
                     f"label)")


def _place(thread):
    return f"thread {thread!r}" if thread else "the scenario channel"


def _validate_send(s, where, labels, fixtures_dir, threads=(),
                   located=None):
    """located: message label -> the thread label it was posted in
    (None: the channel); this send's label is added to it."""
    located = {} if located is None else located
    _need_map(s, where)
    _only_keys(s, ("as", "text", "label", "reply_to", "attach", "in",
                   "typing"), where, required=("as",))
    if "typing" in s and not isinstance(s["typing"], bool):
        _fail(f"{where}.typing", "expected true or false")
    if s["as"] not in DRIVERS:
        _fail(f"{where}.as", f"expected one of {', '.join(DRIVERS)}, got "
                             f"{s['as']!r}")
    if "text" in s:
        _check_text(s["text"], f"{where}.text")
    if not (s.get("text") or "").strip() and not s.get("attach"):
        _fail(where, "needs non-empty text or an attach")
    here = None
    if "in" in s:
        here = s["in"]
        if not isinstance(here, str) or here not in threads:
            known = ", ".join(threads) or "none"
            _fail(f"{where}.in", f"unknown thread label {here!r} (threads "
                                 f"so far: {known}; a thread step defines "
                                 f"one)")
    if "reply_to" in s:
        _label_ref(s["reply_to"], labels, f"{where}.reply_to",
                   allow_last_sut=True)
        ref = s["reply_to"]
        if ref != LAST_SUT and located.get(ref) != here:
            _fail(f"{where}.reply_to", f"{ref!r} is in "
                  f"{_place(located.get(ref))} but this message goes to "
                  f"{_place(here)}: a reply must stay in the same channel "
                  f"or thread")
    if "attach" in s:
        name = s["attach"]
        if not isinstance(name, str) or not name or os.sep in name \
                or name.startswith("."):
            _fail(f"{where}.attach", f"expected a bare file name in "
                                     f"e2e/fixtures/, got {name!r}")
        if not os.path.isfile(os.path.join(fixtures_dir, name)):
            _fail(f"{where}.attach", f"fixture {name!r} not found in "
                                     f"{fixtures_dir}")
    if "label" in s:
        label = s["label"]
        _new_label(label, f"{where}.label", labels, threads)
        labels.append(label)
        located[label] = here


def _validate_wait_for(s, where):
    _need_map(s, where)
    _only_keys(s, ("sut_messages", "outbox_processing", "timeout"), where)
    conds = [k for k in ("sut_messages", "outbox_processing") if k in s]
    if len(conds) != 1:
        _fail(where, "needs exactly one of sut_messages, outbox_processing")
    if "sut_messages" in s:
        _positive(s["sut_messages"], f"{where}.sut_messages", integer=True)
    if "outbox_processing" in s and s["outbox_processing"] is not True:
        _fail(f"{where}.outbox_processing", "only `true` is supported")
    if "timeout" in s:
        _positive(s["timeout"], f"{where}.timeout")


def _validate_converse(s, where):
    _need_map(s, where)
    _only_keys(s, ("as", "lines", "per_turn_timeout"), where,
               required=("as", "lines"))
    if s["as"] not in DRIVERS:
        _fail(f"{where}.as", f"expected one of {', '.join(DRIVERS)}, got "
                             f"{s['as']!r}")
    lines = s["lines"]
    if not isinstance(lines, list) or not lines:
        _fail(f"{where}.lines", "expected a non-empty list of strings")
    for i, line in enumerate(lines):
        _check_text(line, f"{where}.lines[{i}]")
        if not line.strip():
            _fail(f"{where}.lines[{i}]", "empty line")
    if "per_turn_timeout" in s:
        _positive(s["per_turn_timeout"], f"{where}.per_turn_timeout")


def _validate_delete(label, where, labels, deleted):
    """delete: <label>: the driver that sent that message deletes it.
    Checks may still name the label (its id stays in the transcript's
    labels; the message itself is gone from the history)."""
    _label_ref(label, labels, where)
    if label in deleted:
        _fail(where, f"label {label!r} is already deleted")
    deleted.append(label)


def _validate_thread(s, where, labels, deleted, threads, located,
                     threaded):
    """thread: {from, name, label, as?}: a driver (default a) starts a
    public thread on the channel message labelled `from`."""
    _need_map(s, where)
    _only_keys(s, ("from", "name", "label", "as"), where,
               required=("from", "name", "label"))
    if "as" in s and s["as"] not in DRIVERS:
        _fail(f"{where}.as", f"expected one of {', '.join(DRIVERS)}, got "
                             f"{s['as']!r}")
    src = s["from"]
    _label_ref(src, labels, f"{where}.from")
    if located.get(src):
        _fail(f"{where}.from", f"{src!r} is in thread {located[src]!r}; a "
                               f"thread starts on a channel message")
    if src in deleted:
        _fail(f"{where}.from", f"{src!r} is deleted by then")
    if src in threaded:
        _fail(f"{where}.from", f"{src!r} already has a thread "
                               f"({threaded[src]!r})")
    name = s["name"]
    if not isinstance(name, str) or not name.strip():
        _fail(f"{where}.name", "expected a non-empty string")
    if len(name) > THREAD_NAME_MAX:
        _fail(f"{where}.name", f"at most {THREAD_NAME_MAX} characters "
                               f"(Discord's limit), got {len(name)}")
    _new_label(s["label"], f"{where}.label", labels, threads)
    threads.append(s["label"])
    threaded[src] = s["label"]


_MS = []  # [muse_settings module or None], loaded once


def _settings_module():
    """The muse_settings.py next to e2e/ (the code under test), or None
    when it is missing or does not import."""
    if not _MS:
        path = os.path.join(os.path.dirname(E2E_DIR), "muse_settings.py")
        mod = None
        if os.path.isfile(path):
            try:
                spec = importlib.util.spec_from_file_location(
                    "_e2e_muse_settings", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception:
                mod = None
        _MS.append(mod)
    return _MS[0]


def _check_setting(key, scope, where, value=None, has_value=False):
    """key is a settings key /settings can change at scope (and value,
    when given, a valid value for it)."""
    if not isinstance(key, str) or not _SETTING_KEY_RE.match(key):
        _fail(where, f"expected a settings key like who.replies, got "
                     f"{key!r}")
    if key.startswith("plugins."):
        if len(key.split(".")) != 3:
            _fail(where, f"expected plugins.<plugin>.<setting>, got "
                         f"{key!r}")
        if scope == "channel" and not key.endswith(".enabled"):
            _fail(where, "a channel may set only who.replies, style.notes "
                         "and plugins.<plugin>.enabled")
        return  # plugin keys need the manifests: set_value checks them
    ms = _settings_module()
    if ms is None:
        return
    spec = ms.CORE.get(key)
    if spec is None:
        _fail(where, f"unknown setting {key!r} (known: "
                     f"{', '.join(sorted(ms.CORE))}, plugins.<plugin>."
                     f"<setting>)")
    if scope not in spec.scopes:
        ok = [x for x in spec.scopes if x in SETTINGS_SCOPES]
        _fail(where, f"{key} cannot be set at {scope} scope (scopes that "
                     f"can: {', '.join(ok) or 'none'})")
    if not spec.discord_editable or spec.hidden:
        _fail(where, f"{key} is not editable from Discord (/settings "
                     f"cannot change it)")
    if has_value:
        ok, norm = ms.normalize(spec, value)
        if not ok:
            _fail(f"{where.rsplit('.', 1)[0]}.value",
                  f"{norm}; valid: {spec.valid_text()}")


def _validate_settings(s, where):
    """settings: {set: key, value: v, scope: s} | {reset: key, scope: s}
    | {undo: scope}, each with an optional timeout: a live change
    written the way /settings writes it."""
    _need_map(s, where)
    _only_keys(s, SETTINGS_OPS + ("value", "scope", "timeout"), where)
    ops = [k for k in SETTINGS_OPS if k in s]
    if len(ops) != 1:
        _fail(where, "needs exactly one of set, reset, undo")
    op = ops[0]

    def scope_ok(v, w):
        if v not in SETTINGS_SCOPES:
            _fail(w, f"expected one of {', '.join(SETTINGS_SCOPES)}, got "
                     f"{v!r}")

    if op == "undo":
        if "scope" in s or "value" in s:
            _fail(where, "undo takes only its scope (undo: channel) and an "
                         "optional timeout")
        scope_ok(s["undo"], f"{where}.undo")
    else:
        if "scope" not in s:
            _fail(where, f"missing required field(s) scope (one of "
                         f"{', '.join(SETTINGS_SCOPES)})")
        scope_ok(s["scope"], f"{where}.scope")
        if op == "set":
            if "value" not in s:
                _fail(where, "set needs a value")
            if s["value"] == "inherit":
                _fail(f"{where}.value", "use `reset: <key>` to go back to "
                                        "the inherited value")
        elif "value" in s:
            _fail(where, "reset takes no value")
        _check_setting(s[op], s["scope"], f"{where}.{op}",
                       value=s.get("value"), has_value=op == "set")
    if "timeout" in s:
        _positive(s["timeout"], f"{where}.timeout")


def _validate_steps(steps, where, fixtures_dir):
    """Returns (message labels in definition order, step kinds seen)."""
    if not isinstance(steps, list) or not steps:
        _fail(where, "expected a non-empty list of steps")
    labels, kinds, deleted = [], set(), []
    threads, located, threaded = [], {}, {}
    for i, step in enumerate(steps):
        w = f"{where}[{i}]"
        if not isinstance(step, dict) or len(step) != 1:
            _fail(w, f"each step is a mapping with exactly one key "
                     f"({', '.join(STEPS)})")
        (kind, body), = step.items()
        kinds.add(kind)
        w = f"{w}.{kind}"
        if kind == "send":
            _validate_send(body, w, labels, fixtures_dir, threads, located)
        elif kind == "wait":
            _positive(body, w)
        elif kind == "wait_for":
            _validate_wait_for(body, w)
        elif kind == "action":
            if body not in ACTIONS:
                _fail(w, f"unknown action {body!r} (one of "
                         f"{', '.join(ACTIONS)})")
        elif kind == "converse":
            _validate_converse(body, w)
        elif kind == "delete":
            _validate_delete(body, w, labels, deleted)
        elif kind == "thread":
            _validate_thread(body, w, labels, deleted, threads, located,
                             threaded)
        elif kind == "settings":
            _validate_settings(body, w)
        else:
            _fail(f"{where}[{i}]", f"unknown step {kind!r} "
                                   f"({', '.join(STEPS)})")
    return labels, kinds


# ---------------------------------------------------------------------------
# Checks. One validator per check name; checks.CHECKS must list the same
# names (test_checks asserts the two agree).
# ---------------------------------------------------------------------------

def _arg_label(v, w, labels, kinds):
    _label_ref(v, labels, w)


def _arg_true(v, w, labels, kinds):
    if v is not True:
        _fail(w, "expected `true`")


def _arg_count(v, w, labels, kinds):
    _count_spec(v, w)


def _arg_label_list(v, w, labels, kinds):
    if not isinstance(v, list) or not v:
        _fail(w, "expected a non-empty list of labels")
    for i, x in enumerate(v):
        _label_ref(x, labels, f"{w}[{i}]")


def _arg_latency(v, w, labels, kinds):
    _need_map(v, w)
    _only_keys(v, ("from", "max"), w, required=("from", "max"))
    _label_ref(v["from"], labels, f"{w}.from")
    _positive(v["max"], f"{w}.max")


def _arg_max_chars(v, w, labels, kinds):
    _positive(v, w, integer=True)


def _arg_mentions(v, w, labels, kinds):
    _need_map(v, w)
    _only_keys(v, ("label", "user"), w, required=("label", "user"))
    _label_ref(v["label"], labels, f"{w}.label")
    if v["user"] not in DRIVERS:
        _fail(f"{w}.user", f"expected one of {', '.join(DRIVERS)}, got "
                           f"{v['user']!r}")


def _arg_ledger(v, w, labels, kinds):
    _need_map(v, w)
    _only_keys(v, ("label", "status"), w, required=("label", "status"))
    _label_ref(v["label"], labels, f"{w}.label")
    if v["status"] not in LEDGER_STATUSES:
        _fail(f"{w}.status", f"expected one of {', '.join(LEDGER_STATUSES)}"
                             f", got {v['status']!r}")


def _arg_converse(v, w, labels, kinds):
    if "converse" not in kinds:
        _fail(w, "converse_turns needs a converse step")
    _count_spec(v, w)


def _arg_queue_item(v, w, labels, kinds):
    """{label, follow_up?, kind?, from_bot?, owner_command?, settings?}:
    the queue item the fake worker saw for the label's message."""
    _need_map(v, w)
    _only_keys(v, ("label", "settings") + QUEUE_ITEM_FIELDS, w,
               required=("label",))
    _label_ref(v["label"], labels, f"{w}.label")
    if len(v) == 1:
        _fail(w, "needs at least one field to compare (" + ", ".join(
            QUEUE_ITEM_FIELDS + ("settings",)) + ")")
    for k in ("follow_up", "from_bot", "owner_command"):
        if k in v and not isinstance(v[k], bool):
            _fail(f"{w}.{k}", f"expected true/false, got {v[k]!r}")
    if "kind" in v and (not isinstance(v["kind"], str) or not v["kind"]):
        _fail(f"{w}.kind", f"expected a string, got {v['kind']!r}")
    if "settings" in v:
        _need_map(v["settings"], f"{w}.settings")
        if not v["settings"]:
            _fail(f"{w}.settings", "expected at least one card field")


def _arg_regex(v, w, labels, kinds):
    if not isinstance(v, str) or not v:
        _fail(w, "expected a non-empty regular expression string")
    try:
        re.compile(v)
    except re.error as e:
        _fail(w, f"invalid regular expression: {e}")


def _arg_content(v, w, labels, kinds):
    """A regex for every SUT message, or {label, pattern}: the SUT
    messages that reply to that label (at least one)."""
    if isinstance(v, dict):
        _only_keys(v, ("label", "pattern"), w, required=("label", "pattern"))
        _label_ref(v["label"], labels, f"{w}.label")
        _arg_regex(v["pattern"], f"{w}.pattern", labels, kinds)
    else:
        _arg_regex(v, w, labels, kinds)


def _arg_key_suffix(v, w, labels, kinds):
    """A settings key (or the end of one), e.g. who.replies."""
    if not isinstance(v, str) or not re.fullmatch(r"[a-z_]+(\.[a-z_]+)*",
                                                  v):
        _fail(w, f"expected a settings key such as who.replies, got {v!r}")


CHECK_ARGS = {
    "sut_reply_count": _arg_count,
    "replies_to": _arg_label,
    "replies_to_any": _arg_label_list,
    "no_sut_reply_to": _arg_label,
    "no_sut_before": _arg_label,
    "latency": _arg_latency,
    "max_chars": _arg_max_chars,
    "total_chars": _arg_count,
    "content_matches": _arg_content,
    "no_mass_mentions": _arg_true,
    "no_duplicate_replies": _arg_true,
    "mentions": _arg_mentions,
    "quarantined": _arg_label,
    "ledger_status": _arg_ledger,
    "converse_turns": _arg_converse,
    "log_contains": _arg_regex,
    "log_lacks": _arg_regex,
    "held": _arg_label,
    "queue_item": _arg_queue_item,
    "monitor_restarts": _arg_count,
    "overlay_lacks": _arg_key_suffix,
}


def _validate_expect(expect, where, labels, kinds, tier):
    _need_map(expect, where)
    _only_keys(expect, ("settle", "checks"), where, required=("checks",))
    if "settle" in expect:
        _positive(expect["settle"], f"{where}.settle", allow_zero=True)
    checks = expect["checks"]
    if not isinstance(checks, list):
        _fail(f"{where}.checks", "expected a list")
    if tier == 1 and not checks:
        _fail(f"{where}.checks", "a tier-1 scenario needs at least one "
                                 "check")
    for i, chk in enumerate(checks):
        w = f"{where}.checks[{i}]"
        if not isinstance(chk, dict) or len(chk) != 1:
            _fail(w, "each check is a mapping with exactly one key")
        (name, arg), = chk.items()
        if name not in CHECK_ARGS:
            _fail(w, f"unknown check {name!r} (known: "
                     f"{', '.join(sorted(CHECK_ARGS))})")
        CHECK_ARGS[name](arg, f"{w}.{name}", labels, kinds)


def _check_config_placeholders(node, where):
    """Every "{{...}}" in config overrides (keys and values) must be one
    sut.py renders, and a whole string: anything else would reach the
    monitor's config.yaml verbatim."""
    if isinstance(node, dict):
        items = list(node.items())
        for k, _ in items:
            _check_config_placeholder(k, f"{where}.{k}")
        for k, v in items:
            _check_config_placeholders(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _check_config_placeholders(v, f"{where}[{i}]")
    else:
        _check_config_placeholder(node, where)


def _check_config_placeholder(v, where):
    if not isinstance(v, str):
        return
    found = _CONFIG_PH_RE.findall(v)
    if not found:
        return
    if len(found) > 1 or not re.fullmatch(r"\{\{\s*[^{}]*?\s*\}\}", v) \
            or found[0] not in CONFIG_PLACEHOLDERS:
        _fail(where, f"bad placeholder in {v!r}: use exactly one of "
                     + ", ".join("{{%s}}" % p for p in CONFIG_PLACEHOLDERS)
                     + " as the whole value or key")


def validate(sc, source=None, fixtures_dir=None):
    """Validate one scenario dict. source is the file it came from (for
    messages, and the id-equals-file-name rule). Raises ScenarioError;
    returns the scenario unchanged on success."""
    fixtures_dir = fixtures_dir or FIXTURES_DIR
    name = os.path.basename(source) if source else (
        sc.get("id") if isinstance(sc, dict) else None) or "<scenario>"
    if not isinstance(sc, dict):
        _fail(name, "the file must hold a YAML mapping")
    _only_keys(sc, TOP_KEYS, name,
               required=("id", "tier", "title", "steps", "expect"))

    sid = sc["id"]
    if not isinstance(sid, str) or not ID_RE.match(sid):
        _fail(f"{name}: id", f"expected [a-z0-9_]+, got {sid!r}")
    if source:
        stem = os.path.splitext(os.path.basename(source))[0]
        if stem != sid:
            _fail(f"{name}: id", f"id {sid!r} must equal the file name "
                                 f"{stem!r}")
    tier = sc["tier"]
    if tier not in (1, 2) or isinstance(tier, bool):
        _fail(f"{name}: tier", f"expected 1 or 2, got {tier!r}")
    if not isinstance(sc["title"], str) or not sc["title"].strip():
        _fail(f"{name}: title", "expected a non-empty string")
    if "b_is_bot" in sc and not isinstance(sc["b_is_bot"], bool):
        _fail(f"{name}: b_is_bot", f"expected true/false, got "
                                   f"{sc['b_is_bot']!r}")
    if "config" in sc:
        if not isinstance(sc["config"], dict):
            _fail(f"{name}: config", "expected a mapping of config "
                                     "overrides")
        _check_config_placeholders(sc["config"], f"{name}: config")

    labels, kinds = _validate_steps(sc["steps"], f"{name}: steps",
                                    fixtures_dir)
    _validate_expect(sc["expect"], f"{name}: expect", labels, kinds, tier)

    if tier == 2:
        judge = sc.get("judge")
        if not isinstance(judge, dict) or not isinstance(
                judge.get("rubric"), str) or not judge["rubric"].strip():
            _fail(f"{name}: judge.rubric", "a tier-2 scenario needs a "
                                           "non-empty judge.rubric")
        _only_keys(judge, ("rubric",), f"{name}: judge")
    elif "judge" in sc:
        _fail(f"{name}: judge", "only tier-2 scenarios are judged")
    return sc


def load_file(path, fixtures_dir=None):
    name = os.path.basename(path)
    try:
        with open(path) as f:
            sc = yaml.safe_load(f)
    except OSError as e:
        raise ScenarioError(f"{name}: cannot read: {e}") from e
    except yaml.YAMLError as e:
        raise ScenarioError(f"{name}: invalid YAML: {e}") from e
    return validate(sc, source=path, fixtures_dir=fixtures_dir)


def load_all(dir=SCENARIOS_DIR, tier=None, fixtures_dir=None):
    """Load and validate every *.yaml in dir, sorted by file name.
    tier=1 or 2 keeps only that tier. Ids must be unique."""
    if not os.path.isdir(dir):
        raise ScenarioError(f"{dir}: not a directory")
    if fixtures_dir is None:
        fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(dir)),
                                    "fixtures")
    out, seen = [], {}
    for fname in sorted(os.listdir(dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        sc = load_file(os.path.join(dir, fname), fixtures_dir)
        if sc["id"] in seen:
            raise ScenarioError(f"{fname}: id: {sc['id']!r} already used "
                                f"by {seen[sc['id']]}")
        seen[sc["id"]] = fname
        if tier is None or sc["tier"] == int(tier):
            out.append(sc)
    if not seen:
        raise ScenarioError(f"{dir}: no scenario files")
    return out
