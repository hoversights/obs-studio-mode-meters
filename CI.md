# CI and releasing

`.github/workflows/build.yml` builds, signs and (on a tag) notarizes both
platforms, then opens a **draft** GitHub Release with both artifacts
attached. Nothing reaches the public until someone reads the draft and
presses publish.

| trigger | what runs |
|---|---|
| any push | build, test, sign, upload artifacts |
| a `v*` tag | the same, **plus** notarization and a draft release |

Both jobs run on GitHub-hosted runners. That is not a preference — the
self-hosted runners this organisation already has are registered to
`stevets/framesw` and cannot be used from this repository.

## Secrets this repository needs

**GitHub Actions secrets are per-repository.** The identical secrets on
`hoversights/framesw-obs-plugin` do not carry over, and there is no way to
read them back out of that repo — they are write-only by design. They have
to be set again here, from the original material.

| secret | what it is | where the value comes from |
|---|---|---|
| `MACOS_CERTIFICATE` | base64 of a `.p12` export of the Developer ID Application certificate | Keychain Access → export the identity, then `base64 -i cert.p12 \| pbcopy` |
| `MACOS_CERTIFICATE_PWD` | the password set on that `.p12` during export | chosen at export time |
| `KEYCHAIN_PASSWORD` | any random string; it only protects a throwaway keychain that exists for one job | `openssl rand -base64 24` |
| `AZURE_TENANT_ID` | Azure service principal, tenant | the same values already on `framesw-obs-plugin` |
| `AZURE_CLIENT_ID` | Azure service principal, client | " |
| `AZURE_CLIENT_SECRET` | Azure service principal, secret | " |
| `APPLE_API_KEY` | base64 of the App Store Connect API key `.p8` | App Store Connect → Users and Access → Integrations → Keys |
| `APPLE_API_KEY_ID` | that key's ID | shown beside the key |
| `APPLE_API_ISSUER_ID` | the issuer ID for the team | shown above the key list |

Set them with:

```
gh secret set MACOS_CERTIFICATE -R hoversights/obs-studio-mode-meters < cert.b64
gh secret set KEYCHAIN_PASSWORD -R hoversights/obs-studio-mode-meters
```

...or paste them into **Settings → Secrets and variables → Actions** in the
browser, which prompts for each one and is the easier path for a handful.

### Why the Apple key is separate from local notarization

Locally, `scripts/package-macos.sh --release --notarize` uses the
`obs_controller-notary` keychain profile already stored on the maintainer's
Mac. A CI runner has no such profile and no keychain to hold one, so it
supplies an App Store Connect API key instead. The script takes either: set
`APPLE_API_KEY_PATH`, `APPLE_API_KEY_ID` and `APPLE_API_ISSUER_ID` and those
win; set none and the keychain profile is used.

An API key is also the better credential for CI regardless — it is not tied
to a person's Apple ID, survives their 2FA, and can be revoked on its own.

## Why notarization is required here, and is not on the sibling plugin

FrameSW's companion plugin is signed but never notarized, because it ships
*inside* `FrameSW.app` and rides that app's notarization ticket.

This plugin is downloaded on its own. macOS quarantines anything downloaded,
and Gatekeeper refuses to load a quarantined bundle that has not been
notarized — so for a release build notarization is not a nicety, it is the
difference between a plugin that loads and one that silently does not. The
tag build therefore **fails** rather than producing an un-notarized release.

One consequence to keep in the release notes: a bare `.plugin` bundle
**cannot be stapled**. `stapler` targets apps, disk images and installer
packages, not loadable bundles, so the ticket stays on Apple's servers and
the user's Mac has to reach them the first time it loads the plugin. The
draft release body says so.

## Cutting a release

1. Bump `version` in `Cargo.toml`. It is the single source of truth — both
   packaging scripts read it, and so does the artifact name.
2. Commit, then tag: `git tag v0.1.0 && git push origin v0.1.0`.
3. Watch the run. The macOS job takes noticeably longer on a tag because
   notarization blocks on Apple.
4. Open the draft release. **Check both assets are attached** before
   publishing — a job that failed after signing leaves a draft with one.
5. Publish.
6. Download both assets from the published URLs, unauthenticated, and
   confirm they are really there. A release can look complete in the UI and
   still serve an asset that no anonymous user can fetch; this check has
   caught exactly that before.

## Cross-checking the Windows build from a Mac

`cargo check --target x86_64-pc-windows-msvc` compiles the Windows-only
code — `platform.rs`'s `imp` module — without a Windows machine. It does not
link, so it will not catch a linker problem, but it does catch the thing
that actually breaks: editing Windows FFI you cannot compile.

**The target must be installed for the PINNED toolchain, not just for
`stable`.** These are separate directories under `~/.rustup/toolchains`, and
`rust-toolchain.toml` pins `1.95.0`:

```
rustup target add --toolchain 1.95.0 x86_64-pc-windows-msvc
```

Worth stating because of how it fails. `rustup target add` without
`--toolchain` adds to the default, so the check works right up until
`rust-toolchain.toml` is introduced or its channel is bumped — then it stops
with `can't find crate for std`, which reads as a broken checkout rather
than a missing component. That happened here on 2026-08-31, and a commit
message claiming the check was clean was written before the output was read.
