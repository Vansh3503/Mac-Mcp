# Published launcher builds

Users' permission grants are pinned to these values (see [README.md](README.md#why-it-is-frozen)).
`release.yml` refuses to embed a launcher whose checksum or cdhash differs from the row it pins,
and `axpilot doctor` compares the installed `~/Applications/AXPilot.app` against it.

Fill a row from `build/BUILDINFO.txt` and `build/SHA256SUMS` of the CI build that produced the
`launcher-vN` release asset. Never edit a published row; add a new version instead.

| Launcher | Release asset | cdhash (arm64) | cdhash (x86_64) | SHA-256 of `AXPilot.app.zip` | SHA-256 of `Contents/MacOS/AXPilot` | Git commit | Toolchain | Published |
|---|---|---|---|---|---|---|---|---|
| 1.0.0 | `launcher-v1` | `ca3fde20450a55467e2bc927ef154a9275ee4f64` | `560c01f5c4de0d803b601bbfb86953ef4332243c` | `742c696386560e80e9fc0fb539eeccc57ab965cc715b830160aca7752fb05281` | `5c09d639377fff881955809423788cd58b7f573cd2f7a246d6e8415e081a0409` | `d8530054d4b7349673d9b52b87c377dfa85599eb` | Swift 5.10, macOS 14.5 SDK (macOS 14.8.9 arm64 runner) | 2026-10-21 |

## How to read these values from an installed copy

```sh
codesign -dvvv -a arm64  ~/Applications/AXPilot.app 2>&1 | grep '^CDHash='
codesign -dvvv -a x86_64 ~/Applications/AXPilot.app 2>&1 | grep '^CDHash='
shasum -a 256 ~/Applications/AXPilot.app/Contents/MacOS/AXPilot
```
