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

**Worth checking first, in this order:**

1. Whether `obs_source_active()` is true for a source added directly to a
   flat scene, versus one nested inside FrameSW's `PGM-A`/`PGM-B`
   structure. FrameSW's meters work in production, so the flag is probably
   right there — which would make this about scene structure or Studio
   Mode state, not the metering code.
2. Whether Studio Mode being *off* in the fresh collection changes what
   libobs considers active. The test collection has no Studio Mode; the
   FrameSW collection forces it on.
3. Whether the flag is read at attach time and cached, rather than per
   callback.

**Do not publish the plugin until this is settled.** A directory listing
that says "distinguishes Program from Preview" has to be true.
