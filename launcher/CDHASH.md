# Published launcher builds

Users' permission grants are pinned to these values (see [README.md](README.md#why-it-is-frozen)).
`release.yml` refuses to embed a launcher whose checksum or cdhash differs from the row it pins,
and `axpilot doctor` compares the installed `~/Applications/AXPilot.app` against it.

Fill a row from `build/BUILDINFO.txt` and `build/SHA256SUMS` of the CI build that produced the
`launcher-vN` release asset. Never edit a published row; add a new version instead.

| Launcher | Release asset | cdhash (arm64) | cdhash (x86_64) | SHA-256 of `AXPilot.app.zip` | SHA-256 of `Contents/MacOS/AXPilot` | Git commit | Toolchain | Published |
|---|---|---|---|---|---|---|---|---|
| 1.0.0 | `launcher-v1` (not yet published) | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ | _pending_ |

## How to read these values from an installed copy

```sh
codesign -dvvv -a arm64  ~/Applications/AXPilot.app 2>&1 | grep '^CDHash='
codesign -dvvv -a x86_64 ~/Applications/AXPilot.app 2>&1 | grep '^CDHash='
shasum -a 256 ~/Applications/AXPilot.app/Contents/MacOS/AXPilot
```
