import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never touch the real ~/.config/lister (cost log, eBay tokens) from tests."""
    monkeypatch.setenv("LISTER_CONFIG_DIR", str(tmp_path / "lister-config"))
