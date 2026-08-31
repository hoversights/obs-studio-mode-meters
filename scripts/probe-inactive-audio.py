#!/usr/bin/env python3
"""Does OBS produce audio for a source in a scene that is NOT active?

    python3 scripts/probe-inactive-audio.py

Starts OBS itself if it is not already running, and quits it again.
Requires NO plugin — it measures OBS's own `InputVolumeMeters` event.

WHY THIS EXISTS. The premise behind this plugin — and behind FrameSW's
monitoring design — is that OBS gives you no way to hear or meter a source
that is staged but not live. `PLUGIN_SPLIT_PLAN.md` states it outright:
"Monitoring Preview-only audio in headphones is a real gap in OBS." That
belief was never measured; its own bench test (Phase 0.5, Test B) is still
marked parked.

It matters commercially, not only technically. If OBS CAN monitor an
inactive source, a Studio Mode operator can already hear their next guest
and this plugin only adds a picture of something they could already check.
If it cannot, the gap is real and it is the whole pitch.

WHAT THIS CANNOT TELL YOU: whether sound actually reaches your ears.
Nothing here can hear the monitoring device. A negative result means "no
evidence of audio", not "proven silent" — that last step needs a person
with headphones, and it is printed at the end rather than glossed over.

Runs in its own scene collection and restores whatever you had.
"""

import base64
import contextlib
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import time
import wave

WS_CONFIG = os.path.expanduser(
    "~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
)
VENDOR = "studio_mode_meters"
TONE_HZ, TONE_DBFS = 1000, -20.0
COLLECTION = "inactive-audio-probe"


def connect():
    import websocket

    cfg = json.load(open(WS_CONFIG))
    ws = websocket.create_connection(
        f"ws://127.0.0.1:{cfg.get('server_port', 4455)}", timeout=15
    )
    hello = json.loads(ws.recv())["d"]
    ident = {"rpcVersion": 1, "eventSubscriptions": 0x7FFFFFFF}
    if "authentication" in hello:
        a = hello["authentication"]
        secret = base64.b64encode(
            hashlib.sha256((cfg["server_password"] + a["salt"]).encode()).digest()
        ).decode()
        ident["authentication"] = base64.b64encode(
            hashlib.sha256((secret + a["challenge"]).encode()).digest()
        ).decode()
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


def write_tone(path, seconds=30):
    rate, amp = 48000, 10 ** (TONE_DBFS / 20.0)
    with wave.open(path, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        for i in range(rate * seconds):
            v = int(amp * 32767 * math.sin(2 * math.pi * TONE_HZ * i / rate))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return path


def watch(ws, name, seconds=8):
    """Highest level OBS ITSELF reports for `name`, or None if never seen.

    Deliberately OBS's own `InputVolumeMeters` event, not this plugin's
    `audio_levels`. The question is what OBS does, so measuring it through
    our own plugin would be both circular and dependent on the plugin
    being installed — which is what made the first run of this probe
    report an inconclusive result with no level even when live.

    `inputLevelsMul` is linear multiplier, one array per channel, each
    [magnitude, peak, inputPeak]. Converted to dBFS here.
    """
    ws.settimeout(seconds)
    peak, deadline = None, time.time() + seconds
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            break
        d = msg.get("d", {})
        if msg.get("op") == 5 and d.get("eventType") == "InputVolumeMeters":
            for inp in d.get("eventData", {}).get("inputs", []):
                if inp.get("inputName") != name:
                    continue
                for chan in inp.get("inputLevelsMul", []):
                    for mul in chan:
                        if mul and mul > 0:
                            db = 20 * math.log10(mul)
                            if peak is None or db > peak:
                                peak = db
    return peak


def main():
    started_obs = None
    if subprocess.run(["pgrep", "-x", "OBS"], capture_output=True).returncode != 0:
        print("  starting OBS…")
        started_obs = subprocess.Popen(
            ["open", "-W", "-a", "OBS"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws = None
    for _ in range(30):
        time.sleep(2)
        with contextlib.suppress(Exception):
            ws = connect()
            break
    if ws is None:
        sys.exit("could not reach obs-websocket")

    prev = req(ws, "GetSceneCollectionList")["responseData"]["currentSceneCollectionName"]
    if prev == COLLECTION:
        sys.exit(f"OBS is already in {COLLECTION!r} — a previous run did not "
                 f"restore it. Switch back to your own collection and re-run.")

    colls = req(ws, "GetSceneCollectionList")["responseData"]["sceneCollections"]
    if COLLECTION in colls:
        req(ws, "SetCurrentSceneCollection", {"sceneCollectionName": COLLECTION})
    else:
        req(ws, "CreateSceneCollection", {"sceneCollectionName": COLLECTION})
    time.sleep(7)

    tone = write_tone("/tmp/probe-tone.wav")
    try:
        req(ws, "SetStudioModeEnabled", {"studioModeEnabled": False})
        time.sleep(1)
        live = req(ws, "GetCurrentProgramScene")["responseData"]
        live = live.get("currentProgramSceneName") or live.get("sceneName")

        # Establish the baseline explicitly instead of inheriting it. The
        # first run of this probe left 'offstage' as the current scene, and
        # the second run reused the collection — so the scene it called
        # INACTIVE was in fact the live one, and it measured nothing while
        # reporting an inconclusive result rather than a wrong one.
        for name in ("onstage", "offstage"):
            req(ws, "CreateScene", {"sceneName": name})  # no-op if present
        req(ws, "SetCurrentProgramScene", {"sceneName": "onstage"})
        time.sleep(2)
        live = req(ws, "GetCurrentProgramScene")["responseData"]
        live = live.get("currentProgramSceneName") or live.get("sceneName")
        if live != "onstage":
            sys.exit(f"could not make 'onstage' live (still {live!r}) — aborting "
                     f"rather than measuring the wrong scene")
        req(ws, "RemoveInput", {"inputName": "probe-tone"})  # from any prior run
        req(ws, "CreateInput", {
            "sceneName": "offstage", "inputName": "probe-tone",
            "inputKind": "ffmpeg_source",
            "inputSettings": {"local_file": tone, "looping": True, "is_local_file": True},
        })
        req(ws, "TriggerMediaInputAction", {
            "inputName": "probe-tone",
            "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})

        # "Monitor Only (mute output)" — what an operator would set to listen
        # ahead without putting the source into the broadcast mix.
        mon = req(ws, "SetInputAudioMonitorType", {
            "inputName": "probe-tone",
            "monitorType": "OBS_MONITORING_TYPE_MONITOR_ONLY"})

        print(f"\n  live scene            : {live}   (verified, not assumed)")
        print(f"  tone source is in     : 'offstage'  (NOT the live scene)")
        print(f"  Monitor Only accepted : {mon.get('requestStatus', {}).get('result')}")

        time.sleep(8)  # the plugin rescans every 5s

        rd = req(ws, "GetSourceActive", {"sourceName": "probe-tone"}).get("responseData", {})
        print(f"  OBS says videoActive  : {rd.get('videoActive')}")
        print(f"  OBS says videoShowing : {rd.get('videoShowing')}")

        inactive_peak = watch(ws, "probe-tone")
        print(f"\n  INACTIVE : level reported = {inactive_peak}")

        # Control. Without this, "no level" could just mean the tone never
        # played, and the whole measurement would prove nothing.
        req(ws, "SetCurrentProgramScene", {"sceneName": "offstage"})
        time.sleep(2)
        # Restart AFTER activation. A media source in an inactive scene does
        # not play, so the RESTART issued at creation did nothing and the
        # control silently measured silence — which is why three runs of this
        # probe reported "inconclusive" instead of an answer.
        req(ws, "TriggerMediaInputAction", {
            "inputName": "probe-tone",
            "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
        time.sleep(6)
        active_peak = watch(ws, "probe-tone")
        print(f"  ACTIVE   : level reported = {active_peak}")

        print("\n--- what this shows ---")
        if active_peak is None:
            print("  INCONCLUSIVE — no level even when live, so the tone never")
            print("  played and the inactive reading proves nothing.")
        elif inactive_peak is None:
            print(f"  No measurable audio while INACTIVE; {active_peak:.1f} dBFS once live.")
            print("  Consistent with 'an inactive source makes no sound', which is")
            print("  the gap this plugin and FrameSW's monitoring exist to fill.")
        else:
            print(f"  The INACTIVE source reported {inactive_peak:.1f} dBFS.")
            print("  That contradicts the assumption the product story rests on —")
            print("  re-check before repeating 'OBS cannot do this'.")
        print("\n  NOT ANSWERED: whether sound reaches headphones. Nothing here can")
        print("  hear the monitoring device. Put on headphones with the tone")
        print("  offstage and Monitor Only set, and listen.")
    finally:
        with contextlib.suppress(Exception):
            req(ws, "SetCurrentSceneCollection", {"sceneCollectionName": prev})
            os.remove(tone)
            ws.close()
        print(f"\n  restored collection: {prev}")
        if started_obs:
            subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'],
                           capture_output=True)


if __name__ == "__main__":
    main()
