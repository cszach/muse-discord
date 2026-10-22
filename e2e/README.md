# e2e: end-to-end tests on a real Discord server

These tests run a PR's `monitor.py` against a private Discord test
server. Two "driver" bots play people, following scripted scenarios
in `e2e/scenarios/`; the harness then pulls each scenario's channel
transcript and checks it.

- **Tier 1** (every same-repo PR): a scripted fake worker
  (`fake_worker.py`) answers the queue, and code checks
  (`checks.py`) judge the transcript. Deterministic; a failed check
  fails the PR's `E2E tier 1` check.
- **Tier 2** (only with the `e2e-full` label): Claude Code, headless,
  plays the real reply worker from `worker-prompt.template.md`
  (`claude_worker.py`), and a separate Claude run judges each
  transcript against the scenario's rubric (`judge.py`). Advisory: it
  never blocks a PR. It spends the owner's Claude subscription usage,
  which is why it is opt-in per PR.

Each tier posts one sticky PR comment with its summary table
(updated in place on every run) and uploads `out/` (results,
transcripts, logs) as a workflow artifact. Fork PRs get no secrets,
so the workflow skips them; without secrets, `run.py` reports
"skipped" and passes.

The harness's own offline unit tests (`e2e/tests/`, no network) run
in the regular CI job. They guard the harness against passing a broken
bot: the checks, scenario loading, the two workers' protocol, the
judge's parsing, and the runner's pass / fail / flake reporting:

```bash
python3 -m unittest discover -s e2e/tests -t .
```

The package's offline suites (`tests/`, also no network, also in CI)
test `monitor.py`, the watchdog, the hooks and the settings code
directly; see `tests/README.md`.

## Scenarios

A scenario earns its place only by proving something that needs real
Discord: gateway and REST behaviour, real message payloads (replies,
mentions, threads, system notices), permissions, restarts against the
real API, or, in tier 2, Claude following the worker prompt. Logic an
offline suite in `tests/` already proves stays there. Scenarios that
share their setup are one scenario with several checks (checks name
labels, so a failure still says which part broke).

Tier 1 (a few minutes; most of it is monitor restarts and settle
time):

| Scenario | Proves |
| --- | --- |
| `mention_burst` | A mention gets one threaded reply; a burst of fragments gets one reply under its last fragment; two people get one each |
| `reply_mentions` | A long reply that names a user and @everyone pings only that user and is capped to `max_reply_chars` |
| `stays_quiet` | Chatter, a bare name, an injection (quarantined), a `silent` item and a never_share draft (held) get no reply; a mention after them does |
| `follow_ups` | Follow-ups, a reply to the bot without a mention, and what does not count (someone else's message, a thread notice, a closed window) |
| `bot_direct` | A real bot (B, not treated as human) is answered once, with bot burst timing |
| `gateway_down_poll` | With the gateway off, REST polling still answers |
| `reply_to_deleted` | A reply whose target was deleted still goes out, unthreaded |
| `restart_mid_send` | SIGKILL mid-send still gives exactly one reply (Discord's nonce check) |
| `settings_scopes_live` | Live settings changes and undo without a restart; a channel overrides its server; a new thread follows its channel |
| `reminders_watches` | A reminder and a Discord watch (checked against the real server's members and permissions) each fire once, threaded under the request |

Tier 2 (Claude as worker and judge):

| Scenario | Proves |
| --- | --- |
| `persona_voice` | The owner's ordinary request gets a helpful answer in the persona's voice |
| `chat_context_burst` | The bot stays out of a chat, then answers a question split over three messages from that chat |
| `image_seen` | An attached image reaches the worker and is described correctly |
| `bot_conversation_ends` | Banter with another bot is wound down |
| `stranger_privileged_request` | A non-owner claiming the owner's approval gets no owner-only action and no settings change |
| `settings_owner_asks_in_server` | The owner asking for a settings change in a server is pointed to a DM or `/settings` |

Every tier-1 reply also proves the `reply_nonce` binding live: the
fake worker plays the wake hook (it reads each item's nonce from
`item_nonces.json`) and the monitor sends nothing without it.

Covered offline only, because Discord or the clock gets in the way:

- Owner DMs (settings by message, Apply / Undo, the `owner_dm_nonce`,
  owner notices, `who.dms_from`): driver A, the owner, is a bot, and
  Discord does not let a bot DM another bot. So the never_share hold
  DM and the owners' alerts fail ("no owner reachable"), and
  `stays_quiet` checks the hold in `state.json`, not the DM.
  `tests/ownerdm` and `tests/settingsnl`, plus the owner-DM checklist
  below.
- Unprompted messages (`proactive.py`): the shortest heartbeat gap is
  45 minutes, and a fresh channel has nobody to reach out to.
  `tests/proactive`. The fake worker lets any slot expire.
- Web watches (a checker session on the web): `tests/watches`.
- The follow-up window's expiry (a 120 s constant), `role:<id>`
  entries (the drivers hold no roles), category settings (scenario
  channels have no category) and old-layout configs:
  `tests/settings`.
- `/settings` itself: bots cannot run slash commands or click someone
  else's ephemeral buttons. `tests/settingsplumb` and
  `tests/settingsui` cover it (every page crawled against Discord's
  limits, forged ids, the walkthroughs), the checklist below covers
  the client. What it does to the bot is live: the `settings` step
  writes `config.discord.yaml` through the same `muse_settings` calls.

Scenarios run in file-name order, each in its own channel, against one
monitor; it is restarted only when a scenario's config differs from
the one running (a scenario that sets `config:` or `b_is_bot`, and the
one after it) or a step restarts it.

## One-time setup [owner]

1. **A private test server.** Create a new Discord server used only
   for these tests. The harness creates and deletes one channel per
   scenario in it.
2. **Three applications**, at
   https://discord.com/developers/applications, each with a bot:
   `muse-test` (the bot under test), `driver-a`, and `driver-b`. On
   each one's **Bot** page, enable **Message Content Intent** (the
   monitor needs it to read messages, and the drivers read the
   transcripts back through the REST API, which blanks message text
   without it). Server Members Intent stays off.
3. **Invite them to the test server** (replace `<APP_ID>` with each
   application's ID):
   - `muse-test`, with the same permissions as a real install
     (the invite `bin/connect-discord.py` prints: View Channel, Send
     Messages, Send Messages in Threads, Read Message History, Attach
     Files, Embed Links, Add Reactions, Pin Messages):
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=2252074691710016`
     (this already includes Send Messages in Threads, so it can answer
     in the threads the scenarios start)
   - `driver-a`: View Channel, Send Messages, Read Message History,
     Attach Files, Add Reactions, Manage Channels (it creates and
     deletes the scenario channels), Create Public Threads (it starts
     the scenarios' threads) and Send Messages in Threads:
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=309237746768`
   - `driver-b`: View Channel, Send Messages, Read Message History,
     Attach Files, Add Reactions and Send Messages in Threads:
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=274878008384`

4. **Repository secrets** (Settings, Secrets and variables, Actions):
   - `E2E_GUILD_ID`: the test server's ID (Developer Mode on,
     right-click the server icon, **Copy Server ID**).
   - `E2E_SUT_TOKEN`, `E2E_DRIVER_A_TOKEN`, `E2E_DRIVER_B_TOKEN`: the
     three bot tokens (each application's **Bot** page, **Reset
     Token**).
   - `CLAUDE_CODE_OAUTH_TOKEN` (tier 2 only): run `claude setup-token`
     on a machine where Claude Code is logged in to the subscription
     and paste the token it prints. Tier 2 reports "skipped" without
     it.
5. **The `e2e-full` label**: create it (Issues, Labels) so PRs can
   opt in to tier 2.

Never invite these three bots to a real server, and never reuse
their tokens elsewhere: the test config makes the drivers look human
to the bot under test (below), and driver A is its owner.

## `testing.treat_as_human` (test-only config)

```yaml
testing:
  treat_as_human: ["<user id>", ...]
```

Messages from the listed user ids are handled as if their authors
were human, everywhere the monitor treats bots differently (the bots
policy, `from_bot` / `bot_turns`, burst timing, owner gating). The
harness lists the driver bots there, so they can stand in for people.
The monitor logs a WARNING at startup while it is set. It is absent
from `config.example.yaml` on purpose: never set it in a real
install.

In the test config, driver A is the owner (`owners`) and listed in
`treat_as_human`. Driver B is listed too, unless a scenario sets
`b_is_bot: true` to exercise the bot-to-bot path.

## The test config

`config.test.yaml` is the SUT's `config.yaml` in the settings layout
(`muse_settings.py`): `owners` (driver A), `token_from` and `persona`
(filled in by `sut.py`), `everywhere` / `dms` with the shipped
defaults written out, the test server under `servers."<id>"` (with
`who.replies: conversational`), and an `engine:` block with fast
timings (REST poll 10 s, coalesce 5 s). `sut.py` always forces
presence off and `updates.notify: off`. A scenario's `config:` is
deep-merged on top and may use `{{GUILD_ID}}`, `{{A_ID}}`, `{{B_ID}}`
and `{{CHANNEL_ID}}` (the channel the runner created for this
scenario), in keys too, each as a whole key or value:

```yaml
config:
  servers:
    "{{GUILD_ID}}":
      who:
        ignore: ["{{B_ID}}"]
        replies: "off"
      channels:               # channel (or category) scope
        "{{CHANNEL_ID}}":
          replies: when-addressed
```

A channel block is flat, as in `config.example.yaml`: it may set only
`replies`, `notes`, `media.enabled` and `plugins.<plugin>.enabled`
(plus `name`), not
`who:` / `style:` groups; anything else (limits, privacy, ...) is set
on the server. Threads follow their parent channel.

The base config lists one test phrase in `privacy.never_share`
("blue pelican"), so `stays_quiet` can plant it in a reply without a
config of its own. Old-layout configs are covered offline
(`tests/settings`), not here.

## Manual checklist: /settings

Run it before a release that touches `/settings`, `settings_ui.py`,
or the interaction code, with a real install (or `muse-test`), your
own account as an owner, and a second account that is not an owner.
Keep `monitor.log` open (`tail -f`) and `config.discord.yaml` at hand.
Do the client checks (marked *client*) on the desktop app, the web
app, iOS and Android: these are what the offline suites cannot see.

Registration:

- [ ] Start the monitor: the log says `/settings command created` (or
      `updated`), or nothing on a restart with the commands unchanged.
      No `registration failed` line.
- [ ] `/settings` appears in the bot's DM and in a server channel,
      and not in a group DM.
- [ ] *client* Right-click (long-press) a message, Apps: **Why no
      reply?** is there, with the "?". If the log says `Discord
      refused the command name 'Why no reply?'`, it is "Why no reply";
      note it, and the spec's fallback is working. The second account
      without Manage Server does not see it in the menu.
- [ ] Restart again: no `created` / `updated` line (no needless
      write).

Owner, in a server:

- [ ] `/settings` in a channel opens one page for that channel,
      visible only to you; in a thread or forum post, its parent's
      page, saying threads follow it.
- [ ] *client* Components V2 render: the coloured box, the
      separators, the selects, the buttons beside text, `-#` small
      text. Open the server's page (Where › All of the server, about
      28 components) and the admin panel with a banner: nothing is cut
      off or fails to load, on mobile too, and no select label is
      clipped badly at phone width.
- [ ] **When I reply** › "Only when @mentioned": the page updates in
      place with **Done.** and **Undo**, `config.discord.yaml` gains
      the entry with your id as `by`, the log says `Settings
      reloaded`, and the bot's next reply follows it without a
      restart. **Undo** puts it back; "Use server default" (or "Use
      category default" in a category that sets it) removes the
      channel's own setting, and its "Currently: …" says what that
      gives.
- [ ] **Where** › All of the server, then Everywhere: each shows its
      own settings (reply length, images and video, replies a person
      a day; everywhere also who can DM it), and changes land there.
- [ ] **Images & video** › Everyone on the server's page asks first
      (it can cost money); Apply, then **Undo**.
- [ ] **Style** › Community: one click applies it, the **Done** line
      lists what changed, one **Undo** reverts all of it. A server
      that matches no style shows "Customized" selected; picking it
      changes nothing.
- [ ] The last change shows as one short line with **Undo** (which
      settings, who, when), never a long list of values.
- [ ] **Why no reply?** on the channel page lists a message you sent
      while the bot was set to ignore you, with a fix button; the fix
      works and the line says to send again.
- [ ] **Pause here**: the page turns grey and says it is paused;
      **Resume here**. Pause the server from its page, then open a
      channel's page: it says the server is paused and offers
      **Resume server**.

Owner, in a DM:

- [ ] `/settings` opens DMs: who can DM it, reply length, images and
      video. **Where** › Everywhere has **Pause everywhere**, which
      asks first; Apply, then **Undo**.
- [ ] Who can DM me › Anyone asks first; a list of people you picked
      by DMing the bot shows as "You and N people you picked".
- [ ] **Why no reply?** › Check someone by ID with a friend's id says
      whether they can DM the bot, with a **Let … DM me** button.
- [ ] *client* DM user select (flip `DM_USER_SELECT_SEARCHES` in
      `settings_ui.py` to try it): in a bot DM, does a user select
      find a friend or a mutual-server member by name? If search works
      everywhere, leave the constant on.
- [ ] Not offered on the page (the footer says to DM the bot):
      instructions, memory, caps other than replies a person a day,
      other bots, prompt injection, people lists, status line, update
      notices, plugins. Never from Discord: owners, token, persona,
      never_share, categories, installing plugins.

Server admin (the second account, with Manage Server in one server,
not an owner):

- [ ] `/settings` there shows only **Quiet the bot**: quieter buttons
      for here and the whole server, Ignore someone, Stop ignoring
      (admins' picks), Turn off for images and video (or a plugin)
      when on, and Undo of admins' last change. No privacy, caps,
      styles, Where, DMs or other servers.
- [ ] Off in all of the server, then the banner's **Undo**: back to
      what you (the owner) set.
- [ ] Make a change: about a minute later the owner gets one DM
      summing up the admin's changes in that server (several changes
      in that minute arrive together).
- [ ] **Why no reply?** on a message works for the admin; on a
      message in another server it is refused.

Anyone else (the second account, no Manage Server):

- [ ] `/settings` answers "Only the bot's owners can change its
      settings. Server admins (Manage Server) can make it quieter
      here." and nothing is written; **Why no reply?** is refused.

Owner DMs:

- [ ] Invite the bot to a new server: the join DM has a **Configure**
      button that opens that server's page. An old join DM's
      **Configure** still works.
- [ ] Change a setting with `/settings`, then edit the same setting
      in `config.yaml`: the owner gets one DM saying the file wins.

Robustness:

- [ ] *client* An old `/settings` page (from before the update, still
      open in a channel, its buttons `mdset:2:…`): clicking it answers
      "This settings page is out of date" in a new message, nothing is
      written, and the old message stays as it was.
- [ ] Click through pages while the bot is busy replying: every
      click answers at once ("This interaction failed" never shows).
- [ ] Restart the monitor between a confirm card and **Apply**: Apply
      says the confirmation expired and nothing changes.
- [ ] Move `settings_ui.py` away and restart: `/settings` answers
      "Settings UI unavailable." and the log says why, once.

## Manual checklist: owner DMs

No driver can DM the bot (Discord does not let a bot DM another bot),
so settings by message and the owner-DM nonce are tested by hand. Run
it before a release that touches `settings_nl.py`, the owner-DM
handling in `monitor.py`, `hooks/mention-watch.sh`,
`bin/discord-api.py` or the worker prompt, with a real install (the
wake hook and a real worker), your own account as an owner, and
`monitor.log` open.

- [ ] DM the bot "only answer in #<channel> when I @ you": it applies
      at once with an **Undo** button, `config.discord.yaml` gains the
      entry, and **Undo** reverts it.
- [ ] DM a change that opens the bot up ("let anyone DM you"): it
      shows what would change and waits for **Apply**; nothing changes
      until you click it.
- [ ] `bin/settings.py set ...` (as your Muse would): you get a DM
      saying what changed, with **Undo**, and no ping.
- [ ] Mention the bot in a server and DM it at the same moment: both
      are answered, and the log has no `Refused outbox item` line for
      your DM's reply (your DM woke alone, with its nonce).
- [ ] Ask the bot in your DM to pin or react to a message there: the
      worker's `bin/discord-api.py --for <id> --nonce <nonce> ...` call
      works; the same call by hand without `--for` / `--nonce` is
      refused.

## Running locally

The bot under test must not be running anywhere else at the same time
(two monitors on one token both reply), including a CI run.

```bash
pip install -r requirements.txt
python3 e2e/run.py --tree . --tier 1 --dry-run   # no network, no env
export E2E_GUILD_ID=... E2E_SUT_TOKEN=... \
       E2E_DRIVER_A_TOKEN=... E2E_DRIVER_B_TOKEN=...
python3 e2e/run.py --tree . --tier 1
# one scenario, keeping its channel for a look afterwards:
python3 e2e/run.py --tree . --tier 1 --scenario mention_burst \
       --keep-channels
```

The harness runs `monitor.py` itself, from a copy of the tree, with
`updates.notify: off` (no update DMs to the owner) and presence off.
Neither `watchdog.sh` nor the wake hook runs, so nothing restarts the
monitor behind a scenario's `kill_monitor` step.

Results land in `out/` (`results.json`, `summary.md`,
`transcripts/`). Tier 2 also needs the `claude` CLI on PATH, logged
in or with `CLAUDE_CODE_OAUTH_TOKEN` set. `CLAUDE_BIN` points the
worker and the judge at another CLI binary, and `E2E_CLAUDE_MODEL`
picks the model for both (default: the CLI's).

## The tier-2 worker and judge

`claude_worker.py` does what `hooks/mention-watch.sh` does in a Muse
install: it hands each open queue item to exactly one run, with the
hook's payload and nonces (each item's `reply_nonce`; owner DMs and
owner commands wake alone with theirs), and runs `claude -p` with the
PR's `worker-prompt.template.md` (rendered by
`bin/render-worker-prompt.py`, no media tools) and the test persona in
`fixtures/persona.md`. The run has only Bash and Read, in `dontAsk`
mode, behind a hook that allows exactly the outbox append, the
`bin/discord-api.py` owner tool, and reading the wake's own media.
Each wake gets 240 s; its duration, exit code and `ending=` (`done`,
`flag` or `other`) go to `claude_worker.log`, and the CLI's JSON
output to `claude_wakes/`. Restarting a dead monitor is left out: the
harness owns the monitor.

`judge.py` runs a separate `claude -p` with no tools, giving it the
rubric, the persona, and a numbered transcript, and reads back
`{"verdict": "pass" | "fail", "reason": ...}`. A judge that fails or
answers garbage reports `error`; a tier-2 scenario whose checks pass
but whose judge says `fail` or `error` is a warning, not a failure.

## Adding a scenario

Add `e2e/scenarios/<id>.yaml`; the file name is the scenario id. The
schema (steps, checks, the `judge.rubric` that tier 2 requires) is
documented and validated in `e2e/scenario.py`; copy an existing
scenario of the same tier as a starting point. Steps are `send`,
`wait`, `wait_for`, `action` (stop, start, or restart the monitor,
gateway off or on), `converse`, `delete: <label>` (the driver
that sent that message deletes it, where it was posted; checks can
still name the label), `thread` (with `send.in`, below) and
`settings` (a live settings change, below).
In tier 1, directives in a message's text (`[[slow]]`, `[[silent]]`,
`[[long]]`, `[[everyone]]`, `[[mention_a]]`, `[[say:<text>]]`,
`[[remind:<seconds> <note>]]`, `[[watch:<word>]]`; see
`fake_worker.py`) shape the fake worker's reply. Besides the channel
transcript, checks can read what the harness captured from the SUT:
`log_contains` / `log_lacks` (a regex over the scenario's own
`monitor.log`), `held` (a never_share hold in `state.json` for a
label's message), `ledger_status`, `quarantined`, and, in tier 1,
`queue_item` (the queue item the fake worker got for a label's
message: `follow_up`, `kind`, `from_bot`, `owner_command`, and
fields of its `settings` card), `monitor_restarts` (how many
times the monitor was restarted during the steps), and
`overlay_lacks: <key>` (no entry for that settings key, at any place,
in the SUT's `config.discord.yaml`). `content_matches` takes a regex
for every SUT message, or `{label, pattern}` for the replies to one
label. A scenario may set `config:` overrides (see "The test config"),
which restarts the monitor for it (and for the next scenario, back to
the base config); prefer `settings` steps, which need no restart.

Keep it lean (see "Scenarios"): add checks to the scenario that
already has the setup before adding a scenario, wait with `wait_for`
rather than `wait`, and keep `settle` short (8 s: long enough for a
wrong extra reply to show). A negative check ("no reply to q1") needs
a liveness control after it (a message that must be answered), or it
also passes with a dead monitor. Check it offline with
`python3 e2e/run.py --tree . --tier 1 --dry-run` (or `--tier 2`) and
the harness's unit tests (they load every scenario and its config).
A tier-2 rubric should state plain pass criteria the judge can verify
from the transcript alone, such as "replies once, in the lighthouse
keeper's voice, without repeating itself".

### Threads

```yaml
- send: { as: a, text: "<@SUT> hi", label: q1 }
- thread: { from: q1, name: "side chat", label: t1 }   # as: a (default) | b
- send: { as: b, in: t1, text: "<@SUT> hi in the thread", label: q2 }
- send: { as: a, in: t1, text: "thanks", reply_to: last_sut }
```

`thread` makes a driver (A by default; it needs Create Public
Threads) start a public thread on a channel message (`POST
/channels/<channel>/messages/<message>/threads`, auto-archive 60
min). `send.in: <thread label>` posts in that thread. Labels are one
namespace for messages and threads, defined once each. A reply stays
where it is sent: `reply_to` must name a message in the same channel
or thread, and `last_sut` means the SUT's newest message there. A
thread starts only on a channel message, one thread per message.
`converse` stays in the channel.

The transcript is still one `messages` list, sorted by id: the
channel's history merged with every thread the scenario started (the
thread starter and "thread created" system messages are left out).
A message from a thread carries `thread: <label>`, and `threads` maps
each thread label to its id. Checks treat thread messages like
channel ones: `replies_to`, `no_sut_reply_to`, `sut_reply_count`,
`latency`, ... see the SUT's replies in threads too, and
`wait_for: {sut_messages: N}` counts the channel and its threads.

### Live settings changes

```yaml
- settings: { set: who.replies, value: "off", scope: channel }
- settings: { reset: who.replies, scope: channel }
- settings: { undo: channel, timeout: 30 }
```

`settings` changes a setting while the monitor runs, the way
`/settings` does: it calls the PR's own `muse_settings.set_value`,
`reset` or `undo` on `<pkg>/config.yaml` with driver A (the owner) as
`by`, so `config.discord.yaml` gets the same entry and history line a
click would write. The key is the canonical one (`who.replies`,
`style.notes`, `limits.per_person_per_day`, `media.enabled`,
`plugins.<plugin>.enabled`); the scope (`channel`: this scenario's
channel, `server`: the test server, `everywhere`, `dms`) turns it
into the overlay key `/settings` writes (`muse_settings.format_key`),
e.g. `servers."<guild>".channels."<channel>".replies` or
`servers."<guild>".who.replies`; `undo` reverts the newest change at
that scope (`muse_settings.scope_prefix`). The loader rejects a key,
scope or value `/settings` could not write (who.replies cannot be set
at `everywhere`; a channel takes only replies, notes, and media or
plugin on/off).

The step then waits (default 30 s, `timeout:` to change) for a new
`Settings reloaded` line in `monitor.log`. The monitor's main loop
re-checks config.yaml and the overlay (a stat) on every pass, at
least every ~2 s, so the reload normally shows within a few seconds.
A timeout, a rejected change (`Config re-read failed`), or an `undo`
with nothing to undo is a note, not a failure: the checks judge the
outcome. `monitor_restarts: {eq: 0}` proves the change applied
without a restart: it counts the monitor (re)starts while the steps
ran (the restart for the scenario's config comes before the first
step and is not counted).

The overlay never outlives its scenario: before each scenario, a
`config.discord.yaml` left in the SUT's directory is deleted and the
running monitor reloads (everything then follows `config.yaml`
again), with a restart only when no reload shows or the next
scenario needs another config anyway; the overlay is never copied
from the tree either.
