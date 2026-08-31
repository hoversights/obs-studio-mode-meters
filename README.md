# Studio Mode Meters

An OBS Studio plugin that reports **per-source audio levels, separated by
Program and Preview**, over obs-websocket.

**Who this is for: people building on obs-websocket.** If you just want to
*look* at a staged source's level yourself, you do not need this plugin —
OBS's own Audio Mixer already shows Preview-scene sources, with a meter, a
fader and a mute button. Measured 2026-08-31, and worth saying plainly
because an earlier version of this README claimed otherwise.

What OBS does **not** do is put those levels on the wire. Its
`InputVolumeMeters` event covers the output mix only, so a source staged in
Preview is absent from it entirely. Measured on the same source, seconds
apart:

```
LIVE    : InputVolumeMeters reports ['staged-tone']  -20.00 dBFS
PREVIEW : InputVolumeMeters reports []               nothing
```

So if you are writing a client — a control surface, a Stream Deck plugin, a
hardware panel, your own switcher UI — there is no way to know whether the
source you are about to cut to is producing audio. This plugin provides
that, and does nothing else.

- **Read-only.** It attaches audio capture callbacks and reports what it hears.
  It creates nothing, renames nothing, changes no setting.
- **No vendor requests.** There is nothing to ask it to do. It emits events.
- **~10 events/second**, batched.

## Install

Drop the plugin into OBS's plugin directory and restart OBS.

| | path |
|---|---|
| macOS | `~/Library/Application Support/obs-studio/plugins/` |
| Windows | `C:\ProgramData\obs-studio\plugins\` |

On macOS the plugin is a `.plugin` bundle; on Windows a folder containing
`bin/64bit/*.dll`. Requires **obs-websocket**, which ships with OBS 28+.

Linux is not listed because it has never been built or tested here. The code
has nothing platform-specific outside `platform.rs`, so it may well work —
but that is a guess, and a guess does not belong in an install table.

Confirm it loaded — OBS's log will contain:

```
[studio-mode-meters] loaded — metering Program and Preview separately
[studio-mode-meters] registered as obs-websocket vendor "studio_mode_meters" — emitting audio_levels
```

If you see the first line but not the second, obs-websocket is not enabled.
Levels then reach OBS's own log only.

## Using it

Connect any obs-websocket client, subscribe to **vendor events**, and read
`audio_levels` from vendor `studio_mode_meters`:

```json
{
  "op": 5,
  "d": {
    "eventType": "VendorEvent",
    "eventData": {
      "vendorName": "studio_mode_meters",
      "eventType": "audio_levels",
      "eventData": {
        "levels": [
          { "name": "Host Mic",   "peak_db": -18.3, "on_program": true  },
          { "name": "Guest Cam",  "peak_db": -24.0, "on_program": false }
        ]
      }
    }
  }
}
```

| field | meaning |
|---|---|
| `name` | the OBS source name |
| `peak_db` | peak level in dBFS over the batch window; `-60` or lower is effectively silence |
| `on_program` | `true` when the source is in the scene currently on Program, `false` when it is in the Preview scene |

Subscribe with the `Vendors` event category (`1 << 9`). A client subscribed to
`General` only will receive nothing.

### Reading the levels

- **A muted source reports silence, not absence.** It stays in the payload with
  a very low `peak_db`, so a client can distinguish "muted" from "gone".
- **A silent OBS emits nothing at all.** Empty batches are skipped, so no events
  while nothing is producing audio is correct behaviour, not a fault.
- **Sources appear within about five seconds of being added.** The plugin
  rescans on that cadence; a source created a moment ago is not missing, just
  not yet seen.
- **A source appears only once its bus is known.** Until the plugin has seen
  it in the Program or Preview scene it is left out of the payload entirely,
  rather than being reported with a guessed `on_program`. So every
  `on_program` you receive is one the plugin can stand behind — but a brand
  new source is briefly absent rather than briefly wrong. A source in a scene
  that is on neither bus is likewise not reported.

### `on_program` is not obs-websocket's `GetSourceActive`

They can disagree, and when they do, this field is the one to trust for bus
membership.

`GetSourceActive` reports libobs's activation refcount. That refcount can lag:
a source added *directly into the scene currently on Program* while Studio Mode
is on reads `videoActive: false` there until the next transition, then corrects
itself. Measured on OBS 32.1.2 / obs-websocket 5.7.3.

`on_program` is derived from scene membership — which sources are in the scene
OBS reports as Program — so it has no such window. It is also why the field is
not called `active`: that name invited a comparison that misleads.

## What it does not do

- It does not meter what OBS is *outputting*. Use OBS's own
  `InputVolumeMeters` for that.
- It does not tell you a source is muted; ask obs-websocket (`GetInputMute`).
- It does not measure loudness (LUFS), only peak.
- It does not work without obs-websocket.

## Building

```
cargo build --release
```

Rust stable, and nothing else — no OBS SDK, no network access, no submodule
to init. The workspace is this crate plus `crates/core`; the only external
dependency is `windows-sys` on Windows, and none at all on macOS.

libobs is **not** linked at build time. Every libobs function is resolved at
runtime by symbol name, so the plugin builds without OBS headers or libraries
present, and degrades gracefully if a symbol is missing on an older OBS rather
than failing to load.

### Testing it in a real OBS

```
python3 scripts/load-test.py
```

Builds, installs, starts OBS, plays a 1 kHz tone at a known −20.0 dBFS, checks
the reported level against it on both buses, quits OBS, and verifies a clean
unload. It runs in its own OBS profile and scene collection and restores
whatever you had. Requires `websocket-client`.

The unload check matters most: the dangerous failure for an OBS plugin is not
"no events", which you notice, but a crash on shutdown, which you do not.

## Known issue: Windows, alongside the FrameSW Companion Plugin

If you have **both** this plugin and the [FrameSW Companion
Plugin](https://github.com/hoversights/framesw-obs-plugin) installed **on
Windows**, Preview-only metering stops working here — a source staged in
Preview reports no level at all. Program metering is unaffected, and so is
everything else.

Measured 2026-08-31: removing the other plugin and changing nothing else
makes it work. A 20-second wait does not, so it is not the rescan cadence.
**macOS with both installed is unaffected** — verified with both plugins
tapping the same Preview scene 170ms apart.

The mechanism is not yet established and is being investigated rather than
guessed at; see `FINDINGS.md`. If you only need one of the two on Windows,
install one. You do not need both: the FrameSW plugin already reports the
same levels to its own application.

## Tested against

| | |
|---|---|
| OBS Studio | 32.1.2 (macOS) |
| obs-websocket | 5.7.3 |
| Platforms | macOS verified; Windows builds, not yet load-tested |

## Vendor name

Requests and events use the obs-websocket vendor `studio_mode_meters`.

That string is a **public API contract**. Clients match on it, so changing it
later breaks every integration built against it. It is deliberately generic — it
describes what the plugin does, not who wrote it.

## Where this came from

The metering code was extracted from the [FrameSW Companion
Plugin](https://github.com/hoversights/framesw-obs-plugin), which is where it
was written and first proven. This repository has **its own copy** and does not
depend on that one: it builds, releases and versions on its own.

FrameSW is a commercial application. This plugin is not part of it, does not
require it, does not look for it, and sends nothing to it. Everything that only
made sense inside that product was removed rather than carried over — the
vendor requests, the NDI audio tap, the OBS configuration writes, and a Windows
registry lookup for FrameSW's install path. What is left cannot change a
setting in your OBS because there is no longer a function here that could.

## Development disclosure

This plugin is written with substantial use of AI coding tools (Claude), used
for implementation, refactoring and test-writing throughout. Direction, testing
and release engineering are the maintainer's.

Every release is built and tested against a real OBS installation before it
ships, reported bugs are triaged and answered, the complete source is
GPL-2.0-or-later and public here, and the commit history records what changed
and why.

## Licence

GPL-2.0-or-later — the same licence as OBS Studio, and required, since this
plugin links libobs. See `LICENSE`.

## Reporting problems

Open an issue with your OBS version, your platform, and the
`[studio-mode-meters]` lines from OBS's log.
