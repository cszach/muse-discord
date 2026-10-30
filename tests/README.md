# Offline test suites

Offline tests for the package: `monitor.py`, `presence.py`,
`watchdog.sh`, the hooks, `bin/` and the settings, watches and
proactive modules. No Discord and no network: API calls are stubbed,
the gateway is a fake `websockets` package, and everything the code
under test writes goes to a temp dir. CI runs them on every push and
pull request (`.github/workflows/ci.yml`). The end-to-end tests on a
real Discord server are in `e2e/` (see `e2e/README.md`).

## Running

```bash
bash tests/run_all.sh                    # every suite, against this checkout
bash tests/run_all.sh settings locking   # some suites
bash tests/run_all.sh /path/to/tree      # another tree (a dir with monitor.py)
bash tests/run_all.sh --skip-slow        # leave out the suites marked slow
bash tests/run_all.sh -j 8               # 8 suites at a time (default 4)
python3 tests/settings/test_settings.py -k NeverShare   # one file or test
MUT_DIR=/path/to/tree python3 tests/locking/test_locking.py
```

`run_all.sh` prints one line per suite (tests, seconds, result), then
the log of each suite that failed, and exits non-zero on a failure.
`MUT_DIR` is the tree under test (default: this checkout);
`MUSE_TESTS_KEEP_TMP=1` keeps the temp dirs.

Requirements: Python 3.10+ with PyYAML, `bash`, `git`, `jq` and
`unshare` (util-linux). The suites that run `watchdog.sh`
(`reliability`, `updatedm`, `fastrestart`) run it in a user + PID
namespace so its `pgrep` / `pkill` see only their fakes; on Ubuntu
24.04 that needs `sudo sysctl -w
kernel.apparmor_restrict_unprivileged_userns=0`. The security suite's
two video tests need `ffmpeg` and are skipped without it.

## What we test

A test earns its place by catching a regression that does real harm:

- wrong bot behaviour: replying when it should not, not replying, the
  wrong place or person, caps, ignore rules;
- security: prompt injection, the nonces that bind each output to its
  run, permission checks (one test per distinct attack; most came from
  adversarial reviews, named after them);
- data loss or corruption: the config overlay, locking, crash
  recovery, atomic writes;
- crashes, hangs and wedged loops;
- Discord rejecting a payload: character, component and custom_id
  limits (the `settingsui` crawler checks every page).

Not: wording, styling, formatting, labels or the order of text
(assert what happened, not how it was said: a refusal is "nothing was
written"); snapshots of UI copy; near-duplicates (extend a
table-driven test with `subTest`); what another test already covers;
trivial helpers and argument parsing; log lines, unless the line is
the behaviour (an owner alert, the watchdog's markers). No real
sleeps: patch the clock or shorten the interval.

## The suites

| Suite | Covers |
| --- | --- |
| `config` | Config resolution: defaults, nulls, zeros honored or clamped, out-of-range values; an old config's bare-name trigger never fires. |
| `security` | Generated files bound to their queue item, `bin/discord-api.py` file and id gating, safe media fetches (host allowlist, redirects, private addresses), injection quarantine for documents and reply context, `allowed_mentions`, ffmpeg input whitelists; secrets (the bot token, live nonces, disguised forms) never sent by any send path (`test_secrets.py`). |
| `locking` | Worker appends during a send never lost, concurrent appends sent once, no queue resurrection, atomic writes, malformed and corrupt outboxes, retries and crash leftovers, the typing thread, burst windows; the wake hook's item leases (`test_hook.py`); `bin/outbox-append.py`. |
| `reliability` | Token rejection, the burst ledger, gateway gap repair, discovery, reply references and send nonces, the gateway loops (`test_units.py`); `main()` boot and REST 401/429 runs (`test_main.py`); `watchdog.sh` liveness, restarts and alerts (`test_watchdog.py`). |
| `fastloop` (slow) | REST work (polls, sweeps, discovery, hung reads) never blocks the main loop; an outbox written mid-wait is sent promptly. |
| `updatedm` (slow) | Update notices: version fields from release tags (a local bare upstream), the owner DM and its buttons, `hooks/update-watch.sh`, the `UPDATING.md` commands. |
| `fastrestart` (slow) | The wake hook restarting a dead monitor (fast, silent, once, with backoff; never after a token rejection) and the `watchdog.sh` run lock, against fake processes. |
| `settings` | `muse_settings`: invalid files refused, scopes, the Discord overlay and its conflict rule, old layouts and keys still loading, live reload, the per-message card, the `never_share` backstop, who-rules, `testing.treat_as_human`, caps, follow-ups, media per scope, a plugin, the watchdog's settings reader. |
| `settingsengine` | Where writes land, what is risky (asks first), grouped writes and Undo, the no-op guard, the server-admin rule (quieter only, never past the owners; with a fuzz), `who.dms_from` and the DM gate, presets, pause, `bin/settings.py`, history trimming, the review findings. |
| `settingsplumb` | The `/settings` plumbing in `monitor.py`: command registration within Discord's limits, routing, acks while the main loop is blocked, writes under the lock, the stash, outcomes kept to their place, the admin-change digest, the join notice's Configure button, a missing `settings_ui.py`. |
| `settingsui` | The `/settings` pages: a crawl from every entry point (also with hostile names) checking every response against Discord's limits and for names that ping, mention or link; forged custom ids, stash tokens and a fuzz; the review findings; walkthroughs found by custom_id, never by label. |
| `presencelive` | `presence.py` following settings changes on the open gateway session: one Presence Update per change, rate limiting, invalid edits, disabling, heartbeats during slow re-reads. |
| `settingsnl` | Settings by DM (`settings_nl.py`): owners' own DMs only (forged, replayed, forwarded and stale actions refused), all or nothing, apply / confirm / cancel / expiry / Undo, the lockout guard, retries and restarts, notices for `bin/settings.py` changes. |
| `ownerdm` | Every output bound to its run: owner DMs (`owner_dm_nonce`), every item (`reply_nonce`) and owner commands (`bin/discord-api.py`); the wake hook hands each nonce only to its own wake; nonce lifetime and redaction; the review D, H, I and J repros. |
| `monitorfixes` | Daily-cap refusals dropped at once; declined messages never answered later by the sweep; new threads read from their start; Discord system notices never trigger. |
| `telemetry` | Usage stats (`telemetry.py`): no id, name, content, path or error text in a report (and the Worker's `validate.js` accepts it, when `node` is installed); off by setting or env sends nothing; only releases send; a hanging or failing endpoint never blocks the loop; one send a day, a failed one retried later that day with backoff; days unreachable tell the owners once; a send asked for by `bin/telemetry.py send`; proxy passwords never reach the log. The Worker's own tests: `cd telemetry/worker && node --test`. |
| `onboarding` | Setup: `bin/connect-discord.py` refuses wrong pastes and unsafe token files and writes nothing, keeps the token only in its mode-600 file, never breaks or overwrites the owner's config or persona, falls back to the portal steps; `bin/render-worker-prompt.py`; `bin/doctor.py`; an ownerless server answered by nobody; `hooks/watchdog-hook.sh`. Discord is a local fake. |
| `proactive` | Unprompted messages (`proactive.py`): off by default, the heartbeat and quiet hours, who and where is eligible, the slot nonce (fresh, unspent, its own targets only), caps, opt-out by the person only, a damaged store failing closed (review K), the wake hook. |
| `watches` | Reminders and watches (`muse_watches.py`, `watch_*` in `monitor.py`): fires once, on time, to the requester in place (also after a restart or downtime); schedules bound to their item (forgery, review I); poison or huge outbox items never wedging the outbox; watch privacy and debounce; caps and held fires; cancel; web watches from owners' DMs only, with one-time check nonces, the checker hook and `bin/watch-report.py` (`test_web.py`). |

The slow suites have a `SLOW` file saying why; `run_all.sh` starts
them first. The whole run takes about 30 s at 4 suites at a time.

## Layout

```text
tests/
  run_all.sh            the runner
  _lib/                 shared by the suites (not a suite)
    common.py           MUT_DIR, temp dirs, file redirection, git isolation
    monitor_harness.py  load monitor.py / presence.py with the API stubbed
    settings_harness.py the same, for the settings suites
    fakews/             a scripted fake `websockets` package
    fake_proc.py        stands in for monitor.py / presence.py
    hook_runtime.sh     stand-ins for the hatch hook runtime functions
  <suite>/test_*.py     one or more test files per suite
```

## Adding a suite

1. Make `tests/<name>/` with `test_*.py` files; `run_all.sh` finds
   every `tests/*/test_*.py` (directories starting with `_` are
   skipped).
2. Each file runs on its own (`MUT_DIR=<tree> python3
   tests/<name>/test_<x>.py`), uses `unittest` (the runner counts the
   `Ran N tests` line) and exits non-zero on failure.
3. Take the tree from `_lib`, never from a hardcoded path:

   ```python
   sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
       os.path.abspath(__file__))), "_lib"))
   from common import MUT_DIR, scratch  # noqa: E402
   ```

4. Never write into the tree or `tests/`: make temp dirs with
   `scratch()`, and call `redirect_files(mod, tmp)` after importing
   `monitor.py` yourself (`monitor_harness.load_monitor` and
   `settings_harness.load_monitor` do it for you). No network: stub
   `api_get` / `api_post`, and use `_lib/fakews` for the gateway.
5. Follow "What we test". A suite over about 10 s gets a `SLOW` file.
6. Check it: `bash tests/run_all.sh <name>`, `uvx ruff check --select
   E9,F tests`, and `shellcheck` on any shell scripts.
