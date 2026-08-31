#!/usr/bin/env python3
"""Which bus is the scene tap actually hearing?

    python3 scripts/probe-two-tone.py

Every other test in this repo plays the SAME tone — 1 kHz at -20 dBFS — into
whatever it is measuring. So when a level comes back, "we got -20" proves a
level arrived and says nothing about where it came from. For per-source
events that is survivable, because each level carries a source name. For the
SCENE-level tap it is not: the name is the scene's, and what is inside it is
an assumption.

That assumption is the foundation of MONITOR_SPEC.md, which proposes routing
the Preview scene's composited mix to headphones. If that tap is actually
carrying Program, or both summed, the whole feature auditions the wrong
thing — and no existing test would notice.

So: two scenes, two DIFFERENT levels.

    Program  'live'    1 kHz at -20 dBFS
    Preview  'staged'  5 kHz at -35 dBFS

The frequencies differ too, which does not help yet — the plugin emits only
`peak_db`, no spectrum — but it means the same scenes can be reused once
there is real audio to run an FFT over, or simply to listen to. 5 kHz is
unmistakably not 1 kHz by ear.

The levels are what discriminates today:

    Preview tap reads ~-35   -> clean. It hears Preview only.
    Preview tap reads ~-20   -> WRONG. It is hearing Program.
    Preview tap reads ~-19   -> WRONG. Both, summed.

Requires the plugin installed; it is the only thing that reports a
Preview-scene level at all. Installs from this repo's build and restores
whatever was there.
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
COLLECTION = "two-tone-probe"

PROGRAM = dict(scene="live",   name="prog-1k",   hz=1000, dbfs=-20.0)
PREVIEW = dict(scene="staged", name="stage-5k",  hz=5000, dbfs=-35.0)
TOL = 3.0  # dB


def connect():
    import websocket
    cfg = json.load(open(WS_CONFIG))
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{cfg.get('server_port', 4455)}", timeout=15)
    hello = json.loads(ws.recv())["d"]
    ident = {"rpcVersion": 1, "eventSubscriptions": 0x7FFFFFFF}
    if "authentication" in hello:
        a = hello["authentication"]
        sec = base64.b64encode(hashlib.sha256(
            (cfg["server_password"] + a["salt"]).encode()).digest()).decode()
        ident["authentication"] = base64.b64encode(hashlib.sha256(
            (sec + a["challenge"]).encode()).digest()).decode()
    ws.send(json.dumps({"op": 1, "d": ident}))
    ws.recv()
    return ws


def req(ws, kind, data=None, rid="tt"):
    ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": rid,
                                       "requestData": data or {}}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("op") == 7 and m["d"]["requestId"] == rid:
            return m["d"]


def write_tone(path, hz, dbfs, seconds=120):
    rate, amp = 48000, 10 ** (dbfs / 20.0)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        f = bytearray()
        for i in range(rate * seconds):
            v = int(amp * 32767 * math.sin(2 * math.pi * hz * i / rate))
            f += struct.pack("<hh", v, v)
        w.writeframes(bytes(f))
    return path


def levels(ws, seconds=8):
    """Every source name the plugin reports, with its highest peak_db."""
    ws.settimeout(seconds)
    out, dl = {}, time.time() + seconds
    while time.time() < dl:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        d = msg.get("d", {})
        if msg.get("op") == 5 and d.get("eventData", {}).get("vendorName") == VENDOR:
            for lvl in d["eventData"].get("eventData", {}).get("levels", []):
                n, v = lvl.get("name"), lvl.get("peak_db")
                if n and v is not None and (n not in out or v > out[n]):
                    out[n] = v
    return out


def install_plugin():
    lib = os.path.join(REPO, "target/release/libobs_studio_mode_meters.dylib")
    if not os.path.exists(lib):
        subprocess.run(["cargo", "build", "--release"], cwd=REPO, check=True)
    backup = BUNDLE + ".two-tone-backup"
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
                       "CFBundlePackageType": "BNDL", "CFBundleVersion": "0.1.0",
                       "LSMinimumSystemVersion": "11.0"}, f)
    return backup


def restore(backup):
    shutil.rmtree(BUNDLE, ignore_errors=True)
    if backup and os.path.exists(backup):
        shutil.move(backup, BUNDLE)


def main():
    if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode == 0:
        sys.exit("Quit OBS first — the plugin must be installed before it starts.")
    backup = install_plugin()
    subprocess.Popen(["open", "-W", "-a", "OBS"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws = None
    for _ in range(30):
        time.sleep(2)
        with contextlib.suppress(Exception):
            ws = connect(); break
    if ws is None:
        restore(backup); sys.exit("could not reach obs-websocket")

    prev_coll = req(ws, "GetSceneCollectionList")["responseData"]["currentSceneCollectionName"]
    if prev_coll == COLLECTION:
        restore(backup)
        sys.exit(f"OBS is in {COLLECTION!r} from a previous run — switch back first.")
    colls = req(ws, "GetSceneCollectionList")["responseData"]["sceneCollections"]
    req(ws, "SetCurrentSceneCollection" if COLLECTION in colls else "CreateSceneCollection",
        {"sceneCollectionName": COLLECTION})
    time.sleep(7)

    files = {}
    ok = True
    try:
        for spec in (PROGRAM, PREVIEW):
            files[spec["name"]] = write_tone(
                f"/tmp/tt-{spec['name']}.wav", spec["hz"], spec["dbfs"])

        # Studio Mode OFF while creating, so both activate normally — the
        # ordering FINDINGS.md records as necessary for a source to meter.
        req(ws, "SetStudioModeEnabled", {"studioModeEnabled": False})
        time.sleep(1)
        for spec in (PROGRAM, PREVIEW):
            req(ws, "CreateScene", {"sceneName": spec["scene"]})
            req(ws, "RemoveInput", {"inputName": spec["name"]})
            req(ws, "SetCurrentProgramScene", {"sceneName": spec["scene"]})
            time.sleep(1)
            req(ws, "CreateInput", {
                "sceneName": spec["scene"], "inputName": spec["name"],
                "inputKind": "ffmpeg_source",
                "inputSettings": {"local_file": files[spec["name"]],
                                  "looping": True, "is_local_file": True}})
            req(ws, "TriggerMediaInputAction", {
                "inputName": spec["name"],
                "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
            time.sleep(2)

        req(ws, "SetStudioModeEnabled", {"studioModeEnabled": True})
        time.sleep(1)
        req(ws, "SetCurrentProgramScene", {"sceneName": PROGRAM["scene"]})
        req(ws, "SetCurrentPreviewScene", {"sceneName": PREVIEW["scene"]})
        time.sleep(2)
        # Restart both AFTER the buses are set — a media source that ended,
        # or that was restarted while its scene was inactive, plays nothing,
        # and that silently turned three earlier probes into "inconclusive".
        for spec in (PROGRAM, PREVIEW):
            req(ws, "TriggerMediaInputAction", {
                "inputName": spec["name"],
                "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
        time.sleep(9)

        p = req(ws, "GetCurrentProgramScene")["responseData"]
        v = req(ws, "GetCurrentPreviewScene")["responseData"]
        print(f"\n  Program : {p.get('currentProgramSceneName') or p.get('sceneName')}"
              f"   ({PROGRAM['hz']} Hz @ {PROGRAM['dbfs']} dBFS)")
        print(f"  Preview : {v.get('currentPreviewSceneName') or v.get('sceneName')}"
              f"   ({PREVIEW['hz']} Hz @ {PREVIEW['dbfs']} dBFS)\n")

        got = levels(ws)
        for n in sorted(got):
            print(f"    {n:<28} {got[n]:7.1f} dBFS")

        print("\n--- per-source levels ---")
        for spec in (PROGRAM, PREVIEW):
            v_ = got.get(spec["name"])
            if v_ is None:
                print(f"  FAIL  {spec['name']}: no level at all"); ok = False
            elif abs(v_ - spec["dbfs"]) <= TOL:
                print(f"  PASS  {spec['name']}: {v_:.1f} ~ {spec['dbfs']}")
            else:
                print(f"  FAIL  {spec['name']}: {v_:.1f}, expected {spec['dbfs']}"); ok = False

        print("\n--- THE QUESTION: what is the PREVIEW SCENE tap hearing? ---")
        scene_lvl = got.get(PREVIEW["scene"])
        if scene_lvl is None:
            print(f"  scene '{PREVIEW['scene']}' reported no level.")
            print("  Cannot tell what the tap hears. INCONCLUSIVE.")
            ok = False
        else:
            print(f"  scene '{PREVIEW['scene']}' reads {scene_lvl:.1f} dBFS")
            if abs(scene_lvl - PREVIEW["dbfs"]) <= TOL:
                print(f"  PASS — matches the Preview tone ({PREVIEW['dbfs']}).")
                print("  The tap hears Preview ONLY. MONITOR_SPEC.md's premise holds.")
            elif abs(scene_lvl - PROGRAM["dbfs"]) <= TOL:
                print(f"  FAIL — matches the PROGRAM tone ({PROGRAM['dbfs']}).")
                print("  The tap is hearing the wrong bus. The monitor feature")
                print("  would audition what is already live.")
                ok = False
            else:
                print("  FAIL — matches neither tone cleanly; likely both summed.")
                ok = False

        print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    finally:
        with contextlib.suppress(Exception):
            req(ws, "SetCurrentSceneCollection", {"sceneCollectionName": prev_coll})
            for f in files.values():
                os.remove(f)
            ws.close()
        subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'],
                       capture_output=True)
        time.sleep(5)
        restore(backup)
        print(f"  restored collection {prev_coll} and the previous plugin")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
