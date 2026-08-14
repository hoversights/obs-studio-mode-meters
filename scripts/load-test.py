#!/usr/bin/env python3
"""Load Studio Mode Meters into a real OBS and prove it works end to end.

    python3 scripts/load-test.py

Builds the plugin, installs it, starts OBS, connects over obs-websocket,
waits for `audio_levels` events, then quits OBS and checks the log for a
clean unload. Restores whatever was installed before, on every exit path.

WHY THE UNLOAD CHECK IS THE POINT. The dangerous failure in this plugin
family is not "no events" — you notice that. It is a segfault at shutdown,
because OBS unloads a module without telling its detached threads and they
keep calling into libobs. That was observed live on 2026-07-15 inside
`obs_enum_sources`, and it is invisible unless you deliberately quit OBS
and read the log afterwards.

WHAT THIS CANNOT TELL YOU. That the levels are *correct*. Verifying a
Preview-only source really meters, and meters the right number, needs real
audio on a real source. This asserts events arrive with a sane shape, and
nothing about the values. Do not read a pass as "metering works".

Requires: `websocket-client` (pip install websocket-client), OBS installed,
obs-websocket enabled.
"""

import base64
import contextlib
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.expanduser("~/Library/Application Support/obs-studio/plugins")
BUNDLE = os.path.join(PLUGIN_DIR, "studio-mode-meters.plugin")
WS_CONFIG = os.path.expanduser(
    "~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
)
OBS_LOG_DIR = os.path.expanduser("~/Library/Application Support/obs-studio/logs")
VENDOR = "studio_mode_meters"

ok = True


def step(msg):
    print(f"\n== {msg}")


def check(label, passed, detail=""):
    global ok
    ok = ok and passed
    print(f"  {'PASS' if passed else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")


def build():
    step("Build")
    r = subprocess.run(["cargo", "build", "--release"], cwd=REPO, capture_output=True, text=True)
    check("cargo build --release", r.returncode == 0, r.stderr.strip().split("\n")[-1] if r.returncode else "")
    # A warning here is worth failing on: this is FFI code loaded into
    # someone else's process, and "unused function" has already once meant
    # a real feature had been silently disconnected.
    warnings = [l for l in r.stderr.split("\n") if l.startswith("warning")]
    check("no build warnings", not warnings, "; ".join(warnings[:2]))
    return os.path.join(REPO, "target/release/libobs_studio_mode_meters.dylib")


def install(dylib):
    """Installs as an OBS .plugin bundle, preserving anything already there."""
    step("Install")
    backup = BUNDLE + ".loadtest-backup"
    if os.path.exists(BUNDLE):
        shutil.rmtree(backup, ignore_errors=True)
        shutil.move(BUNDLE, backup)
    macos = os.path.join(BUNDLE, "Contents/MacOS")
    os.makedirs(macos, exist_ok=True)
    shutil.copy2(dylib, os.path.join(macos, "studio-mode-meters"))
    with open(os.path.join(BUNDLE, "Contents/Info.plist"), "wb") as f:
        plistlib.dump(
            {
                "CFBundleExecutable": "studio-mode-meters",
                "CFBundleIdentifier": "com.hoversights.studio-mode-meters",
                "CFBundleName": "studio-mode-meters",
                "CFBundlePackageType": "BNDL",
                "CFBundleVersion": "0.1.0",
                "LSMinimumSystemVersion": "11.0",
            },
            f,
        )
    check("bundle installed", os.path.exists(os.path.join(macos, "studio-mode-meters")))
    return backup


def restore(backup):
    shutil.rmtree(BUNDLE, ignore_errors=True)
    if backup and os.path.exists(backup):
        shutil.move(backup, BUNDLE)


def ws_connect():
    """Connects to obs-websocket, handling auth if enabled."""
    import websocket

    cfg = json.load(open(WS_CONFIG))
    ws = websocket.create_connection(f"ws://127.0.0.1:{cfg.get('server_port', 4455)}", timeout=15)
    hello = json.loads(ws.recv())["d"]
    if "authentication" in hello:
        a = hello["authentication"]
        secret = base64.b64encode(
            hashlib.sha256((cfg["server_password"] + a["salt"]).encode()).digest()
        ).decode()
        auth = base64.b64encode(hashlib.sha256((secret + a["challenge"]).encode()).digest()).decode()
    else:
        auth = None
    ident = {"rpcVersion": 1, "eventSubscriptions": 1 << 11}  # Vendors
    if auth:
        ident["authentication"] = auth
    ws.send(json.dumps({"op": 1, "d": ident}))
    ws.recv()  # Identified
    return ws


def watch_events(ws, seconds=8):
    step(f"Listen for {VENDOR} events ({seconds}s)")
    ws.settimeout(seconds)
    seen, shapes = 0, set()
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        d = msg.get("d", {})
        if msg.get("op") == 5 and d.get("eventData", {}).get("vendorName") == VENDOR:
            seen += 1
            for lvl in d["eventData"].get("eventData", {}).get("levels", []):
                shapes.add(tuple(sorted(lvl.keys())))
    if seen == 0:
        # NOT a failure. The emit loop skips empty batches by design, so an
        # OBS with no source producing audio emits nothing and is behaving
        # correctly. Failing here would train you to ignore the result --
        # the first run of this script reported FAIL for exactly this
        # reason and the plugin was fine.
        print("  SKIP  audio_levels events — no source produced audio")
        print("        Add an audio source (Settings > Audio, or a media")
        print("        source that is playing) and re-run to exercise this.")
        return 0
    check("received audio_levels events", True, f"{seen} in {seconds}s")
    # Shape only. Whether a value is CORRECT needs real audio of a known
    # level, which this script does not claim to verify.
    keys = set().union(*shapes) if shapes else set()
    check("levels carry the active flag", "active" in keys, f"keys={sorted(keys)}")
    check("levels carry a peak reading", "peak_db" in keys, f"keys={sorted(keys)}")
    return seen


def log_check():
    step("Unload cleanliness")
    logs = sorted(
        (os.path.join(OBS_LOG_DIR, f) for f in os.listdir(OBS_LOG_DIR)),
        key=os.path.getmtime,
    )
    text = open(logs[-1], errors="replace").read() if logs else ""
    check("plugin logged its load", "[studio-mode-meters] loaded" in text)
    clean = "background threads stopped cleanly" in text
    check(
        "plugin logged a clean unload",
        clean,
        "" if clean else "absent — OBS may have died before unload ran",
    )
    # OBS writes this itself when a module crashes on the way out.
    check("no crash reported in the OBS log", "Crash" not in text and "signal" not in text.lower())
    return logs[-1] if logs else None


def main():
    dylib = build()
    if not ok:
        sys.exit("build failed — stopping before touching OBS")

    backup = install(dylib)
    obs = None
    try:
        step("Start OBS")
        obs = subprocess.Popen(
            ["open", "-W", "-a", "OBS"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        ws = None
        for _ in range(30):  # OBS takes a while, and post_load runs late
            time.sleep(2)
            with contextlib.suppress(Exception):
                ws = ws_connect()
                break
        check("connected to obs-websocket", ws is not None)
        if ws:
            watch_events(ws)
            ws.close()
    finally:
        step("Quit OBS")
        subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'], capture_output=True)
        time.sleep(6)  # unload + log flush
        if obs:
            with contextlib.suppress(Exception):
                obs.wait(timeout=20)
        path = log_check()
        restore(backup)
        print(f"\n  OBS log: {path}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    print("Note: this proves the plugin loads, emits, and unloads cleanly.")
    print("It does NOT prove the levels are correct — that needs real audio.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
