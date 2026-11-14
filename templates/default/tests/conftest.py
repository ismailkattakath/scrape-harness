import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ["SCRAPE_HARNESS_PIPELINE"] = str(ROOT / "pipeline.toml")


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """Throwaway roots for every test: nothing writes under $HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
