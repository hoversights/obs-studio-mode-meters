# Windows load test — Studio Mode Meters

**For the Windows machine.** One job: run the load test and report what it
says. This is the last thing blocking a public v0.1.0 that claims Windows
support.

The README currently says *"Windows builds, not yet load-tested"* — that
sentence is the reason this document exists, and the point is to be able to
delete it honestly.

## Before you start — what "clean" means here

The test installs the plugin, drives a real OBS, and puts everything back.
It only behaves if it starts from a known state:

- **OBS must be closed.** The script starts its own.
- **obs-websocket must be enabled** (Tools → WebSocket Server Settings →
  Enable WebSocket server). The script reads the port and password from
  OBS's own config, so you do not need to tell it either.
- **A previously hand-installed copy must be gone.** Check
  `C:\ProgramData\obs-studio\plugins\` — if `studio-mode-meters` is already
  there from earlier testing, the script backs it up and restores it, but
  knowing it was there tells you which build you were looking at. A stale
  copy loading instead of a fresh build has cost real debugging time on this
  project before.

## Steps

```powershell
# 1. Get the current code
cd <wherever the clone lives>\obs-studio-mode-meters
git pull

# 2. One-time, if needed
pip install websocket-client
```

Then, **from an elevated PowerShell** (right-click → Run as administrator):

```powershell
python scripts\load-test.py
```

Elevation is required because OBS loads plugins from `%ProgramData%`, which
a normal user cannot write to. Without it the script stops at the install
step and tells you so — it will not misreport that as a plugin failure.

The run takes a couple of minutes: it builds, installs, starts OBS, plays a
1 kHz tone at a known −20.0 dBFS, measures it on Program and again with the
source only in Preview, then quits OBS and reads the log.

## What to send back

**The whole output, verbatim.** Not a summary — the PASS/FAIL lines are the
result, and a SKIP is not a failure.

If it stops early, also send:

- the last 40 lines of the newest file in
  `%APPDATA%\obs-studio\logs\`
- whether an OBS window appeared at all, and whether any dialog came up

## What a pass looks like

```
== Quantified metering (a 1000 Hz tone at -20.0 dBFS)
  PASS  the tone is metered on Program
  PASS  reported level matches the tone
  PASS  on_program is true while on Program

== Preview-only metering — what obs-websocket cannot do
  PASS  a Preview-only source is still metered
  PASS  on_program is false while only in Preview

== Unload cleanliness
  PASS  plugin logged its load
  PASS  plugin logged a clean unload
  PASS  no crash after unload

ALL CHECKS PASSED
```

`SKIP audio_levels events — no source produced audio` is **expected** and
not a failure. The plugin skips empty batches by design, so an OBS with
nothing making noise correctly emits nothing. The quantified stage below it
is the one that proves metering works.

## Three failures worth naming in advance

These are the ones where the message will not obviously mean what it says:

| what you see | what it actually means |
|---|---|
| Stops at `found obs64.exe` | OBS is installed somewhere the script did not look. Send the real path to `obs64.exe`. |
| OBS opens then immediately dies, or a "Failed to load locale" dialog | The working directory was wrong when OBS launched. That is the script's bug, not the plugin's — say so and send the path. |
| `no crash after unload` FAILS | The one genuinely serious result. Send the full OBS log; a crash on shutdown is the failure this whole script exists to catch. |

## Why this platform matters more than it looks

The Windows symbol-resolution path is genuinely different code. macOS gets
`RTLD_DEFAULT` and one `dlsym` call; Windows has no equivalent and instead
enumerates every module loaded in the process, trying `GetProcAddress`
against each until one answers. That path has never been exercised by this
test. If a libobs symbol fails to resolve there, the plugin degrades quietly
rather than crashing — which is the right behaviour and also means nobody
would notice without a test that checks the numbers.

## After it passes

Nothing for you to do. Reply with the output, and the README's "Tested
against" table and the disclosure line both get updated to say Windows is
verified — at which point v0.1.0 can go out for both platforms.
