# Spec: hearing the Preview mix

Sending Preview-scene audio to a headphone device, from inside the plugin,
bypassing the path OBS blocks.

Written 2026-08-31. **Not built.** This is the design to argue with before
any audio code exists.

---

## The gap it closes, confirmed not assumed

| for a Preview-staged source | OBS? |
|---|---|
| See its level | yes — mixer lists it, greyed, live meter |
| Adjust fader / mute | yes — measured exact: 6.0 dB, 12.0 dB, −100 muted |
| **Hear it** | **NO** — `Monitoring Enabled`, `Status: Inactive`, silent |
| Read it over obs-websocket | NO — absent from `InputVolumeMeters` |

The hearing row is the operator's own test with headphones and both outputs
on: a staged source with monitoring enabled plays nothing and becomes
audible only once it is on Program. That is the gap.

---

## Where the audio comes from — corrected 2026-08-31

**An earlier version of this spec claimed the plugin already receives the
Preview scene's composited mix, so the feature was just "route a stream we
already have". That is wrong, and the correction is the main cost in this
document.**

Measured twice: `attach_role_tap` attaches an audio capture callback to the
Preview and Program *scenes*, the OBS log confirms both attachments — and
neither scene ever produces a level. In OBS, audio goes from sources
straight to the mix; a scene composites *video*, it does not sum audio, so
a capture callback on a scene has nothing to fire on.

Two consequences:

- **`attach_role_tap` is dead code in this plugin.** It attaches, logs
  reassuringly, and delivers nothing. Worth deleting or documenting rather
  than leaving as a comfort.
- **The feature must sum the staged sources itself.** Per-source callbacks
  do work — the two-tone probe reads exactly −20.0 dBFS from Program's
  1 kHz source and −35.0 dBFS from Preview's 5 kHz source, so the plugin
  can identify and capture each staged source individually.

**FrameSW already does exactly this**, and its Preview/Program separation is
verified in streaming and recording. So the approach is proven in practice;
this is not new ground. FrameSW also has a known, separate issue in its
summing — individual channels being attenuated to hold the summed level —
which is worth knowing about before writing the same code twice.

### What summing adds to the job

- Sum every source in the Preview scene, each already post-fader (the
  plugin multiplies by `obs_source_get_volume` and honours mute — measured
  exact at 6.0 dB, 12.0 dB, and −100 when muted).
- Handle differing channel counts between sources.
- Decide headroom policy deliberately, given FrameSW's experience: naive
  summing clips, and per-channel attenuation to prevent it changes what the
  operator hears relative to what goes to air. Auditioning a mix that is
  quieter than the real one is its own kind of wrong.

## Path

```
OBS audio thread                    │  output thread (ours)
                                    │
preview scene capture callback      │
  │                                 │
  ├─ existing: peak → LEVELS        │
  └─ new: samples → ring buffer ────┼──> pull ──> device
                                    │
  MUST NOT: allocate, lock, log,    │  may block, may allocate,
  block, or call libobs             │  may recover from errors
```

**The ring buffer is the whole design.** Single-producer/single-consumer,
fixed capacity allocated once at startup, lock-free. The capture side only
ever writes and advances an atomic index.

### Why that side is unforgiving

The capture callback runs on OBS's audio thread. Anything slow there
degrades **OBS's own output** — the stream the user is broadcasting. This
is the same "never destabilise the host" rule that has already cost this
project real time (see `FINDINGS.md`).

Concretely, on the capture side:

- **No allocation.** Buffer sized once.
- **No mutex.** Note the existing code takes `LEVELS.lock()` in this same
  callback. That is a short, uncontended lock today, and it is a latent
  risk this feature makes worse rather than introduces. Worth revisiting.
- **No logging**, no formatting, no syscalls.
- **Overrun drops, never blocks.** If the consumer stalls, the producer
  overwrites and moves on. A glitch in the operator's headphones is
  acceptable; a glitch in the broadcast is not.

---

## Format

`obs_get_audio_info` gives OBS's sample rate and speaker layout. Note this
was **removed during the standalone split** and would need restoring.

- **Sample rate:** open the device at OBS's rate. If the device refuses,
  fail with a message naming both rates rather than resampling. Resampling
  is a v2 problem and a source of subtle bugs.
- **Channels:** take the first two planes, or duplicate mono to both. A
  headphone mix does not need surround.
- **Format:** OBS is 32-bit float planar by the time the callback fires
  (verified live). Most output APIs want interleaved — interleave on the
  *output* thread, not the capture thread.

---

## Device selection, and the honest problem with it

The plugin has **no UI**, and that is the real obstacle, not the audio.

Three options, in increasing cost:

**A. System default output.** Zero configuration, zero UI. Fails badly in
the common case: OBS's own output usually goes to the same default device,
so the operator hears the broadcast and the preview mixed together. Not
acceptable on its own.

**B. Config file.** A device name in a small file next to OBS's config.
Zero dependencies, works, and is invisible to a normal user. Reasonable as
a v1 to prove the audio path while the picker is being decided, not
reasonable as the shipped product.

**C. A Qt dialog in the Tools menu.** The real answer, and the expensive
one: it means linking OBS's Qt and giving up the "builds with no OBS SDK"
property this crate currently enjoys, on both platforms in CI.

**Recommendation: build B first, ship C.** B proves the hard part — the
ring buffer, the device, the realtime behaviour — with no dependency risk.
If the audio path turns out to be glitchy or high-latency, that is found
before paying for Qt.

---

## Failure modes, and what each must do

| what happens | required behaviour |
|---|---|
| Device disappears mid-show (unplugged) | stop cleanly, log once, keep OBS running. **Never** retry in the capture callback. |
| Device rate unsupported | refuse at startup with both rates named; do not silently resample |
| Consumer thread stalls | producer overwrites; drop audio, never block |
| No Preview scene (Studio Mode off) | no tap exists, so nothing to do — silent, not an error |
| OBS shutting down | the output thread must join before `obs_module_unload` returns, same rule the existing threads follow |

The unplug case deserves emphasis: the operator is *wearing* the device
that fails. A crash there takes OBS down mid-broadcast.

---

## What must be measured before this is called done

Not assumed:

1. **Latency, end to end.** Tone in Preview to sound in headphones. Above
   roughly 100 ms it stops being useful for judging a live mic.
2. **Does enabling it change OBS's own output?** Record with the monitor
   on and off, compare. This is the question that matters most, and the
   only acceptable answer is "no measurable difference".
3. **Sustained run.** An hour, watching for drift, buffer growth, or
   accumulating latency.
4. **Unplug the device while running.** Deliberately.

---

## Scope boundary

**In:** the Preview scene's composited mix, to one device.

**Out, deliberately:**

- Per-source solo/mute — OBS's mixer already does this for staged sources
- Any level control of its own — the OBS fader already applies, measured
- Program monitoring — OBS does this natively
- Resampling
- More than one output device

## Cost, stated plainly

A few days with real testing, not an afternoon, and the risk is
concentrated in one place: audio on OBS's own thread. It also changes what
this plugin *is* — from something that reports to something that produces
sound, with a dependency to match.

The counter-argument is that today the plugin does nothing a person can
see or use, which is a worse problem.
