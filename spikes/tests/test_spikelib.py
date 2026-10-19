from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import spikelib


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([5.0], 50, 5.0),
        ([1.0, 2.0, 3.0, 4.0], 0, 1.0),
        ([1.0, 2.0, 3.0, 4.0], 100, 4.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
        ([4.0, 1.0, 3.0, 2.0], 50, 2.5),  # unsorted input
        ([10.0, 20.0], 95, 19.5),
        (list(range(1, 101)), 95, 95.05),
    ],
)
def test_percentile_interpolates_between_ranks(values: list[float], q: float, expected: float) -> None:
    assert spikelib.percentile(values, q) == pytest.approx(expected)


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        spikelib.percentile([], 50)
    with pytest.raises(ValueError, match="within"):
        spikelib.percentile([1.0], 101)


def test_summarize() -> None:
    assert spikelib.summarize([]) == {"count": 0}
    summary = spikelib.summarize([1.0, 2.0, 3.0, 4.0, 100.0])
    assert summary == {"count": 5, "min": 1.0, "p50": 3.0, "p95": 80.8, "max": 100.0, "mean": 22.0}


def test_utc_stamp_is_filesystem_safe() -> None:
    assert re.fullmatch(r"\d{8}T\d{6}Z", spikelib.utc_stamp())


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("fresh-grant", "fresh-grant"),
        ("Claude Desktop/1.2", "Claude-Desktop-1.2"),
        ("../..", "run"),
        ("  ", "run"),
    ],
)
def test_safe_label(label: str, expected: str) -> None:
    assert spikelib.safe_label(label) == expected


def test_write_results_default_location_and_extra_copies(tmp_path: Path) -> None:
    extra = tmp_path / "copy" / "spike.json"
    written = spikelib.write_results(tmp_path, {"a": 1}, label="my label", extra_paths=[extra])
    assert len(written) == 2
    assert written[0].parent == tmp_path / "results"
    assert written[0].name.endswith("-my-label.json")
    assert json.loads(written[0].read_text()) == {"a": 1}
    assert json.loads(extra.read_text()) == {"a": 1}


def test_write_results_to_explicit_file(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "report.json"
    assert spikelib.write_results(tmp_path, {"b": Path("/x")}, out_file=target) == [target]
    assert json.loads(target.read_text()) == {"b": "/x"}  # non-JSON values are stringified
    assert [p.name for p in target.parent.iterdir()] == ["report.json"]  # no partial file left


def test_result_header_and_environment() -> None:
    header = spikelib.result_header("s9_test", "label")
    assert header["spike"] == "s9_test"
    assert header["label"] == "label"
    assert header["format"] == spikelib.RESULT_FORMAT_VERSION
    assert {"system", "machine", "python"} <= set(header["environment"])
    assert "hostname" not in json.dumps(header).lower()


def test_app_support_dir_honours_axpilot_home(isolated_home: Path) -> None:
    assert spikelib.app_support_dir() == isolated_home


def test_run_text_handles_missing_and_failing_commands() -> None:
    assert spikelib.run_text(["definitely-not-a-command-xyz"]) is None
    assert spikelib.run_text(["false"]) is None
    assert spikelib.run_text(["echo", " hi "]) == "hi"


def test_print_table(capsys: pytest.CaptureFixture[str]) -> None:
    spikelib.print_table([["a", 1], ["bbb", 22]], ["name", "n"])
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["name  n", "----  --", "a     1", "bbb   22"]


def test_require_macos_exits_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spikelib.sys, "platform", "linux")
    with pytest.raises(SystemExit, match="needs macOS"):
        spikelib.require_macos("S9")
