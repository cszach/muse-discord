"""Run monitor.main() with a process_outbox that blocks (up to 3 s)
until the interactions are acked, then stops main(); inject a /settings
command, a mdset: button click and a mdset: modal submit mid-block, and
time each callback. Prints one JSON line.
Usage: [MUT_DIR=<tree>] python3 main_scenario.py"""
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
import monitor_harness as H  # noqa: E402
from common import scratch  # noqa: E402

tmp = scratch("muse-plumb-main-")
mod = H.load_monitor(tmp)
mod.api.routes["/users/@me"] = {"id": H.BOT, "username": "musebot"}
mod.BOT_ID = mod.BOT_USER_ID = None
mod.discover_channels = lambda previous=None: (
    {"C1": H.chan()}, {H.G1: {"name": "Srv", "system_channel_id": None,
                              "joined_at": None}})
with open(mod.STATE_FILE, "w") as f:
    json.dump({"guild_joined": {H.G1: 0}, "known_guilds": {H.G1: "Srv"}}, f)


in_outbox = threading.Event()
acked = threading.Event()


class Stop(BaseException):
    pass


def slow_outbox(state):
    in_outbox.set()
    acked.wait(3)   # only acks that come while the main loop is blocked
    raise Stop()


mod.process_outbox = slow_outbox
res = {"acks": [], "handled_on": []}


def handle(ix, ctx):
    res["handled_on"].append(threading.current_thread().name)
    return {"type": 4 if ix["type"] == 2 else 7,
            "data": {"content": f"page for {ix['id']}", "flags": 64,
                     "seen_guilds": [g["id"] for g in ctx.guilds()]}}


mod._SETTINGS_UI["mod"] = types.SimpleNamespace(handle=handle)


def cb(i, resp):
    res["acks"].append({"id": i["id"],
                        "after": time.time() - res["put_at"][i["id"]],
                        "resp": resp})
    if len(res["acks"]) == 3:
        acked.set()


mod.interaction_callback = cb
res["put_at"] = {}


def fake_gw(token, inbox, status):
    in_outbox.wait(5)
    for n, (t, data) in enumerate([
            (2, {"name": "settings", "type": 1}),
            (3, {"custom_id": "mdset:p:e", "component_type": 2}),
            (5, {"custom_id": "mdset:m:x", "components": []})]):
        iid = f"90{n}"
        res["put_at"][iid] = time.time()
        status["interactions"].put({
            "id": iid, "token": "tok", "type": t, "data": data,
            "user": {"id": H.OWNER}})
        time.sleep(0.05)


mod._gateway_thread_main = fake_gw


def on_alarm(*_):
    raise Stop()


signal.signal(signal.SIGALRM, on_alarm)
signal.setitimer(signal.ITIMER_REAL, 10)
out = io.StringIO()
with contextlib.redirect_stdout(out):
    try:
        mod.main()
    except Stop:
        pass
res["out"] = out.getvalue()
print(json.dumps(res))
