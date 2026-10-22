"""The /settings crawler: from every entry point, click every control a
page renders and check every response against Discord's limits (types,
flags, component counts and nesting, labels, options, custom_id length
and uniqueness, modal shapes, the text limit) and against names that
could ping, mention or link (hostile server, channel and user names).
A response that crashes or calls its own control out of date is a
problem too. Nothing here reads the pages' words: the redesign may
change any copy.

    python3 tests/settingsui/crawl.py     # print the problems, if any

Navigation is followed breadth first; every write control is clicked on
a copy of the settings (and the controls on its answer, one level
deeper: confirm, Undo, Try again), then the settings are put back. The
60 filler servers and 74 filler channels count as one each.
MUT_DIR = the package tree under test."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness as H  # noqa: E402
from harness import UI  # noqa: E402

# Discord's own limits for a Components V2 message (not the page's
# tighter budgets in settings_ui.py, which may change).
DISCORD_MAX_COMPONENTS = 40
DISCORD_MAX_TEXT = 4000         # all Text Displays together
SELECT_TYPES = (3, 5, 6, 7, 8)
OURS = f"{UI.PREFIX}:{UI.VERSION}:"     # every custom_id the pages render
NAV_VERBS = ("pg", "wh")
# Text Displays: the only links are ours (messages), names never ping or
# mention anyone (spec §7, review B #4).
FOREIGN_LINK = re.compile(r"(?<!\\)\]\((?!https://discord\.com/channels/)")
MASS_PING = re.compile(r"@(everyone|here)\b")
RAW_MENTION = re.compile(r"<(@|@&|#)(?!\d{15,25}>)")
# A bare URL autolinks too; ours all go to discord.com/channels.
BARE_URL = re.compile(r"https?://(?!discord\.com/channels/)", re.I)
BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
FILLER = re.compile(r"(6[12]000000000000000\d{3}|7[12]000000000000000\d{3})")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check(resp, where):
    """Problems with one response (empty: fine)."""
    bad = []

    def no(cond, msg):
        if cond:
            bad.append(f"{where}: {msg}")
    if not isinstance(resp, dict):
        return [f"{where}: not a dict"]
    t = resp.get("type")
    data = resp.get("data") or {}
    no(t not in (4, 7, 9), f"type {t}")
    if t == 4:
        no(data.get("flags") != 64 | 32768, f"flags {data.get('flags')}")
    if t == 7:
        no(not (data.get("flags", 0) & 32768), "type 7 without V2 flag")
    if t in (4, 7):
        no("content" in data or "embeds" in data, "content or embeds")
        no(data.get("allowed_mentions") != {"parse": []}, "mentions")
    if t == 9:
        no(not data.get("custom_id", "").startswith(OURS),
           f"modal id {data.get('custom_id')}")
        no(len(data.get("custom_id", "")) > 100, "modal id too long")
        no(not 1 <= len(data.get("title", "")) <= 45,
           f"modal title {data.get('title')!r}")
        comps = data.get("components") or []
        no(not 1 <= len(comps) <= 5, f"{len(comps)} modal components")
        for c in comps:
            no(c.get("type") not in (10, 18), f"modal top type {c}")
            if c.get("type") == 18:
                no(not 1 <= len(c.get("label", "")) <= 45,
                   f"modal label {c.get('label')!r}")
                no(len(c.get("description", "")) > 100,
                   "modal label description")
                inner = c.get("component") or {}
                no(inner.get("type") not in (3, 4), f"label holds {inner}")
                if inner.get("type") == 4:
                    no("label" in inner, "text input with its own label")
                    no(len(inner.get("value", "")) > inner.get(
                        "max_length", 4000), "prefill too long")
                    no(str(inner.get("value", "")).lower() == "none",
                       "prefilled none")
    total = len(list(H.walk(data.get("components"))))
    no(total > DISCORD_MAX_COMPONENTS, f"{total} components")
    shown = sum(len(c.get("content") or "")
                for c in H.walk(data.get("components"))
                if c.get("type") == 10)
    no(shown > DISCORD_MAX_TEXT, f"{shown} characters of text")
    ids = []
    for c in H.walk(data.get("components")):
        ct = c.get("type")
        if ct == 1:
            kids = c.get("components") or []
            sels = [k for k in kids if k.get("type") in SELECT_TYPES]
            no(not kids, "empty row")
            no(sels and len(kids) != 1, "a select shares its row")
            no(len(kids) > 5, f"{len(kids)} in a row")
            no(any(k.get("type") not in (2,) + SELECT_TYPES for k in kids),
               "row holds a non-control")
        if ct == 9:
            no(not isinstance(c.get("accessory"), dict), "section with no "
               "accessory")
            no(not 1 <= len(c.get("components") or []) <= 3,
               "section texts")
        if ct == 10:
            no(not c.get("content"), "empty text")
        if ct == 2:
            no(not 1 <= len(c.get("label", "")) <= 80,
               f"button label {c.get('label')!r}")
            no(c.get("style") not in (1, 2, 3, 4), "button style")
        if ct in SELECT_TYPES:
            no(len(c.get("placeholder", "")) > 150, "placeholder")
            mn, mx = c.get("min_values", 1), c.get("max_values", 1)
            no(not 0 <= mn <= mx <= 25, f"min/max {mn}/{mx}")
            no(len(c.get("default_values") or []) > mx, "defaults")
        if ct == 3:
            opts = c.get("options") or []
            no(not 1 <= len(opts) <= 25, f"{len(opts)} options")
            vals = [o.get("value") for o in opts]
            no(len(vals) != len(set(vals)), "duplicate option values")
            no(sum(1 for o in opts if o.get("default")) > c.get(
                "max_values", 1), "too many defaults")
            for o in opts:
                no(not 1 <= len(o.get("label", "")) <= 100, "option label")
                no(not 1 <= len(o.get("value", "")) <= 100, "option value")
                no(len(o.get("description", "")) > 100, "option desc")
        if "custom_id" in c and t != 9:
            cid = c["custom_id"]
            ids.append(cid)
            no(not cid.startswith(OURS), f"custom id {cid}")
            no(len(cid) > 100, f"custom id {len(cid)} chars: {cid}")
    no(len(ids) != len(set(ids)), "duplicate custom ids")
    for c in H.walk(data.get("components")):
        if c.get("type") != 10:
            continue
        t = c.get("content") or ""
        no(bool(FOREIGN_LINK.search(t)), f"a link a name made in {t!r}")
        no(bool(MASS_PING.search(t)), f"@everyone / @here in {t!r}")
        no(bool(RAW_MENTION.search(t)), f"a mention a name made in {t!r}")
        no(bool(BARE_URL.search(t)), f"a bare URL a name made in {t!r}")
        no(bool(BIDI.search(t)), f"a bidi control a name made in {t!r}")
    return bad


def out_of_date(resp):
    return UI.OUT_OF_DATE in "\n".join(H.texts(resp))


def crashed(ctx):
    """settings_ui.handle logs a traceback for an exception it caught."""
    return any("Traceback" in str(m) for m in ctx.logs)


# ---------------------------------------------------------------------------
# Clicking
# ---------------------------------------------------------------------------
class Actor:
    """Who is clicking, and where."""

    def __init__(self, user, guild=None, perms=None, channel=H.MUSECHAT):
        self.user, self.guild, self.perms, self.channel = \
            user, guild, perms, channel

    def click(self, ctx, custom_id, values=None, resolved=None,
              message=None):
        ctx.refresh()
        return UI.handle(H.component(
            self.user, custom_id, values, self.guild, self.perms,
            self.channel, resolved, message), ctx)

    def submit(self, ctx, custom_id, fields):
        ctx.refresh()
        return UI.handle(H.submit(self.user, custom_id, fields, self.guild,
                                  self.perms, self.channel), ctx)


def clicks_for(c):
    """[(values, resolved)] to try on one control."""
    ct = c.get("type")
    cid = c.get("custom_id", "")
    if ct == 2:
        return [(None, None)]
    if ct == 3:
        return [([o["value"]], None) for o in c.get("options") or []]
    if ct == 5:
        return [([H.SAM], H.resolved_for(5, [H.SAM]))]
    if ct == 7:
        if ":ig" in cid:
            return [([H.SAM, H.ROLE], H.resolved_for(7, [H.SAM, H.ROLE])),
                    ([], {})] if c.get("min_values", 1) == 0 else \
                [([H.SAM], H.resolved_for(7, [H.SAM]))]
        return [([H.SAM], H.resolved_for(7, [H.SAM]))]
    if ct == 8:
        return [([H.GENERAL], H.resolved_for(8, [H.GENERAL]))]
    return []


def modal_fields(resp):
    """Plausible answers for a modal: what it is prefilled with (a cap
    box gets a number, a search box a word)."""
    out = {}
    for c in (resp.get("data") or {}).get("components") or []:
        if c.get("type") != 18:
            continue
        inner = c["component"]
        if inner.get("type") == 4:
            v = inner.get("value") or ""
            if not v and inner.get("max_length") == 7:
                v = "12"
            if inner.get("custom_id") == "q":
                v = "room"
            if inner.get("custom_id") == "ids":
                v = f"<@{H.KAI}>"
            out[inner["custom_id"]] = v
        elif inner.get("type") == 3:
            d = [o["value"] for o in inner.get("options") or []
                 if o.get("default")]
            out[inner["custom_id"]] = d or [inner["options"][0]["value"]]
    return out


def canonical(custom_id, values):
    return FILLER.sub("F", custom_id) + "|" + ",".join(
        FILLER.sub("F", v) for v in values or [])


class Crawl:
    def __init__(self, ctx, actor, max_pages=5000, seen=None):
        self.ctx, self.actor = ctx, actor
        self.problems = []
        self.count = 0           # responses checked
        self.ids = set()         # every custom_id a response rendered
        self.seen = set() if seen is None else seen
        self.max_pages = max_pages

    def note(self, path, resp):
        self.count += 1
        for p in check(resp, path):
            self.problems.append(p)
        if out_of_date(resp):
            self.problems.append(f"{path}: answered out of date")
        if crashed(self.ctx):
            self.problems.append(f"{path}: crashed: {self.ctx.logs[-1:]}")
            self.ctx.logs.clear()
        self.ids.update(c["custom_id"] for c in H.controls(resp))

    def do(self, path, c, values, resolved, message):
        cid = c["custom_id"]
        r = self.actor.click(self.ctx, cid, values, resolved, message)
        step = f"{path} > {cid}" + (f"={','.join(values)}" if values
                                    else "")
        self.note(step, r)
        if r.get("type") == 9:
            fields = modal_fields(r)
            r2 = self.actor.submit(self.ctx, r["data"]["custom_id"], fields)
            self.note(step + " > submit", r2)
            return step + " > submit", r2
        return step, r

    def run(self, path, first):
        self.note(path, first)
        queue = [(path, first)]
        while queue and self.count < self.max_pages:
            path, resp = queue.pop(0)
            if resp.get("type") not in (4, 7):
                continue
            msg = {"id": "9", "flags": 64 | 32768,
                   "components": resp["data"].get("components")}
            for c in H.controls(resp):
                verb = c["custom_id"].split(":")[2]
                for values, resolved in clicks_for(c):
                    key = canonical(c["custom_id"], values)
                    if key in self.seen:
                        continue
                    self.seen.add(key)
                    if verb in NAV_VERBS:
                        step, r = self.do(path, c, values, resolved, msg)
                        queue.append((step, r))
                        continue
                    snap = self.ctx.snapshot()
                    try:
                        step, r = self.do(path, c, values, resolved, msg)
                        self.second(step, r)
                    finally:
                        self.ctx.restore(snap)
        return self

    def second(self, path, resp):
        """One level deeper after a write: the controls it added (Apply,
        Undo, Try again), each on its own copy."""
        if resp.get("type") not in (4, 7):
            return
        msg = {"id": "9", "flags": 64 | 32768,
               "components": resp["data"].get("components")}
        for c in H.controls(resp):
            verb = c["custom_id"].split(":")[2]
            if verb not in ("ok", "un", "re"):
                continue
            snap = self.ctx.snapshot()
            try:
                self.do(path, c, None, None, msg)
            finally:
                self.ctx.restore(snap)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def entries():
    """[(name, ctx factory, actor, first interaction)]."""
    O = H.OWNER
    MS_ = H.MEMBER
    out = [
        ("owner_channel", H.FakeCtx, Actor(O, H.G1, "0", H.MUSECHAT),
         H.command(O, H.G1, "0", H.MUSECHAT)),
        ("owner_general", H.FakeCtx, Actor(O, H.G1, "0", H.GENERAL),
         H.command(O, H.G1, "0", H.GENERAL)),
        ("owner_thread", H.FakeCtx, Actor(O, H.G1, "0", H.THREAD),
         H.command(O, H.G1, "0", H.THREAD, 11, H.MUSECHAT)),
        ("owner_dm", H.FakeCtx, Actor(O), H.command(O)),
        ("owner_configure", H.FakeCtx, Actor(O),
         H.component(O, f"mdset:open:server:{H.G2}")),
        ("admin_panel", H.FakeCtx,
         Actor(H.ADMIN, H.G1, H.MANAGE, H.GENERAL),
         H.command(H.ADMIN, H.G1, H.MANAGE, H.GENERAL)),
        ("member", H.FakeCtx, Actor(MS_, H.G1, "0"),
         H.command(MS_, H.G1, "0")),
        ("member_dm", H.FakeCtx, Actor(MS_), H.command(MS_)),
        ("why_owner", H.FakeCtx, Actor(O, H.G1, "0"),
         H.context_command(O, H.OUTCOMES[0]["message_id"], author=H.SPAM)),
        ("why_admin", H.FakeCtx, Actor(H.ADMIN, H.G1, H.MANAGE),
         H.context_command(H.ADMIN, H.OUTCOMES[1]["message_id"],
                           perms=H.MANAGE, author=H.ANA)),
        ("why_member", H.FakeCtx, Actor(MS_, H.G1, "0"),
         H.context_command(MS_, H.OUTCOMES[0]["message_id"])),
        ("old_id", H.FakeCtx, Actor(O), H.component(O, "mdset:p:e")),
        ("expired", H.FakeCtx, Actor(O, H.G1, "0"),
         H.component(O, f"{OURS}ok:s{H.G1}:t999999999", None, H.G1,
                     "0")),
        ("overlay_broken", lambda: H.FakeCtx(broken_overlay=True),
         Actor(O, H.G1, "0", H.MUSECHAT),
         H.command(O, H.G1, "0", H.MUSECHAT)),
    ]
    return out


def crawl_all(names=None, max_pages=5000):
    """{entry name: Crawl}. Entries clicking as the same person in the
    same place share what they have clicked already (the later one
    records only what is new). max_pages caps each entry's crawl
    (breadth first: the nearest pages come first)."""
    out = {}
    seen = {}
    for name, factory, actor, first in entries():
        if names and name not in names:
            continue
        H.install_determinism()
        ctx = factory()
        ctx.refresh()
        resp = UI.handle(first, ctx)
        key = (actor.user, actor.guild, factory)
        out[name] = Crawl(ctx, actor, max_pages,
                          seen.setdefault(key, set())).run(name, resp)
        if name == "old_id":
            # Out of date is the right answer here.
            out[name].problems = [p for p in out[name].problems
                                  if "out of date" not in p]
    return out


def main():
    rc = 0
    for name, c in crawl_all().items():
        print(f"{name}: {c.count} responses, {len(c.problems)} problems")
        for p in c.problems[:40]:
            print("  " + p)
        rc = rc or bool(c.problems)
    return rc


if __name__ == "__main__":
    sys.exit(main())
