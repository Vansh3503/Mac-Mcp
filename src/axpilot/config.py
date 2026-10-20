"""Configuration model and loader.

Precedence (highest first): explicit ``overrides`` (CLI flags) → ``AXPILOT_<SECTION>_<KEY>``
environment variables → the TOML config file → defaults.

Example: ``AXPILOT_TOOLS_PROFILE=minimal`` sets ``[tools] profile``. List values in env vars
are comma-separated (``AXPILOT_TOOLS_ENABLE=scripting,browser``).
"""

from __future__ import annotations

import difflib
import logging
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from axpilot import paths
from axpilot.errors import AXPilotError, ErrorCode

Profile = Literal["minimal", "standard", "full", "computer"]
Naming = Literal["default", "windows-mcp"]
ConfirmMode = Literal["never", "destructive", "always"]
ArgLogging = Literal["none", "redacted", "full"]

_log = logging.getLogger(__name__)

DEFAULT_DENY_APPS: tuple[str, ...] = (
    "com.apple.keychainaccess",
    "com.apple.Passwords",
    "com.1password.1password",
    "com.agilebits.onepassword7",
    "com.bitwarden.desktop",
    "com.lastpass.LastPass",
    "com.dashlane.dashlanephonefinal",
    "org.keepassxc.keepassxc",
)

DEFAULT_DENY_WINDOWS: tuple[str, ...] = (
    # "<bundle id>::<window title substring>" — blocks a single pane of an otherwise allowed app.
    "com.apple.systempreferences::Privacy & Security",
    "com.apple.settings.PrivacySecurity.extension::Privacy & Security",
)


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ServerConfig(_Section):
    http_enabled: bool = False
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=7788, ge=1, le=65535)
    allow_remote: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    engine_timeout_s: float = Field(default=20.0, ge=1.0, le=300.0)
    """Deadline for one engine call made by a tool; slower calls fail with ``TIMEOUT``."""
    input_lock_timeout_s: float = Field(default=5.0, ge=0.1, le=5.0)
    """How long an input tool waits for the single input lock before failing with ``BUSY``."""
    token_store: Literal["auto", "keychain", "file"] = "auto"  # noqa: S105 - a mode, not a secret
    """Where the HTTP bearer token is kept: ``auto`` is the macOS keychain on a Mac and a ``0600``
    file elsewhere."""


class ToolsConfig(_Section):
    profile: Profile = "standard"
    naming: Naming = "default"
    enable: list[str] = Field(default_factory=list)
    """Tool or toolset names to add on top of the profile (e.g. ``"scripting"``)."""
    disable: list[str] = Field(default_factory=list)
    """Tool or toolset names to remove from the profile."""
    prefix: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,24}$")
    """Put before every tool name (``mac_`` → ``mac_snapshot``), for clients that mix the tools of
    several servers without namespacing them."""


class SafetyConfig(_Section):
    confirm: ConfirmMode = "destructive"
    yield_to_user: bool = True
    user_idle_threshold_s: float = Field(default=1.0, ge=0.0, le=30.0)
    indicator: bool = True
    deny_apps: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_APPS))
    deny_windows: list[str] = Field(default_factory=lambda: list(DEFAULT_DENY_WINDOWS))
    allow_apps: list[str] = Field(default_factory=list)
    """If non-empty, only these apps (bundle ids or names) may be observed or controlled."""
    allow_secure_fields: bool = False
    max_actions_per_minute: int = Field(default=120, ge=1, le=10_000)
    require_pairing: bool = True
    confirm_timeout_s: float = Field(default=120.0, ge=5.0, le=120.0)
    """How long a confirmation waits for the user; no answer in time counts as ``USER_DENIED``."""
    elicitation: bool = True
    """Direct mode: ask confirmations through the MCP client (elicitation) when it supports that."""


class CaptureConfig(_Section):
    max_edge: int = Field(default=1280, ge=256, le=8192)
    format: Literal["png", "jpeg"] = "jpeg"
    quality: int = Field(default=80, ge=10, le=100)


class PerceptionConfig(_Section):
    max_elements: int = Field(default=150, ge=10, le=2000)
    max_nodes: int = Field(default=4000, ge=100, le=50_000)
    max_depth: int = Field(default=40, ge=5, le=200)
    ax_timeout_ms: int = Field(default=1000, ge=50, le=30_000)
    value_max_chars: int = Field(default=200, ge=20, le=10_000)
    enhance_electron_chrome: bool = True
    settle_ms: int = Field(default=150, ge=0, le=5000)
    capture_timeout_s: float = Field(default=5.0, ge=0.5, le=60.0)
    """Budget for one accessibility-tree capture; the snapshot is marked truncated when it runs out."""


class ScriptingConfig(_Section):
    shell: str = "/bin/zsh"
    shell_timeout_s: int = Field(default=30, ge=1, le=120)
    output_max_chars: int = Field(default=20_000, ge=500, le=1_000_000)


class AuditConfig(_Section):
    enabled: bool = True
    log_args: ArgLogging = "redacted"
    path: str | None = None
    """Defaults to ``~/Library/Logs/AXPilot/audit.jsonl``."""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    scripting: ScriptingConfig = Field(default_factory=ScriptingConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    def audit_path(self) -> Path:
        return Path(self.audit.path).expanduser() if self.audit.path else paths.audit_log_file()


_SECTIONS: dict[str, type[_Section]] = {
    "server": ServerConfig,
    "tools": ToolsConfig,
    "safety": SafetyConfig,
    "capture": CaptureConfig,
    "perception": PerceptionConfig,
    "scripting": ScriptingConfig,
    "audit": AuditConfig,
}


def _coerce_env_value(section: str, key: str, raw: str) -> Any:
    model = _SECTIONS[section]
    field = model.model_fields[key]
    origin = getattr(field.annotation, "__origin__", None)
    if origin is list:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw  # pydantic coerces "true"/"1"/"120" for bool/int fields


def _env_layer(env: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    layer: dict[str, dict[str, Any]] = {}
    for name, raw in env.items():
        if not name.startswith("AXPILOT_"):
            continue
        rest = name.removeprefix("AXPILOT_").lower()
        for section, model in _SECTIONS.items():
            prefix = section + "_"
            if rest.startswith(prefix):
                key = rest.removeprefix(prefix)
                if key in model.model_fields:
                    layer.setdefault(section, {})[key] = _coerce_env_value(section, key, raw)
                else:
                    close = difflib.get_close_matches(key, list(model.model_fields), n=1)
                    _log.warning(
                        "Ignoring %s: [%s] has no key %r%s.",
                        name,
                        section,
                        key,
                        f" (did you mean AXPILOT_{section.upper()}_{close[0].upper()}?)" if close else "",
                    )
                break
    return layer


def _merge(base: dict[str, Any], layer: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in layer.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _dotted_to_nested(overrides: Mapping[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for dotted, value in overrides.items():
        if value is None:
            continue
        parts = dotted.split(".")
        cursor = nested
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return nested


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Config file {path} is not valid TOML: {exc}",
            hint="Fix the file or run `axpilot config edit`.",
        ) from exc


def load_config(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load configuration. ``overrides`` uses dotted keys, e.g. ``{"tools.profile": "full"}``;
    ``None`` values are ignored so CLI options can be passed straight through."""
    data = read_config_file(path or paths.config_file())
    data = _merge(data, _env_layer(os.environ if env is None else env))
    data = _merge(data, _dotted_to_nested(overrides or {}))
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Invalid AXPilot configuration: {exc}",
            hint="Check ~/.config/axpilot/config.toml and AXPILOT_* environment variables.",
        ) from exc


def dump_config(config: Config) -> str:
    data = config.model_dump(mode="json", exclude_none=True)
    return tomli_w.dumps(data)


def set_config_value(path: Path, dotted_key: str, value: Any) -> Config:
    """Set one dotted key in the TOML file (creating it if needed) and return the new config."""
    data = read_config_file(path)
    merged = _merge(data, _dotted_to_nested({dotted_key: value}))
    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Invalid value for {dotted_key}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(merged), encoding="utf-8")
    return config
