"""Shared helpers for the AXPilot Phase-0 spikes.

Stdlib only and Python 3.9 compatible, because spike S1's probe must run with any interpreter,
including the Command Line Tools' ``/usr/bin/python3``. Spike scripts import it with::

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import spikelib
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import platform
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SPIKES_DIR = Path(__file__).resolve().parent
RESULT_FORMAT_VERSION = 1


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)  # noqa: UP017 - datetime.UTC needs Python 3.11


def utc_stamp(moment: _dt.datetime | None = None) -> str:
    """A filesystem-safe UTC timestamp such as ``20261019T104200Z``."""
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def app_support_dir() -> Path:
    """AXPilot's application-support directory (``$AXPILOT_HOME`` overrides, as in axpilot.paths)."""
    override = os.environ.get("AXPILOT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "AXPilot"


def run_text(args: Sequence[str], timeout: float = 10.0) -> str | None:
    """stdout of a command, stripped, or ``None`` if it is missing, fails or times out."""
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def environment_info() -> dict[str, Any]:
    """The machine and interpreter a result was produced on (no hostname or user name)."""
    info: dict[str, Any] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "python_implementation": platform.python_implementation(),
    }
    if sys.platform == "darwin":
        info["macos"] = platform.mac_ver()[0] or None
        info["macos_build"] = run_text(["sw_vers", "-buildVersion"])
        info["hardware_model"] = run_text(["sysctl", "-n", "hw.model"])
        info["cpu"] = run_text(["sysctl", "-n", "machdep.cpu.brand_string"])
        info["rosetta"] = run_text(["sysctl", "-in", "sysctl.proc_translated"]) == "1"
    else:
        info["release"] = platform.release()
    return info


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile (0-100) with linear interpolation between closest ranks."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= q <= 100:
        raise ValueError(f"q must be within [0, 100], got {q}")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(values: Sequence[float], digits: int = 3) -> dict[str, Any]:
    """count / min / p50 / p95 / max / mean of ``values``, rounded; empty input gives count 0."""
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), digits),
        "p50": round(percentile(values, 50), digits),
        "p95": round(percentile(values, 95), digits),
        "max": round(max(values), digits),
        "mean": round(sum(values) / len(values), digits),
    }


def write_json(path: Path, data: Any) -> Path:
    """Write ``data`` as pretty JSON, creating parent directories; returns ``path``.

    The file appears atomically (written next to it, then renamed), because run_s1.sh polls for it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(data, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    os.replace(partial, path)
    return path


def write_results(
    spike_dir: Path,
    data: dict[str, Any],
    *,
    label: str | None = None,
    out_file: Path | None = None,
    extra_paths: Iterable[Path] = (),
) -> list[Path]:
    """Write a result document to ``out_file`` (default ``<spike_dir>/results/<stamp>[-label].json``)
    and to every path in ``extra_paths``. Returns the paths written."""
    if out_file is None:
        suffix = f"-{safe_label(label)}" if label else ""
        out_file = spike_dir / "results" / f"{utc_stamp()}{suffix}.json"
    return [write_json(path, data) for path in (out_file, *extra_paths)]


def safe_label(label: str) -> str:
    """``label`` reduced to characters that are safe in a file name."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in label.strip())
    return cleaned.strip("-.") or "run"


def result_header(spike: str, label: str | None = None) -> dict[str, Any]:
    """The fields every spike result starts with."""
    return {
        "spike": spike,
        "format": RESULT_FORMAT_VERSION,
        "label": label,
        "timestamp": utc_now().isoformat(timespec="seconds"),
        "environment": environment_info(),
    }


def require_macos(what: str) -> None:
    """Exit with a clear message when not on macOS."""
    if sys.platform != "darwin":
        sys.exit(f"{what} needs macOS (this is {platform.system()}).")


def print_table(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> None:
    """Print rows as a plain left-aligned text table."""
    cells = [[str(c) for c in header], *[[str(c) for c in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(header))]
    for index, row in enumerate(cells):
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())  # noqa: B905
        if index == 0:
            print("  ".join("-" * width for width in widths))
