"""Shared fixtures: isolated temp db + config."""

from __future__ import annotations

import sys
from pathlib import Path

import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never write into the live desk logs: point agent loggers at a temp dir
# before any agent module creates its logger.
import util  # noqa: E402

util.LOG_DIR = Path(tempfile.mkdtemp(prefix="desk-test-logs-"))

from config import Config
from db import DB


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "test.db")


@pytest.fixture
def cfg(db):
    return Config(db=db, bootstrap={
        "analyst_mode": "rules",
        "fomo_handle": "MalevAngel84749",
        "notifier": "telegram",
        "hosting": "none",
        "owner_chat_id": 7659480276,
    })
