// SPDX-License-Identifier: GPL-2.0-or-later
//! Studio Mode Meters — per-source audio levels that know Program from Preview.
//!
//! obs-websocket can tell you the level of a source that is live. It
//! cannot tell you the level of one that is only staged in Preview, which
//! is exactly what you need in order to check a source *before* cutting to
//! it. This plugin fills that hole and does nothing else.
//!
//! **It is read-only.** It attaches audio capture callbacks and reports
//! what it hears. It creates nothing, renames nothing, and changes no OBS
//! setting. It registers no vendor requests at all — there is nothing to
//! ask it to do.
//!
//! # Using it
//!
//! Connect any obs-websocket client and subscribe to vendor events. This
//! plugin emits `audio_levels` under vendor `studio_mode_meters`, roughly
//! ten times a second:
//!
//! ```json
//! {
//!   "vendorName": "studio_mode_meters",
//!   "eventType": "audio_levels",
//!   "eventData": { "levels": [ { "name": "Mic/Aux", "peak_db": -18.3, "on_program": false } ] }
//! }
//! ```
//!
//! `on_program` is true when the source is in the scene currently on
//! Program, false when it is in the Preview scene — the distinction
//! obs-websocket cannot otherwise report a level for.
//!
//! Deliberately not called `active`, and deliberately not the same as
//! obs-websocket's `GetSourceActive`. That call reports libobs's
//! activation refcount, which can lag: a source added straight into the
//! live Program scene reads false there until the next transition. This
//! flag is derived from scene membership and has no such window.
//!
//! A muted source reports silence rather than disappearing, so a client can
//! distinguish "muted" from "gone". Nothing is emitted at all while no
//! source is producing audio — an idle OBS is silent on this channel by
//! design, not broken.

use std::ffi::{c_char, c_void};
use studio_mode_meters_core::{calldata, metering, set_identity, Identity};

/// obs-websocket clients match on this string, so it is permanent.
const VENDOR: &str = "studio_mode_meters";

/// OBS's module ABI version, from `libobs/obs-module.h`'s `LIBOBS_API_VER`.
/// Kept identical to the plugin this was extracted from.
const LIBOBS_API_MAJOR_VER: u32 = 30;

static mut MODULE_POINTER: *mut c_void = std::ptr::null_mut();

#[no_mangle]
pub extern "C" fn obs_module_set_pointer(module: *mut c_void) {
    // Safety: OBS calls this once, on its own thread, before any other
    // entry point — the same contract every OBS plugin relies on.
    unsafe { MODULE_POINTER = module };
}

#[no_mangle]
pub extern "C" fn obs_current_module() -> *mut c_void {
    unsafe { MODULE_POINTER }
}

#[no_mangle]
pub extern "C" fn obs_module_ver() -> u32 {
    LIBOBS_API_MAJOR_VER << 24
}

#[no_mangle]
pub extern "C" fn obs_module_name() -> *const c_char {
    c"Studio Mode Meters".as_ptr()
}

#[no_mangle]
pub extern "C" fn obs_module_description() -> *const c_char {
    c"Per-source audio levels that distinguish Program from Preview, over obs-websocket.".as_ptr()
}

#[no_mangle]
pub extern "C" fn obs_module_load() -> bool {
    metering::ffi_guard("obs_module_load", false, || {
        // Before anything can log, so no line is ever tagged with core's
        // neutral fallback prefix.
        set_identity(Identity { vendor: VENDOR, log_prefix: "[studio-mode-meters]" });
        // No audio sink is installed, and that is the point: with none,
        // audio never leaves the metering module. This plugin reads levels
        // and forwards nothing.
        studio_mode_meters_core::log_line("loaded — metering Program and Preview separately");
        metering::spawn_periodic_rescan();
        true
    })
}

#[no_mangle]
pub extern "C" fn obs_module_post_load() {
    metering::ffi_guard("obs_module_post_load", (), || {
        let vendor = calldata::register_vendor(VENDOR);
        if vendor.is_null() {
            // Not fatal: levels still reach OBS's own log. Say so plainly,
            // since "no events arriving" otherwise looks like this
            // plugin's fault rather than a missing obs-websocket.
            studio_mode_meters_core::log_line(
                "obs-websocket not installed/loaded — levels will only reach OBS's log",
            );
            return;
        }
        metering::VENDOR.store(vendor, std::sync::atomic::Ordering::Release);
        studio_mode_meters_core::log_line(
            "registered as obs-websocket vendor \"studio_mode_meters\" — emitting audio_levels",
        );
        metering::spawn_emit_loop();
    })
}

#[no_mangle]
pub extern "C" fn obs_module_unload() {
    metering::ffi_guard("obs_module_unload", (), || {
        // Must complete before returning. OBS unloads a module without
        // telling its detached threads, and they keep calling into libobs
        // afterwards — a segfault inside obs_enum_sources, observed live
        // in the plugin this was extracted from.
        metering::shutdown();
        studio_mode_meters_core::log_line("unloaded — background threads stopped cleanly");
    })
}
