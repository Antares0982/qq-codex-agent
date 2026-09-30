import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_agent import storage


@pytest.fixture(autouse=True)
def fast_database(monkeypatch, tmp_path):
    def connect(database):
        assert Path(database).is_relative_to(tmp_path)
        db = sqlite3.connect(database)
        db.execute("PRAGMA synchronous=OFF")
        return db

    monkeypatch.setattr(storage, "sqlite3", SimpleNamespace(connect=connect))
