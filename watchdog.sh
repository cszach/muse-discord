#!/bin/bash
# muse-discord watchdog: keeps the monitor and presence processes alive.
#
# Who runs it:
#   - A scheduler (cron, a scheduled agent job) every ~10 minutes: the
#     full run, and the only one whose output anyone reads. It is the
#     backstop for everything: presence restarts, stuck-heartbeat
#     restarts, alerts, update checks.
#   - The wake hook (hooks/mention-watch.sh), as `watchdog.sh
#     --from-hook`, within seconds of seeing the monitor process gone
#     (say, after the VM was replaced). Same checks and restarts, but
#     its output goes nowhere, so it leaves the alert state and the
#     update check to the scheduled run: a failure it hits is still
#     announced by the next scheduled run, and a recovery it makes is
#     reported there as "recovered".
#   - Runs never overlap: each holds an flock on watchdog.lock, so two
#     runs can never start two monitors (or two presence clients). A
#     scheduled run waits up to LOCK_WAIT_SECS for a running one to
#     finish; a hook run just leaves the work to the run in progress.
#
# Fully automatic: both processes read the bot token themselves from
# where token_from in config.yaml points (old layout: bot.token_file /
# bot.token_env): a token file (mode 600), or an environment variable,
# which must then be set in this script's own environment (the processes
# inherit it). This script never uses the token value; it only checks
# that one of the two is present. Settings are read through
# muse_settings.py, so both config layouts work.
#
# Output contract (for whatever scheduler runs this, e.g. cron):
#   - "OK ..." lines for healthy or recovered state.
#   - "ALERT: ..." printed ONLY on a NEW failure needing the operator's
#     attention. Repeat runs while the same failure persists print
#     "OK (already alerted)".
#   - "UPDATE: ..." printed ONCE per new release, a vX.Y.Z tag (checked
#     at most daily; updates.notify in config.yaml turns it off). Never an
#     alert, and the watchdog never applies updates: see UPDATING.md.
#   - State lives in watchdog_state.json and update_check.json; log in
#     watchdog.log.
#
# Health contract with the processes (do not change casually):
#   - monitor.py prints "Bot user id: <id>" once its identity is
#     confirmed, or "Bot identity pending" when /users/@me failed for a
#     reason that can heal (a 429 storm, the network) and it keeps
#     retrying in-loop. Either counts as a healthy start; both print
#     BEFORE its (slow) channel discovery.
#   - Either process prints "TOKEN REJECTED" and exits when Discord
#     refuses the token (REST 401 or gateway close 4004): the token alert.
#   - monitor.py prints "GATEWAY DISABLED" when realtime ingest stops for
#     good (e.g. the Message Content intent is off) while REST polling
#     carries on: alerted as degraded.
#   - monitor.py touches monitor.heartbeat from its main loop; a live
#     monitor whose heartbeat is older than HEARTBEAT_STALE_SECS is stuck
#     and gets restarted.
#   - presence.py prints a line containing "READY" once connected.
#   - Both are started fully detached (double-fork) so a scheduler
#     timeout killing this script's process tree cannot take them down.
#     The previous run's log is kept as <name>.log.1 on each restart.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The package's own Python: bin/install.sh makes .venv when pip cannot
# install into the system Python, and marks it .muse-ok once the
# packages import there. Every python3 below (and the monitor and
# presence it starts) then has the packages.
PIP_FLAGS=(--break-system-packages)
if [ -x "$DIR/.venv/bin/python3" ] && [ -e "$DIR/.venv/.muse-ok" ]; then
  PATH="$DIR/.venv/bin:$PATH"
  PIP_FLAGS=()
fi
LOG="$DIR/watchdog.log"
STATE="$DIR/watchdog_state.json"
CONFIG="$DIR/config.yaml"
FROM_HOOK=0
if [ "${1:-}" = "--from-hook" ]; then
  FROM_HOOK=1
fi

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# ---------------- one run at a time ----------------
# fd 9 holds an exclusive flock on watchdog.lock until this script
# exits. The lock belongs to the open file, which this shell keeps, so
# it outlives the python3 that takes it. Long-lived children must not
# inherit fd 9 (they would hold the lock forever): daemonize and git
# fetch close it.
LOCK_WAIT_SECS=300
lock_wait=$LOCK_WAIT_SECS
[ "$FROM_HOOK" = "1" ] && lock_wait=0
exec 9>>"$DIR/watchdog.lock"
if ! python3 - "$lock_wait" <<'PYEOF'
import fcntl, sys, time
deadline = time.time() + float(sys.argv[1])
while True:
    try:
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError:
        if time.time() >= deadline:
            sys.exit(1)
        time.sleep(1)
PYEOF
then
  if [ "$FROM_HOOK" = "1" ]; then
    log "wake hook asked for a restart; another watchdog run is in progress, leaving it to that run"
  else
    log "another watchdog run held the lock for ${LOCK_WAIT_SECS}s; skipping this run"
    echo "OK another watchdog run is still in progress; skipped this one"
  fi
  exit 0
fi
[ "$FROM_HOOK" = "1" ] && log "run triggered by the wake hook (monitor process not found)"

alive() { pgrep -f "^python3 $1" >/dev/null 2>&1; }
# The alert text and the path go in as arguments, never into the code.
get_state() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("last_alert",""))' "$STATE" 2>/dev/null || echo ""; }
set_state() { python3 -c 'import json,sys;json.dump({"last_alert":sys.argv[1]},open(sys.argv[2],"w"))' "$1" "$STATE" 2>/dev/null; }

# Read one setting (token_file, token_env, presence_enabled,
# updates_notify) the way the monitor does: through muse_settings.py,
# which reads both config layouts. Without that module next to this
# script, straight from the old layout's keys. Prints the fallback
# (arg 2) when it cannot tell (an invalid config: the monitor then
# refuses to start and says why in monitor.log).
setting() {
  python3 - "$DIR" "$CONFIG" "$1" "$2" <<'PYEOF' 2>/dev/null || echo "$2"
import os
import sys
d, cfg, name, fallback = sys.argv[1:5]
sys.path.insert(0, d)
try:
    import muse_settings
except ImportError:
    muse_settings = None
try:
    if muse_settings is not None:
        st = muse_settings.load(cfg, os.path.join(d, "plugins"))
        print(muse_settings.shell_value(st, name, d))
        sys.exit(0)
    import yaml
    with open(cfg) as f:
        cur = yaml.safe_load(f) or {}
    old = {"token_file": "bot.token_file", "token_env": "bot.token_env",
           "presence_enabled": "presence.enabled",
           "updates_notify": "updates.check"}[name]
    for part in old.split("."):
        cur = (cur or {}).get(part)
        if cur is None:
            break
    if cur is None:
        print(fallback)
    elif isinstance(cur, bool):
        print("true" if cur else "false")
    else:
        print(cur)
except SystemExit:
    raise
except Exception:
    print(fallback)
PYEOF
}

TOKEN_FILE="$(setting token_file "$HOME/.config/muse-discord/bot_token")"
TOKEN_FILE="${TOKEN_FILE/#\~/$HOME}"
TOKEN_ENV="$(setting token_env "")"
PRESENCE_ENABLED="$(setting presence_enabled true)"
HEARTBEAT="$DIR/monitor.heartbeat"
HEARTBEAT_STALE_SECS=600

# True when the processes will find a token: the token_from variable set
# in this environment (they inherit it), or a non-empty token file.
token_available() {
  if [[ "$TOKEN_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      && [ -n "${!TOKEN_ENV:-}" ]; then
    return 0
  fi
  [ -n "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ]
}
if [ -n "$TOKEN_ENV" ] && [ -n "$TOKEN_FILE" ]; then
  TOKEN_WHERE="env var \$$TOKEN_ENV unset and no token file at $TOKEN_FILE"
elif [ -n "$TOKEN_ENV" ]; then
  TOKEN_WHERE="env var \$$TOKEN_ENV unset"
else
  TOKEN_WHERE="no token file at $TOKEN_FILE"
fi

# Seconds since the monitor last touched its heartbeat, or -1 when there
# is no heartbeat file (a monitor from before heartbeats existed).
heartbeat_age() {
  python3 - "$HEARTBEAT" <<'PYEOF' 2>/dev/null || echo -1
import os, sys, time
print(int(time.time() - os.path.getmtime(sys.argv[1])))
PYEOF
}

# Stop a process: SIGTERM, up to 10s to exit, then SIGKILL.
# $1 = alive() pattern.
stop_proc() {
  pkill -f "^python3 $1" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive "$1" || return 0
    sleep 1
  done
  pkill -9 -f "^python3 $1" 2>/dev/null
  sleep 1
}

# Start a fully detached daemon via double-fork: the final process is
# reparented to PID 1 immediately, so it is NOT part of this script's
# process tree. (Plain `setsid ... &` leaves the child under this shell;
# when a scheduler's exec session times out, the runtime may kill the
# whole tree and the bot dies with it.)
# $1 = script (monitor.py/presence.py), $2 = log file. The previous log
# moves to $2.1 rather than being truncated: it holds the crash or token
# error that made this restart necessary.
daemonize() {
  if [ -f "$2" ]; then
    mv -f "$2" "$2.1"
  fi
  python3 - "$DIR" "$1" "$2" 9>&- <<'EOF'
import os, sys
d, script, logf = sys.argv[1], sys.argv[2], sys.argv[3]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(d)
si = open(os.devnull, 'r')
so = open(logf, 'a')
se = open(logf, 'a')
os.dup2(si.fileno(), 0)
os.dup2(so.fileno(), 1)
os.dup2(se.fileno(), 2)
# argv[0]='python3' keeps the pgrep "^python3 ..." health checks matching.
os.execv(sys.executable, ['python3', script])
EOF
}

# Poll for a healthy start, breaking early instead of sleeping a fixed time.
# $1 = alive() pattern, $2 = log marker (extended regex), $3 = log file,
# $4 = max seconds. A rejected token ends the wait at once.
wait_healthy() {
  local n=$(( ($4 + 4) / 5 ))
  for _ in $(seq 1 "$n"); do
    sleep 5
    if alive "$1" && grep -Eq "$2" "$3" 2>/dev/null; then
      return 0
    fi
    if grep -q "TOKEN REJECTED" "$3" 2>/dev/null; then
      return 1
    fi
  done
  return 1
}

ALERT_SIG=""
TOKEN_BAD=0

# Environment refreshes can wipe pip packages; reinstall BEFORE any
# restart so monitor's gateway thread (which imports websockets at
# thread start and never retries) comes up healthy too. If this check
# lived after the monitor restart, every refresh would leave the monitor
# running with a permanently dead gateway thread.
WEBSOCKETS_OK=1
if ! python3 -c "import websockets" 2>/dev/null; then
  log "websockets missing; reinstalling"
  if ! python3 -m pip install ${PIP_FLAGS[@]+"${PIP_FLAGS[@]}"} -q websockets >>"$LOG" 2>&1; then
    log "websockets reinstall failed"
    WEBSOCKETS_OK=0
  fi
fi
if ! python3 -c "import yaml" 2>/dev/null; then
  log "PyYAML missing; reinstalling"
  python3 -m pip install ${PIP_FLAGS[@]+"${PIP_FLAGS[@]}"} -q pyyaml >>"$LOG" 2>&1 || \
    log "PyYAML reinstall failed (monitor cannot load config without it)"
fi

# ---------------- monitor ----------------
MONITOR_HUNG=""
DEGRADED_SIG=""
if alive "[m]onitor.py"; then
  HB_AGE="$(heartbeat_age)"
  if [ "$HB_AGE" -ge "$HEARTBEAT_STALE_SECS" ]; then
    # The process lives but its main loop has not moved in 10+ minutes:
    # it answers nobody, and only a restart helps.
    log "monitor alive but its heartbeat is ${HB_AGE}s old (stuck); restarting"
    stop_proc "[m]onitor.py"
    MONITOR_HUNG="$HB_AGE"
  else
    echo "OK monitor alive"
    GW_OFF="$(grep -o -m1 'GATEWAY DISABLED ([^)]*)' "$DIR/monitor.log" 2>/dev/null)"
    if [ -n "$GW_OFF" ]; then
      DEGRADED_SIG="monitor degraded: $GW_OFF in monitor.log; realtime ingest is off, so replies wait for the REST poll. See TROUBLESHOOTING.md, then restart the monitor."
    fi
  fi
fi
if ! alive "[m]onitor.py"; then
  [ -z "$MONITOR_HUNG" ] && log "monitor not running; attempting restart"
  if ! token_available; then
    ALERT_SIG="monitor DOWN and $TOKEN_WHERE; cannot restart"
    log "ALERT: $ALERT_SIG"
  else
    daemonize monitor.py "$DIR/monitor.log"
    # The health window must comfortably exceed monitor startup:
    # the identity marker prints immediately, but give slow systems room.
    if wait_healthy "[m]onitor.py" "Bot user id|Bot identity pending" \
        "$DIR/monitor.log" 120; then
      NOTE=""
      if [ -n "$MONITOR_HUNG" ]; then
        NOTE=" (it was stuck: heartbeat ${MONITOR_HUNG}s old; its log is monitor.log.1)"
      fi
      if ! grep -q "Bot user id" "$DIR/monitor.log" 2>/dev/null; then
        NOTE="$NOTE (identity pending: Discord is rate limiting or unreachable; it keeps retrying)"
      fi
      log "monitor restarted OK$NOTE"
      echo "OK monitor restarted$NOTE"
    else
      stop_proc "[m]onitor.py"
      if grep -q "TOKEN REJECTED" "$DIR/monitor.log" 2>/dev/null; then
        TOKEN_BAD=1
        ALERT_SIG="monitor DOWN: Discord rejected the token; it was probably reset. Reset it again on the bot's Developer Portal page and give the new one to your Muse to connect; the watchdog recovers on its next run."
      else
        ALERT_SIG="monitor DOWN: restart attempted but the process is not healthy; check $DIR/monitor.log"
      fi
      log "ALERT: $ALERT_SIG"
    fi
  fi
fi

# ---------------- presence ----------------
if [ "$PRESENCE_ENABLED" != "true" ]; then
  echo "OK presence disabled in config"
elif alive "[p]resence.py"; then
  echo "OK presence alive"
else
  if [ "$TOKEN_BAD" = "1" ]; then
    log "skipping presence restart (token already known bad)"
  else
    log "presence not running; attempting restart"
    # websockets already reinstalled above (before the monitor section);
    # fail here only if that reinstall failed.
    if [ "$WEBSOCKETS_OK" != "1" ]; then
      ALERT_SIG="presence DOWN: could not reinstall the websockets package"
      log "ALERT: $ALERT_SIG"
    fi
    if [ -z "$ALERT_SIG" ]; then
      if ! token_available; then
        ALERT_SIG="presence DOWN and $TOKEN_WHERE; cannot restart"
        log "ALERT: $ALERT_SIG"
      else
        daemonize presence.py "$DIR/presence.log"
        if wait_healthy "[p]resence.py" "READY" "$DIR/presence.log" 60; then
          log "presence restarted OK"
          echo "OK presence restarted"
        else
          stop_proc "[p]resence.py"
          if grep -q "TOKEN REJECTED" "$DIR/presence.log" 2>/dev/null; then
            ALERT_SIG="presence DOWN: Discord rejected the token; it was probably reset. Reset it again on the bot's Developer Portal page and give the new one to your Muse to connect; the watchdog recovers on its next run."
          else
            ALERT_SIG="presence DOWN: restart attempted but no READY in presence.log; check $DIR/presence.log"
          fi
          log "ALERT: $ALERT_SIG"
        fi
      fi
    fi
  fi
fi

# A hook-triggered run has no reader for its output. It leaves the alert
# state and the update check to the scheduled run, which then announces
# a failure this run hit (as a new alert) or a recovery it made.
if [ "$FROM_HOOK" = "1" ]; then
  [ -n "$ALERT_SIG" ] && log "hook-triggered run: leaving this alert for the scheduled run to announce"
  exit 0
fi

# ---------------- transition-based alerting ----------------
# A degraded-but-running monitor alerts only when nothing is down.
if [ -z "$ALERT_SIG" ] && [ -n "$DEGRADED_SIG" ]; then
  ALERT_SIG="$DEGRADED_SIG"
  log "ALERT: $ALERT_SIG"
fi
if [ -n "$ALERT_SIG" ]; then
  if [ "$(get_state)" != "$ALERT_SIG" ]; then
    set_state "$ALERT_SIG"
    echo "ALERT: $ALERT_SIG"
  else
    echo "OK already alerted (still failing): $ALERT_SIG"
  fi
else
  if [ -n "$(get_state)" ]; then
    set_state ""
    log "recovered; alert state cleared"
    echo "OK recovered; previous alert cleared"
  fi
fi

# ---------------- updates ----------------
# At most once a day, look for a new muse-discord RELEASE and print ONE
# "UPDATE:" line per new version. A release is a semver tag vX.Y.Z on
# origin (pre-release tags like v1.2.0-rc.1 and other tags are ignored).
# The installed version is the newest release tag HEAD contains, or
# "unreleased" before the first one. Tags are listed with
# `git ls-remote` (not the GitHub API, whose unauthenticated rate limit
# is per IP and shared on hosted VMs); only the newest release's tag is
# fetched. Never merges: applying an update is the owner's call, and
# their Muse does it by following UPDATING.md. Skipped quietly for
# installs that are not a git clone with an origin.
# Each check also records the version in update_check.json for the
# monitor, which DMs the owners once per new version with [Update now]
# / [Later] buttons (PROTOCOL.md, "Update notices"): latest (the new
# release's tag, "" when nothing is new), installed, entries (headings
# of that release's CHANGELOG entries newer than installed, newest
# first) and action_needed (true when one of them lists something to
# do).
UPDATE_STATE="$DIR/update_check.json"
upd_get() {
  python3 - "$UPDATE_STATE" "$1" <<'PYEOF' 2>/dev/null || echo ""
import json, sys
print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
PYEOF
}
# upd_py COMMAND ARGS: the version logic.
#   installed                 newest release tag HEAD contains, or
#                             "unreleased"
#   latest REFS               newest release in REFS (`git ls-remote
#                             --tags` output) newer than installed and
#                             not already in HEAD, as "TAG COMMIT";
#                             nothing when there is none
#   record CHECKED ANNOUNCED [LATEST INSTALLED]
#                             rewrite update_check.json atomically. With
#                             LATEST, also record that release's entries
#                             (from CHANGELOG.md at its tag); with
#                             LATEST empty, clear them. With no third
#                             argument (the check failed), keep them.
UPD_PY=$(cat <<'PYEOF'
import json, os, re, subprocess, sys, tempfile
d, path, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
SEMVER = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def ver(tag):
    m = SEMVER.fullmatch(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def git(*a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True,
                          text=True, timeout=30)


def installed():
    tags = git("tag", "--merged", "HEAD", "--list", "v*").stdout.split()
    tags = [t for t in tags if ver(t)]
    return max(tags, key=ver) if tags else "unreleased"


def entries(text):
    out = []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append((line[3:].strip(), []))
        elif out:
            out[-1][1].append(line)
    return out


def action_needed(body):
    m = re.search(r"\*\*Action needed:?\*\*:?[ \t]*\n(.*)", "\n".join(body),
                  re.S)
    items = [ln.strip()[2:] for ln in (m.group(1) if m else "").splitlines()
             if ln.strip().startswith("- ")]
    return any(not i.lower().startswith("none") for i in items)


if cmd == "installed":
    print(installed())
elif cmd == "latest":
    commits = {}
    for line in args[0].splitlines():
        sha, _, ref = line.partition("\t")
        name = ref[len("refs/tags/"):]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if ver(name) and (peeled or name not in commits):
            commits[name] = sha  # an annotated tag's commit is its ^{}
    if commits:
        tag = max(commits, key=ver)
        have = ver(installed())
        contained = git("merge-base", "--is-ancestor", commits[tag],
                        "HEAD").returncode == 0
        if (have is None or ver(tag) > have) and not contained:
            print(tag, commits[tag])
elif cmd == "record":
    try:
        with open(path) as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    for k in ("upstream", "behind"):  # pre-release fields
        cur.pop(k, None)
    cur.update(checked=int(args[0]), announced=args[1])
    if len(args) > 3:
        latest, have = args[2], args[3]
        new = []
        if latest:
            text = git("show", f"refs/tags/{latest}:CHANGELOG.md").stdout
            for title, body in entries(text):
                v = ver(re.split(r"[\s:(]", title, maxsplit=1)[0])
                if v and v <= ver(latest) and (
                        v > ver(have) if ver(have) else v == ver(latest)):
                    new.append((title, body))
        cur.update(latest=latest, installed=have,
                   entries=[t for t, _ in new],
                   action_needed=any(action_needed(b) for _, b in new))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".update_check.json.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, path)
PYEOF
)
upd_py() { python3 -c "$UPD_PY" "$DIR" "$UPDATE_STATE" "$@" 2>/dev/null; }
if [ "$(setting updates_notify true)" = "true" ] \
    && git -C "$DIR" remote get-url origin >/dev/null 2>&1; then
  now=$(date +%s)
  last_check="$(upd_get checked)"
  announced="$(upd_get announced)"
  if [ $(( now - ${last_check:-0} )) -ge 86400 ]; then
    to=""
    command -v timeout >/dev/null 2>&1 && to="timeout 60"
    installed=$(upd_py installed || echo unreleased)
    if refs=$(GIT_TERMINAL_PROMPT=0 $to git -C "$DIR" ls-remote --tags \
        origin 2>>"$LOG" 9>&-); then
      read -r latest _ <<<"$(upd_py latest "$refs")"
      if [ -z "$latest" ]; then
        upd_set_args=("" "$installed")  # nothing new
      elif GIT_TERMINAL_PROMPT=0 $to git -C "$DIR" fetch --quiet --no-tags \
          origin "+refs/tags/$latest:refs/tags/$latest" >>"$LOG" 2>&1 9>&-; then
        upd_set_args=("$latest" "$installed")
        if [ "$latest" != "$announced" ]; then
          log "update available: $latest (installed: $installed)"
          echo "UPDATE: muse-discord $latest is out (installed: $installed). Ask your Muse to update it by following UPDATING.md."
          announced="$latest"
        fi
      else
        log "update check: fetching tag $latest failed; retrying tomorrow"
        upd_set_args=()
      fi
    else
      log "update check: listing upstream tags failed; retrying tomorrow"
      upd_set_args=()
    fi
    upd_py record "$now" "$announced" \
      ${upd_set_args[@]+"${upd_set_args[@]}"}
  fi
fi
