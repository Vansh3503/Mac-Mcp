"""Spike S5: grant_tcc.py against throwaway TCC databases with real macOS schemas."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

VENTURA_COLUMNS = """
    service TEXT NOT NULL, client TEXT NOT NULL, client_type INTEGER NOT NULL,
    auth_value INTEGER NOT NULL, auth_reason INTEGER NOT NULL, auth_version INTEGER NOT NULL,
    csreq BLOB, policy_id INTEGER, indirect_object_identifier_type INTEGER,
    indirect_object_identifier TEXT NOT NULL DEFAULT 'UNUSED', indirect_object_code_identity BLOB,
    flags INTEGER, last_modified INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
"""
SONOMA_EXTRA = """,
    pid INTEGER, pid_version INTEGER, boot_uuid TEXT NOT NULL DEFAULT 'UNUSED',
    last_reminded INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
"""
PRIMARY_KEY = ", PRIMARY KEY (service, client, client_type, indirect_object_identifier)"


@pytest.fixture(scope="module")
def tcc(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s5_ci_permissions/grant_tcc.py")


def make_db(path: Path, columns: str) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE access ({columns}{PRIMARY_KEY})")
    return path


@pytest.fixture(params=["ventura", "sonoma"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    columns = VENTURA_COLUMNS + (SONOMA_EXTRA if request.param == "sonoma" else "")
    return make_db(tmp_path / "TCC.db", columns)


def rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT service, client, client_type, auth_value, indirect_object_identifier, csreq FROM access"
        ).fetchall()


def test_grant_a_path_client(tcc: ModuleType, db: Path) -> None:
    code = tcc.main(
        [
            "grant",
            "--service",
            "Accessibility",
            "--client",
            "/usr/bin/python3",
            "--db-path",
            str(db),
            "--no-csreq",
        ]
    )
    assert code == 0
    assert rows(db) == [("kTCCServiceAccessibility", "/usr/bin/python3", 1, 2, "UNUSED", None)]


def test_grant_is_idempotent(tcc: ModuleType, db: Path) -> None:
    args = [
        "grant",
        "--service",
        "ScreenCapture",
        "--client",
        "io.github.vansh3503.axpilot",
        "--db-path",
        str(db),
    ]
    assert tcc.main(args) == 0
    assert tcc.main(args) == 0
    assert rows(db) == [("kTCCServiceScreenCapture", "io.github.vansh3503.axpilot", 0, 2, "UNUSED", None)]


def test_apple_events_need_an_indirect_object(tcc: ModuleType, db: Path) -> None:
    args = [
        "grant",
        "--service",
        "AppleEvents",
        "--client",
        "/usr/bin/python3",
        "--indirect",
        "com.apple.finder",
    ]
    assert tcc.main([*args, "--db-path", str(db), "--no-csreq"]) == 0
    assert rows(db)[0][4] == "com.apple.finder"


def test_dry_run_writes_nothing(tcc: ModuleType, db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["grant", "--service", "Accessibility", "--client", "/x/python", "--db-path", str(db), "--dry-run"]
    assert tcc.main([*args, "--no-csreq"]) == 0
    assert rows(db) == []
    assert "would write" in capsys.readouterr().out


def test_list(tcc: ModuleType, db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tcc.main(
        ["grant", "--service", "Accessibility", "--client", "/x/python", "--db-path", str(db), "--no-csreq"]
    )
    assert tcc.main(["list", "--db-path", str(db), "--service", "Accessibility"]) == 0
    out = capsys.readouterr().out
    assert "1 row(s)" in out
    assert "/x/python" in out
    assert tcc.list_grants(db, ["kTCCServiceScreenCapture"]) == []


def test_unknown_required_column_is_refused(tcc: ModuleType, tmp_path: Path) -> None:
    db = make_db(tmp_path / "future.db", VENTURA_COLUMNS + ", future_required TEXT NOT NULL")
    row = tcc.grant_row("kTCCServiceAccessibility", "/x", 1, csreq=None, indirect=None, now=1)
    with pytest.raises(tcc.GrantError, match="future_required"):
        tcc.insert_grant(db, row)


def test_missing_or_tableless_database(
    tcc: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.db"
    args = ["grant", "--service", "Accessibility", "--client", "/x", "--no-csreq", "--db-path"]
    assert tcc.main([*args, str(missing)]) == 1
    assert not missing.exists()  # not created as a side effect
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert tcc.main([*args, str(empty)]) == 1
    assert "no 'access' table" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("client", "requested", "expected"),
    [("/usr/bin/python3", "auto", 1), ("com.example.app", "auto", 0), ("/x", "bundle", 0), ("x", "path", 1)],
)
def test_client_type(tcc: ModuleType, client: str, requested: str, expected: int) -> None:
    assert tcc.client_type_for(client, requested) == expected


def test_relative_path_client_is_refused(tcc: ModuleType, db: Path) -> None:
    args = ["grant", "--service", "Accessibility", "--client", "python3", "--client-type", "path"]
    assert tcc.main([*args, "--db-path", str(db), "--no-csreq"]) == 1


def test_database_selection(tcc: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tcc.databases("system", None) == [tcc.SYSTEM_DB]
    assert tcc.databases("user", None) == [tmp_path / tcc.USER_DB_RELATIVE]
    assert tcc.databases("both", tmp_path / "x.db") == [tmp_path / "x.db"]
