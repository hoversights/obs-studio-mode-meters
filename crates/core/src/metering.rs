// SPDX-License-Identifier: GPL-2.0-or-later
//! Per-source audio metering that distinguishes Program from Preview.
//!
//! The whole reason this plugin exists: obs-websocket can report a level
//! for a source that is live, but not for one only staged in Preview.
//! Everything here is read-only with respect to OBS state — it attaches
//! audio capture callbacks and reads levels, and changes nothing.
//!
//! Nothing here names the plugin it serves: the vendor name and log prefix
//! arrive via `crate::set_identity`. That indirection is kept because it is
//! what holds a product name out of the FFI layer, and it costs one call at
//! module load.
//!
//! See this crate's `lib.rs` for the second-copy warning — the FFI in this
//! file has a sibling in another repository.

#![allow(dead_code)]

use crate::obs_data::{self, SourceLevel};
use crate::{calldata, log_line};
use std::collections::HashMap;
use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
use std::sync::Mutex;

// The `AudioSink` hook was removed with the split. FrameSW installs a
// sink here to forward the same audio to its NDI monitor tap; this
// plugin never installed one. With no hook at all, "audio never leaves
// this module" is structural rather than a convention to be trusted.

// ---------------------------------------------------------------------
// libobs FFI surface — only what this phase needs.
// ---------------------------------------------------------------------

/// Opaque — same story for `obs_source_t` (`libobs/obs.h`).
pub enum ObsSourceT {}

/// `libobs/media-io/media-io-defs.h`: `#define MAX_AV_PLANES 8`.
pub const MAX_AV_PLANES: usize = 8;

/// `libobs/media-io/audio-io.h`'s `struct audio_data` — verbatim field
/// order/types, required for correct ABI since this is a real (not
/// opaque) struct passed by pointer into our callback.
#[repr(C)]
pub struct AudioData {
    pub data: [*mut u8; MAX_AV_PLANES],
    pub frames: u32,
    pub timestamp: u64,
}

/// `libobs/obs.h`:
/// `typedef void (*obs_source_audio_capture_t)(void *param, obs_source_t *source, const struct audio_data *audio_data, bool muted);`
pub type ObsSourceAudioCaptureT =
    extern "C" fn(param: *mut c_void, source: *mut ObsSourceT, audio_data: *const AudioData, muted: bool);

/// `libobs/obs.h`: `void obs_enum_sources(bool (*enum_proc)(void *, obs_source_t *), void *param);`
pub type ObsEnumSourcesProc = extern "C" fn(param: *mut c_void, source: *mut ObsSourceT) -> bool;

// Resolved at runtime (`platform::resolve_as` via `resolved_fn!`), not
// linked at build time — see `platform.rs`'s module doc for why. Exact
// signatures confirmed against obs-studio@master's `libobs/obs.h`.
crate::resolved_fn!(obs_enum_sources: extern "C" fn(ObsEnumSourcesProc, *mut c_void));
crate::resolved_fn!(obs_source_add_audio_capture_callback: extern "C" fn(*mut ObsSourceT, ObsSourceAudioCaptureT, *mut c_void));
// Removing before every add keeps the callback list at exactly one entry
// per source: libobs's add is a bare `da_push_back` with NO dedup
// (obs-source.c, confirmed 2026-07-19), so the 5s re-attach loops would
// otherwise grow the list unboundedly (~720 duplicates/hour/source).
// Remove of a not-present entry is a safe no-op, which is what makes
// remove-then-add idempotent without tracking attach state ourselves
// (any name/pointer-based "already attached" set would go stale the
// moment something destroys and recreates a same-named input).
crate::resolved_fn!(obs_source_remove_audio_capture_callback: extern "C" fn(*mut ObsSourceT, ObsSourceAudioCaptureT, *mut c_void));
// Capture callbacks receive PRE-fader audio by design in libobs (volume
// is applied later, at mix time). OBS's own mixer meter gets these same
// raw samples and multiplies by the source's current volume itself
// (obs-audio-controls.c, volmeter_source_data_received) — any meter that
// should track the slider must do the same, hence this lookup.
crate::resolved_fn!(obs_source_get_volume: extern "C" fn(*const ObsSourceT) -> f32);
crate::resolved_fn!(obs_source_get_name: extern "C" fn(*const ObsSourceT) -> *const c_char);
// `libobs/obs.h`: "Gets a source by its name. Increments the source
// reference counter, use obs_source_release to release it when complete."
// Needed because `obs_enum_sources` (confirmed against the real
// `obs.c` — `if (s->info.type == OBS_SOURCE_TYPE_INPUT ...)`)
// deliberately excludes scenes (`OBS_SOURCE_TYPE_SCENE`) entirely — the
// only way to reach the Program/Preview scenes themselves is a direct
// name lookup, not the general rescan.
crate::resolved_fn!(obs_get_source_by_name: extern "C" fn(*const c_char) -> *mut ObsSourceT);
crate::resolved_fn!(obs_source_release: extern "C" fn(*mut ObsSourceT));
// `obs_scene_create`/`obs_scene_release` were removed with the split.
// Nothing here called them — creating scenes is the sibling plugin's
// job — and a plugin that says it changes nothing should not be
// declaring the function that makes scenes.

// libobs/obs.h: `void obs_queue_task(enum obs_task_type type, obs_task_t
// task, void *param, bool wait)`, where `obs_task_t` is
// `void (*)(void *param)` and `enum obs_task_type`'s first variant is
// `OBS_TASK_UI` (= 0, see `OBS_TASK_UI` below). With `wait = true` OBS
// runs the task on its own UI thread and blocks the caller until it
// returns — the sanctioned way for a plugin on a background thread to
// touch frontend state. obs-websocket itself uses exactly this for
// `SetStudioModeEnabled`; it just fails to for the getters below.
crate::resolved_fn!(obs_queue_task: extern "C" fn(c_int, extern "C" fn(*mut c_void), *mut c_void, bool));
// frontend/api/obs-frontend-api.h. Both getters return a *new strong
// reference*, and both are genuinely nullable: `obs_frontend_get_current_
// scene` resolves `main->programScene` (a weak ref that goes dead when the
// program scene is deleted) in Studio Mode, or reads the scene-list
// widget's current item otherwise, and the preview getter returns null
// whenever Studio Mode is off. Resolved process-wide rather than against
// libobs specifically — these live in `obs-frontend-api`, which
// `platform::resolve` already searches on both platforms.
/// `libobs/obs.h`: `void obs_source_enum_active_sources(obs_source_t *source,
/// obs_source_enum_proc_t enum_callback, void *param);` — for a scene, the
/// items it is currently rendering.
type ObsSourceEnumProc = extern "C" fn(parent: *mut ObsSourceT, child: *mut ObsSourceT, param: *mut c_void);
crate::resolved_fn!(obs_source_enum_active_sources: extern "C" fn(*mut ObsSourceT, ObsSourceEnumProc, *mut c_void));

crate::resolved_fn!(obs_frontend_get_current_scene: extern "C" fn() -> *mut ObsSourceT);
crate::resolved_fn!(obs_frontend_get_current_preview_scene: extern "C" fn() -> *mut ObsSourceT);
crate::resolved_fn!(obs_frontend_preview_program_mode_active: extern "C" fn() -> bool);

/// `enum obs_task_type`'s first variant in libobs/obs.h — run on OBS's
/// Qt UI thread.
pub const OBS_TASK_UI: c_int = 0;

// Removed with the split, all of it dead in a metering-only plugin:
//
//   * output enumeration (`obs_enum_outputs` and friends) served a
//     `list_video_outputs` request this plugin does not register;
//   * OBS's live user.ini config object, `config_set_bool` and
//     `config_save_safe`, plus the `[BasicWindow] ProjectorAlwaysOnTop`
//     and `[NDIPlugin]` keys they were resolved for;
//   * the frontend profile API (`obs_frontend_*_profile`).
//
// The config writers matter most. They were the only functions in this
// crate that could change a setting in someone's OBS, and the README
// says the plugin changes none. That claim is now enforced by there
// being no such function to call, rather than by nobody calling one.

// ---------------------------------------------------------------------
// FFI panic safety: every function OBS calls into this plugin — either
// directly (the module entry points below) or via function pointer (the
// two callbacks it hands to `obs_enum_sources`/
// `obs_source_add_audio_capture_callback`) — must never let a Rust panic
// unwind across that boundary. Unwinding into the C frames on the other
// side is undefined behavior, and in this plugin's case that's inside a
// user's live-streaming process, not a sandbox. `catch_unwind` turns any
// panic into an `Err` here instead, logged via the existing `log_line`
// path, with the entry point returning its safe "did nothing" value to
// OBS exactly as if this call had never been made — never propagated,
// never aborted.
pub fn ffi_guard<R>(entry_point: &str, fallback: R, f: impl FnOnce() -> R + std::panic::UnwindSafe) -> R {
    match std::panic::catch_unwind(f) {
        Ok(value) => value,
        Err(payload) => {
            let msg = payload
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "non-string panic payload".to_string());
            log_line(&format!("PANIC caught at FFI boundary in {entry_point} — {msg}"));
            fallback
        }
    }
}

// ---------------------------------------------------------------------
// Attach an audio capture callback to every source we can find; each
// callback updates a shared map (not a direct log/emit — that's far too
// often to usefully log or send over the wire) that a separate,
// slower-cadence thread drains and emits.
// ---------------------------------------------------------------------

/// Set from `obs_module_unload`, checked at the top of every iteration
/// (and right before each libobs call) in both background loops below.
/// Without this, a crash is guaranteed sooner or later: these threads are
/// detached and loop forever with no other way to learn that OBS is
/// shutting down, so they keep calling into libobs (`obs_enum_sources`,
/// etc.) even after OBS has started tearing down the very state those
/// calls read/lock — confirmed live, 2026-07-15: OBS segfaulted inside
/// `obs_enum_sources`'s internal mutex lock, called from
/// `spawn_periodic_rescan`, at the moment the user closed OBS.
pub static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);

/// Lets the rescan loop be told to skip its work for a while, without
/// stopping the loop.
///
/// **Inert in this plugin — nothing ever sets it.** Setting it needs a
/// vendor request, and none is registered here. It is kept because the
/// rescan loop's shape is the FFI-critical part and this is one branch,
/// and because the sibling copy of this crate (see `lib.rs`) does drive it.
///
/// It exists because the rescan calls `obs_get_source_by_name` and
/// `obs_source_add_audio_capture_callback` on its own 5-second timer,
/// entirely independent of whatever else is talking to OBS. A client that
/// creates or swaps scenes can therefore be doing so at the same moment
/// this thread is attaching a tap to one. The sibling plugin's application
/// pauses the rescan around its own scene setup for that reason. A client
/// using *this* plugin has no way to do the same; see FINDINGS.md.
pub static RESCAN_PAUSED: AtomicBool = AtomicBool::new(false);

/// Join handles for both background threads, so `obs_module_unload` can
/// block until they've actually exited rather than merely requesting a
/// stop and hoping — the flag alone leaves a window where a thread is
/// mid-call into libobs at the exact moment unload fires; joining closes
/// it, at the cost of unload blocking for at most one loop iteration
/// (~100ms).
pub static THREADS: Mutex<Vec<std::thread::JoinHandle<()>>> = Mutex::new(Vec::new());

/// name -> (peak_db, obs_source_active). Updated on every audio callback
/// (cheap, in-memory only); drained by `spawn_emit_loop` at a much slower,
/// human/UI-appropriate cadence. `active` is the whole point of this
/// plugin existing — it's exactly what `InputVolumeMeters` can't report
/// for Preview-only content.
pub static LEVELS: Mutex<Option<HashMap<String, (f32, Option<bool>)>>> = Mutex::new(None);

pub extern "C" fn audio_capture_callback(
    param: *mut c_void,
    source: *mut ObsSourceT,
    audio_data: *const AudioData,
    muted: bool,
) {
    ffi_guard(
        "audio_capture_callback",
        (),
        std::panic::AssertUnwindSafe(|| audio_capture_callback_impl(param, source, audio_data, muted)),
    );
}

pub fn audio_capture_callback_impl(
    _param: *mut c_void,
    source: *mut ObsSourceT,
    audio_data: *const AudioData,
    muted: bool,
) {
    if audio_data.is_null() {
        return;
    }
    // Safety: libobs guarantees `audio_data` is valid for the duration of
    // this callback (it's a stack-allocated struct on the audio thread's
    // side, not something we're expected to retain past this call).
    let audio_data = unsafe { &*audio_data };
    if audio_data.frames == 0 || audio_data.data[0].is_null() {
        return;
    }

    // Verified live against real sources, not assumed from the headers:
    // OBS's internal audio pipeline is 32-bit float, planar
    // (AUDIO_FORMAT_FLOAT_PLANAR) by the time a source's own audio
    // capture callback fires.
    let samples = unsafe {
        std::slice::from_raw_parts(audio_data.data[0].cast::<f32>(), audio_data.frames as usize)
    };
    let peak = samples.iter().fold(0.0f32, |m, &s| m.max(s.abs()));
    // Post-fader, matching OBS's mixer meter: these samples are pre-fader
    // (libobs applies volume at mix time, after this callback), so scale
    // by the source's current volume and honor the mute flag here —
    // otherwise a meter keeps showing full signal with the source's
    // volume slider pulled to silence. Missing symbol degrades to
    // 1.0 (the old pre-fader behavior), never to silence.
    let volume = obs_source_get_volume().map_or(1.0, |get_volume| get_volume(source));
    let peak = peak * volume;
    let peak_db = if muted || peak <= 0.0 { -100.0 } else { 20.0 * peak.log10() };

    let Some(obs_source_get_name) = obs_source_get_name() else {
        return;
    };
    let name = unsafe {
        let ptr = obs_source_get_name(source);
        if ptr.is_null() {
            return;
        }
        CStr::from_ptr(ptr).to_string_lossy().into_owned()
    };
    // Bus by scene membership, not `obs_source_active(source)` — see
    // `SOURCE_BUS` for the measurement that retired that call. `None` here
    // means "not seen in either scene yet", and is carried as such rather
    // than flattened into `false`.
    let bus = source_bus(&name);

    // Preview-layer monitor taps (`audio_tap.rs`) — reuses this exact
    // callback (already attached to every source, unconditionally) rather
    // than a second `obs_source_add_audio_capture_callback` registration,
    // so a tap adds no new attachment for libobs's callback list to
    // dedupe (see the `resolved_fn!` comment on
    // `obs_source_remove_audio_capture_callback` above for why that
    // matters). Channel count/sample rate come from OBS's one global
    // audio setting (`obs_get_audio_info`), since `audio_data` itself
    // carries neither — missing symbol or a not-yet-tapped source both
    // degrade to `forward_if_tapped` doing nothing, same as every other
    // best-effort path in this crate.
    if let Ok(mut guard) = LEVELS.lock() {
        guard.get_or_insert_with(HashMap::new).insert(name, (peak_db, bus));
    }
}

pub extern "C" fn attach_callback_enum_proc(param: *mut c_void, source: *mut ObsSourceT) -> bool {
    // Fallback `false` on a caught panic: stop this enumeration pass early
    // rather than risk repeating whatever triggered it against the rest
    // of the sources — the next 5s rescan tries again from scratch.
    ffi_guard(
        "attach_callback_enum_proc",
        false,
        std::panic::AssertUnwindSafe(|| attach_callback_enum_proc_impl(param, source)),
    )
}

pub fn attach_callback_enum_proc_impl(_param: *mut c_void, source: *mut ObsSourceT) -> bool {
    // Remove-then-add: net exactly one list entry per source per cycle
    // (see the resolved_fn comment on remove — libobs's add never dedups).
    if let Some(remove) = obs_source_remove_audio_capture_callback() {
        remove(source, audio_capture_callback, std::ptr::null_mut());
    }
    if let Some(obs_source_add_audio_capture_callback) = obs_source_add_audio_capture_callback() {
        obs_source_add_audio_capture_callback(source, audio_capture_callback, std::ptr::null_mut());
    }
    true // keep enumerating
}

/// Tapping the Program and Preview scenes themselves, not just the
/// individual sources in them, gives the real composited-mix audio for
/// whichever scene is live — the same way `attach_callback_enum_proc`
/// does for individual inputs.
/// The scene each role is currently tapped on, so a role that moves to a
/// different scene detaches from the old one.
///
/// Under the previous fixed-name scheme this could not happen: the taps sat
/// on `PGM-A`/`PGM-B` forever. Resolving by role means the underlying scene
/// changes whenever the operator switches, and attaching without detaching
/// would leave a live audio callback on every scene that was ever Program —
/// growing for the life of the OBS session and reporting levels for scenes
/// that are no longer on air.
pub static ATTACHED_PROGRAM_SCENE: Mutex<Option<String>> = Mutex::new(None);
pub static ATTACHED_PREVIEW_SCENE: Mutex<Option<String>> = Mutex::new(None);

/// Last Program/Preview scene names read on the UI thread, as
/// `(program, preview)`. `None` until the first refresh lands.
///
/// **This cache exists to break a shutdown deadlock, not for speed.**
/// `obs_module_unload` runs on OBS's UI thread — measured, not assumed:
/// instrumenting both paths showed unload and the UI tasks on the same
/// `ThreadId` — and it joins the rescan thread. If the rescan thread were
/// blocked in `obs_queue_task(OBS_TASK_UI, .., wait: true)`, it would be
/// waiting for a queue only the UI thread can drain, while the UI thread
/// waits in `join()`. OBS hangs on exit, roughly once in a hundred quits:
/// the worst kind of hang.
///
/// The `SHUTTING_DOWN` check before the call cannot fix that — the check
/// and the blocking wait are not atomic, so a thread already past the check
/// enters the wait regardless.
///
/// So the rescan thread never waits. It queues a refresh that writes here
/// whenever the UI thread gets to it, and attaches using whatever the last
/// refresh produced. One cycle of staleness (~5s) is harmless: taps are
/// re-attached every cycle anyway, and this loop was already eventual by
/// design.
pub static CACHED_SCENE_ROLES: Mutex<Option<(Option<String>, Option<String>)>> = Mutex::new(None);

/// Attaches the composited-mix audio callback to whichever scenes OBS
/// currently reports as **Program and Preview**, by role.
///
/// Previously this looked up two hardcoded scene names, inherited from
/// the application this code was extracted from. In anyone else's OBS
/// those scenes do not exist, so scene-level metering silently did
/// nothing at all. Resolving by role instead is what makes this work in
/// an arbitrary OBS — and it is strictly more correct even where those
/// names *do* exist, because a scene's role can move and its name cannot.
///
/// **The frontend read is marshalled onto OBS's UI thread**, reusing the
/// same `read_current_scenes_on_ui_thread` task. That is not incidental: this function runs on the
/// periodic rescan thread and on obs-websocket request threads, and reading
/// the frontend's Qt-owned scene state from a worker thread is exactly the
/// pattern this plugin exists to avoid — see
/// `handle_get_current_scenes_impl`'s own comment. The task is tiny (two
/// getters and two name copies) and only runs every ~5s.
///
/// `obs_enum_sources` cannot reach scenes at all (confirmed: it filters to
/// `OBS_SOURCE_TYPE_INPUT`), which is why they need this separate path.
/// Refreshes `CACHED_SCENE_ROLES`. Queued onto the UI thread **without
/// waiting**, so it takes no pointer to caller stack — it writes into a
/// `'static` and the caller may have moved on by the time this runs.
pub extern "C" fn cache_scene_roles_on_ui_thread(_param: *mut c_void) {
    ffi_guard(
        "cache_scene_roles_on_ui_thread",
        (),
        std::panic::AssertUnwindSafe(|| {
            let program = frontend_scene_name(obs_frontend_get_current_scene());
            let preview = frontend_scene_name(obs_frontend_get_current_preview_scene());
            if let Ok(mut cached) = CACHED_SCENE_ROLES.lock() {
                *cached = Some((program.clone(), preview.clone()));
            }
            // Rebuild the bus map HERE, holding the names that were just
            // read, rather than back on the rescan thread from the cache.
            //
            // This used to happen in `attach_scene_audio_taps`, which
            // queues this task without waiting and then immediately reads
            // the cache — so it always built the map from the PREVIOUS
            // cycle's roles, up to 5 seconds old. Measured 2026-08-31: with
            // Studio Mode switched off mid-test, the rescan walked the one
            // scene as *Preview* using the stale roles and marked a source
            // sitting on Program as not-on-Program. The old comment calling
            // one cycle of staleness "harmless" was wrong — it is harmless
            // for the taps, which only decide where audio is sampled, and
            // it is the whole answer for the bus map, which decides what
            // the event claims.
            refresh_source_bus(program.as_deref(), preview.as_deref());
        }),
    );
}

/// Source name -> true when that source is on the Program bus.
///
/// Replaces `obs_source_active()`, which was measured on 2026-08-14
/// returning false for sources demonstrably in the Program scene — even
/// ones with video. Whatever that call tracks, it is not "is this source
/// on the Program bus", and no other per-source query answers that
/// question either: `obs_source_showing` is true for both buses, which is
/// the distinction being asked about.
///
/// The authoritative answer is membership. The plugin already resolves
/// which scene is Program and which is Preview (`attach_scene_audio_taps`),
/// so walking each of those two scenes and recording what is inside it
/// gives the bus by construction, with nothing left to disagree.
///
/// Rebuilt on every rescan, so a source that moves between scenes is
/// re-classified within one cycle rather than keeping a stale bus forever.
static SOURCE_BUS: Mutex<Option<HashMap<String, bool>>> = Mutex::new(None);

/// Collects one scene's active children into `SOURCE_BUS`.
extern "C" fn collect_bus_member(
    _parent: *mut ObsSourceT,
    child: *mut ObsSourceT,
    param: *mut c_void,
) {
    let is_program = !param.is_null();
    let Some(get_name) = obs_source_get_name() else {
        return;
    };
    let raw = get_name(child);
    if raw.is_null() {
        return;
    }
    let name = unsafe { CStr::from_ptr(raw) }.to_string_lossy().to_string();
    if let Ok(mut guard) = SOURCE_BUS.lock() {
        let map = guard.get_or_insert_with(HashMap::new);
        // Program wins if a source is somehow in both: what is on air is
        // the more consequential fact to report.
        let entry = map.entry(name).or_insert(is_program);
        *entry = *entry || is_program;
    }
}

/// Rebuilds `SOURCE_BUS` from the current Program and Preview scenes.
pub fn refresh_source_bus(program: Option<&str>, preview: Option<&str>) {
    let (Some(by_name), Some(release), Some(enum_active)) = (
        obs_get_source_by_name(),
        obs_source_release(),
        obs_source_enum_active_sources(),
    ) else {
        return;
    };
    if let Ok(mut guard) = SOURCE_BUS.lock() {
        *guard = Some(HashMap::new());
    }
    for (scene, is_program) in [(program, true), (preview, false)] {
        let Some(scene) = scene else { continue };
        let Ok(cname) = CString::new(scene) else { continue };
        let src = by_name(cname.as_ptr());
        if src.is_null() {
            continue;
        }
        // A non-null param marks the Program walk; null marks Preview.
        let marker = if is_program { 1usize as *mut c_void } else { std::ptr::null_mut() };
        enum_active(src, collect_bus_member, marker);
        release(src);
    }
}

/// Which bus a source is on, or false when it is on neither/unknown.
/// `Some(true)` on Program, `Some(false)` on Preview, **`None` when the bus
/// is not known** — the source has not been seen in either scene yet.
///
/// The `None` case is the point. This returned a bare `bool` ending in
/// `.unwrap_or(false)`, so a source the plugin had never walked reported
/// exactly what a genuinely Preview-only source reports. Those are not the
/// same claim, and a client cannot tell them apart, which made the one
/// capability this plugin exists to provide impossible to verify: an
/// assertion that "a Preview source reads false" passes just as well when
/// the plugin knows nothing at all.
///
/// Same mistake as the `active` field this replaced — a field whose name
/// promises more than the value behind it knows. Caught before release
/// again, on 2026-08-31, and this time the type makes it unrepresentable.
pub fn source_bus(name: &str) -> Option<bool> {
    if let Some(known) = SOURCE_BUS
        .lock()
        .ok()
        .and_then(|g| g.as_ref().and_then(|m| m.get(name).copied()))
    {
        return Some(known);
    }
    // A SCENE is not in SOURCE_BUS. That map is built by walking each
    // scene's CHILDREN (`collect_bus_member` records `child`), so the scene
    // itself never appears in it — but the plugin taps scenes too, for the
    // composited mix, and a scene's bus is simply the role it was tapped as.
    //
    // Without this, every scene-level level was dropped by the emit loop's
    // "unknown bus is not reported" rule, added 2026-08-31. That rule fixed
    // a real defect (an unseen source reported as Preview) and silently
    // took the scene taps with it — which then looked exactly like "OBS
    // produces no audio for scenes", a conclusion drawn and retracted the
    // same night. The taps were fine; the filter was eating them.
    for (attached, is_program) in
        [(&ATTACHED_PROGRAM_SCENE, true), (&ATTACHED_PREVIEW_SCENE, false)]
    {
        let guard = match attached.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        };
        if guard.as_deref() == Some(name) {
            return Some(is_program);
        }
    }
    None
}

pub fn attach_scene_audio_taps() {
    // Ask the UI thread for fresh names, but never wait for the answer —
    // see `CACHED_SCENE_ROLES`. This call is what unload's `join()` would
    // otherwise deadlock against.
    if let Some(obs_queue_task) = obs_queue_task() {
        obs_queue_task(
            OBS_TASK_UI,
            cache_scene_roles_on_ui_thread,
            std::ptr::null_mut(),
            false,
        );
    }

    let snapshot = match CACHED_SCENE_ROLES.lock() {
        Ok(cached) => cached.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    };
    let Some((program, preview)) = snapshot else {
        return; // first cycle — nothing read yet, attach on the next one
    };
    attach_role_tap("program", program.as_deref(), &ATTACHED_PROGRAM_SCENE);
    attach_role_tap("preview", preview.as_deref(), &ATTACHED_PREVIEW_SCENE);
    // `refresh_source_bus` is deliberately NOT called here any more. It now
    // runs inside `cache_scene_roles_on_ui_thread`, with the names that
    // task just read. The snapshot above is up to one cycle old, which is
    // fine for deciding where to sample audio and was NOT fine for deciding
    // what bus to report — see that function.
}

/// Moves one role's audio tap to `scene`, detaching from whatever that role
/// was tapped on before.
///
/// Re-attaching to the same scene is deliberately still done every cycle:
/// it is the same remove-then-add idempotency `attach_callback_enum_proc`
/// relies on, and it re-establishes the tap if OBS destroyed and recreated
/// the scene under the same name. Only the *logging* is suppressed for an
/// unchanged scene, so the log records role changes rather than repeating
/// every 5 seconds.
pub fn attach_role_tap(role: &str, scene: Option<&str>, attached: &Mutex<Option<String>>) {
    let (Some(get_by_name), Some(add_cb), Some(release)) = (
        obs_get_source_by_name(),
        obs_source_add_audio_capture_callback(),
        obs_source_release(),
    ) else {
        return;
    };
    let remove_cb = obs_source_remove_audio_capture_callback();

    let mut attached = match attached.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    let changed = attached.as_deref() != scene;

    // Detach from the scene this role has moved off, or from the scene it
    // held when the role went away entirely (no Program scene at all is a
    // real OBS state — see handle_get_current_scenes_impl).
    if changed {
        if let (Some(old), Some(remove)) = (attached.as_deref(), remove_cb) {
            if let Ok(cold) = CString::new(old) {
                let src = get_by_name(cold.as_ptr());
                if !src.is_null() {
                    remove(src, audio_capture_callback, std::ptr::null_mut());
                    release(src);
                }
            }
        }
        *attached = None;
    }

    let Some(scene) = scene else {
        return;
    };
    let Ok(cname) = CString::new(scene) else {
        return;
    };
    let source = get_by_name(cname.as_ptr());
    if source.is_null() {
        return; // named scene not present yet
    }
    if let Some(remove) = remove_cb {
        remove(source, audio_capture_callback, std::ptr::null_mut());
    }
    add_cb(source, audio_capture_callback, std::ptr::null_mut());
    release(source);

    if changed {
        log_line(&format!("attached real audio tap to {role} scene '{scene}'"));
    }
    *attached = Some(scene.to_string());
}

/// Periodically re-enumerates and (re-)attaches the callback, rather than
/// hooking libobs's `source_create` signal — deliberately the simplest
/// thing that could prove the hypothesis, not the final design. Remaining
/// rough edge: sources created between scans (this fires every 5s) aren't
/// instrumented until the next scan. (The former rough edge — duplicate
/// attachment growing libobs's callback list unboundedly, confirmed real
/// 2026-07-19: libobs's add is a bare `da_push_back` — is closed by the
/// remove-then-add pattern in both attach paths below.)
pub fn spawn_periodic_rescan() {
    let handle = std::thread::spawn(|| loop {
        if SHUTTING_DOWN.load(Ordering::Acquire) {
            return;
        }
        // See `RESCAN_PAUSED`'s doc comment — skip this cycle's work
        // entirely while paused, but keep the loop (and its shutdown
        // responsiveness) alive. Checked only here, once per ~5s cycle —
        // a `resume_rescan` sent while this thread is mid-sleep takes
        // effect at the next cycle, not instantly. That's fine for this
        // flag's actual use (a consumer pauses around its own scene
        // setup) — instant pickup isn't needed the way it is for
        // shutdown.
        if !RESCAN_PAUSED.load(Ordering::Acquire) {
            if let Some(obs_enum_sources) = obs_enum_sources() {
                if !SHUTTING_DOWN.load(Ordering::Acquire) {
                    obs_enum_sources(attach_callback_enum_proc, std::ptr::null_mut());
                }
            }
            if !SHUTTING_DOWN.load(Ordering::Acquire) {
                attach_scene_audio_taps();
            }
        }
        // Slept in short increments rather than one 5s call so a shutdown
        // request is noticed within ~100ms instead of up to 5s later.
        for _ in 0..50 {
            if SHUTTING_DOWN.load(Ordering::Acquire) {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
    });
    if let Ok(mut threads) = THREADS.lock() {
        threads.push(handle);
    }
}

pub static VENDOR: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());

pub const EMIT_INTERVAL: std::time::Duration = std::time::Duration::from_millis(100);

pub fn spawn_emit_loop() {
    let handle = std::thread::spawn(|| loop {
        std::thread::sleep(EMIT_INTERVAL);
        if SHUTTING_DOWN.load(Ordering::Acquire) {
            return;
        }
        let vendor = VENDOR.load(Ordering::Acquire);
        if vendor.is_null() {
            continue;
        }
        let drained: Vec<SourceLevel> = {
            let Ok(mut guard) = LEVELS.lock() else {
                continue;
            };
            guard
                .get_or_insert_with(HashMap::new)
                .drain()
                // A source whose bus is not known yet is DROPPED, not
                // emitted with a guessed flag. `on_program` is the only
                // reason a client uses this plugin, so a value it cannot
                // stand behind must not be sent at all — the alternative
                // is a confident-looking `false` that means "no idea",
                // which is what shipped until 2026-08-31.
                //
                // Consistent with what the README already promises: a
                // source appears within about five seconds of being added,
                // because that is the rescan cadence. This just means it
                // appears when its bus is known rather than before.
                .filter_map(|(name, (peak_db, bus))| {
                    bus.map(|on_program| SourceLevel { name, peak_db, on_program })
                })
                .collect()
        };
        if drained.is_empty() {
            continue;
        }
        if SHUTTING_DOWN.load(Ordering::Acquire) {
            return;
        }
        let payload = obs_data::build_levels_payload(&drained);
        calldata::vendor_emit_event(vendor, "audio_levels", obs_data::as_void(payload));
        obs_data::release(payload);
    });
    if let Ok(mut threads) = THREADS.lock() {
        threads.push(handle);
    }
}

// ---------------------------------------------------------------------
// Required OBS module entry points — see `OBS_DECLARE_MODULE()` in
// `libobs/obs-module.h`; hand-expanded here since we're not using the C
// macro (no C compilation step in this crate).
// ---------------------------------------------------------------------



/// Resolves one of the two nullable frontend scene getters to a name,
/// releasing the strong reference it hands back. `None` covers both "OBS
/// has no such scene right now" and "the symbol didn't resolve"; the
/// caller separates those via `CurrentScenes::ran`.
pub fn frontend_scene_name(getter: Option<extern "C" fn() -> *mut ObsSourceT>) -> Option<String> {
    let getter = getter?;
    let obs_source_get_name = obs_source_get_name()?;
    let obs_source_release = obs_source_release()?;
    let source = getter();
    if source.is_null() {
        return None;
    }
    let name = unsafe {
        let ptr = obs_source_get_name(source);
        if ptr.is_null() {
            None
        } else {
            Some(CStr::from_ptr(ptr).to_string_lossy().into_owned())
        }
    };
    obs_source_release(source);
    name
}

/// Stops the background threads and waits for them to actually exit.
///
/// Not "asks them to stop" — waits. OBS unloads a module without telling
/// its detached threads, and they keep calling into libobs afterwards:
/// confirmed live 2026-07-15 as a segfault inside `obs_enum_sources`'s
/// internal mutex at the moment OBS closed. Blocks briefly, at most one
/// loop iteration (~100ms).
///
/// A consumer's `obs_module_unload` must call this before doing anything
/// else, and must do its own teardown *after* it returns.
pub fn shutdown() {
    SHUTTING_DOWN.store(true, Ordering::Release);
    let handles: Vec<std::thread::JoinHandle<()>> = match THREADS.lock() {
        Ok(mut threads) => threads.drain(..).collect(),
        Err(_) => Vec::new(),
    };
    for handle in handles {
        let _ = handle.join();
    }
}

#[cfg(test)]
/// Serialises tests that mutate the module's global bus state.
///
/// `SOURCE_BUS` and the ATTACHED_* statics are process-wide, and cargo runs
/// tests in parallel — so without this one test wipes the map another is
/// mid-assert on. Observed 2026-08-31: adding the scene-tap tests made
/// `an_unknown_source_is_none_not_false` fail with `left: None, right:
/// Some(false)`, which looks exactly like a real regression and was not one.
static TEST_STATE: Mutex<()> = Mutex::new(());

#[cfg(test)]
mod bus_tests {
    use super::*;

    /// The load test CANNOT cover this, which is why it is here.
    ///
    /// Measured 2026-08-31: with the staleness fix in place, `SOURCE_BUS`
    /// is always populated by the time the load test measures, so the
    /// missing-key path is never taken and the load test passed against a
    /// deliberately reintroduced `.unwrap_or(false)`. A test that cannot
    /// fail is worse than no test, so the guarantee is asserted directly
    /// on the function instead.
    fn set_bus(entries: &[(&str, bool)]) {
        let mut guard = SOURCE_BUS.lock().unwrap();
        *guard = Some(entries.iter().map(|(n, b)| (n.to_string(), *b)).collect());
    }

    #[test]
    fn an_unknown_source_is_none_not_false() {
        let _g = TEST_STATE.lock().unwrap_or_else(|e| e.into_inner());
        set_bus(&[("on-program", true), ("on-preview", false)]);
        // The whole point: "not seen yet" must not be reportable as
        // "staged in Preview". Those are different claims and a client
        // cannot tell them apart once both are `false`.
        assert_eq!(source_bus("never-walked"), None);
        assert_eq!(source_bus("on-preview"), Some(false));
        assert_eq!(source_bus("on-program"), Some(true));
    }

    #[test]
    fn an_empty_map_still_answers_none() {
        let _g = TEST_STATE.lock().unwrap_or_else(|e| e.into_inner());
        set_bus(&[]);
        assert_eq!(source_bus("anything"), None);
    }

    #[test]
    fn a_never_initialised_map_answers_none() {
        let _g = TEST_STATE.lock().unwrap_or_else(|e| e.into_inner());
        // Before the first rescan completes there is no map at all. That
        // is the widest window in which a level could be emitted with a
        // fabricated bus, so it is asserted explicitly.
        *SOURCE_BUS.lock().unwrap() = None;
        assert_eq!(source_bus("anything"), None);
    }
}

#[cfg(test)]
mod scene_bus_tests {
    use super::*;

    /// A scene is never in SOURCE_BUS — that map holds each scene's
    /// CHILDREN. Its bus comes from the role it was tapped as, and without
    /// that the emit loop's "unknown bus is not reported" rule silently
    /// discards every scene-level level.
    #[test]
    fn a_tapped_scene_reports_the_bus_it_was_tapped_as() {
        let _g = TEST_STATE.lock().unwrap_or_else(|e| e.into_inner());
        *SOURCE_BUS.lock().unwrap() = Some(
            [("child-mic".to_string(), false)].into_iter().collect(),
        );
        *ATTACHED_PROGRAM_SCENE.lock().unwrap() = Some("live".into());
        *ATTACHED_PREVIEW_SCENE.lock().unwrap() = Some("staged".into());

        assert_eq!(source_bus("live"), Some(true), "program scene tap");
        assert_eq!(source_bus("staged"), Some(false), "preview scene tap");
        // Children still answer from the map, unchanged.
        assert_eq!(source_bus("child-mic"), Some(false));
        // And a genuine unknown is still unknown — the defect the emit
        // filter exists to prevent must not come back.
        assert_eq!(source_bus("never-seen"), None);
    }

    #[test]
    fn an_untapped_scene_is_still_unknown() {
        let _g = TEST_STATE.lock().unwrap_or_else(|e| e.into_inner());
        *SOURCE_BUS.lock().unwrap() = Some(std::collections::HashMap::new());
        *ATTACHED_PROGRAM_SCENE.lock().unwrap() = None;
        *ATTACHED_PREVIEW_SCENE.lock().unwrap() = None;
        assert_eq!(source_bus("anything"), None);
    }
}
