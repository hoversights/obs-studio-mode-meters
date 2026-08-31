#!/usr/bin/env python3
"""Can an operator control a Preview-only source with OBS's own mixer?

    python3 scripts/probe-preview-fader.py

Answers two questions that decide whether a meter dock is enough, or
whether it has to be a mixer:

  1. Does OBS's Audio Mixer LIST a source that is only in the Preview
     scene? If not, there is no fader and no mute button for it, and a
     display-only dock leaves the operator able to see a problem and
     unable to do anything about it.

  2. Does gain still APPLY to such a source? The plugin reports post-fader
     levels (it multiplies by `obs_source_get_volume`), so if OBS still
     honours volume on a staged source, the level should drop by the
     amount asked for.

Question 1 is answered by a screenshot of the real OBS window, because it
is a question about what a person can see. Question 2 is answered by
measurement.

Requires the plugin installed — it is the only thing that reports levels
for a Preview-only source, which is the entire point. Installs it from
this repo's build and restores whatever was there.
"""

import base64
import contextlib
import hashlib
import json
import math
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import time
import wave

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.expanduser("~/Library/Application Support/obs-studio/plugins")
BUNDLE = os.path.join(PLUGIN_DIR, "studio-mode-meters.plugin")
WS_CONFIG = os.path.expanduser(
    "~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
)
VENDOR = "studio_mode_meters"
TONE_HZ, TONE_DBFS = 1000, -20.0
COLLECTION = "preview-fader-probe"
SHOT = "/tmp/preview-mixer.png"


def connect():
    import websocket

    cfg = json.load(open(WS_CONFIG))
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{cfg.get('server_port', 4455)}", timeout=15)
    hello = json.loads(ws.recv())["d"]
    ident = {"rpcVersion": 1, "eventSubscriptions": 0x7FFFFFFF}
    if "authentication" in hello:
        a = hello["authentication"]
        secret = base64.b64encode(hashlib.sha256(
            (cfg["server_password"] + a["salt"]).encode()).digest()).decode()
        ident["authentication"] = base64.b64encode(hashlib.sha256(
            (secret + a["challenge"]).encode()).digest()).decode()
    ws.send(json.dumps({"op": 1, "d": ident}))
    ws.recv()
    return ws


def req(ws, kind, data=None, rid="probe"):
    ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": rid,
                                       "requestData": data or {}}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("op") == 7 and m["d"]["requestId"] == rid:
            return m["d"]


def write_tone(path, seconds=60):
    rate, amp = 48000, 10 ** (TONE_DBFS / 20.0)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        f = bytearray()
        for i in range(rate * seconds):
            v = int(amp * 32767 * math.sin(2 * math.pi * TONE_HZ * i / rate))
            f += struct.pack("<hh", v, v)
        w.writeframes(bytes(f))
    return path


def plugin_level(ws, name, seconds=8):
    """Highest peak_db OUR plugin reports — OBS reports nothing here."""
    ws.settimeout(seconds)
    peak, deadline = None, time.time() + seconds
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        d = msg.get("d", {})
        if msg.get("op") == 5 and d.get("eventData", {}).get("vendorName") == VENDOR:
            for lvl in d["eventData"].get("eventData", {}).get("levels", []):
                if lvl.get("name") == name:
                    v = lvl.get("peak_db")
                    if v is not None and (peak is None or v > peak):
                        peak = v
    return peak


def install_plugin():
    lib = os.path.join(REPO, "target/release/libobs_studio_mode_meters.dylib")
    if not os.path.exists(lib):
        subprocess.run(["cargo", "build", "--release"], cwd=REPO, check=True)
    backup = BUNDLE + ".fader-probe-backup"
    if os.path.exists(BUNDLE):
        shutil.rmtree(backup, ignore_errors=True)
        shutil.move(BUNDLE, backup)
    macos = os.path.join(BUNDLE, "Contents/MacOS")
    os.makedirs(macos, exist_ok=True)
    shutil.copy2(lib, os.path.join(macos, "studio-mode-meters"))
    with open(os.path.join(BUNDLE, "Contents/Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleExecutable": "studio-mode-meters",
                       "CFBundleIdentifier": "com.hoversights.studio-mode-meters",
                       "CFBundleName": "studio-mode-meters",
                       "CFBundlePackageType": "BNDL",
                       "CFBundleVersion": "0.1.0",
                       "LSMinimumSystemVersion": "11.0"}, f)
    return backup


def main():
    if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0:
        sys.exit("Quit OBS first — the plugin has to be installed before it starts.")

    backup = install_plugin()
    obs = subprocess.Popen(["open", "-W", "-a", "OBS"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    for _ in range(30):
        time.sleep(2)
        with contextlib.suppress(Exception):
            ws = connect(); break
    if ws is None:
        sys.exit("could not reach obs-websocket")

    prev = req(ws, "GetSceneCollectionList")["responseData"]["currentSceneCollectionName"]
    if prev == COLLECTION:
        sys.exit(f"OBS is in {COLLECTION!r} from a previous run — switch back first.")
    colls = req(ws, "GetSceneCollectionList")["responseData"]["sceneCollections"]
    req(ws, "SetCurrentSceneCollection" if COLLECTION in colls else "CreateSceneCollection",
        {"sceneCollectionName": COLLECTION})
    time.sleep(7)

    tone = write_tone("/tmp/fader-tone.wav")
    try:
        # Baseline, stated rather than inherited.
        for n in ("live", "staged"):
            req(ws, "CreateScene", {"sceneName": n})
        req(ws, "SetStudioModeEnabled", {"studioModeEnabled": False})
        time.sleep(1)
        req(ws, "SetCurrentProgramScene", {"sceneName": "live"})
        time.sleep(1)
        req(ws, "RemoveInput", {"inputName": "staged-tone"})

        # Created while Studio Mode is OFF so it activates normally, then
        # staged — the ordering FINDINGS.md records as necessary.
        req(ws, "CreateInput", {
            "sceneName": "staged", "inputName": "staged-tone",
            "inputKind": "ffmpeg_source",
            "inputSettings": {"local_file": tone, "looping": True, "is_local_file": True}})
        req(ws, "SetCurrentProgramScene", {"sceneName": "staged"})
        time.sleep(2)
        req(ws, "TriggerMediaInputAction", {
            "inputName": "staged-tone",
            "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
        time.sleep(3)

        # Now put it in PREVIEW only.
        req(ws, "SetStudioModeEnabled", {"studioModeEnabled": True})
        time.sleep(1)
        req(ws, "SetCurrentProgramScene", {"sceneName": "live"})
        req(ws, "SetCurrentPreviewScene", {"sceneName": "staged"})
        time.sleep(8)

        prog = req(ws, "GetCurrentProgramScene")["responseData"]
        prev_s = req(ws, "GetCurrentPreviewScene")["responseData"]
        print(f"\n  Program : {prog.get('currentProgramSceneName') or prog.get('sceneName')}")
        print(f"  Preview : {prev_s.get('currentPreviewSceneName') or prev_s.get('sceneName')}")
        print("  the tone source is in 'staged' — Preview only\n")

        print("--- Q2: does gain still apply to a staged source? ---")
        req(ws, "SetInputVolume", {"inputName": "staged-tone", "inputVolumeMul": 1.0})
        time.sleep(6)
        full = plugin_level(ws, "staged-tone")
        req(ws, "SetInputVolume", {"inputName": "staged-tone", "inputVolumeMul": 0.5})
        time.sleep(6)
        half = plugin_level(ws, "staged-tone")
        print(f"  volume 1.0 -> {full}")
        print(f"  volume 0.5 -> {half}")
        if full is not None and half is not None:
            drop = full - half
            print(f"  drop = {drop:.1f} dB (expect ~6.0 if OBS honours the fader)")
            print("  ANSWER: gain APPLIES to a staged source."
                  if 4.5 < drop < 7.5 else
                  "  ANSWER: unexpected — gain does NOT behave as a 6 dB cut.")
        else:
            print("  INCONCLUSIVE — no level; is the plugin loaded and the tone playing?")

        print("\n--- Q1: does OBS's mixer LIST the staged source? ---")
        shot = subprocess.run(
            ["python3", os.path.join(REPO, "..", "Documents/framesw/scripts/screenshot-app.py"),
             "--app", "OBS", "--out", SHOT],
            capture_output=True, text=True)
        if shot.returncode == 0:
            print(f"  screenshot: {SHOT}")
            print("  Look at the Audio Mixer: is 'staged-tone' listed with a fader?")
        else:
            print(f"  screenshot failed: {shot.stderr.strip()[:200]}")
            print("  Look at OBS's Audio Mixer by eye instead.")
        print("\n  Leaving OBS open so you can see it. Quit it when done;")
        print(f"  then restore your collection: {prev}")
    finally:
        with contextlib.suppress(Exception):
            os.remove(tone)
        print(f"\n  plugin backup at: {backup}")


if __name__ == "__main__":
    main()
