// SPDX-License-Identifier: GPL-2.0-or-later
//
// Studio Mode Meters — an OBS Studio plugin
// Copyright (C) 2026 Hoversights
//
// This program is free software; you can redistribute it and/or modify it
// under the terms of the GNU General Public License as published by the
// Free Software Foundation; either version 2 of the License, or (at your
// option) any later version.
//
// This program is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
// General Public License for more details.
//
// You should have received a copy of the GNU General Public License along
// with this program; if not, see <https://www.gnu.org/licenses/>.

//! Minimal `obs_data_t`/`obs_data_array_t` FFI — just enough to build the
//! `audio_levels` event payload. Both types are fully opaque from this
//! plugin's side (we only ever hold pointers and call real `EXPORT`ed
//! libobs functions on them, never touch their layout), so there's no ABI
//! risk here the way there was for `calldata_t`. Signatures verified
//! against `obsproject/obs-studio@master`'s `libobs/obs-data.h` via
//! `curl`, same as `calldata.rs`. Resolved at runtime, not linked at
//! build time — see `platform.rs`'s module doc for why.

use std::ffi::{c_char, c_void, CString};

pub enum ObsDataT {}
pub enum ObsDataArrayT {}

crate::resolved_fn!(obs_data_create: extern "C" fn() -> *mut ObsDataT);
crate::resolved_fn!(obs_data_release: extern "C" fn(*mut ObsDataT));
crate::resolved_fn!(obs_data_set_string: extern "C" fn(*mut ObsDataT, *const c_char, *const c_char));
crate::resolved_fn!(obs_data_set_double: extern "C" fn(*mut ObsDataT, *const c_char, f64));
crate::resolved_fn!(obs_data_set_bool: extern "C" fn(*mut ObsDataT, *const c_char, bool));
crate::resolved_fn!(obs_data_set_array: extern "C" fn(*mut ObsDataT, *const c_char, *mut ObsDataArrayT));
crate::resolved_fn!(obs_data_array_create: extern "C" fn() -> *mut ObsDataArrayT);
crate::resolved_fn!(obs_data_array_release: extern "C" fn(*mut ObsDataArrayT));
crate::resolved_fn!(obs_data_array_push_back: extern "C" fn(*mut ObsDataArrayT, *mut ObsDataT) -> usize);

fn cstr(s: &str) -> CString {
    CString::new(s).unwrap_or_else(|e| {
        let valid_len = e.nul_position();
        CString::new(&e.into_vec()[..valid_len]).unwrap_or_default()
    })
}

/// One source's level, as emitted in the `audio_levels` event.
///
/// `on_program` carries exactly what `InputVolumeMeters` cannot: which bus
/// this source is on. A Preview-only source is not in the output mix, so
/// OBS's own meters never report it at all — that gap is the entire reason
/// this plugin exists.
///
/// The field was called `active` while the emitted key was in transition.
/// It is `on_program` in both places now: the value is derived from scene
/// membership, not from libobs's activation refcount, and the old name
/// invited a comparison with `GetSourceActive` that misleads.
pub struct SourceLevel {
    pub name: String,
    pub peak_db: f32,
    pub on_program: bool,
}

/// Builds `{"levels": [{"name": ..., "peak_db": ..., "on_program": ...}, ...]}`
/// as a real `obs_data_t*`, ready to hand to
/// `calldata::vendor_emit_event`. Caller owns the returned pointer and
/// must release it via `release` once the emit call returns (per
/// `obs_websocket_vendor_emit_event`'s own doc comment: it does not touch
/// `event_data`'s refcount itself). Returns null if any required
/// `obs_data_*` symbol couldn't be resolved — these are extremely core,
/// universally-present libobs functions, so that would mean something is
/// fundamentally wrong (not the expected/normal degraded-gracefully case
/// the way a missing obs-websocket is), and building a silently-partial
/// payload would be worse than just not emitting one at all.
pub fn build_levels_payload(levels: &[SourceLevel]) -> *mut ObsDataT {
    let (
        Some(obs_data_create),
        Some(obs_data_release),
        Some(obs_data_set_string),
        Some(obs_data_set_double),
        Some(obs_data_set_bool),
        Some(obs_data_set_array),
        Some(obs_data_array_create),
        Some(obs_data_array_release),
        Some(obs_data_array_push_back),
    ) = (
        self::obs_data_create(),
        self::obs_data_release(),
        self::obs_data_set_string(),
        self::obs_data_set_double(),
        self::obs_data_set_bool(),
        self::obs_data_set_array(),
        self::obs_data_array_create(),
        self::obs_data_array_release(),
        self::obs_data_array_push_back(),
    )
    else {
        return std::ptr::null_mut();
    };

    let root = obs_data_create();
    let array = obs_data_array_create();
    for level in levels {
        let entry = obs_data_create();
        let name_key = cstr("name");
        let name_val = cstr(&level.name);
        obs_data_set_string(entry, name_key.as_ptr(), name_val.as_ptr());
        let peak_key = cstr("peak_db");
        obs_data_set_double(entry, peak_key.as_ptr(), level.peak_db as f64);
        // `on_program`, and only that. The old name was `active`, which
        // invited comparison with obs-websocket's own `GetSourceActive` —
        // a different question, with a genuinely different answer (a
        // source added straight into the live Program scene reads false
        // there until the next transition).
        //
        // One key, not two. The rename happened before this plugin was
        // ever released, so there is no client anywhere expecting the old
        // name and nothing to stay compatible with.
        let on_program_key = cstr("on_program");
        obs_data_set_bool(entry, on_program_key.as_ptr(), level.on_program);
        // `obs_data_array_push_back` addrefs its own copy internally
        // (standard OBS refcounting convention for every "add/set" call
        // in this API) — release our local ref immediately afterward
        // rather than leak it.
        obs_data_array_push_back(array, entry);
        obs_data_release(entry);
    }
    let levels_key = cstr("levels");
    obs_data_set_array(root, levels_key.as_ptr(), array);
    obs_data_array_release(array);
    root
}

// `get_optional_bool`, `NamedKind` and `set_pair_array` were removed
// with the split: all three read or build vendor-request payloads, and
// this plugin registers no requests.

pub fn release(data: *mut ObsDataT) {
    if data.is_null() {
        return;
    }
    if let Some(obs_data_release) = obs_data_release() {
        obs_data_release(data);
    }
}

// Re-exported for `lib.rs` to pass through `calldata::vendor_emit_event`,
// which takes an opaque `*mut c_void` so `calldata.rs` doesn't need to
// know about `obs_data.rs`'s types.
pub fn as_void(data: *mut ObsDataT) -> *mut c_void {
    data.cast()
}

// `from_void`, `get_string`, `set_string` and `set_bool` went the same
// way, and for the same reason: they exist to read a request's
// arguments and fill in its response.
