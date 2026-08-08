# Studio Mode Meters

An OBS Studio plugin that reports **per-source audio levels, separated by
Program and Preview**, over obs-websocket.

OBS already meters audio, but it meters what is going *out*. In Studio Mode
the thing an operator actually needs before cutting is different: is the
source I am about to take live producing audio *right now*, while it is still
only in Preview? OBS's own meters cannot answer that, because a Preview-only
source is not in the output mix.

This plugin answers it. It attaches to the current Program and Preview scenes
by **role** — whatever OBS reports those scenes to be — and publishes levels
for the sources in each, so a controller can show two independent meter sets.

## Status

**Early. Nothing here is released yet.** The repository exists ahead of the
code so the name and the vendor string are claimed before anything depends
on them.

## Vendor name

Requests are registered under the obs-websocket vendor `studio_mode_meters`.

That string is a **public API contract**. obs-websocket clients match on it,
so changing it later breaks every integration built against it. It is
deliberately generic — it describes what the plugin does, not who wrote it.

## Licence

GPL-2.0-or-later — the same licence as OBS Studio, and required: this plugin
links libobs.

See `LICENSE` for the full text.

## Relationship to FrameSW

The metering core here is shared with the [FrameSW Companion
Plugin](https://github.com/hoversights/framesw-obs-plugin) via a Rust crate
consumed at **compile time**. That is the whole relationship.

Two independent plugin binaries are built and a user installs one of them.
Neither talks to the other, neither requires the other, and this plugin does
not look for FrameSW, read its configuration, or behave differently when it
is present.

## Reporting problems

Open an issue on this repository. Include your OBS version, your platform,
and the relevant lines from OBS's own log (Help → Log Files → View Current
Log).

## Development disclosure

This plugin is written with substantial use of AI coding tools (Claude), used
for implementation, refactoring and test-writing throughout. Direction,
testing and release engineering are the maintainer's.

Every release is built and tested against real OBS installations on macOS and
Windows before it ships, reported bugs are triaged and answered, the complete
source is GPL-2.0-or-later and public here, and the commit history records
what changed and why.
