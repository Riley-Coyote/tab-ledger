import pytest


@pytest.fixture(autouse=True)
def _sample_taxonomy(tmp_path, monkeypatch):
    """Tests use the sample taxonomy, never a taxonomy.json on this machine."""
    from tab_ledger import kb_taxonomy

    monkeypatch.setattr(kb_taxonomy, "TAXONOMY_FILE", tmp_path / "no-taxonomy.json")
