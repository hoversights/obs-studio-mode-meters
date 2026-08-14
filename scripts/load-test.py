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

STATUS OF THE QUANTIFIED STAGE (2026-08-14). It runs, but has never yet
produced a reading on this Mac: OBS's OWN meters report exactly 0.0 for
every input while the tone source sits in OBS_MEDIA_STATE_PLAYING, so
there is nothing for the plugin to meter and its silence is correct. OBS's
audio subsystem initialises fine (device permission granted, monitoring
device set), so the gap is in getting a generated WAV to produce audio in
this OBS, not in the plugin. Treat a SKIP there as "not yet exercised",
never as "metering verified".

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
    # 0x7FFFFFFF: every category including the high-volume ones.
    #
    # This was `1 << 11` and that is not Vendors — it is 1 << 9. With the
    # wrong bit the script reports "no events" no matter how well the
    # plugin works, which is the worst kind of test: one that cannot pass.
    # Subscribing to everything removes the question entirely, and the
    # volume is irrelevant for a script that runs for seconds.
    ident = {"rpcVersion": 1, "eventSubscriptions": 0x7FFFFFFF}
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



# A pure sine of known amplitude is what makes metering *quantifiable*:
# 0.1 linear is exactly -20 dBFS, so a correct meter must report -20.
TONE_DBFS = -20.0
TONE_HZ = 1000
# Generous, and deliberately so. This is asserting "the meter is reading the
# real signal", not calibrating it: OBS resamples, and the peak of a sine
# sampled at 48 kHz lands slightly under its true peak. A wrong reading
# fails by tens of dB (silence reads -60 or worse), not by two.
TONE_TOLERANCE_DB = 3.0


def write_tone(path, seconds=30):
    """A 48 kHz stereo sine at exactly TONE_DBFS, using only the stdlib."""
    import math
    import struct
    import wave

    rate, amp = 48000, 10 ** (TONE_DBFS / 20.0)
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            v = int(amp * 32767 * math.sin(2 * math.pi * TONE_HZ * i / rate))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return path


def request(ws, kind, data=None, rid="lt"):
    ws.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": rid,
                                       "requestData": data or {}}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("op") == 7 and m["d"]["requestId"] == rid:
            return m["d"]


def measure(ws, source_name, seconds=6):
    """Highest peak_db reported for `source_name`, and its active flag."""
    ws.settimeout(seconds)
    peak, active, deadline = None, None, time.time() + seconds
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        d = msg.get("d", {})
        if msg.get("op") == 5 and d.get("eventData", {}).get("vendorName") == VENDOR:
            for lvl in d["eventData"].get("eventData", {}).get("levels", []):
                if lvl.get("name") == source_name:
                    v = lvl.get("peak_db")
                    if v is not None and (peak is None or v > peak):
                        peak, active = v, lvl.get("active")
    return peak, active


def quantified_test(ws):
    """Plays a known tone and checks the reported level against it.

    This is the difference between "events arrive" and "metering works".
    Stage two is the one that justifies the plugin existing at all: a source
    that is ONLY in the Preview scene still meters, which is exactly what
    obs-websocket cannot report on its own.
    """
    step(f"Quantified metering (a {TONE_HZ} Hz tone at {TONE_DBFS} dBFS)")
    tone = write_tone(os.path.join(REPO, "target", "loadtest-tone.wav"))
    name = "loadtest-tone"
    scene = request(ws, "GetCurrentProgramScene")["responseData"]
    program_scene = scene.get("currentProgramSceneName") or scene.get("sceneName")

    created = request(ws, "CreateInput", {
        "sceneName": program_scene,
        "inputName": name,
        "inputKind": "ffmpeg_source",
        "inputSettings": {"local_file": tone, "looping": True, "is_local_file": True},
    })
    # Creating a media source does not start it — measured here, and the
    # same invariant FrameSW follows for every media shot it stages.
    # Without this the source sits in OBS_MEDIA_STATE_NONE forever.
    request(ws, "TriggerMediaInputAction", {
        "inputName": name, "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
    })
    if not created.get("requestStatus", {}).get("result"):
        check("created a test-tone source", False,
              created.get("requestStatus", {}).get("comment", "")[:80])
        return
    try:
        # The plugin discovers new sources on a 5s rescan (50 x 100ms in
        # spawn_periodic_rescan), so a freshly created source is invisible
        # to it for up to five seconds. Waiting 2s made the first run of
        # this test report "no level" for a plugin that was working
        # perfectly.
        time.sleep(7)
        peak, active = measure(ws, name)
        check("the tone is metered on Program", peak is not None,
              "" if peak is not None else "no level reported for the source")
        if peak is not None:
            off = abs(peak - TONE_DBFS)
            check(f"reported level matches the tone", off <= TONE_TOLERANCE_DB,
                  f"expected {TONE_DBFS} dBFS, read {peak:.1f} (off by {off:.1f} dB)")
            check("active is true while on Program", active is True, f"active={active}")

        # Stage two: the reason this plugin exists.
        step("Preview-only metering — what obs-websocket cannot do")
        request(ws, "SetStudioModeEnabled", {"studioModeEnabled": True})
        time.sleep(1)
        scenes = [s["sceneName"] for s in request(ws, "GetSceneList")["responseData"]["scenes"]]
        other = next((s for s in scenes if s != program_scene), None)
        if other is None:
            print("  SKIP  needs a second scene to stage into; only one exists")
            return
        request(ws, "SetCurrentPreviewScene", {"sceneName": other})
        # Move the tone into the scene that is now Preview only.
        request(ws, "CreateInput", {
            "sceneName": other, "inputName": name + "-preview",
            "inputKind": "ffmpeg_source",
            "inputSettings": {"local_file": tone, "looping": True, "is_local_file": True},
        })
        time.sleep(7)  # same rescan wait as above
        p2, a2 = measure(ws, name + "-preview")
        check("a Preview-only source is still metered", p2 is not None,
              "no level — this is the capability the plugin exists to provide")
        if p2 is not None:
            check("active is false while only in Preview", a2 is False, f"active={a2}")
        request(ws, "RemoveInput", {"inputName": name + "-preview"})
    finally:
        request(ws, "RemoveInput", {"inputName": name})
        with contextlib.suppress(Exception):
            os.remove(tone)


TEST_PROFILE = "studio-mode-meters-test"
TEST_COLLECTION = "studio-mode-meters-test"


def isolate(ws):
    """Switches OBS into a profile and scene collection of our own.

    Two reasons, and the second is why this exists at all.

    Isolation: the first runs of this script executed inside FrameSW's
    profile and scene collection — PGM-A/PGM-B, Studio Mode forced on,
    FrameSW's own plugin attaching taps alongside ours. Creating and
    deleting sources in someone's real production collection is rude at
    best.

    Determinism: a profile carries the audio settings. Measuring a level
    inside a profile configured for something else means measuring through
    whatever that profile does to audio, which is not a controlled
    experiment. A fresh profile has OBS's defaults.

    Returns what to restore, and the caller must restore it on every path.
    """
    step("Isolate into our own profile and scene collection")
    prev_profile = request(ws, "GetProfileList")["responseData"]["currentProfileName"]
    prev_coll = request(ws, "GetSceneCollectionList")["responseData"]["currentSceneCollectionName"]
    # If a previous run died before restoring, OBS is already sitting in
    # the test profile — and restoring "to what we found" would then be a
    # no-op that reports success. Say so instead of quietly leaving OBS
    # somewhere the operator did not put it.
    if prev_profile == TEST_PROFILE or prev_coll == TEST_COLLECTION:
        # Abort rather than run. Continuing would measure inside a
        # half-restored state — which is exactly what produced a run of
        # confusing FAILs — and would then "restore" OBS to the test
        # profile, cementing the mess. There is no way to guess what the
        # operator's real profile was.
        sys.exit(
            f"\n  OBS is in {prev_profile!r}/{prev_coll!r} — a previous run did not "
            f"restore it.\n  Open OBS, switch Profile and Scene Collection back to "
            f"your own, quit, and re-run.\n  Nothing was changed."
        )

    profiles = request(ws, "GetProfileList")["responseData"]["profiles"]
    if TEST_PROFILE not in profiles:
        request(ws, "CreateProfile", {"profileName": TEST_PROFILE})
    else:
        request(ws, "SetCurrentProfile", {"profileName": TEST_PROFILE})
    time.sleep(2)

    colls = request(ws, "GetSceneCollectionList")["responseData"]["sceneCollections"]
    if TEST_COLLECTION not in colls:
        request(ws, "CreateSceneCollection", {"sceneCollectionName": TEST_COLLECTION})
    else:
        request(ws, "SetCurrentSceneCollection", {"sceneCollectionName": TEST_COLLECTION})
    # A collection switch tears down and rebuilds every source; give the
    # plugin's 5s rescan a chance to see the new world before measuring.
    time.sleep(7)

    check("switched to an isolated profile", 
          request(ws, "GetProfileList")["responseData"]["currentProfileName"] == TEST_PROFILE)
    return prev_profile, prev_coll


def deisolate(ws, prev):
    """Puts OBS back exactly as it was found."""
    if not prev:
        return
    prev_profile, prev_coll = prev
    step("Restore OBS")
    with contextlib.suppress(Exception):
        request(ws, "SetCurrentSceneCollection", {"sceneCollectionName": prev_coll})
        time.sleep(3)
        request(ws, "SetCurrentProfile", {"profileName": prev_profile})
        time.sleep(2)
        now = request(ws, "GetProfileList")["responseData"]["currentProfileName"]
        now_coll = request(ws, "GetSceneCollectionList")["responseData"][
            "currentSceneCollectionName"
        ]
        restored = now == prev_profile and now_coll == prev_coll
        check(
            "restored the original profile and collection",
            restored,
            "" if restored else f"now {now!r}/{now_coll!r}, wanted {prev_profile!r}/{prev_coll!r}",
        )


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
    # Only what OBS wrote AFTER our unload. The whole-log version failed on
    # "Crash or unclean shutdown detected", which OBS prints at STARTUP
    # about the previous session — so an earlier killed run made every
    # later run report a crash that had not happened.
    tail = text.split("[studio-mode-meters] unloaded", 1)[-1] if clean else ""
    check(
        "no crash after unload",
        not any(w in tail.lower() for w in ("crash", "segmentation", "signal")),
        tail.strip().split("\n")[0][:80] if tail.strip() else "",
    )
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
            prev = None
            try:
                prev = isolate(ws)
                watch_events(ws)
                quantified_test(ws)
            finally:
                deisolate(ws, prev)
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
