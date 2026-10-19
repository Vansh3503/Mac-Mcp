"""Well-known filesystem locations.

Every path can be redirected with an environment variable so tests (and Linux CI) never
touch the real home directory:

* ``AXPILOT_CONFIG_DIR``  → config directory (default ``$XDG_CONFIG_HOME/axpilot`` or ``~/.config/axpilot``)
* ``AXPILOT_HOME``        → application-support directory (default ``~/Library/Application Support/AXPilot``)
* ``AXPILOT_LOG_DIR``     → log directory (default ``~/Library/Logs/AXPilot``)
* ``AXPILOT_SOCKET``      → host socket path
* ``AXPILOT_APP_PATH``    → installed launcher app (default ``~/Applications/AXPilot.app``)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APP_NAME = "AXPilot"
_MAX_UNIX_SOCKET_PATH = 103  # sun_path is 104 bytes on macOS, including the NUL terminator


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    if p := _env_path("AXPILOT_CONFIG_DIR"):
        return p
    if xdg := _env_path("XDG_CONFIG_HOME"):
        return xdg / "axpilot"
    return Path.home() / ".config" / "axpilot"


def config_file() -> Path:
    return config_dir() / "config.toml"


def app_support_dir() -> Path:
    if p := _env_path("AXPILOT_HOME"):
        return p
    return Path.home() / "Library" / "Application Support" / APP_NAME


def runtime_dir() -> Path:
    """The dedicated venv the launcher runs (created by ``axpilot setup``)."""
    return app_support_dir() / "runtime"


def runtime_manifest() -> Path:
    """``runtime.json``: tells the launcher which interpreter and module to spawn."""
    return app_support_dir() / "runtime.json"


def pairing_file() -> Path:
    return app_support_dir() / "paired-clients.json"


def log_dir() -> Path:
    if p := _env_path("AXPILOT_LOG_DIR"):
        return p
    return Path.home() / "Library" / "Logs" / APP_NAME


def audit_log_file() -> Path:
    return log_dir() / "audit.jsonl"


def socket_path() -> Path:
    """Per-user Unix socket of the host. Falls back to ``$TMPDIR`` if the default is too long."""
    if p := _env_path("AXPILOT_SOCKET"):
        return p
    default = app_support_dir() / "axpilot.sock"
    if len(os.fsencode(default)) <= _MAX_UNIX_SOCKET_PATH:
        return default
    return Path(tempfile.gettempdir()) / f"axpilot-{os.getuid()}.sock"


def launcher_app_path() -> Path:
    if p := _env_path("AXPILOT_APP_PATH"):
        return p
    return Path.home() / "Applications" / f"{APP_NAME}.app"
