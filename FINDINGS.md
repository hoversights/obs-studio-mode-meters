# Open findings

## `active` is the wrong mechanism — RESOLVED as a design defect, 2026-08-14

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
