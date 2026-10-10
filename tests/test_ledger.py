from ifl.ledger import Ledger


def test_creates_file_and_starts_empty(tmp_path):
    path = tmp_path / "sub" / "ledger.sqlite"
    ledger = Ledger(path)
    try:
        assert path.exists()
        assert ledger.counts() == {"moved": 0, "reclaimed": 0}
    finally:
        ledger.close()
