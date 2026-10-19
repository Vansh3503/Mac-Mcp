"""Configuration: defaults, precedence (overrides > environment > file > defaults), environment
coercion, validation errors that name the key, ``config set`` and the documentation of every key."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from axpilot.config import (
    DEFAULT_DENY_APPS,
    Config,
    dump_config,
    load_config,
    read_config_file,
    set_config_value,
)
from axpilot.errors import AXPilotError, ErrorCode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def test_defaults_without_a_file(config_file: Path) -> None:
    config = load_config(config_file, env={})
    assert config == Config()
    assert config.tools.profile == "standard"
    assert config.safety.deny_apps == list(DEFAULT_DENY_APPS)
    assert not config_file.exists()


def test_precedence(config_file: Path) -> None:
    config_file.write_text('[tools]\nprofile = "full"\nprefix = "file_"\n[safety]\nconfirm = "always"\n')
    env = {"AXPILOT_TOOLS_PROFILE": "minimal", "AXPILOT_TOOLS_PREFIX": "env_"}
    config = load_config(config_file, env=env, overrides={"tools.profile": "computer", "tools.prefix": None})
    assert config.tools.profile == "computer"  # flag beats environment
    assert config.tools.prefix == "env_"  # environment beats file; a None flag is ignored
    assert config.safety.confirm == "always"  # file beats defaults
    assert config.safety.yield_to_user is True


def test_environment_values_are_coerced(config_file: Path) -> None:
    env = {
        "AXPILOT_TOOLS_ENABLE": "scripting, browser,,",
        "AXPILOT_SAFETY_ALLOW_SECURE_FIELDS": "true",
        "AXPILOT_SAFETY_MAX_ACTIONS_PER_MINUTE": "30",
        "AXPILOT_SERVER_ENGINE_TIMEOUT_S": "2.5",
        "AXPILOT_HOME": "/elsewhere",  # a path variable, not a setting
        "PATH": "/usr/bin",
    }
    config = load_config(config_file, env=env)
    assert config.tools.enable == ["scripting", "browser"]
    assert config.safety.allow_secure_fields is True
    assert config.safety.max_actions_per_minute == 30
    assert config.server.engine_timeout_s == 2.5


@given(st.lists(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_-.*", min_size=1, max_size=12), max_size=6))
def test_list_variables_round_trip(names: list[str]) -> None:
    config = load_config(Path("/nonexistent/config.toml"), env={"AXPILOT_SAFETY_DENY_APPS": ",".join(names)})
    assert config.safety.deny_apps == names


def test_a_misspelled_variable_is_reported(config_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="axpilot.config"):
        config = load_config(config_file, env={"AXPILOT_TOOLS_PROFLE": "minimal"})
    assert config.tools.profile == "standard"
    assert "AXPILOT_TOOLS_PROFLE" in caplog.text
    assert "AXPILOT_TOOLS_PROFILE" in caplog.text  # the suggestion


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('[tools]\nprofile = "huge"\n', "profile"),
        ('[tools]\nprofle = "full"\n', "profle"),
        ("[nonsense]\nx = 1\n", "nonsense"),
        ("[safety]\nmax_actions_per_minute = 0\n", "max_actions_per_minute"),
        ('[tools]\nprefix = "a b"\n', "prefix"),
    ],
)
def test_invalid_files_name_the_key(config_file: Path, text: str, key: str) -> None:
    config_file.write_text(text)
    with pytest.raises(AXPilotError) as caught:
        load_config(config_file, env={})
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert key in caught.value.message
    assert caught.value.hint


def test_invalid_environment_values_are_errors(config_file: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        load_config(config_file, env={"AXPILOT_SAFETY_MAX_ACTIONS_PER_MINUTE": "lots"})
    assert "max_actions_per_minute" in caught.value.message


def test_invalid_toml(config_file: Path) -> None:
    config_file.write_text("[tools\nprofile = ")
    with pytest.raises(AXPilotError) as caught:
        read_config_file(config_file)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert str(config_file) in caught.value.message


def test_set_config_value(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.toml"
    config = set_config_value(path, "safety.confirm", "always")
    assert config.safety.confirm == "always"
    set_config_value(path, "tools.enable", ["scripting"])
    written = tomllib.loads(path.read_text())
    assert written == {"safety": {"confirm": "always"}, "tools": {"enable": ["scripting"]}}
    before = path.read_text()
    with pytest.raises(AXPilotError) as caught:
        set_config_value(path, "safety.confirm", "sometimes")
    assert "safety.confirm" in caught.value.message
    assert path.read_text() == before  # nothing written


def test_dump_config_round_trips(config_file: Path) -> None:
    config = load_config(config_file, env={}, overrides={"tools.prefix": "mac_", "audit.log_args": "none"})
    config_file.write_text(dump_config(config))
    assert load_config(config_file, env={}) == config


def test_audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXPILOT_LOG_DIR", str(tmp_path / "logs"))
    assert Config().audit_path() == tmp_path / "logs" / "audit.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path))
    custom = load_config(tmp_path / "none.toml", env={}, overrides={"audit.path": "~/audit.jsonl"})
    assert custom.audit_path() == tmp_path / "audit.jsonl"


def test_every_setting_is_documented() -> None:
    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    sections = text.split("\n## `[")
    for section, field in Config.model_fields.items():
        model = field.annotation
        assert isinstance(model, type)
        table = next((part for part in sections if part.startswith(f"{section}]`")), "")
        assert table, section
        for key in model.model_fields:
            assert f"`{key}`" in table, f"[{section}] {key} is missing from docs/configuration.md"
