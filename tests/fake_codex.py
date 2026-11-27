#!/usr/bin/env python3
"""Stand-in for the codex CLI: records its call and mimics `exec` and `login`.

`exec` writes a 48x32 PNG: flat magenta (#FF00FF) with a grey 8x8 square whose
top-left corner is at x=10, y=12, or the bytes of $FAKE_IMAGE when it is set.
"""
import json
import os
import struct
import sys
import time
import uuid
import zlib

MAGENTA, GREY = b"\xff\x00\xff", b"\x80\x80\x80"


def png(w, h):
    rows = []
    for y in range(h):
        row = b"".join(GREY if 10 <= x < 18 and 12 <= y < 20 else MAGENTA for x in range(w))
        rows.append(b"\x00" + row)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


mode = os.environ.get("FAKE_MODE", "ok")
log_dir = os.environ.get("FAKE_LOG_DIR")
args = sys.argv[1:]
if args[:1] == ["login"]:
    if log_dir and os.path.isdir(log_dir):
        with open(os.path.join(log_dir, "login-" + uuid.uuid4().hex[:8] + ".json"), "w") as f:
            json.dump({"login": args, "codex_home": os.environ.get("CODEX_HOME")}, f)
    if "--device-auth" in args:
        print("Follow these steps to sign in with ChatGPT using device code authorization:", flush=True)
        print("1. Open this link in your browser and sign in to your account", flush=True)
        print("   https://auth.openai.com/codex/device", flush=True)
        print("2. Enter this one-time code (expires in 15 minutes)", flush=True)
        print("   ABCD-12345", flush=True)
        time.sleep(30)
    else:
        print("Logged in using ChatGPT")
    sys.exit(0)

# Per-home usage state for `app-server` and `exec`: $CODEX_HOME/fake_state.json holds
# {"used": [five_hour_percent, weekly_percent], "credits": n}; 100% means the limit is hit.
# The state follows the login (as real limits do), so a lean home's symlinked auth.json
# leads back to the account's own state.
_home = os.environ.get("CODEX_HOME", ".")
_auth = os.path.join(_home, "auth.json")
state_path = os.path.join(os.path.dirname(os.path.realpath(_auth)) if os.path.exists(_auth) else _home,
                          "fake_state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"used": [10, 5], "credits": 0}


def save_state():
    with open(state_path, "w") as f:
        json.dump(state, f)


def limits():
    win = lambda used, mins, at: {"usedPercent": used, "windowDurationMins": mins, "resetsAt": at}
    snap = {"limitId": "codex", "planType": "plus", "rateLimitReachedType": None,
            "primary": win(state["used"][0], 300, 4102444800),
            "secondary": win(state["used"][1], 10080, 4103049600)}
    creds = [{"id": f"credit-{i}", "resetType": "codexRateLimits", "status": "available",
              "grantedAt": 1, "expiresAt": 4102444800 + i, "title": "Full reset"}
             for i in range(state["credits"])]
    return {"rateLimits": snap, "rateLimitsByLimitId": {"codex": snap},
            "rateLimitResetCredits": {"availableCount": state["credits"], "credits": creds}}


if args[:1] == ["app-server"]:
    if log_dir and os.path.isdir(log_dir):
        with open(os.path.join(log_dir, "appserver-" + uuid.uuid4().hex[:8] + ".json"), "w") as f:
            json.dump({"argv": args, "codex_home": os.environ.get("CODEX_HOME")}, f)
    if os.environ.get("FAKE_HANG"):
        time.sleep(60)  # a stalled network call: never answers
        sys.exit(0)
    for line in sys.stdin:
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "account/rateLimits/read":
            result = limits()
        elif method == "account/rateLimitResetCredit/consume":
            if log_dir:
                with open(os.path.join(log_dir, "consume-" + uuid.uuid4().hex[:8] + ".json"), "w") as f:
                    json.dump({"consume": msg["params"], "codex_home": os.environ.get("CODEX_HOME")}, f)
            if state["credits"] < 1:
                result = {"outcome": "noCredit"}
            elif max(state["used"]) < 100:
                result = {"outcome": "nothingToReset"}
            else:
                state.update(used=[0, 0], credits=state["credits"] - 1)
                save_state()
                result = {"outcome": "reset"}
        else:
            print(json.dumps({"id": mid, "error": {"message": f"unknown method {method}"}}), flush=True)
            continue
        print(json.dumps({"id": mid, "result": result}), flush=True)
    sys.exit(0)

stdin = sys.stdin.read()
tid = "thread-" + uuid.uuid4().hex[:8]
if log_dir:
    with open(os.path.join(log_dir, tid + ".json"), "w") as f:
        json.dump({"argv": args, "stdin": stdin, "cwd": os.getcwd(), "start": time.time(),
                   "codex_home": os.environ.get("CODEX_HOME"),
                   "auth": open(os.path.join(os.environ["CODEX_HOME"], "auth.json")).read()
                   if os.path.exists(os.path.join(os.environ["CODEX_HOME"], "auth.json")) else None}, f)
print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
# FAKE_REFRESH mimics a token refresh: "inplace" rewrites auth.json through any symlink,
# "rename" replaces it with a new file the way an atomic write does.
refresh = os.environ.get("FAKE_REFRESH")
auth_path = os.path.join(os.environ.get("CODEX_HOME", "."), "auth.json")
if refresh == "inplace":
    with open(auth_path, "w") as f:
        f.write('{"tokens": "refreshed-inplace"}')
elif refresh == "rename":
    tmp = f"{auth_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write('{"tokens": "refreshed-rename"}')
    os.replace(tmp, auth_path)
if max(state["used"]) >= 100:
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}}))
    sys.exit(1)
if mode == "fail":
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit reached"}}))
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
if mode == "ok":
    d = os.path.join(os.environ["CODEX_HOME"], "generated_images", tid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "exec-a.png"), "wb") as f:
        fixed = os.environ.get("FAKE_IMAGE")
        f.write(open(fixed, "rb").read() if fixed else png(48, 32))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}}))
else:
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "I can't call the image tool here."}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 20}}))
