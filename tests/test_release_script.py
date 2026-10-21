"""``scripts/release.py``: the checks ``release.yml`` runs before anything is built or published."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from axpilot.setup.launcher import parse_cdhash_file

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


release = _load()

ARM = "a" * 40
INTEL = "b" * 40
CHANGELOG = """\
# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-02

### Added

- Something new.

## [0.1.0] - 2026-10-30

### Fixed

- Something old.

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/releases/v0.2.0
"""


def table(*rows: str) -> str:
    header = (
        "| Launcher | Release asset | cdhash (arm64) | cdhash (x86_64) | SHA-256 of `AXPilot.app.zip` | "
        "SHA-256 of `Contents/MacOS/AXPilot` | Git commit | Toolchain | Published |"
    )
    return "\n".join(["# Published launcher builds", "", header, "|---" * 9 + "|", *rows, ""])


def row(zip_sha: str = "c" * 64, binary_sha: str = "d" * 64, asset: str = "`launcher-v1`") -> str:
    cells = [
        "1.0.0",
        asset,
        f"`{ARM}`",
        f"`{INTEL}`",
        f"`{zip_sha}`",
        f"`{binary_sha}`",
        "`abc1234`",
        "Xcode 16",
    ]
    return "| " + " | ".join([*cells, "2026-10-29"]) + " |"


def make_repo(tmp_path: Path, *, version: str = "0.2.0", pin: str | None = None) -> Path:
    (tmp_path / "src" / "axpilot").mkdir(parents=True)
    (tmp_path / "src" / "axpilot" / "__init__.py").write_text(
        f'"""AXPilot."""\n\n__version__ = "{version}"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "launcher").mkdir()
    (tmp_path / "launcher" / "CDHASH.md").write_text(table(pin if pin is not None else row()))
    (tmp_path / "packaging").mkdir()
    shutil.copy(ROOT / "packaging" / "server.json", tmp_path / "packaging" / "server.json")
    return tmp_path


# =========================================================================== versions and notes
@pytest.mark.parametrize(
    ("tag", "version"),
    [("v0.1.0", "0.1.0"), ("v1.20.3", "1.20.3"), ("v0.3.0rc1", "0.3.0rc1"), ("v1.0.0a2", "1.0.0a2")],
)
def test_versions_come_from_v_tags(tag: str, version: str) -> None:
    assert release.version_from_tag(tag) == version


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0.dev0", "v0.1.0-rc1", "launcher-v1", "v0.1.0 "])
def test_other_tags_are_refused(tag: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.version_from_tag(tag)


def test_prereleases() -> None:
    assert release.is_prerelease("0.3.0rc1")
    assert release.is_prerelease("1.0.0b2")
    assert not release.is_prerelease("1.0.0")


def test_the_package_version_is_read_without_importing() -> None:
    assert release.package_version((ROOT / "src" / "axpilot" / "__init__.py").read_text())
    with pytest.raises(release.ReleaseError):
        release.package_version("VERSION = '1'")


def test_release_notes_are_the_changelog_section() -> None:
    assert release.changelog_section(CHANGELOG, "0.2.0") == "### Added\n\n- Something new."
    assert release.changelog_section(CHANGELOG, "0.1.0") == "### Fixed\n\n- Something old."


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (CHANGELOG, "no [0.3.0] section"),
        ("## [0.3.0]\n\n- New.\n", "YYYY-MM-DD"),
        ("## [0.3.0] - 2026-11-05\n\n## [0.2.0] - 2026-11-02\n\n- Old.\n", "is empty"),
    ],
)
def test_missing_undated_or_empty_sections_are_refused(text: str, message: str) -> None:
    with pytest.raises(release.ReleaseError, match=message.replace("[", r"\[").replace("]", r"\]")):
        release.changelog_section(text, "0.3.0")


# =========================================================================== the launcher pin
def test_the_repository_pins_the_published_launcher_v1() -> None:
    pins = release.parse_pins((ROOT / "launcher" / "CDHASH.md").read_text())
    assert (pins[-1].launcher, pins[-1].asset) == ("1.0.0", "launcher-v1")
    assert release.pin_problems(pins[-1]) == []


def test_a_filled_pin_has_no_problems() -> None:
    (pin,) = release.parse_pins(table(row()))
    assert pin.cdhashes == {"arm64": ARM, "x86_64": INTEL}
    assert pin.zip_sha256 == "c" * 64
    assert pin.binary_sha256 == "d" * 64
    assert release.pin_problems(pin) == []


def test_the_newest_row_is_the_last_one() -> None:
    newer = row(asset="`launcher-v2`").replace("| 1.0.0 |", "| 2.0.0 |")
    assert release.parse_pins(table(row(), newer))[-1].asset == "launcher-v2"


@pytest.mark.parametrize(
    "bad", [row(zip_sha="c" * 63), row(binary_sha="_pending_"), row(asset="`v1`"), row().replace(ARM, "xyz")]
)
def test_malformed_pins_are_refused(bad: str) -> None:
    (pin,) = release.parse_pins(table(bad))
    assert release.pin_problems(pin)


def test_a_file_without_the_table_is_refused() -> None:
    with pytest.raises(release.ReleaseError):
        release.parse_pins("# Published launcher builds\n\nNothing yet.\n")


def test_the_cdhash_file_is_what_setup_and_doctor_read() -> None:
    (pin,) = release.parse_pins(table(row()))
    assert parse_cdhash_file(release.cdhash_file_text(pin)) == {"arm64": ARM, "x86_64": INTEL}


# =========================================================================== check
def test_a_ready_repository_passes(tmp_path: Path) -> None:
    outputs, problems = release.check("v0.2.0", make_repo(tmp_path))
    assert problems == []
    assert outputs == {"version": "0.2.0", "prerelease": "false", "launcher_asset": "launcher-v1"}


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, version="0.2.0.dev0", pin=row(zip_sha="_pending_"))
    _, problems = release.check("v0.3.0", repo)
    assert len(problems) == 3
    assert "0.2.0.dev0" in problems[0]
    assert "no [0.3.0] section" in problems[1]
    assert "AXPilot.app.zip" in problems[2]


def test_the_real_repository_is_releasable_at_its_version() -> None:
    version = release.package_version((ROOT / "src" / "axpilot" / "__init__.py").read_text())
    outputs, problems = release.check(f"v{version}")
    assert problems == []
    assert outputs["launcher_asset"] == "launcher-v1"


# =========================================================================== server.json
def test_server_json_gets_the_release_version() -> None:
    original = json.loads((ROOT / "packaging" / "server.json").read_text())
    updated = json.loads(release.set_server_json_version(json.dumps(original), "0.3.0"))
    assert updated["version"] == "0.3.0"
    assert [package["version"] for package in updated["packages"]] == ["0.3.0"]
    updated["version"] = original["version"]
    updated["packages"][0]["version"] = original["packages"][0]["version"]
    assert updated == original


def test_server_json_problems() -> None:
    assert release.server_json_problems((ROOT / "packaging" / "server.json").read_text()) == []
    assert release.server_json_problems("{") != []
    assert release.server_json_problems(json.dumps({"description": "x" * 101, "packages": []})) == [
        "packaging/server.json: description is over 100 characters.",
        "packaging/server.json lists no PyPI package.",
    ]


# =========================================================================== verify-launcher
def fake_launcher(tmp_path: Path) -> tuple[Path, Path, object]:
    app = tmp_path / "AXPilot.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "AXPilot").write_bytes(b"launcher binary")
    archive = tmp_path / "AXPilot.app.zip"
    archive.write_bytes(b"zip bytes")
    (pin,) = release.parse_pins(
        table(
            row(
                zip_sha=hashlib.sha256(b"zip bytes").hexdigest(),
                binary_sha=hashlib.sha256(b"launcher binary").hexdigest(),
            )
        )
    )
    return app, archive, pin


def codesign(cdhashes: dict[str, str]) -> object:
    def run(argv: Sequence[str]) -> str:
        arch = argv[argv.index("-a") + 1]
        return f"Executable=/x\nIdentifier=io.github.vansh3503.axpilot\nCDHash={cdhashes[arch].upper()}\n"

    return run


def test_the_pinned_launcher_verifies(tmp_path: Path) -> None:
    app, archive, pin = fake_launcher(tmp_path)
    run = codesign({"arm64": ARM, "x86_64": INTEL})
    assert release.verify_launcher(pin, zip_path=archive, app=app, cdhash=True, run=run) == []


def test_a_different_zip_or_binary_is_refused(tmp_path: Path) -> None:
    app, archive, pin = fake_launcher(tmp_path)
    archive.write_bytes(b"other zip")
    (app / "Contents" / "MacOS" / "AXPilot").write_bytes(b"other binary")
    problems = release.verify_launcher(pin, zip_path=archive, app=app)
    assert len(problems) == 2


def test_a_bundle_without_its_binary_is_refused(tmp_path: Path) -> None:
    app, _, pin = fake_launcher(tmp_path)
    (app / "Contents" / "MacOS" / "AXPilot").unlink()
    assert "no Contents/MacOS/AXPilot" in release.verify_launcher(pin, app=app)[0]


def test_a_different_or_unreadable_cdhash_is_refused(tmp_path: Path) -> None:
    app, _, pin = fake_launcher(tmp_path)
    problems = release.verify_launcher(
        pin, app=app, cdhash=True, run=codesign({"arm64": "e" * 40, "x86_64": INTEL})
    )
    assert problems == [f"{app}: arm64 cdhash is {'e' * 40}, pinned {ARM}."]
    unreadable = release.verify_launcher(
        pin, app=app, cdhash=True, run=lambda argv: "code object is not signed"
    )
    assert len(unreadable) == 2
    assert all("unreadable" in problem for problem in unreadable)


# =========================================================================== command line
def test_check_prints_github_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(release, "ROOT", make_repo(tmp_path, version="0.3.0rc1"))
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG.replace("0.2.0", "0.3.0rc1"))
    assert release.main(["check", "v0.3.0rc1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "version=0.3.0rc1",
        "prerelease=true",
        "launcher_asset=launcher-v1",
    ]


def test_check_reports_problems_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(release, "ROOT", make_repo(tmp_path))
    assert release.main(["check", "v9.9.9"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "9.9.9" in captured.err


def test_the_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setattr(release, "ROOT", repo)
    assert release.main(["notes", "0.2.0"]) == 0
    assert capsys.readouterr().out == "### Added\n\n- Something new.\n"
    assert release.main(["server-json", "0.2.0"]) == 0
    assert json.loads((repo / "packaging" / "server.json").read_text())["version"] == "0.2.0"
    assert release.main(["write-cdhash", str(repo / "out")]) == 0
    assert parse_cdhash_file((repo / "out" / "CDHASH").read_text()) == {"arm64": ARM, "x86_64": INTEL}
    assert release.main(["verify-launcher"]) == 1
    assert release.main(["verify-launcher", "--app", str(repo / "missing.app")]) == 1


def test_commands_needing_the_pin_fail_while_it_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(release, "ROOT", make_repo(tmp_path, pin=row(binary_sha="_pending_")))
    assert release.main(["write-cdhash", str(tmp_path / "out")]) == 1
    assert "Contents/MacOS/AXPilot" in capsys.readouterr().err
    assert not (tmp_path / "out" / "CDHASH").exists()
