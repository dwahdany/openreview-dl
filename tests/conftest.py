from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Keep every test away from the real ~/.config/openreview-dl (key, credentials, token)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
