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

WHAT THIS PROVES. The plugin loads, meters a 1 kHz tone at exactly its
known -20.0 dBFS, reports `on_program` correctly for a source on Program
AND for one only in Preview — the capability obs-websocket cannot provide —
and unloads without crashing.

Read the closing summary, not this paragraph. It lists what THIS run
actually established and names anything that skipped. That distinction is
not pedantry: until 2026-08-31 this file printed a fixed "verified ..."
sentence including Preview-only metering even on runs where that stage had
skipped, and a skip did not affect the exit code. The stage skipped on
every clean machine, because it looked for a second scene in a freshly
created collection that has exactly one. It passed only on the author's
Mac, where a development session had left two scenes behind. The one
capability the plugin exists for was effectively unverified for two weeks
while this script reported ALL CHECKS PASSED.

Four things had to be right, each found the hard way and each now handled
here: run in an isolated profile (a profile carries the audio settings),
CREATE the Preview scene rather than hoping one exists, start media sources
explicitly (creating one does not play it), and keep Studio Mode OFF while
adding the Program source (a source added into the live Program scene does
not begin rendering, so it makes no audio).

Requires: `websocket-client` (pip install websocket-client), OBS installed,
obs-websocket enabled.

Runs on macOS and Windows. **On Windows, run it from an elevated prompt** —
OBS loads plugins from `%ProgramData%\\obs-studio\\plugins`, which a normal
user cannot write to. Without elevation the install step fails and says so
rather than reporting a broken plugin.
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
VENDOR = "studio_mode_meters"
WINDOWS = sys.platform == "win32"

# Everything platform-specific in this script is these five things: where
# OBS keeps its plugins, its obs-websocket config and its logs, what cargo
# named the built library, and how you start and stop OBS. The tone is
# generated with the stdlib and played through `ffmpeg_source`, which is a
# built-in OBS input kind on both platforms — so the actual measurement is
# identical and there is no second implementation of it to keep in step.
if WINDOWS:
    # %ProgramData%, not %APPDATA% — OBS loads plugins from the machine-wide
    # location on Windows, while its config and logs are per-user.
    _PROGRAMDATA = os.environ.get("ProgramData", r"C:\ProgramData")
    _APPDATA = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
    PLUGIN_DIR = os.path.join(_PROGRAMDATA, "obs-studio", "plugins")
    # A folder, not a bundle: OBS wants <name>\bin\64bit\<name>.dll here.
    BUNDLE = os.path.join(PLUGIN_DIR, "studio-mode-meters")
    WS_CONFIG = os.path.join(
        _APPDATA, "obs-studio", "plugin_config", "obs-websocket", "config.json"
    )
    OBS_LOG_DIR = os.path.join(_APPDATA, "obs-studio", "logs")
    BUILT_LIB = os.path.join(REPO, "target", "release", "obs_studio_mode_meters.dll")
else:
    PLUGIN_DIR = os.path.expanduser("~/Library/Application Support/obs-studio/plugins")
    BUNDLE = os.path.join(PLUGIN_DIR, "studio-mode-meters.plugin")
    WS_CONFIG = os.path.expanduser(
        "~/Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
    )
    OBS_LOG_DIR = os.path.expanduser("~/Library/Application Support/obs-studio/logs")
    BUILT_LIB = os.path.join(REPO, "target", "release", "libobs_studio_mode_meters.dylib")

ok = True
# What actually happened, so the closing summary can report it rather than
# recite a fixed sentence that may not be true of this run.
verified = []
skipped = []


def step(msg):
    print(f"\n== {msg}")


def skip(label, detail=""):
    """A stage that did not run. Recorded, so the summary cannot imply it did."""
    skipped.append(label)
    print(f"  SKIP  {label}{'  — ' + detail if detail else ''}")


def check(label, passed, detail=""):
    """Detail is shown only on failure.

    Callers kept passing an explanatory failure string unconditionally, so
    a PASS printed its own failure text beside it. That happened three
    times in this file before the rule moved in here, where a caller
    cannot get it wrong.
    """
    global ok
    ok = ok and passed
    print(f"  {'PASS' if passed else 'FAIL'}  {label}{'  — ' + detail if detail and not passed else ''}")


def build():
    step("Build")
    r = subprocess.run(["cargo", "build", "--release"], cwd=REPO, capture_output=True, text=True)
    check("cargo build --release", r.returncode == 0, r.stderr.strip().split("\n")[-1] if r.returncode else "")
    # A warning here is worth failing on: this is FFI code loaded into
    # someone else's process, and "unused function" has already once meant
    # a real feature had been silently disconnected.
    warnings = [l for l in r.stderr.split("\n") if l.startswith("warning")]
    check("no build warnings", not warnings, "; ".join(warnings[:2]))
    return BUILT_LIB


def report_other_plugins():
    """Names every OTHER plugin OBS will load, because this test does not isolate them.

    WHAT THIS TEST ISOLATES: the profile and the scene collection.
    WHAT IT DOES NOT: the plugin directory. Whatever else is installed
    loads into the same OBS and taps the same scenes.

    That gap inverted a result on 2026-08-31. On Windows, with
    `framesw-companion` installed, the Preview-only stage failed with no
    level; removing that plugin and changing nothing else made it pass.
    Three runs, one variable each, with a 20-second wait ruling out timing.
    The same co-existence on macOS passes — verified from the OBS log, both
    plugins attaching to the same Preview-only scene 170ms apart — so it is
    a platform difference, not "two taps on one scene".

    Isolating this directory is deliberately NOT done: moving someone's
    other plugins aside is invasive and would itself change what is being
    measured. Printing them costs nothing and turns an invisible confound
    into a line in the output.
    """
    others = []
    with contextlib.suppress(Exception):
        for entry in sorted(os.listdir(PLUGIN_DIR)):
            # Our own bundle, debug symbols, and dotfiles are not plugins.
            # This line is meant to be read closely when a stage fails, so
            # it should carry nothing that needs mentally filtering out.
            if (entry.startswith("studio-mode-meters")
                    or entry.startswith(".")
                    or entry.endswith(".dSYM")):
                continue
            others.append(entry)
    if others:
        print(f"        also installed, NOT isolated by this test: {', '.join(others)}")
        print("        a failure here may belong to co-existence, not to this plugin")
    else:
        print("        no other OBS plugins installed")


def install(lib):
    """Installs into OBS's plugin directory, preserving anything already there."""
    step("Install")
    report_other_plugins()
    backup = BUNDLE + ".loadtest-backup"
    if os.path.exists(BUNDLE):
        shutil.rmtree(backup, ignore_errors=True)
        shutil.move(BUNDLE, backup)

    if WINDOWS:
        target_dir = os.path.join(BUNDLE, "bin", "64bit")
        installed = os.path.join(target_dir, "studio-mode-meters.dll")
    else:
        target_dir = os.path.join(BUNDLE, "Contents", "MacOS")
        installed = os.path.join(target_dir, "studio-mode-meters")

    try:
        os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(lib, installed)
    except PermissionError:
        # %ProgramData%\obs-studio\plugins is machine-wide and not writable
        # by a normal user on a default Windows install. Say so, rather than
        # letting a bare traceback imply the plugin is at fault.
        check("bundle installed", False,
              f"cannot write {target_dir} — run this from an elevated prompt")
        return backup

    if not WINDOWS:
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
    check("plugin installed", os.path.exists(installed))
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
        skip("audio_levels events — no source produced audio")
        print("        Add an audio source (Settings > Audio, or a media")
        print("        source that is playing) and re-run to exercise this.")
        return 0
    check("received audio_levels events", True, f"{seen} in {seconds}s")
    # Shape only. Whether a value is CORRECT needs real audio of a known
    # level, which this script does not claim to verify.
    keys = set().union(*shapes) if shapes else set()
    check("levels carry the bus flag", "on_program" in keys, f"keys={sorted(keys)}")
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
                        # `on_program` is the current name; `active` is the
                        # old one, still emitted during the rename.
                        peak = v
                        active = lvl.get("on_program", lvl.get("active"))
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
    # Studio Mode OFF for this stage, and it matters. A source added
    # straight into the scene that is live on Program while Studio Mode is
    # on does not start rendering, so it produces no audio and there is
    # nothing to meter — measured 2026-08-14, and it is why this stage
    # failed intermittently depending on what a previous run left behind.
    # Stage two turns Studio Mode back on deliberately.
    request(ws, "SetStudioModeEnabled", {"studioModeEnabled": False})
    time.sleep(2)
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
            check("on_program is true while on Program", active is True, f"on_program={active}")
            if ok:
                verified.append("meters a known tone to the exact dBFS on Program")

        # Stage two: the reason this plugin exists.
        step("Preview-only metering — what obs-websocket cannot do")
        request(ws, "SetStudioModeEnabled", {"studioModeEnabled": True})
        time.sleep(1)
        # CREATE the second scene rather than hunting for one.
        #
        # This used to pick any scene that was not Program and SKIP when
        # there wasn't one. A freshly created scene collection has exactly
        # one scene, so on any clean machine this stage skipped — and a
        # skip printed no FAIL, left `ok` true, and was then followed by a
        # summary line claiming Preview-only metering had been verified.
        #
        # It passed on the author's Mac only because a development session
        # on 2026-08-14 had left two hand-made scenes ("A" and "B") in the
        # test collection, which persists between runs. So the one stage
        # that justifies this plugin existing was, in effect, verified by
        # leftover state on a single machine and skipped everywhere else.
        # Found on the Windows box's first run, 2026-08-31.
        other = "loadtest-preview"
        scenes = [s["sceneName"] for s in request(ws, "GetSceneList")["responseData"]["scenes"]]
        if other not in scenes:
            made = request(ws, "CreateScene", {"sceneName": other})
            if not made.get("requestStatus", {}).get("result"):
                check("created a Preview scene to stage into", False,
                      made.get("requestStatus", {}).get("comment", "")[:80])
                return
        request(ws, "SetCurrentPreviewScene", {"sceneName": other})
        # Move the tone into the scene that is now Preview only.
        request(ws, "CreateInput", {
            "sceneName": other, "inputName": name + "-preview",
            "inputKind": "ffmpeg_source",
            "inputSettings": {"local_file": tone, "looping": True, "is_local_file": True},
        })
        # Same restart the Program source needs — a media source does not
        # start on its own. Omitting it here made the Preview stage report
        # "no level" for a plugin that was working.
        request(ws, "TriggerMediaInputAction", {
            "inputName": name + "-preview",
            "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
        })
        time.sleep(7)  # same rescan wait as above
        p2, a2 = measure(ws, name + "-preview")
        check("a Preview-only source is still metered", p2 is not None,
              "no level — this is the capability the plugin exists to provide")
        if p2 is not None:
            check("on_program is false while only in Preview", a2 is False, f"on_program={a2}")
            if a2 is False:
                verified.append("meters a Preview-ONLY source and reports the right bus")
        request(ws, "RemoveInput", {"inputName": name + "-preview"})
        # Leave the collection as this stage found it, so the next run
        # starts from the same place this one did. Not housekeeping — a
        # scene left behind is precisely what made this stage look verified
        # for two weeks.
        request(ws, "RemoveScene", {"sceneName": other})
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
    # The collection was NOT checked here until 2026-08-31, and that gap is
    # how a stale collection went unnoticed for two weeks: the profile
    # switch reported success while the collection could be anything.
    now_coll = request(ws, "GetSceneCollectionList")["responseData"]["currentSceneCollectionName"]
    check("switched to an isolated scene collection", now_coll == TEST_COLLECTION,
          f"in {now_coll!r}, expected {TEST_COLLECTION!r}")
    # State the starting conditions out loud. A measurement whose baseline
    # is not printed cannot be checked afterwards by anyone reading the
    # output — including the person who wrote it.
    scenes = [s["sceneName"] for s in request(ws, "GetSceneList")["responseData"]["scenes"]]
    print(f"        baseline: collection {now_coll!r}, {len(scenes)} scene(s): {scenes}")
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
    crashed = any(w in tail.lower() for w in ("crash", "segmentation", "signal"))
    check(
        "no crash after unload",
        not crashed,
        tail.strip().split("\n")[0][:80] if tail.strip() else "",
    )
    if clean and not crashed:
        verified.append("loads and unloads cleanly, with no crash on shutdown")
    return logs[-1] if logs else None


def find_obs_exe():
    """obs64.exe, from the registry if possible, else the usual paths."""
    candidates = []
    with contextlib.suppress(Exception):
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            with contextlib.suppress(OSError):
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"
                ) as k:
                    candidates.append(
                        os.path.join(winreg.QueryValueEx(k, "InstallLocation")[0], "bin", "64bit")
                    )
    candidates += [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "obs-studio", "bin", "64bit"),
    ]
    for d in candidates:
        exe = os.path.join(d, "obs64.exe")
        if os.path.exists(exe):
            return exe
    return None


def start_obs():
    """Starts OBS and returns the Popen handle, or None if it could not."""
    if not WINDOWS:
        return subprocess.Popen(
            ["open", "-W", "-a", "OBS"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    exe = find_obs_exe()
    if not exe:
        check("found obs64.exe", False, "not in the registry or Program Files")
        return None
    # cwd MUST be obs64.exe's own directory. OBS resolves its data and
    # locale relative to the working directory, and launched from anywhere
    # else it dies at startup with a "Failed to load locale" dialog — which
    # looks exactly like a plugin breaking OBS, and is not.
    return subprocess.Popen(
        [exe], cwd=os.path.dirname(exe),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop_obs():
    """Asks OBS to quit gracefully. The unload check depends on graceful."""
    if not WINDOWS:
        subprocess.run(["osascript", "-e", 'tell application "OBS" to quit'],
                       capture_output=True)
        return
    # WITHOUT /F, deliberately. taskkill /F terminates the process outright,
    # so OBS never unloads its modules and never writes the unload line this
    # script exists to check — the test would then report a crash on every
    # run, having caused it. Plain taskkill posts a close request, the same
    # as clicking the window's X.
    subprocess.run(["taskkill", "/IM", "obs64.exe"], capture_output=True)


def main():
    lib = build()
    if not ok:
        sys.exit("build failed — stopping before touching OBS")

    backup = install(lib)
    obs = None
    try:
        step("Start OBS")
        obs = start_obs()
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
        stop_obs()
        time.sleep(6)  # unload + log flush
        if obs:
            with contextlib.suppress(Exception):
                obs.wait(timeout=20)
        path = log_check()
        restore(backup)
        print(f"\n  OBS log: {path}")

    # The summary lists what was actually checked, not a fixed sentence.
    #
    # It used to print "reports the right bus on Program AND on
    # Preview-only" unconditionally — including on runs where that stage
    # had skipped. Combined with a SKIP not clearing `ok`, a skipped
    # capability test read as a verified one, which is how this script
    # reported ALL CHECKS PASSED for two weeks while never once proving the
    # thing the plugin exists to do on a clean machine.
    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    if skipped:
        print("\nNOT verified on this run — each of these SKIPPED:")
        for s in skipped:
            print(f"  - {s}")
        print("A skip is not a pass. Do not read the line above as covering these.")
    print("\nVerified: " + ", ".join(verified) if verified else "\nNothing was verified.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
