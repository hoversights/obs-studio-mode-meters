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

## The key simplification: nothing new gets created

The audio already arrives. `attach_role_tap("preview", …)` attaches an
audio capture callback to the **Preview scene source itself**, and a
scene's callback delivers its **composited mix** — every staged source
together, at their current faders, already summed.

So this feature is not "capture some sources and mix them". It is: take a
stream the plugin already receives and write it to a device.

- No new OBS source.
- No new capture callback.
- No mixing code.
- No interaction with OBS's blocked monitoring path at all.

What arrives is exactly what the operator would hear on TAKE, which is the
right thing to audition.

---

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
