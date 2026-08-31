#!/usr/bin/env bash
# Builds a distributable macOS plugin bundle.
#
#   ./scripts/package-macos.sh            # dev: native arch, ad-hoc signed
#   ./scripts/package-macos.sh --release  # universal, Developer ID signed
#   ./scripts/package-macos.sh --release --notarize
#   ./scripts/package-macos.sh --release "Developer ID Application: ..."
#
# A bare argument is a signing identity, for CI: it imports the certificate
# into a throwaway keychain whose identity string cannot be known ahead of
# time. Locally, omit it and the maintainer's own identity is used.
#
# Notarization credentials come from a keychain profile by default
# (NOTARY_PROFILE, default `obs_controller-notary`). CI has no keychain
# profile, so it sets APPLE_API_KEY_ID / APPLE_API_ISSUER_ID /
# APPLE_API_KEY_PATH instead and those take precedence.
#
# WHY THIS EXISTS SEPARATELY FROM FrameSW'S PACKAGER. That one deliberately
# does NOT notarize: its bundle is nested inside FrameSW.app and rides the
# app's own notarization ticket. A plugin distributed on its own has no app
# to ride, so a downloaded copy is quarantined and Gatekeeper refuses it
# unless the bundle itself has been through notarization.
set -euo pipefail

RELEASE=0
NOTARIZE=0
SIGN_IDENTITY="-"
KEYCHAIN_PROFILE="${NOTARY_PROFILE:-obs_controller-notary}"
# Default identity for a local release build. CI imports its own copy of
# the certificate into a throwaway keychain, where the identity string is
# not knowable in advance, so it discovers it and passes it as a bare
# argument instead.
DEFAULT_IDENTITY="Developer ID Application: Steve Pence (KF8QMVBSAM)"
for arg in "$@"; do
  case "$arg" in
    --release) RELEASE=1 ;;
    --notarize) NOTARIZE=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 1 ;;
    *) SIGN_IDENTITY="$arg" ;;
  esac
done
# Resolved after the loop, so `--release` and an explicit identity can be
# given in either order without one clobbering the other.
if [ "$RELEASE" = "1" ] && [ "$SIGN_IDENTITY" = "-" ]; then
  SIGN_IDENTITY="$DEFAULT_IDENTITY"
fi
[ "$NOTARIZE" = "1" ] && [ "$RELEASE" = "0" ] && {
  echo "error: --notarize requires --release (nothing to notarize when ad-hoc signed)" >&2
  exit 1
}

NAME="studio-mode-meters"
BUNDLE="target/${NAME}.plugin"
VERSION="$(grep -m1 '^version' Cargo.toml | sed -E 's/version = "(.*)"/\1/')"

# Keep the builder's home directory out of the shipped binary.
#
# rustc embeds absolute source paths in panic metadata, so an unconfigured
# build ships strings like
# `/Users/<name>/.rustup/toolchains/.../library/std/src/sync/once.rs` to
# everyone who downloads it. Small, but it is a real name in a binary that
# strangers load into their own process, and it costs one flag to avoid.
#
# `[profile.release] trim-paths` is the natural way to do this and is still
# unstable in Cargo 1.95.0 — it fails the build rather than warning. This
# is the stable equivalent. Appended, not assigned, so an existing RUSTFLAGS
# is not silently discarded.
#
# Deliberately NOT `strip`: symbols are what make an OBS crash report
# readable, which matters most for a plugin inside someone's live stream.
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=${HOME}=~ --remap-path-prefix=${PWD}=."

rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS"

if [ "$RELEASE" = "1" ]; then
  echo "==> Universal release build"
  cargo build --release --target aarch64-apple-darwin
  cargo build --release --target x86_64-apple-darwin
  lipo -create \
    "target/aarch64-apple-darwin/release/libobs_studio_mode_meters.dylib" \
    "target/x86_64-apple-darwin/release/libobs_studio_mode_meters.dylib" \
    -output "$BUNDLE/Contents/MacOS/$NAME"
else
  echo "==> Dev build (native arch, ad-hoc signed)"
  cargo build --release
  cp "target/release/libobs_studio_mode_meters.dylib" "$BUNDLE/Contents/MacOS/$NAME"
fi
chmod +x "$BUNDLE/Contents/MacOS/$NAME"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key><string>en</string>
	<key>CFBundleExecutable</key><string>$NAME</string>
	<key>CFBundleIdentifier</key><string>com.hoversights.studio-mode-meters</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>CFBundleName</key><string>$NAME</string>
	<key>CFBundlePackageType</key><string>BNDL</string>
	<key>CFBundleShortVersionString</key><string>$VERSION</string>
	<key>CFBundleVersion</key><string>$VERSION</string>
	<key>CFBundleSupportedPlatforms</key><array><string>MacOSX</string></array>
	<key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

# Hardened runtime + secure timestamp are required for notarization, and
# harmless otherwise. Ad-hoc mode omits them: they are meaningless without
# a real identity.
if [ "$RELEASE" = "1" ]; then
  codesign --force --options runtime --timestamp --sign "$SIGN_IDENTITY" "$BUNDLE"
else
  codesign --force --sign "$SIGN_IDENTITY" "$BUNDLE"
fi
codesign --verify --strict "$BUNDLE" && echo "==> Signature verifies"

ZIP="target/${NAME}-${VERSION}-macos.zip"
rm -f "$ZIP"
# ditto, not zip: it preserves the bundle's symlinks and extended
# attributes, which a plain zip flattens and which breaks the signature.
ditto -c -k --keepParent "$BUNDLE" "$ZIP"
echo "==> $ZIP"

if [ "$NOTARIZE" = "1" ]; then
  echo "==> Notarizing (this takes minutes)"
  # An App Store Connect API key wins when supplied, because CI has no
  # keychain to hold a stored profile. Locally neither is set and the
  # keychain profile is used.
  if [ -n "${APPLE_API_KEY_PATH:-}" ]; then
    NOTARY_AUTH=(--key "$APPLE_API_KEY_PATH"
                 --key-id "${APPLE_API_KEY_ID:?APPLE_API_KEY_ID is required alongside APPLE_API_KEY_PATH}"
                 --issuer "${APPLE_API_ISSUER_ID:?APPLE_API_ISSUER_ID is required alongside APPLE_API_KEY_PATH}")
  else
    NOTARY_AUTH=(--keychain-profile "$KEYCHAIN_PROFILE")
  fi
  # See NOTARIZATION_LOG.md in the FrameSW repo: a local notarytool SIGBUS
  # predicts a submission that never resolves, so resubmit rather than wait.
  xcrun notarytool submit "$ZIP" "${NOTARY_AUTH[@]}" --wait
  # A bare .plugin bundle cannot be stapled — stapler targets apps, dmgs and
  # pkgs. The ticket lives on Apple's servers instead, so first launch needs
  # the user to be online once. Say so in the release notes rather than
  # letting someone hit it cold.
  echo "==> Notarized. NOT stapled (not possible for a bare bundle):"
  echo "    the user's Mac checks Apple online the first time it loads."
fi

echo ""
echo "Install with:"
echo "  cp -R $BUNDLE ~/Library/Application\\ Support/obs-studio/plugins/"
