#!/bin/sh
# AXPilot installer for macOS: https://github.com/vansh3503/Mac-Mcp
#
#   curl -LsSf https://vansh3503.github.io/axpilot/install.sh | sh
#   curl -LsSf https://vansh3503.github.io/axpilot/install.sh | sh -s -- --yes
#
# What it does, and nothing else:
#   1. checks that this is macOS 14 or later and that it runs as a normal user (never sudo);
#   2. prints what it is about to do and asks for confirmation (skip with --yes);
#   3. installs uv with Astral's official installer (https://astral.sh/uv/install.sh), only if uv
#      is not installed yet;
#   4. installs AXPilot with `uv tool install axpilot`, or upgrades it if it is already installed;
#   5. runs `axpilot setup` (skip with --no-setup).
# Running it again is safe: steps that are already done are skipped or become upgrades.
#
# Status: AXPilot is not yet published on PyPI. This script works once v0.1 is released; until then
# use the from-source instructions in docs/install.md.
#
# Options:
#   -y, --yes       don't ask for confirmation
#       --no-setup  don't run `axpilot setup` at the end
#   -n, --dry-run   print the commands that would run, change nothing
#   -h, --help      show this help
#
# Environment:
#   AXPILOT_PACKAGE  package spec for `uv tool install` (default: axpilot), e.g. "axpilot==0.1.0"
#   AXPILOT_YES=1    same as --yes

set -eu

UV_INSTALLER_URL="https://astral.sh/uv/install.sh"
MIN_MACOS_MAJOR=14
DOCS_URL="https://github.com/vansh3503/Mac-Mcp/blob/main/docs/install.md"

PACKAGE="${AXPILOT_PACKAGE:-axpilot}"
ASSUME_YES="${AXPILOT_YES:-0}"
RUN_SETUP=1
DRY_RUN=0

UV=""
UV_PRESENT=0
AXPILOT_PRESENT=0
MACOS_VERSION=""
ARCH=""
TMP_FILE=""

info() {
    printf '%s\n' "$*"
}

err() {
    printf 'axpilot-install: error: %s\n' "$*" >&2
}

die() {
    err "$*"
    exit 1
}

usage() {
    cat <<'EOF'
AXPilot installer for macOS

Usage: install.sh [--yes] [--no-setup] [--dry-run] [--help]

  -y, --yes       don't ask for confirmation
      --no-setup  don't run `axpilot setup` at the end
  -n, --dry-run   print the commands that would run, change nothing
  -h, --help      show this help

Environment:
  AXPILOT_PACKAGE  package spec for `uv tool install` (default: axpilot)
  AXPILOT_YES=1    same as --yes

Through a pipe, pass options after `sh -s --`:
  curl -LsSf https://vansh3503.github.io/axpilot/install.sh | sh -s -- --yes
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -y | --yes) ASSUME_YES=1 ;;
            --no-setup) RUN_SETUP=0 ;;
            -n | --dry-run) DRY_RUN=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *)
                err "unknown option: $1"
                usage >&2
                exit 2
                ;;
        esac
        shift
    done
}

# Print a command, then run it (unless --dry-run).
run() {
    printf '+ %s\n' "$*"
    if [ "$DRY_RUN" = 1 ]; then
        return 0
    fi
    "$@"
}

have_tty() {
    (: </dev/tty) 2>/dev/null
}

cleanup() {
    if [ -n "$TMP_FILE" ]; then
        rm -f "$TMP_FILE"
    fi
}

check_platform() {
    os="$(uname -s)"
    if [ "$os" != "Darwin" ]; then
        die "AXPilot needs macOS $MIN_MACOS_MAJOR or later; this system is $os. To develop on other systems, see $DOCS_URL"
    fi
    MACOS_VERSION="$(sw_vers -productVersion 2>/dev/null)" || die "could not read the macOS version (sw_vers)."
    major="${MACOS_VERSION%%.*}"
    case "$major" in
        '' | *[!0-9]*) die "unexpected macOS version: '$MACOS_VERSION'" ;;
    esac
    if [ "$major" -lt "$MIN_MACOS_MAJOR" ]; then
        die "AXPilot needs macOS $MIN_MACOS_MAJOR Sonoma or later; this Mac runs macOS $MACOS_VERSION."
    fi
    if [ "$(id -u)" -eq 0 ]; then
        die "don't run this installer as root or with sudo: AXPilot installs for your user only."
    fi
    ARCH="$(uname -m)"
}

# Find uv on PATH or in the places Astral's installer uses.
find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.local/bin/uv" "${CARGO_HOME:-$HOME/.cargo}/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

detect_state() {
    if UV="$(find_uv)"; then
        UV_PRESENT=1
        if "$UV" tool list 2>/dev/null | grep -q '^axpilot '; then
            AXPILOT_PRESENT=1
        fi
    else
        UV="uv"
        UV_PRESENT=0
    fi
}

print_plan() {
    info "AXPilot installer: macOS $MACOS_VERSION ($ARCH)"
    info ""
    info "This will:"
    if [ "$UV_PRESENT" = 1 ]; then
        info "  1. use the uv already installed at $UV"
    else
        info "  1. install uv, the Python package manager by Astral, with its official installer:"
        info "       $UV_INSTALLER_URL"
        info "     It puts uv in ~/.local/bin and may add that directory to your shell profile."
    fi
    if [ "$AXPILOT_PRESENT" = 1 ]; then
        info "  2. upgrade AXPilot: uv tool upgrade axpilot"
    else
        info "  2. install AXPilot: uv tool install $PACKAGE"
    fi
    if [ "$RUN_SETUP" = 1 ]; then
        info "  3. run: axpilot setup (checks your Mac and guides you through the macOS permissions)"
    fi
    info ""
    info "Nothing runs with sudo. Details: $DOCS_URL"
    info ""
}

confirm() {
    if [ "$ASSUME_YES" = 1 ] || [ "$DRY_RUN" = 1 ]; then
        return 0
    fi
    if ! have_tty; then
        die "no terminal to ask for confirmation. Re-run with --yes (through a pipe: sh -s -- --yes)."
    fi
    printf 'Continue? [Y/n] '
    answer=""
    read -r answer </dev/tty || answer="n"
    case "$answer" in
        '' | y | Y | yes | Yes | YES) return 0 ;;
        *)
            info "Cancelled. Nothing was changed."
            exit 1
            ;;
    esac
}

install_uv() {
    if [ "$UV_PRESENT" = 1 ]; then
        return 0
    fi
    command -v curl >/dev/null 2>&1 || die "curl is required to download the uv installer."
    if [ "$DRY_RUN" = 1 ]; then
        info "+ curl --proto '=https' --tlsv1.2 -LsSf $UV_INSTALLER_URL -o <temporary file>"
        info "+ sh <temporary file>"
        return 0
    fi
    # Download completely before running, so a broken connection can't run half a script.
    TMP_FILE="$(mktemp "${TMPDIR:-/tmp}/uv-installer.XXXXXX")"
    run curl --proto '=https' --tlsv1.2 -LsSf "$UV_INSTALLER_URL" -o "$TMP_FILE"
    run sh "$TMP_FILE"
    cleanup
    TMP_FILE=""
    UV="$(find_uv)" || die "uv was installed but can't be found. Open a new terminal and run this installer again."
}

install_axpilot() {
    if [ "$AXPILOT_PRESENT" = 1 ]; then
        run "$UV" tool upgrade axpilot
    else
        run "$UV" tool install "$PACKAGE"
    fi
}

axpilot_command() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '%s\n' "axpilot"
        return 0
    fi
    bin_dir="$("$UV" tool dir --bin)" || die "could not find uv's tool directory (uv tool dir --bin)."
    if [ ! -x "$bin_dir/axpilot" ]; then
        die "axpilot was not found in $bin_dir after installation."
    fi
    printf '%s\n' "$bin_dir/axpilot"
}

run_setup() {
    axpilot="$1"
    if [ "$RUN_SETUP" != 1 ]; then
        return 0
    fi
    if have_tty; then
        run "$axpilot" setup </dev/tty
    else
        info "No terminal is attached, so setup was skipped. Finish in a terminal with: axpilot setup"
    fi
}

print_next_steps() {
    axpilot="$1"
    info ""
    if [ "$DRY_RUN" = 1 ]; then
        info "Dry run: nothing was changed."
        return 0
    fi
    info "Done."
    case ":${PATH}:" in
        *":${axpilot%/*}:"*) ;;
        *)
            if [ "$axpilot" != "axpilot" ]; then
                info "Note: ${axpilot%/*} is not on your PATH. Run 'uv tool update-shell' and open a new terminal."
            fi
            ;;
    esac
    info "Next: connect an MCP client, for example: axpilot connect claude-desktop"
    info "      (see all clients with: axpilot connect --list)"
}

main() {
    parse_args "$@"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    check_platform
    detect_state
    print_plan
    confirm
    install_uv
    install_axpilot
    axpilot="$(axpilot_command)"
    run_setup "$axpilot"
    print_next_steps "$axpilot"
}

main "$@"
