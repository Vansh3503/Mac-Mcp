"""Helpers for the spike-kit tests: load spike scripts as modules by path.

The spike scripts are standalone programs, not a package, so tests load them with importlib.
They import ``spikelib`` from ``spikes/`` themselves.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SPIKES = Path(__file__).resolve().parents[1]
REPO = SPIKES.parent

if str(SPIKES) not in sys.path:
    sys.path.insert(0, str(SPIKES))


def load_script(relative: str) -> ModuleType:
    """Import ``spikes/<relative>`` under a unique module name."""
    path = SPIKES / relative
    name = "spike_" + relative.replace("/", "_").removesuffix(".py")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses need the module registered while executing
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def load() -> Callable[[str], ModuleType]:
    return load_script


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never let a spike write into the real Application Support directory."""
    home = tmp_path / "axpilot-home"
    monkeypatch.setenv("AXPILOT_HOME", str(home))
    return home


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
