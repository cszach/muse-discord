#!/usr/bin/env bash
# muse-discord mention watcher (the reply worker's wake hook).
# Polls the queue the monitor writes (zero tokens: no network, no
# secrets of its own) and wakes a worker only for open items that no
# live lease covers:
# - Leases live in hook state (no lock file to forget). A wake carries
#   only unleased items, so a message arriving while a worker is busy
#   wakes a second worker for just that message; an item still open
#   after LEASE_SECS (its worker died) is handed out again. Items with a
#   valid outbox entry are never handed out.
# - Capabilities: owner DMs (and fires in them), a heartbeat slot, and
#   an owner's server item holding a command or web watch nonce each
#   wake alone, and only that wake gets their nonces; every item's
#   reply_nonce goes only to the wake holding its lease (PROTOCOL.md).
# - The payload carries the items and the persona text, so the worker's
#   happy path is one append per item.
# - Monitor liveness: when the monitor process is gone (the VM was
#   replaced, say), it starts `watchdog.sh --from-hook` fully detached,
#   with a backoff in hook state (at once, then after 1, 2 and 5
#   minutes, then every 10) and never after a token rejection: the
#   scheduled watchdog run owns that, and everything else.
# It targets Muse's hook runtime ($HATCH_HOOK_RUNTIME; SETUP.md, step 5).
# Other runtimes can poll reply_queue.json themselves (PROTOCOL.md).
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

# The package dir (this hooks/ dir's parent, unless overridden).
PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The package's own Python (bin/install.sh may have made a .venv, and
# marks it .muse-ok once the packages import there).
if [ -x "$PKG_DIR/.venv/bin/python3" ] && [ -e "$PKG_DIR/.venv/.muse-ok" ]; then
    PATH="$PKG_DIR/.venv/bin:$PATH"
fi
# Proof for bin/doctor.py that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-mention-watch" 2>/dev/null || true
QUEUE="$PKG_DIR/reply_queue.json"
OUTBOX="$PKG_DIR/reply_outbox.json"
SENDING="$PKG_DIR/reply_outbox.processing.json"
LEASE_SECS=600

# Hook state (one JSON object): .woken holds the item leases, .restart
# the monitor-restart backoff ({next, fails}).
state=$(hook_state_get 2>/dev/null \
    | jq -c 'if type == "object" then . else {} end' 2>/dev/null) \
    || state='{}'
[ -n "$state" ] || state='{}'
printf -v now '%(%s)T' -1

# ---- Monitor liveness (zero tokens; see the header) ----
# Alive when monitor.pid names a live monitor.py (read without touching
# its flock: probing the lock could make a monitor that is starting
# right then think another one holds it), else when watchdog.sh's own
# pgrep finds one.
monitor_alive() {
    local pid="" arg1=""
    IFS= read -r pid 2>/dev/null < "$PKG_DIR/monitor.pid" || true
    if [[ $pid =~ ^[0-9]+$ ]] \
        && { IFS= read -r -d '' _ && IFS= read -r -d '' arg1; } \
            2>/dev/null < "/proc/$pid/cmdline" \
        && [[ $arg1 == *monitor.py ]]; then
        return 0
    fi
    pgrep -f '^python3 [m]onitor.py' >/dev/null 2>&1
}

# True after a token rejection: the scheduled watchdog run handles that
# (and alerts), so the hook never retries a refused token on its own.
token_rejected() {
    if tail -c 4096 "$PKG_DIR/monitor.log" 2>/dev/null \
        | grep -q "TOKEN REJECTED"; then
        return 0
    fi
    grep -qs "rejected the token" "$PKG_DIR/watchdog_state.json"
}

# Run watchdog.sh --from-hook fully detached (double fork, new session,
# every inherited descriptor closed, stdio on /dev/null): this script
# returns at once, never waits on it, and a hook-runtime timeout that
# kills this script's process tree cannot take the restart down.
start_watchdog() {
    python3 - "$PKG_DIR" >/dev/null 2>&1 <<'PYEOF'
import os
import sys
d = sys.argv[1]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(d)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
try:
    fds = [int(x) for x in os.listdir("/proc/self/fd")]
except OSError:
    fds = range(3, 1024)
for fd in fds:
    if fd > 2:
        try:
            os.close(fd)
        except OSError:
            pass
os.execvp("bash", ["bash", os.path.join(d, "watchdog.sh"), "--from-hook"])
PYEOF
}

if [ -f "$PKG_DIR/watchdog.sh" ]; then
    rs_next=0
    rs_fails=0
    if [[ $state == *'"restart"'* ]]; then
        read -r rs_next rs_fails < <(printf '%s' "$state" \
            | jq -r '.restart | "\(.next // 0) \(.fails // 0)"' \
            2>/dev/null) || true
        [[ $rs_next =~ ^[0-9]+$ ]] || rs_next=0
        [[ $rs_fails =~ ^[0-9]+$ ]] || rs_fails=0
    fi
    restart=""
    if monitor_alive; then
        # Up for a whole backoff window: reset the backoff.
        if [ "$rs_next" -gt 0 ] && [ "$now" -ge "$rs_next" ]; then
            restart=null
            log "monitor is running again" '{"attempts":"'"$rs_fails"'"}'
        fi
    elif [ "$now" -ge "$rs_next" ]; then
        if token_rejected; then
            restart="{\"next\":$((now + 600)),\"fails\":$rs_fails}"
            log "monitor down after a token rejection; leaving it to the scheduled watchdog run" '{}'
        else
            rs_fails=$((rs_fails + 1))
            case $rs_fails in
                1) wait_secs=60 ;;
                2) wait_secs=120 ;;
                3) wait_secs=300 ;;
                *) wait_secs=600 ;;
            esac
            restart="{\"next\":$((now + wait_secs)),\"fails\":$rs_fails}"
            if start_watchdog; then
                log "monitor not running; started watchdog.sh" \
                    '{"attempt":"'"$rs_fails"'","retry_after_secs":"'"$wait_secs"'"}'
            else
                log "monitor not running; could not start watchdog.sh" \
                    '{"attempt":"'"$rs_fails"'"}'
            fi
        fi
    fi
    if [ -n "$restart" ]; then
        state=$(printf '%s' "$state" | jq -c --argjson r "$restart" \
            'if $r == null then del(.restart) else .restart = $r end')
        hook_state_set "$state"
    fi
fi

# No queue file yet -> nothing to do
if [ ! -f "$QUEUE" ]; then
    silent "no queue file" '{}'
fi

# The worker's answers so far, read BEFORE the queue: the monitor removes
# an answered item from the queue before it drops the item's outbox
# entry, so this order never sees one without the other. Each is the
# ids it names, the owner-DM nonce and the reply nonce it carries.
answered='[]'
for f in "$OUTBOX" "$SENDING"; do
    [ -f "$f" ] || continue
    ids=$(jq -c '[.[]? | objects | {ids: [(.item, .reply_to) | strings],
        nonce: ([.owner_dm_nonce, (.settings | objects | .nonce)]
            | map(strings) | first),
        reply: ((.reply_nonce | strings) // null)}]' "$f" 2>/dev/null) \
        || ids='[]'
    answered=$(jq -cn --argjson a "$answered" --argjson b "$ids" '$a + $b')
done

# The queue, then the owner-DM nonces (the monitor's private file; see
# the header). In that order: the monitor writes an owner DM's nonce
# before it queues the DM, so every owner DM read here has its nonce
# unless the monitor has yet to mint it (then it waits, below). Read to
# tell a real answer to an owner DM from a forged one; they reach a
# payload only in an owner-DM-only wake.
# An owner's own DM, a reminder or web watch firing in one, or a
# heartbeat slot (kind "proactive"), as jq: prefixed to the filters below
# that hand them out alone and with their nonce.
OWNER_DM='def owner_dm: ((.kind == "dm" or ((.kind == "reminder"
    or .kind == "watch-met") and (.settings.where? // "") == "dm"))
    and (.settings.speaker? // "") == "owner") or .kind == "proactive";'
queue=$(jq -c 'if type == "array" then . else [] end' "$QUEUE" \
    2>/dev/null) || queue='[]'
[ -n "$queue" ] || queue='[]'
all_nonces='{}'
if [ -f "$PKG_DIR/owner_dm_nonces.json" ]; then
    all_nonces=$(jq -c 'if type == "object" then . else {} end' \
        "$PKG_DIR/owner_dm_nonces.json" 2>/dev/null) || all_nonces='{}'
    [ -n "$all_nonces" ] || all_nonces='{}'
fi
# A live heartbeat slot's nonce: its own file (never mistaken for an
# owner DM's), used here exactly like one.
if [ -f "$PKG_DIR/proactive_nonce.json" ]; then
    slot_nonce=$(jq -c 'if type == "object" then with_entries(select(
        .value | type == "string")) else {} end' \
        "$PKG_DIR/proactive_nonce.json" 2>/dev/null) || slot_nonce='{}'
    [ -n "$slot_nonce" ] || slot_nonce='{}'
    all_nonces=$(jq -cn --argjson a "$all_nonces" --argjson b "$slot_nonce" \
        '$a + $b')
fi
# The owner-command nonces (the monitor's private owner_items.json,
# written before the item is queued): handed only to an owner-only wake.
# No file at all: a monitor from before it existed (nothing waits).
cmd_nonces='{}'
cmd_file=0
if [ -f "$PKG_DIR/owner_items.json" ]; then
    cmd_file=1
    cmd_nonces=$(jq -c 'if type == "object" then map_values(
        if type == "object" then (.nonce | strings) else strings end)
        else {} end' "$PKG_DIR/owner_items.json" 2>/dev/null) \
        || cmd_nonces='{}'
    [ -n "$cmd_nonces" ] || cmd_nonces='{}'
fi
# Every item's reply_nonce (the monitor's private item_nonces.json,
# written before the item is queued): every outbox item must echo its
# item's, so only the wake holding an item's lease gets it, and only an
# outbox entry carrying it counts as answering the item. No file at
# all: a monitor from before it existed.
reply_nonces='{}'
reply_file=0
if [ -f "$PKG_DIR/item_nonces.json" ]; then
    reply_file=1
    reply_nonces=$(jq -c 'if type == "object" then . else {} end' \
        "$PKG_DIR/item_nonces.json" 2>/dev/null) || reply_nonces='{}'
    [ -n "$reply_nonces" ] || reply_nonces='{}'
fi
# The web watch capabilities ({message_id: web_nonce}, the monitor's
# private file): an item whose author may set web watches is handed out
# alone, with its web_nonce, so nobody else's text shares that run.
web_nonces='{}'
if [ -f "$PKG_DIR/web_nonces.json" ]; then
    web_nonces=$(jq -c 'if type == "object" then . else {} end' \
        "$PKG_DIR/web_nonces.json" 2>/dev/null) || web_nonces='{}'
    [ -n "$web_nonces" ] || web_nonces='{}'
fi

# Open items: queued and not answered (a burst may be answered under any
# of its fragments). An item counts as answered only by an entry that
# carries its reply_nonce, and an owner DM only by one that also carries
# its owner-DM nonce: anything else in the outbox naming it is a forgery
# the monitor refuses, and must not keep the item from its own wake.
open=$(printf '%s' "$queue" | jq -c --argjson answered "$answered" \
    --argjson nonces "$all_nonces" --argjson rn "$reply_nonces" \
    "$OWNER_DM"'[.[]? | objects | select(.message_id != null)
    | ($nonces[.message_id | tostring]) as $n
    | (if .kind == "proactive" then null
       else $rn[.message_id | tostring] end) as $r
    | owner_dm as $owner
    | select(([.message_id] + [(.burst_fragments // [])[]? | .message_id])
        as $ids | [$answered[] | select(.ids as $a
            | any($ids[]; . as $i | any($a[]; . == $i)))
          | select(if $n != null then .nonce == $n else ($owner | not)
              end)
          | select($r == null or .reply == $r)] | length == 0)]') \
    || open='[]'
count=$(printf '%s' "$open" | jq 'length')
if [ "$count" -eq 0 ]; then
    silent "queue empty" '{}'
fi

woken=$(printf '%s' "$state" | jq -c '.woken // {}' 2>/dev/null) \
    || woken='{}'

# Due: open items with no live lease.
due=$(printf '%s' "$open" | jq -c --argjson w "$woken" --argjson now "$now" \
    --argjson lease "$LEASE_SECS" \
    '[.[] | select((($w[.message_id] // 0) + $lease) <= $now)]')
# An owner DM without its nonce yet (the monitor mints one within
# seconds) waits, unleased: a run handed it could not answer it.
nonceless=$(printf '%s' "$due" | jq --argjson nonces "$all_nonces" \
    "$OWNER_DM"'[.[] | select(owner_dm
        and $nonces[.message_id | tostring] == null)] | length')
if [ "$nonceless" -gt 0 ]; then
    log "owner DMs wait for their nonce" '{"items":"'"$nonceless"'"}'
    due=$(printf '%s' "$due" | jq -c --argjson nonces "$all_nonces" \
        "$OWNER_DM"'[.[] | select((owner_dm
            and $nonces[.message_id | tostring] == null) | not)]')
fi
# (A heartbeat slot has no reply_nonce: its slot nonce, above, binds
# it, and only an entry carrying that counts as answering it.)
# Likewise an item whose reply_nonce, or an owner's command whose
# owner_command_nonce, is not written yet: for at most NONCE_WAIT_SECS
# after it was queued (the monitor's upkeep mints a missing one within
# seconds). Past that it is handed out anyway (an owner command without
# owner powers), so a mangled file or an author who is no longer an
# owner never leaves it waiting for good.
NONCE_WAIT_SECS=60
waiting=$(printf '%s' "$due" | jq -c --argjson c "$cmd_nonces" \
    --argjson rn "$reply_nonces" --argjson cf "$cmd_file" \
    --argjson rf "$reply_file" --argjson now "$now" \
    --argjson lim "$NONCE_WAIT_SECS" '[.[] | select(
        ($now - ((.queued_at // 0) | if type == "number" then . else 0
            end)) < $lim
        and (($rf == 1 and .kind != "proactive"
                and $rn[.message_id | tostring] == null)
            or ($cf == 1 and .owner_command == true
                and $c[.message_id | tostring] == null)))
        | .message_id]')
if [ "$waiting" != "[]" ]; then
    log "items wait for their nonce" \
        '{"items":"'"$(printf '%s' "$waiting" | jq 'length')"'"}'
    due=$(printf '%s' "$due" | jq -c --argjson w "$waiting" \
        '[.[] | select(.message_id as $m | $w | index($m) | not)]')
fi
ndue=$(printf '%s' "$due" | jq 'length')
if [ "$ndue" -eq 0 ]; then
    silent "a worker already holds every open item" \
        '{"open":"'"$count"'"}'
fi
# Owner DMs wake alone: a run that handles an owner's DM (which may ask
# to change settings) never also reads a server message or anyone
# else's, so text written by someone else can never share a context
# with an owner's settings request. The rest are handed out on the
# next run, a few seconds later (they keep no lease meanwhile).
# The owner's own messages, a heartbeat slot, and the bot's notices to
# the owners (reminders, watches: they carry text from elsewhere, such as
# a web page's summary) never share a run either: real DMs first, else
# one slot alone, else the notices.
owner_dms=$(printf '%s' "$due" | jq -c "$OWNER_DM"'[.[] | select(owner_dm)]
    | if any(.[]; .kind == "dm") then map(select(.kind == "dm"))
      elif any(.[]; .kind == "proactive")
      then map(select(.kind == "proactive"))[:1] else . end')
nowner=$(printf '%s' "$owner_dms" | jq 'length')
if [ "$nowner" -gt 0 ] && [ "$nowner" -lt "$ndue" ]; then
    log "owner DMs wake alone; the rest wait for the next run" \
        '{"owner_dms":"'"$nowner"'","held":"'"$((ndue - nowner))"'"}'
    due=$owner_dms
    ndue=$nowner
fi
# Then, one at a time, an owner's server item that holds a capability
# beyond answering itself: a command (owner_command_nonce:
# bin/discord-api.py) or a web watch capability (web_nonce; web watches
# are the owners' only). Each wakes alone, so no other item's text
# (another item's history included, even the same owner's) shares the
# run that holds it. The rest wait for the next run, a few seconds
# later.
ncmd=0
if [ "$nowner" -eq 0 ]; then
    owner_cmds=$(printf '%s' "$due" | jq -c --argjson wn "$web_nonces" \
        '[.[] | select(.owner_command == true
            or ((.settings.speaker? // "") == "owner"
                and $wn[.message_id | tostring] != null))][:1]')
    ncmd=$(printf '%s' "$owner_cmds" | jq 'length')
    if [ "$ncmd" -gt 0 ] && [ "$ncmd" -lt "$ndue" ]; then
        log "an owner's command wakes alone; the rest wait for the next run" \
            '{"held":"'"$((ndue - ncmd))"'"}'
    fi
    if [ "$ncmd" -gt 0 ]; then
        due=$owner_cmds
        ndue=$ncmd
    fi
fi
# A wake of owner DMs only carries each item's owner_dm_nonce (the
# capability every outbox item for an owner DM must echo; PROTOCOL.md).
# The monitor keeps the nonces out of the queue file, in a private file
# only this hook reads, so a worker woken for anything else never sees
# one.
nonces='{}'
if [ "$nowner" -gt 0 ] && [ "$nowner" -eq "$ndue" ]; then
    nonces=$all_nonces
fi
# Each owner command's owner_command_nonce, only in a wake that holds
# nothing but owner items (owner DMs, or that one item); a web_nonce only
# in the wake of its own (owner's) item alone. (An owner DM's
# owner_dm_nonce does the web nonce's job already.)
cmds='{}'
wnonces='{}'
if [ "$nowner" -eq "$ndue" ] || [ "$ncmd" -eq "$ndue" ]; then
    cmds=$cmd_nonces
fi
if [ "$ncmd" -gt 0 ] && [ "$ncmd" -eq "$ndue" ]; then
    wnonces=$web_nonces
fi
expired=$(printf '%s' "$due" | jq --argjson w "$woken" \
    '[.[] | select($w[.message_id] != null)] | length')
if [ "$expired" -gt 0 ]; then
    log "re-waking: item lease expired" '{"items":"'"$expired"'"}'
fi

# Leases: keep the ones of items still queued (answered ones too, in case
# their outbox entry is briefly unreadable), stamp the items this wake
# hands out. The rest of the hook state (.restart) is kept as is.
# hook_state_set is a no-op on dry runs, so testing never consumes a detection.
queued=$(jq -c '[.[]? | objects | .message_id | strings]' "$QUEUE" \
    2>/dev/null) || queued='[]'
leases=$(jq -cn --argjson ids "$queued" --argjson due "$due" \
    --argjson w "$woken" --argjson now "$now" --argjson st "$state" '
    $st + {woken: (($w | with_entries(select(.key as $k | $ids | index($k))))
        + ($due | map({key: .message_id, value: $now}) | from_entries))}')
hook_state_set "$leases"

# The persona text (persona in config.yaml, old layout persona.file,
# relative to the package dir; default persona.md), so the worker need
# not read it. null when unreadable: the worker then reads the file
# itself.
CONFIG="${MUSE_DISCORD_CONFIG:-$PKG_DIR/config.yaml}"
persona_file=$(python3 - "$CONFIG" "$PKG_DIR" 2>/dev/null <<'PY'
import os
import sys
try:
    import yaml
    with open(sys.argv[1]) as f:
        name = (yaml.safe_load(f) or {}).get("persona")
    if isinstance(name, dict):
        name = name.get("file")
    if not isinstance(name, str):
        name = None
except Exception:
    name = None
print(os.path.join(sys.argv[2], os.path.expanduser(name or "persona.md")))
PY
) || persona_file="$PKG_DIR/persona.md"

# Usage counts (telemetry.py): one line per wake, once a monitor that
# keeps them (telemetry.json) has run; it counts and removes the file.
[ -e "$PKG_DIR/telemetry.json" ] \
    && { echo >> "$PKG_DIR/telemetry.wakes"; } 2>/dev/null || true

if [ -r "$persona_file" ]; then
    persona_args=(--rawfile persona "$persona_file")
else
    persona_args=(--argjson persona null)
fi

payload=$(printf '%s' "$due" | jq -c "${persona_args[@]}" --argjson nonces "$nonces" --argjson cmds "$cmds" --argjson wn "$wnonces" --argjson rn "$reply_nonces" '{count: length, persona: $persona, items: [.[] | {message_id, channel_id, channel_name, kind, author, author_id, content, media, documents, unviewable, history, may_generate, owner_command, reply_to, burst, burst_fragments, from_bot, bot_turns, participants, follow_up, settings, watches, reminder, proactive} + (if $nonces[.message_id] then {owner_dm_nonce: $nonces[.message_id]} else {} end) + (if .owner_command == true and $cmds[.message_id] then {owner_command_nonce: $cmds[.message_id]} else {} end) + (if $wn[.message_id] then {web_nonce: $wn[.message_id]} else {} end) + (if $rn[.message_id] then {reply_nonce: $rn[.message_id]} else {} end)]}')
wake "muse-discord mentions pending" "$payload"
