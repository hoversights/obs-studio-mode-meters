# Studio Mode Meters

An OBS Studio plugin that reports **per-source audio levels, separated by
Program and Preview**, over obs-websocket.

OBS already meters audio, but it meters what is going *out*. In Studio Mode the
thing an operator needs before cutting is different: is the source I am about to
take live producing audio *right now*, while it is still only in Preview? OBS's
own meters cannot answer that — a Preview-only source is not in the output mix,
so it never appears in `InputVolumeMeters` at all.

This plugin answers it, and does nothing else.

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
| Linux | `~/.config/obs-studio/plugins/` |

On macOS the plugin is a `.plugin` bundle; on Windows a folder containing
`bin/64bit/*.dll`. Requires **obs-websocket**, which ships with OBS 28+.

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

Rust stable. The only direct dependency is the shared metering crate (see
"Relationship to FrameSW"); it in turn pulls `windows-sys` on Windows and
nothing at all elsewhere.

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

## Relationship to FrameSW

The metering engine is currently shared with the [FrameSW Companion
Plugin](https://github.com/hoversights/framesw-obs-plugin) through a Rust crate,
pinned to a specific revision so a change there can never ship into your OBS on
its own.

That sharing is temporary and deliberate: while the code is still moving, one
copy means a fix lands in both. Once this plugin has proven itself, the crate is
copied in here and the dependency deleted, leaving this repository standalone.

FrameSW is a commercial application. This plugin is not part of it, does not
require it, and sends nothing to it.

## Licence

GPL-2.0-or-later — the same licence as OBS Studio, and required, since this
plugin links libobs. See `LICENSE`.

## Reporting problems

Open an issue with your OBS version, your platform, and the
`[studio-mode-meters]` lines from OBS's log.
