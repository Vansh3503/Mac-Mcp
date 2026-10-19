from __future__ import annotations

from pathlib import Path

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.defaults import (
    GLOBAL_DOMAIN,
    defaults_read,
    defaults_write,
    defaults_write_argv,
    encode_value,
)
from axpilot.system.runner import FakeRunner

NOT_FOUND_STDERR = (
    "\n2026-10-19 10:00:00.000 defaults[123:456] \n"
    "The domain/default pair of (com.apple.dock, nope) does not exist\n"
)


def test_read_key(runner: FakeRunner) -> None:
    runner.register(["defaults", "read"], stdout="48\n")
    assert defaults_read(runner, "com.apple.dock", "tilesize") == "48"
    assert runner.last_call.argv == ["defaults", "read", "com.apple.dock", "tilesize"]


def test_read_whole_domain_keeps_multiline_output(runner: FakeRunner) -> None:
    runner.register(["defaults", "read"], stdout="{\n    autohide = 1;\n    tilesize = 48;\n}\n")
    assert defaults_read(runner, "com.apple.dock") == "{\n    autohide = 1;\n    tilesize = 48;\n}"
    assert runner.last_call.argv == ["defaults", "read", "com.apple.dock"]


@pytest.mark.parametrize("alias", ["-g", "-globalDomain", "NSGlobalDomain", " -g "])
def test_global_domain_aliases(runner: FakeRunner, alias: str) -> None:
    runner.register(["defaults", "read"], stdout="Dark\n")
    defaults_read(runner, alias, "AppleInterfaceStyle")
    assert runner.last_call.argv == ["defaults", "read", GLOBAL_DOMAIN, "AppleInterfaceStyle"]


def test_plist_path_domain_expands_home(runner: FakeRunner, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HOME", str(tmp_path))
    runner.register(["defaults", "read"], stdout="1\n")
    defaults_read(runner, "~/Library/Preferences/com.example.plist", "k")
    assert runner.last_call.argv[2] == str(tmp_path / "Library/Preferences/com.example.plist")


def test_read_missing_key(runner: FakeRunner) -> None:
    runner.register(["defaults", "read"], stderr=NOT_FOUND_STDERR, exit_code=1)
    with pytest.raises(AXPilotError) as info:
        defaults_read(runner, "com.apple.dock", "nope")
    err = info.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.details["reason"] == "not_found"
    assert "'nope'" in err.message
    assert "com.apple.dock" in err.message


def test_read_other_failure(runner: FakeRunner) -> None:
    runner.register(["defaults", "read"], stderr="Could not read\n", exit_code=2)
    with pytest.raises(AXPilotError) as info:
        defaults_read(runner, "com.apple.dock")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "reason" not in info.value.details


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, ("-bool", "true")),
        (False, ("-bool", "false")),
        (48, ("-int", "48")),
        (-3, ("-int", "-3")),
        (0.5, ("-float", "0.5")),
        (1e-05, ("-float", "1e-05")),
        ("Dark", ("-string", "Dark")),
        ("-starts-with-dash", ("-string", "-starts-with-dash")),
        ("", ("-string", "")),
    ],
)
def test_type_is_inferred(value: object, expected: tuple[str, str]) -> None:
    assert encode_value(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "type_", "expected"),
    [
        ("yes", "bool", ("-bool", "true")),
        ("NO", "boolean", ("-bool", "false")),
        (1, "bool", ("-bool", "true")),
        (" 42 ", "int", ("-int", "42")),
        (3.0, "integer", ("-int", "3")),
        ("2.5", "float", ("-float", "2.5")),
        (7, "real", ("-float", "7.0")),
        (42, "string", ("-string", "42")),
        ("x", "str", ("-string", "x")),
    ],
)
def test_explicit_types_convert_compatible_values(
    value: object, type_: str, expected: tuple[str, str]
) -> None:
    assert encode_value(value, type_) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "type_"),
    [
        ("maybe", "bool"),
        (2, "bool"),
        (True, "int"),
        ("4.5", "int"),
        (4.5, "int"),
        (True, "float"),
        ("abc", "float"),
        (float("nan"), None),
        (float("inf"), "float"),
        (True, "string"),
        ("x", "date"),
        ([1, 2], None),
        (None, None),
        ("a\x00b", None),
    ],
)
def test_incompatible_values_are_rejected(value: object, type_: str | None) -> None:
    with pytest.raises(AXPilotError) as info:
        encode_value(value, type_)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_write_builds_typed_argv(runner: FakeRunner) -> None:
    runner.register(["defaults", "write"])
    defaults_write(runner, "com.apple.dock", "autohide", True)
    assert runner.last_call.argv == ["defaults", "write", "com.apple.dock", "autohide", "-bool", "true"]
    defaults_write(runner, "-g", "AppleShowScrollBars", "Always", type_="string")
    assert runner.last_call.argv == [
        "defaults",
        "write",
        GLOBAL_DOMAIN,
        "AppleShowScrollBars",
        "-string",
        "Always",
    ]


def test_write_failure(runner: FakeRunner) -> None:
    runner.register(["defaults", "write"], stderr="Could not write domain\n", exit_code=1)
    with pytest.raises(AXPilotError) as info:
        defaults_write(runner, "com.apple.dock", "tilesize", 48)
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "tilesize" in info.value.message


@pytest.mark.parametrize(
    ("domain", "key"),
    [
        ("", "k"),
        ("-currentHost", "k"),
        (" -currentHost", "k"),
        ("\t-app", "k"),
        ("-app", "k"),
        ("com.x\x00y", "k"),
        ("com.x\ny", "k"),
        ("com.example", ""),
        ("com.example", "-delete"),
        ("com.example", "k\x00"),
        ("x" * 1025, "k"),
    ],
)
def test_domain_and_key_validation(runner: FakeRunner, domain: str, key: str) -> None:
    with pytest.raises(AXPilotError) as info:
        defaults_write(runner, domain, key, 1)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        defaults_read(runner, domain, key)
    assert runner.calls == []


def test_defaults_write_argv() -> None:
    assert defaults_write_argv("com.example", "k", 1.25) == [
        "defaults",
        "write",
        "com.example",
        "k",
        "-float",
        "1.25",
    ]
