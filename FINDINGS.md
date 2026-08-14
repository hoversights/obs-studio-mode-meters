# Open findings

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
