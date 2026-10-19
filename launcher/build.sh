#!/usr/bin/env bash
# Build the frozen AXPilot.app launcher: universal2 (arm64 + x86_64), ad-hoc signed.
#
#   launcher/build.sh                      # writes launcher/build/
#   AXPILOT_LAUNCHER_OUT=/tmp/x launcher/build.sh
#
# Outputs (in the output directory):
#   AXPilot.app        the signed bundle (Contents/Info.plist, Contents/MacOS/AXPilot)
#   AXPilot.app.zip    the same bundle zipped with ditto (the release asset)
#   SHA256SUMS         checksums of every file in the bundle and of the zip
#   BUILDINFO.txt      cdhash per architecture, toolchain and source revision
#
# The cdhash printed at the end is what macOS pins users' permission grants to. Releases embed
# the bundle that CI built once per launcher version; a rebuild with another Xcode produces a
# different cdhash and is NOT a drop-in replacement. See launcher/README.md.
set -euo pipefail

readonly MIN_MACOS=14
readonly ARCHS=(arm64 x86_64)
readonly BUNDLE_ID=io.github.vansh3503.axpilot

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${AXPILOT_LAUNCHER_OUT:-$SRC_DIR/build}"
APP="$OUT_DIR/AXPilot.app"
BIN="$APP/Contents/MacOS/AXPilot"
PLISTBUDDY="${PLISTBUDDY:-/usr/libexec/PlistBuddy}"  # overridable so tests can stub Apple tools

die() { printf 'build.sh: error: %s\n' "$*" >&2; exit 1; }
step() { printf '==> %s\n' "$*"; }

# --- preflight -----------------------------------------------------------------------------------
[[ "$(uname -s)" == Darwin ]] || die "the launcher can only be built on macOS (this is $(uname -s))"
command -v xcrun >/dev/null 2>&1 || die "xcrun not found; install the Xcode Command Line Tools: xcode-select --install"
xcrun --find swiftc >/dev/null 2>&1 || die "swiftc not found; install Xcode or the Command Line Tools"
xcrun --find lipo >/dev/null 2>&1 || die "lipo not found; install Xcode or the Command Line Tools"
command -v codesign >/dev/null 2>&1 || die "codesign not found"

plutil -lint -s "$SRC_DIR/Info.plist" || die "Info.plist is not a valid property list"
plist_version="$("$PLISTBUDDY" -c 'Print :CFBundleShortVersionString' "$SRC_DIR/Info.plist")"
plist_id="$("$PLISTBUDDY" -c 'Print :CFBundleIdentifier' "$SRC_DIR/Info.plist")"
swift_version="$(sed -n 's/^let launcherVersion = "\([^"]*\)".*/\1/p' "$SRC_DIR/main.swift")"
[[ -n "$swift_version" ]] || die "could not find launcherVersion in main.swift"
[[ "$plist_version" == "$swift_version" ]] ||
  die "version mismatch: Info.plist says $plist_version, main.swift says $swift_version"
[[ "$plist_id" == "$BUNDLE_ID" ]] || die "unexpected CFBundleIdentifier $plist_id (expected $BUNDLE_ID)"

# --- compile -------------------------------------------------------------------------------------
step "Building AXPilot launcher $swift_version for ${ARCHS[*]} (macOS $MIN_MACOS+) into $OUT_DIR"
rm -rf "$APP" "$OUT_DIR/obj" "$OUT_DIR/AXPilot.app.zip" "$OUT_DIR/SHA256SUMS" "$OUT_DIR/BUILDINFO.txt"
mkdir -p "$OUT_DIR/obj" "$APP/Contents/MacOS"
sdk="$(xcrun --sdk macosx --show-sdk-path)"
slices=()
for arch in "${ARCHS[@]}"; do
  step "swiftc -O -target $arch-apple-macos$MIN_MACOS"
  # Compile from SRC_DIR with a relative source path so no absolute build path ends up in the binary.
  (cd "$SRC_DIR" && xcrun swiftc -O -swift-version 5 -module-name AXPilot -sdk "$sdk" \
    -target "$arch-apple-macos$MIN_MACOS" -o "$OUT_DIR/obj/AXPilot-$arch" main.swift)
  slices+=("$OUT_DIR/obj/AXPilot-$arch")
done
xcrun lipo -create -output "$BIN" "${slices[@]}"
cp "$SRC_DIR/Info.plist" "$APP/Contents/Info.plist"
rm -rf "$OUT_DIR/obj"

archs="$(xcrun lipo -archs "$BIN")"
for arch in "${ARCHS[@]}"; do
  [[ " $archs " == *" $arch "* ]] || die "the universal binary has no $arch slice (found: $archs)"
done

# --- sign ----------------------------------------------------------------------------------------
# Ad-hoc ("-"): free, no certificate and no private key that could leak. TCC then pins grants to
# the exact cdhash. No hardened runtime: it is only required for notarization, which an ad-hoc
# signature cannot get, and it would make TCC-attributed features depend on entitlements (for
# example com.apple.security.automation.apple-events). --timestamp=none keeps signing offline.
step "Ad-hoc signing"
xattr -cr "$APP"  # codesign rejects Finder info and other extended-attribute detritus
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict --verbose=2 "$APP"

details="$(codesign -dvvv "$APP" 2>&1)"
grep -qx "Identifier=$BUNDLE_ID" <<<"$details" || die "unexpected signing identifier:"$'\n'"$details"
grep -qx 'Signature=adhoc' <<<"$details" || die "expected an ad-hoc signature:"$'\n'"$details"

# Every architecture slice has its own code directory, so its own cdhash. TCC pins the one of
# the slice that runs (arm64 on Apple silicon, x86_64 on Intel). Plain arrays: macOS bash is 3.2.
cdhashes=()
for arch in "${ARCHS[@]}"; do
  value="$(codesign -dvvv -a "$arch" "$APP" 2>&1 | sed -n 's/^CDHash=//p')"
  [[ -n "$value" ]] || die "could not read the $arch cdhash"
  cdhashes+=("$value")
done

# --- smoke test ----------------------------------------------------------------------------------
step "Smoke test"
version_out="$("$BIN" --version)"
[[ "$version_out" == "AXPilot launcher $swift_version" ]] || die "unexpected --version output: $version_out"
if [[ "$(uname -m)" == arm64 ]] && arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
  [[ "$(arch -x86_64 "$BIN" --version)" == "$version_out" ]] || die "the x86_64 slice failed under Rosetta"
else
  echo "    (x86_64 slice not executed: not on Apple silicon with Rosetta)"
fi

# --- package -------------------------------------------------------------------------------------
step "Packaging"
(cd "$OUT_DIR" && ditto -c -k --norsrc --noextattr --noacl --keepParent AXPilot.app AXPilot.app.zip)
(cd "$OUT_DIR" && find AXPilot.app -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 &&
  shasum -a 256 AXPilot.app.zip) >"$OUT_DIR/SHA256SUMS"

tree_state=unknown
if git -C "$SRC_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  tree_state=clean
  [[ -z "$(git -C "$SRC_DIR" status --porcelain -- .)" ]] || tree_state=dirty
fi
{
  echo "launcher_version=$swift_version"
  echo "bundle_id=$BUNDLE_ID"
  for i in "${!ARCHS[@]}"; do echo "cdhash_${ARCHS[$i]}=${cdhashes[$i]}"; done
  echo "git_commit=$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "launcher_tree=$tree_state"
  echo "swiftc=$(xcrun swiftc --version 2>&1 | head -n 1)"
  echo "sdk=macosx$(xcrun --sdk macosx --show-sdk-version)"
  echo "build_host=macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)) $(uname -m)"
  echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$OUT_DIR/BUILDINFO.txt"

# --- report --------------------------------------------------------------------------------------
step "Done"
echo "    bundle:  $APP"
echo "    zip:     $OUT_DIR/AXPilot.app.zip"
echo "    archs:   $archs"
codesign -dvvv "$APP" 2>&1 | grep CDHash | sed 's/^/    /'
for i in "${!ARCHS[@]}"; do printf '    cdhash (%s): %s\n' "${ARCHS[$i]}" "${cdhashes[$i]}"; done
codesign -d -r- "$APP" 2>&1 | sed -n 's/^#* *designated => /    designated requirement: /p'
if [[ "$tree_state" == dirty ]]; then
  echo "    WARNING: launcher/ has uncommitted changes; do not publish this build."
fi
echo "    Record these values in launcher/CDHASH.md when publishing a launcher-v* release."
