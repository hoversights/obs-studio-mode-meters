# Open findings

## `active` reads false for a source on the Program scene — UNRESOLVED

Found 2026-08-14 by `scripts/load-test.py`, in an isolated profile and
scene collection:

```
PASS  the tone is metered on Program
PASS  reported level matches the tone — expected -20.0 dBFS, read -20.0 (off by 0.0 dB)
FAIL  active is true while on Program — active=False
```

The level is exact. The flag is wrong, and the flag is the entire point of
this plugin: `active` is what tells a client whether a metered source is on
Program or only staged in Preview. If it is unreliable, the plugin has
nothing to offer over obs-websocket's own meters.

**Not explainable as a failed symbol resolve.** `obs_source_active` is
fetched with `let Some(..) = obs_source_active() else { return }`, so an
unresolved symbol yields no level at all. A level arrived. The function ran
and returned false.

**What the test did**: created an `ffmpeg_source` in the scene returned by
`GetCurrentProgramScene`, triggered `RESTART`, waited past the plugin's 5s
rescan, and read the level it emitted for that source.

**FrameSW's success says nothing about this flag — it does not use it.**
Confirmed 2026-08-14 in the app's own handler:

```rust
for (name, peak_db, _active) in levels {
```

The underscore is the answer. FrameSW destructures `active` and discards
it, because FrameSW staged the shots itself and already knows which are on
Program and which are on Preview. So the flag has never been consumed by
anything, and no amount of production use could have exercised it. The
first thing that ever read it found it false.

That makes this more serious, not less. For FrameSW the field is spare. For
this plugin it is the entire product: a client with no FrameSW has no other
way to tell which bus a source is on, and the README promises exactly that.

**Worth checking first, in this order:**

1. Whether `obs_source_active()` is true for a source added directly to a
   flat scene versus one nested in FrameSW's `PGM-A`/`PGM-B` structure.
2. Whether Studio Mode being *off* changes what libobs considers active.
   The test collection has no Studio Mode; FrameSW's forces it on.
3. Whether the flag is read once at attach time and cached, rather than
   evaluated per callback.
4. Whether `obs_source_active` is the right libobs call at all.
   `obs_source_showing` covers Preview visibility, and the distinction
   between the two is precisely what this plugin claims to report.

**Do not publish the plugin until this is settled.** A directory listing
that says "distinguishes Program from Preview" has to be true.
