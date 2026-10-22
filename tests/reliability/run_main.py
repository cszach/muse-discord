"""Run monitor.main() offline for one scenario and print a JSON result:
{"exit": code|"stopped", "out": stdout, ...}. main() is stopped as soon
as the scenario's `until` holds (checked at the end of each loop pass),
or by the alarm."""
import contextlib
import io
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
import monitor_harness as H  # noqa: E402
from monitor_harness import G1, BOT, msg, chan, HTTPErr  # noqa: E402

scenario = sys.argv[1]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5


class Stop(BaseException):
    pass


mod = H.load_monitor()
extra = {}
calls = {"list_dm_channels": 0, "sweep_media": 0}

# No real gateway: a stub thread target that can poke the status dict.
gw_action = {"fn": None}


def fake_gw(token, inbox, status):
    if gw_action["fn"]:
        gw_action["fn"](inbox, status)


mod._gateway_thread_main = fake_gw
_orig_ldc = mod.list_dm_channels


def counting_ldc():
    calls["list_dm_channels"] += 1
    return _orig_ldc()


mod.list_dm_channels = counting_ldc
_orig_sm = mod.sweep_media


def counting_sm():
    calls["sweep_media"] += 1
    return _orig_sm()


mod.sweep_media = counting_sm
me = {"id": BOT, "username": "musebot"}
mod.api.routes["/users/@me"] = me
mod.BOT_ID = mod.BOT_USER_ID = None  # main() fetches it
until = None  # () -> True once the scenario has seen enough
one_channel = ({"C1": chan()}, {G1: {"name": "Srv",
                                     "system_channel_id": None,
                                     "joined_at": None}})


def write_state(st):
    st.setdefault("guild_joined", {G1: 0})
    st.setdefault("known_guilds", {G1: "Srv"})
    with open(mod.STATE_FILE, "w") as f:
        json.dump(st, f)


if scenario == "boot401":
    mod.api.errors["/users/@me"] = HTTPErr(401)

    def stop_discovery(previous=None):
        raise Stop()
    mod.discover_channels = stop_discovery
elif scenario == "boot429":
    mod.api.errors["/users/@me"] = HTTPErr(429, {"Retry-After": "1"})

    def stop_discovery(previous=None):
        raise Stop()
    mod.discover_channels = stop_discovery
elif scenario == "gw4004":
    mod.discover_channels = lambda previous=None: one_channel

    def act(inbox, status):
        time.sleep(0.5)
        status["token_rejected"] = True
        status["wake"].set()
    gw_action["fn"] = act
elif scenario == "rest401":
    mod.discover_channels = lambda previous=None: one_channel
    write_state({"last_seen": {"C1": "1"}})
    mod.api.errors["/channels/C1/messages"] = HTTPErr(401)

    def act(inbox, status):
        time.sleep(0.5)  # an event wakes the loop after the poll's 401
        status["wake"].set()
    gw_action["fn"] = act
elif scenario == "phases":
    mod.discover_channels = lambda previous=None: one_channel
    write_state({"last_seen": {"C1": "1"}})

    def broken_outbox(state):
        raise RuntimeError("corrupt outbox")
    mod.process_outbox = broken_outbox
    until = lambda: calls["sweep_media"]  # noqa: E731  (a full cycle)
elif scenario == "identity_pending":
    # Non-401 identity failure; REST otherwise fine.
    mod.api.errors["/users/@me"] = HTTPErr(500)
    mod.discover_channels = lambda previous=None: one_channel
    write_state({"last_seen": {"C1": "1"}})
    until = lambda: calls["sweep_media"]  # noqa: E731  (a full cycle)
elif scenario == "ready_identity":
    mod.api.errors["/users/@me"] = HTTPErr(500)
    mod.discover_channels = lambda previous=None: one_channel
    write_state({"last_seen": {"C1": "1"}})

    def act(inbox, status):
        time.sleep(0.5)
        status["user"] = me
        status["wake"].set()
    gw_action["fn"] = act
    until = lambda: mod.BOT_USER_ID  # noqa: E731
elif scenario == "batch_fold":
    mod.discover_channels = lambda previous=None: one_channel
    m0 = msg("C1", "U2", "earlier", age=60)
    write_state({"last_seen": {"C1": m0["id"]}})
    m1 = msg("C1", "U1", "@bot quick question", mention=True, age=5)
    m2 = msg("C1", "U1", "what's the deploy command?", age=4)
    mod.api.msgs["C1"] = [m0, m1, m2]
    until = lambda: mod.load_queue()  # noqa: E731
else:
    sys.exit(f"unknown scenario {scenario}")


def on_alarm(*_):
    raise Stop()


_orig_pending_wait = mod.pending_wait


def check_then_wait(state, now):
    """Called at the end of every main-loop pass, before its wait."""
    if until and until():
        raise Stop()
    return _orig_pending_wait(state, now)


mod.pending_wait = check_then_wait


signal.signal(signal.SIGALRM, on_alarm)
signal.setitimer(signal.ITIMER_REAL, seconds)
out = io.StringIO()
result = {}
with contextlib.redirect_stdout(out):
    try:
        mod.main()
        result["exit"] = "returned"
    except SystemExit as e:
        result["exit"] = e.code
    except Stop:
        result["exit"] = "stopped"
signal.setitimer(signal.ITIMER_REAL, 0)
result["out"] = out.getvalue()
result["calls"] = calls
result["heartbeat"] = os.path.exists(mod.HEARTBEAT_FILE)
if os.path.exists(mod.QUEUE_FILE):
    with open(mod.QUEUE_FILE) as f:
        result["queue"] = json.load(f)
print(json.dumps(result))
