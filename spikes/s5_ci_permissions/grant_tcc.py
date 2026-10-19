#!/usr/bin/env python3
"""Spike S5: write TCC grants straight into TCC.db, for disposable CI machines only.

GitHub's macOS runners ship with System Integrity Protection disabled, which makes the TCC
databases writable by root. That is how CI can grant Accessibility without a human clicking in
System Settings. Never run this on a machine you care about.

    sudo python3 grant_tcc.py grant --service Accessibility --client /path/to/python3.12
    sudo python3 grant_tcc.py grant --service Accessibility --client io.github.vansh3503.axpilot \\
        --csreq-from ~/Applications/AXPilot.app
    python3 grant_tcc.py list --db both

Accessibility, Screen Recording, PostEvent and ListenEvent live in the system database
(``/Library/Application Support/com.apple.TCC/TCC.db``); per-user services such as AppleEvents
live in ``~/Library/Application Support/com.apple.TCC/TCC.db``. ``--db both`` writes both, since
the split has moved between macOS versions. Rows are written by column name, so the differing
``access`` schemas of macOS 11-27 all work; tccd is restarted afterwards so it rereads them.
Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

SERVICES = {
    "Accessibility": "kTCCServiceAccessibility",
    "ScreenCapture": "kTCCServiceScreenCapture",
    "PostEvent": "kTCCServicePostEvent",
    "ListenEvent": "kTCCServiceListenEvent",
    "AppleEvents": "kTCCServiceAppleEvents",
}
SYSTEM_DB = Path("/Library/Application Support/com.apple.TCC/TCC.db")
USER_DB_RELATIVE = Path("Library/Application Support/com.apple.TCC/TCC.db")
CLIENT_TYPE_BUNDLE, CLIENT_TYPE_PATH = 0, 1
AUTH_ALLOWED = 2
AUTH_REASON_SYSTEM_SET = 4  # what the runner images use for their own pre-granted rows


class GrantError(Exception):
    """A refusal with a message for the user."""


def client_type_for(client: str, requested: str = "auto") -> int:
    if requested == "bundle":
        return CLIENT_TYPE_BUNDLE
    if requested == "path":
        return CLIENT_TYPE_PATH
    return CLIENT_TYPE_PATH if client.startswith("/") else CLIENT_TYPE_BUNDLE


def grant_row(
    service: str, client: str, client_type: int, *, csreq: bytes | None, indirect: str | None, now: int
) -> dict[str, Any]:
    """The ``access`` row that allows ``client`` to use ``service``."""
    return {
        "service": service,
        "client": client,
        "client_type": client_type,
        "auth_value": AUTH_ALLOWED,
        "auth_reason": AUTH_REASON_SYSTEM_SET,
        "auth_version": 1,
        "csreq": csreq,
        "policy_id": None,
        "indirect_object_identifier_type": CLIENT_TYPE_BUNDLE if indirect else None,
        "indirect_object_identifier": indirect or "UNUSED",
        "indirect_object_code_identity": None,
        "flags": 0,
        "last_modified": now,
        "last_reminded": now,
        "boot_uuid": "UNUSED",
    }


def access_columns(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Columns of the ``access`` table: name -> {notnull, default}."""
    rows = connection.execute("PRAGMA table_info(access)").fetchall()
    if not rows:
        raise GrantError("this database has no 'access' table")
    return {row[1]: {"notnull": bool(row[3]), "default": row[4]} for row in rows}


def insert_grant(db_path: Path, row: dict[str, Any]) -> list[str]:
    """INSERT OR REPLACE ``row`` using only the columns this schema has; returns the columns written."""
    if not db_path.is_file():
        raise GrantError(f"{db_path} does not exist")
    with closing(sqlite3.connect(db_path)) as connection, connection:
        columns = access_columns(connection)
        values = {name: value for name, value in row.items() if name in columns}
        missing = [
            name
            for name, meta in columns.items()
            if meta["notnull"] and meta["default"] is None and values.get(name) is None
        ]
        if missing:
            raise GrantError(f"unknown required columns in {db_path}: {', '.join(missing)} (new schema?)")
        names = list(values)
        placeholders = ", ".join("?" for _ in names)
        statement = f"INSERT OR REPLACE INTO access ({', '.join(names)}) VALUES ({placeholders})"
        connection.execute(statement, [values[name] for name in names])
    return names


def list_grants(db_path: Path, services: list[str]) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)) as connection:
        marks = ", ".join("?" for _ in services)
        query = (
            "SELECT service, client, client_type, auth_value, auth_reason, indirect_object_identifier "
            f"FROM access WHERE service IN ({marks}) ORDER BY service, client"
        )
        rows = connection.execute(query, services).fetchall()
    keys = ("service", "client", "client_type", "auth_value", "auth_reason", "indirect_object_identifier")
    return [dict(zip(keys, row)) for row in rows]  # noqa: B905


def user_home() -> Path:
    """The invoking user's home, also under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def databases(which: str, override: Path | None) -> list[Path]:
    if override is not None:
        return [override]
    user = user_home() / USER_DB_RELATIVE
    return {"system": [SYSTEM_DB], "user": [user], "both": [SYSTEM_DB, user]}[which]


def designated_requirement(code_path: str) -> str | None:
    """The designated requirement text of signed code, e.g. ``cdhash H"..."`` for ad-hoc code."""
    try:
        result = subprocess.run(
            ["codesign", "-d", "-r-", code_path], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (result.stdout + result.stderr).splitlines():
        match = re.match(r"^#?\s*designated => (.+)$", line.strip())
        if match:
            return match.group(1)
    return None


def compile_requirement(text: str) -> bytes | None:
    """The binary form of a requirement (``csreq -r=... -b file``), as TCC.db stores it."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "csreq.bin"
        try:
            subprocess.run(
                ["csreq", f"-r={text}", "-b", str(output)], check=True, capture_output=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return output.read_bytes()


def restart_tccd() -> None:
    """tccd caches the databases; launchd restarts it on demand."""
    subprocess.run(["killall", "tccd"], capture_output=True, timeout=30, check=False)


def command_grant(args: argparse.Namespace) -> int:
    service = SERVICES[args.service]
    client_type = client_type_for(args.client, args.client_type)
    if client_type == CLIENT_TYPE_PATH and not os.path.isabs(args.client):
        raise GrantError("a path client must be an absolute path")
    csreq = None
    code = args.csreq_from or (args.client if client_type == CLIENT_TYPE_PATH else None)
    if code and not args.no_csreq:
        requirement = designated_requirement(code)
        csreq = compile_requirement(requirement) if requirement else None
        print(f"code requirement for {code}: {requirement or 'none (unsigned?)'}")
    row = grant_row(
        service, args.client, client_type, csreq=csreq, indirect=args.indirect, now=int(time.time())
    )
    for db_path in databases(args.db, args.db_path):
        if args.dry_run:
            print(f"would write to {db_path}: {row | {'csreq': bool(csreq)}}")
            continue
        try:
            columns = insert_grant(db_path, row)
        except sqlite3.Error as exc:
            raise GrantError(
                f"cannot write {db_path}: {exc}. SIP must be disabled and this must run as root (sudo)."
            ) from exc
        print(f"granted {service} to {args.client} in {db_path} ({len(columns)} columns)")
    if not args.dry_run and not args.no_restart and args.db_path is None:
        restart_tccd()
    return 0


def command_list(args: argparse.Namespace) -> int:
    services = [SERVICES[name] for name in args.service] if args.service else list(SERVICES.values())
    for db_path in databases(args.db, args.db_path):
        try:
            rows = list_grants(db_path, services)
        except sqlite3.Error as exc:
            print(f"{db_path}: unreadable ({exc}); reading needs Full Disk Access or root")
            continue
        print(f"{db_path}: {len(rows)} row(s)")
        for row in rows:
            print(f"  {row}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant", help="allow a client to use a service")
    grant.add_argument("--service", choices=sorted(SERVICES), required=True)
    grant.add_argument("--client", required=True, help="absolute executable path or bundle identifier")
    grant.add_argument("--client-type", choices=("auto", "path", "bundle"), default="auto")
    grant.add_argument(
        "--csreq-from", help="code to derive the stored code requirement from (default: the path)"
    )
    grant.add_argument("--no-csreq", action="store_true", help="store NULL instead of a code requirement")
    grant.add_argument("--indirect", help="target bundle id (AppleEvents only)")
    grant.add_argument("--db", choices=("system", "user", "both"), default="both")
    grant.add_argument("--db-path", type=Path, help="write this database file instead (testing)")
    grant.add_argument("--no-restart", action="store_true", help="do not restart tccd")
    grant.add_argument("--dry-run", action="store_true")
    listing = commands.add_parser("list", help="show grants for the AXPilot-relevant services")
    listing.add_argument("--service", choices=sorted(SERVICES), action="append")
    listing.add_argument("--db", choices=("system", "user", "both"), default="both")
    listing.add_argument("--db-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return command_grant(args) if args.command == "grant" else command_list(args)
    except GrantError as exc:
        print(f"grant_tcc.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
