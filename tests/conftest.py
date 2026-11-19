import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.store.db import ModelStore


@pytest.fixture
def store(tmp_path):
    instance = ModelStore(tmp_path / "models.db")
    try:
        yield instance
    finally:
        instance.close()
