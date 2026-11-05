#!/usr/bin/env python3
"""Stand-in for the codex CLI: records its call and mimics `exec` and `login`.

`exec` writes a 48x32 PNG: flat magenta (#FF00FF) with a grey 8x8 square whose
top-left corner is at x=10, y=12.
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

stdin = sys.stdin.read()
tid = "thread-" + uuid.uuid4().hex[:8]
if log_dir:
    with open(os.path.join(log_dir, tid + ".json"), "w") as f:
        json.dump({"argv": args, "stdin": stdin, "cwd": os.getcwd(), "start": time.time()}, f)
print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
if mode == "fail":
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit reached"}}))
    sys.exit(1)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
if mode == "ok":
    d = os.path.join(os.environ["CODEX_HOME"], "generated_images", tid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "exec-a.png"), "wb") as f:
        f.write(png(48, 32))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}}))
else:
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "I can't call the image tool here."}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 20}}))
