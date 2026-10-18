import os

# FastAPI's test client calls the app as http://testserver; set before runapp.config is imported.
os.environ.setdefault("RUNAPP_ALLOWED_HOSTS", "testserver")

import pytest


@pytest.fixture(autouse=True)
def own_data_folder(tmp_path, monkeypatch):
    """Every test gets an empty data folder of its own, connected to Garmin (what most tests assume),
    so nothing reads or writes the real one."""
    from runapp import config, source

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "TOKEN_PATH", data / "garmin_tokens.json")
    monkeypatch.setattr(config, "RAW_DIR", data / "raw")
    source.choose("garmin")
