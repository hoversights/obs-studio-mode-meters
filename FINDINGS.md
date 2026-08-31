# Open findings

## CORRECTION 2026-08-31: OBS's mixer DOES show and control staged sources

**This supersedes the README's original framing and it narrows what this
plugin is for.** The claim was that OBS "cannot answer" whether a
Preview-staged source is producing audio. In OBS's own Audio Mixer it
plainly can: a Preview-scene source is listed, badged `Preview`, with a
live meter, a working fader, and mute and monitor buttons. Seen directly in
the OBS window, not inferred.

Three things were measured to pin down what is and is not true:

**1. A source in a scene that is on neither bus makes no sound.**

```
Monitor Only accepted : True      videoActive : False
INACTIVE : no level               ACTIVE : -20.00 dBFS
```

**2. A Preview-staged source's fader and mute work exactly.** Volume driven
through the same API the OBS fader uses, measured through this plugin:

```
volume 1.00  (0 dB)  -> -20.00 dBFS
volume 0.50  (-6 dB) -> -26.02 dBFS      drop  6.0 dB
volume 0.25 (-12 dB) -> -32.04 dBFS      drop 12.0 dB
muted                -> -100.0 dBFS
```

**3. But obs-websocket does NOT report it.** Same source, seconds apart:

```
LIVE    : InputVolumeMeters reports ['staged-tone']  -20.00 dBFS
PREVIEW : InputVolumeMeters reports []               nothing
```

### What this changes

- **The gap is on the WIRE, not on the screen.** An operator looking at OBS
  can already see and adjust a staged source. A *client* — control surface,
  Stream Deck plugin, hardware panel, another switcher UI — cannot, because
  `InputVolumeMeters` covers the output mix only.
- **A meter/mixer dock in OBS is not worth building.** It would duplicate
  the Audio Mixer sitting a few inches away. This was seriously considered
  and is dropped on the evidence.
- **The audience is integrators, not end users.** The plugin has no UI and
  needs a client to be useful at all, which is consistent with that.

### How the wrong belief survived so long

`PLUGIN_SPLIT_PLAN.md` asserted it in 2026-08-08 and parked the bench test
meant to check it (Phase 0.5, Test B). Everything downstream — this repo's
README, the framing of FrameSW's monitoring — was built on the unchecked
assertion. It was settled in minutes by looking at the OBS window, which no
one had done.

**The measurable half of the claim is true and is what the README now
says.** It was the *scope* that was wrong: "OBS cannot show you this"
should always have been "obs-websocket cannot tell your client this."


## MEASURED 2026-08-31: an inactive source produces no audio, and Monitor Only does not change that

The premise this plugin rests on, and which `PLUGIN_SPLIT_PLAN.md` asserted
without measuring ("Monitoring Preview-only audio in headphones is a real
gap in OBS"). Its own bench test, Phase 0.5 Test B, had been parked since
2026-08-08. **Now measured** — `scripts/probe-inactive-audio.py`, macOS,
OBS's own `InputVolumeMeters` rather than this plugin's event:

```
Monitor Only accepted : True          <- OBS accepts the setting
videoActive           : False         <- source genuinely inactive
INACTIVE : level = None               <- no audio at all
ACTIVE   : level = -20.00 dBFS        <- the exact tone, once live
```

The control is what makes it mean anything: the *same source*, at the same
settings, reads exactly its known −20.0 dBFS when live and produces nothing
when its scene is not. So OBS accepts "Monitor Only" on an inactive source
and it does nothing.

**Consistent with, and explained by, the mixer's own behaviour:** the Audio
Mixer lists sources in the *current* scene plus the global devices from
Settings → Audio. A source in an inactive scene is not in that list, so its
monitor (headphone) button does not exist to be pressed.

### It took four runs, and the failures are the useful part

- **Run 1** measured through *this plugin's* event — circular, and the
  plugin was not installed, so nothing was ever reported.
- **Run 2** switched to OBS's native `InputVolumeMeters`, but reused the
  previous run's scene collection, which had left `offstage` as the current
  scene. The scene it called INACTIVE was live.
- **Run 3** established the baseline explicitly and still read nothing,
  because `TriggerMediaInputAction: RESTART` had been issued while the
  scene was inactive — a media source in an inactive scene does not play,
  so the control measured silence and proved nothing.
- **Run 4** restarted the media *after* activation and produced the result
  above.

Each bad run reported **INCONCLUSIVE**, never a confident wrong answer,
purely because the control was checked before the result. A probe without
that control would have "confirmed" the premise on run 1 by accident.

### Still not answered

Whether sound reaches headphones. Nothing in this probe can hear the
monitoring device; a negative reading is "no evidence of audio", not
"proven silent". If OBS routed audio to a monitor device with no meter, this
would not see it. That last step needs a person with headphones.


## OPEN 2026-08-31: Preview-only metering fails on WINDOWS when FrameSW's plugin is also installed

**Windows only. macOS is unaffected, and that is measured, not assumed.**

Reported from the Windows machine after the `on_program` fix landed. Three
runs, one variable changed each time:

| run | `framesw-companion` | preview wait | Preview-only result |
|---|---|---|---|
| 1 | installed | 7s | FAIL — no level |
| 2 | installed | 20s | FAIL — no level |
| 3 | absent | 7s | **PASS** |

Run 2 rules out timing: the tap was live 14.7 seconds before measurement
and still read nothing. Run 3 isolates the cause to co-existence.

Also established there, and worth keeping because it closes off the
obvious suspects: no symbol-resolution failure appears in any log, and
Studio Mode Meters attaches its taps correctly every time — including to
the right Preview scene, 33ms after FrameSW attaches to the same one. So
the Windows enumerate-every-module resolution path is exercised and works.

### macOS does the same thing and passes

From the OBS log of the passing macOS run on 2026-08-30 (20-03-17), with
both plugins installed and loaded:

```
20:03:18.048  [studio-mode-meters] loaded
20:03:18.049  [framesw] registered as obs-websocket vendor "framesw"
20:03:59.282  [framesw]             attached real audio tap to preview scene 'loadtest-preview'
20:03:59.452  [studio-mode-meters]  attached real audio tap to preview scene 'loadtest-preview'
```

Both plugins, both tapping the same Preview-only scene 170ms apart, and the
stage passed.

**This matters for where to look.** "Two audio capture callbacks on an
inactive source" is not on its own the mechanism — that is exactly what
macOS does here, and it works. Whatever this is, it is specific to Windows.

### Why it is not a release blocker, and is not nothing either

Most people installing a community metering plugin will not have
`framesw-companion`. But FrameSW's own users are precisely the people who
would have both, so this cannot go undocumented.

Not yet measured, and not guessed at here. The next step is to establish
what libobs on Windows does with a second `obs_source_add_audio_capture_callback`
on a source that is not active — measured, not reasoned about.

### What it already changed

`scripts/load-test.py` now prints every other installed plugin at the
Install step. The test isolates the profile and the scene collection but
NOT the plugin directory, so its result depends on what else is present —
invisible on a machine where that set never changes, and it inverted the
outcome here.


## FIXED 2026-08-31: `on_program` was wrong for a source on Program

Two independent defects, found together, fixed separately. The second is
the one that nearly shipped a lie; the first is the one that was actually
producing the wrong answer.

**1. The bus map was built from scene roles up to 5 seconds old.**
`attach_scene_audio_taps` queued the UI-thread role read *without waiting*
and then immediately read the cache — so it always used the previous
cycle's answer. `refresh_source_bus` now runs inside
`cache_scene_roles_on_ui_thread`, holding the names that task just read, so
the map can never disagree with the roles it was built from. The old
comment calling one cycle of staleness "harmless" was wrong: harmless for
the taps, which only decide where audio is sampled; the entire answer for
the bus map, which decides what the event claims.

**This was the cause of the observed failure.** Isolated by reintroducing
the `.unwrap_or(false)` below on top of the staleness fix: the load test
then passed, which proves the staleness alone produced the wrong reading.

**2. `source_is_on_program` conflated "unknown" with "Preview".** Now
`source_bus() -> Option<bool>`, and the emit loop DROPS a source whose bus
is unknown rather than sending a guessed flag. A level is only emitted once
the plugin can stand behind the bus on it.

### The load test does NOT cover defect 2 — read this before trusting it

With the staleness fixed, `SOURCE_BUS` is always populated by the time the
load test measures, so the missing-key path is never taken. Verified by
deliberately reintroducing `.unwrap_or(false)` and re-running: **the load
test passed.** It cannot catch that defect, and claiming otherwise would
repeat the mistake this whole entry is about.

Covered instead by three unit tests in `metering.rs` (`bus_tests`), each
checked against the broken behaviour first and observed to fail with
`left: Some(false), right: None`.

### Verified on a clean baseline

Leftover test profile and collection removed first, so OBS created both
fresh — the same starting state any other machine has. macOS, OBS 32.1.2:

```
baseline: collection 'studio-mode-meters-test', 1 scene(s): ['Scene']
PASS  the tone is metered on Program
PASS  reported level matches the tone
PASS  on_program is true while on Program
PASS  a Preview-only source is still metered
PASS  on_program is false while only in Preview
```

Still outstanding: **Windows has not re-run since the fix.**

---

## The original report, kept for the reasoning

**Do not release, and do not claim Preview-only metering is verified — on
either platform.**

```rust
pub fn source_is_on_program(name: &str) -> bool {
    SOURCE_BUS.lock().ok()
        .and_then(|g| g.as_ref().and_then(|m| m.get(name).copied()))
        .unwrap_or(false)
}
```

`SOURCE_BUS` is rebuilt every rescan by walking the Program and Preview
scenes. A source that is not in that map — because it was created since the
last walk, or the walk could not resolve a symbol, or the scene roles were
not yet cached — returns **`false`**, which is the same answer as a source
genuinely staged in Preview.

### How it surfaced, and what it invalidates

`scripts/load-test.py` staged the tone into the **Program** scene with
Studio Mode off and read `on_program=False`. Measured on macOS against a
clean profile and collection, OBS 32.1.2:

```
== Quantified metering (a 1000 Hz tone at -20.0 dBFS)
  PASS  the tone is metered on Program
  PASS  reported level matches the tone
  FAIL  on_program is true while on Program  — on_program=False
```

The plugin's own log for the same run shows the ordering:

```
19:45:44  attached real audio tap to preview scene 'Scene'
19:45:55  (tone source created in 'Scene')
19:46:00  attached real audio tap to program scene 'Scene'
```

**The consequence for the test is worse than the failure itself.** Stage two
asserts `on_program is false while only in Preview`. With this default, that
assertion passes when the plugin is working *and* when it knows nothing at
all. It is an assertion that cannot fail for the reason it claims to test —
so the Preview-only capability, the entire justification for this plugin,
has never actually been demonstrated by an automated test on any platform.

Everything else in that run stands: the tone meters at the exact dBFS, and
the plugin loads and unloads cleanly.

### Why it was invisible until now

The Preview stage used to look for any scene other than Program and SKIP
when there was none. A freshly created scene collection has exactly one
scene, so it skipped on every clean machine — while the closing summary
printed a fixed sentence claiming Preview-only metering had been verified,
and a skip did not affect the exit code. It only ran at all on the author's
Mac, where a development session on 2026-08-14 had left scenes `A` and `B`
in the test collection.

Found when the Windows machine ran it for the first time on 2026-08-31 and
asked why the stage skipped.

### What a fix has to do

Not simply flip the default. The three states are genuinely different and
the API currently has two:

| state | correct answer |
|---|---|
| in the Program scene | `on_program: true` |
| in the Preview scene | `on_program: false` |
| not seen in either yet | **neither** — the source should be absent from the payload, or carry an explicit unknown |

Emitting a level with a confident-looking `on_program` for a source whose
bus is unknown is the same class of mistake as the original `active` field
(below): a field whose name promises more than the value behind it knows.
Whatever is chosen, the load test must be able to fail for the right reason
— today it cannot.


## OPEN 2026-08-30: the rescan cannot be paused, and nothing can pause it

The plugin re-attaches its audio capture callbacks every 5 seconds, on its
own thread, on its own timer. That means it calls `obs_get_source_by_name`
and `obs_source_add_audio_capture_callback` at moments it does not choose
and cannot be told about — including while another obs-websocket client is
creating, renaming or swapping the very scenes it is attaching to.

`RESCAN_PAUSED` exists for exactly that window, and the plugin it was
extracted from drives it: that application pauses the rescan around its own
scene setup and resumes afterwards. **Here it is inert.** Setting it needs a
vendor request, and this plugin deliberately registers none.

**Not known to be a problem, and deliberately not claimed to be one.** The
crash that motivated the flag was in a different product, under a specific
sequence, and a later root-cause pass on a related crash found the first
explanation of it was wrong — so it is not repeated here as fact. What can
be said honestly:

- no crash has been observed in this plugin from this cause;
- the load test does not exercise it, because it never creates scenes while
  the rescan is running;
- and if it does turn out to matter, the fix is a design decision, not a
  patch: adding `pause_rescan`/`resume_rescan` means this plugin starts
  registering vendor requests, which is the one thing it currently promises
  not to do.

Worth writing down before release rather than discovering in an issue.

## `active` is the wrong mechanism — FIXED 2026-08-14

**Not a bug in the plugin's reporting.** Measured directly: for the same
source at the same moment, obs-websocket's own `GetSourceActive` returns
exactly what the plugin reports.

```
OBS says a_only     : {'videoActive': False, 'videoShowing': False}
OBS says with_video : {'videoActive': False, 'videoShowing': False}
plugin reports      : active=False
```

The plugin faithfully relays `obs_source_active()`. It is not lying.

**The problem is that `obs_source_active()` answers a different question.**
Both sources above were in `PGM-B`, which `GetCurrentProgramScene` reported
as the Program scene, with Studio Mode on and `PGM-A` as Preview. Even the
source *with video* read false. Whatever `obs_source_active` tracks, it is
not "is this source on the Program bus", which is what this plugin claims
to report.

**Why nothing caught it before.** FrameSW discards the flag —

```rust
for (name, peak_db, _active) in levels {
```

— because FrameSW staged the shots itself and already knows which bus each
is on. The field has therefore never been consumed by anything. Production
use could not have exercised it, and the first thing that ever read it
found it wrong.

**Is this a bug report?** No. Nothing user-facing is broken today: FrameSW
does not use the flag, and this plugin is unreleased. It is a design defect
in an unpublished feature, caught before anyone depended on it.

### The fix

Stop asking each source whether it is active. **The plugin already knows
the roles**: `attach_role_tap` attaches a tap to the Program scene and
another to the Preview scene, and tracks which is which. The bus a sample
belongs to is *which tap it arrived through* — authoritative by
construction, with no per-source query that can disagree.

`obs_source_showing` is not the answer either. It would be true for a
source visible in either bus, which is the distinction being asked about.

Until this is done, the README's claim to distinguish Program from Preview
is not supported by the code. **Do not publish.**


---

## Why `obs_source_active` could never have worked — measured 2026-08-14

Clean collection, plain scenes, Studio Mode on, program `A` and preview
`B`, one colour source in each, measured idle and again while **recording**
so OBS was definitely rendering to a real output:

```
onprog : videoActive=False  videoShowing=True
onprev : videoActive=False  videoShowing=True
```

**Both false, both showing, recording or not.** `obs_source_active()` is
false for everything in Studio Mode, so it never distinguished the buses —
it was not returning a wrong answer, it was returning the same answer for
both. `obs_source_showing()` is true for both and cannot distinguish them
either.

No per-source libobs query answers "which bus". Scene membership, which is
what the plugin now uses, is the only thing that can.

### Consequence for the public API: rename the field before publishing

A client that compares our `active: true` against obs-websocket's
`GetSourceActive` will see `videoActive: false` and conclude we are wrong.
We are not — OBS's flag is uninformative here — but the name invites
precisely that comparison.

Rename `active` to `on_program` before anyone depends on it. Blocked on a
coordinated change: FrameSW parses the event with
`entry.get("active")?.as_bool()?`, and `?` means a missing key fails the
whole parse, so both repositories must change together. FrameSW never reads
the value, only requires the key to exist.


---

## CORRECTION 2026-08-14: `obs_source_active` does work in Studio Mode

The section above concluded it is "false for everything in Studio Mode".
That is wrong, and this supersedes it.

Same source, same scene, OBS 32.1.2 / obs-websocket 5.7.3:

```
studio mode OFF, source in the current scene : videoActive=True
studio mode ON,  same source on Program      : videoActive=True
studio mode OFF again                        : videoActive=True
```

Studio Mode is not the variable. **How the source was created is.** In this
run it was added while Studio Mode was OFF and stayed active through the
transition. In every earlier run it was added *while Studio Mode was
already on*, and those read false — including while recording, so it was
not a rendering-idle artefact.

### The open question, and it may be an OBS bug

A source added to the Program scene while Studio Mode is on appears never
to gain an activation reference. If that reproduces cleanly it is worth
reporting upstream, the same way the obs-websocket NULL-scene crash was
(obsproject/obs-websocket#1349).

Not yet isolated enough to file: it needs confirming that a Preview-only
source reports `videoActive=false` under the same conditions, which would
establish that the call *does* distinguish the buses when sources are
created normally.

### Does the membership fix still stand?

Yes, and for a better reason than the one first given. It does not depend
on when or how a source was added, so it cannot be caught by whatever
causes the missing activation reference. But the earlier justification —
"no per-source query can distinguish the buses" — is not established, and
should not be repeated until the Preview-only case is measured.


---

## SETTLED 2026-08-14: the call works; the anomaly is creation-order

Both sources created with Studio Mode OFF, so both activated normally,
then Studio Mode enabled. OBS 32.1.2 / obs-websocket 5.7.3:

```
studio ON, program=A preview=B
   srcA (Program): videoActive=True   videoShowing=True
   srcB (Preview): videoActive=False  videoShowing=True

swapped:   program=B preview=A
   srcA: videoActive=False  srcB: videoActive=True
```

`obs_source_active` distinguishes Program from Preview correctly, and
follows a TAKE. It is the right call for this job, and every earlier
conclusion here that said otherwise was measuring one specific anomaly and
generalising from it.

**The anomaly, precisely:** a source created via obs-websocket's
`CreateInput` *while Studio Mode is already on* never gains an activation
reference — it reads `videoActive=false` on the Program scene indefinitely,
including while recording. A source created before Studio Mode is enabled
behaves correctly.

Still unknown, and required before filing upstream: whether adding a source
through the OBS **UI** while in Studio Mode has the same problem. If it
does, it is a libobs issue; if not, it belongs to obs-websocket's
`CreateInput`.

### Why the plugin keeps scene membership anyway

Not because `obs_source_active` is wrong — it is not. Because the anomaly
lands exactly where operators live: **adding a source during a show, with
Studio Mode on.** Such a source would meter correctly and report the wrong
bus forever. Membership does not care when or how a source was added.

The cost is that our `active` will disagree with `GetSourceActive` in that
one case — ours right, OBS's stale. That is the correct side to be on, and
it strengthens the case for renaming the field to `on_program` so nobody
reads the two as the same claim.


---

## CLOSED 2026-08-14: not an OBS bug, not worth filing

The UI check settled it, then isolating the real variable settled it
further. `uitest`, added through OBS's UI in Studio Mode and transitioned
to Program, read `videoActive=True`. So did `ws_taken`, added through
obs-websocket into Preview and then transitioned. The API is not the
variable.

| how the source got there | result |
|---|---|
| into Preview, then transitioned (UI or API) | correct immediately |
| directly into the live Program scene (API only) | false until the next transition, then self-corrects |

It recovers: after one transition cycle `ws_direct` read `videoActive=True`,
and stayed correct with Studio Mode off.

**No upstream report.** It is transient staleness that fixes itself, and it
is only reachable through the API — OBS's UI does not let you add a source
to the Program scene while Studio Mode is on. Earlier notes in this file
implying a libobs bug were premature.

**Membership stays**, on modest grounds rather than the sweeping ones
claimed earlier: it is immune to that window and now costs nothing. With
FrameSW driving transitions the window would close on the next TAKE anyway.
