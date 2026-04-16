"""Shared test fixtures for orchestrator tests."""
import os
import tempfile

import pytest

# Override DB path BEFORE any orchestrator imports
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("ORCH_TEST_DB", _tmp_db.name)

from orchestrator.db import OrchestratorDB  # noqa: E402
from orchestrator.models import OrchestratorConfig  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = str(tmp_path / "test.db")
    _db = OrchestratorDB(db_path)
    yield _db
    _db.close()


@pytest.fixture()
def build(db):
    """Create a basic build and return (db, build_dict)."""
    b = db.create_build("Test feature")
    return db, b


@pytest.fixture()
def build_with_sprints(db):
    """Create a build with 3 sprints, return (db, build, [sprints])."""
    b = db.create_build("Test feature with sprints")
    s1 = db.create_sprint(b["id"], 1, "Sprint 1", "First sprint")
    s2 = db.create_sprint(b["id"], 2, "Sprint 2", "Second sprint",
                          depends_on='["' + s1["id"] + '"]')
    s3 = db.create_sprint(b["id"], 3, "Sprint 3", "Third sprint",
                          depends_on='["' + s2["id"] + '"]')
    db.update_build(b["id"], total_sprints=3)
    b = db.get_build(b["id"])
    return db, b, [s1, s2, s3]
