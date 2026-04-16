"""Tests for CLI command logic."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.db import OrchestratorDB


class TestSprintCreateFromPlan:
    """Test _cmd_sprint_create_from_plan logic via the CLI module."""

    def _run_create(self, db, plan_data, tmp_path):
        """Helper: write plan to file, call the CLI function."""
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan_data))

        # Import the CLI function directly
        from orchestrator.cli import _cmd_sprint_create_from_plan

        class Args:
            build_id = None
            plan = str(plan_path)

        args = Args()
        b = db.create_build("Test")
        args.build_id = b["id"]
        _cmd_sprint_create_from_plan(db, args)
        return b["id"]

    def test_bare_list_format(self, db, tmp_path):
        plan = [
            {"title": "Sprint 1", "description": "First"},
            {"title": "Sprint 2", "description": "Second"},
        ]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        assert len(sprints) == 2
        assert sprints[0]["title"] == "Sprint 1"
        assert sprints[1]["title"] == "Sprint 2"

    def test_dict_format_with_sprints_key(self, db, tmp_path):
        plan = {"sprints": [{"title": "A"}, {"title": "B"}]}
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        assert len(sprints) == 2

    def test_auto_numbering(self, db, tmp_path):
        plan = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        numbers = [s["sprint_number"] for s in sprints]
        assert numbers == [1, 2, 3]

    def test_explicit_numbering(self, db, tmp_path):
        plan = [
            {"number": 10, "title": "A"},
            {"number": 20, "title": "B"},
        ]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        numbers = [s["sprint_number"] for s in sprints]
        assert numbers == [10, 20]

    def test_depends_on_resolves_numbers_to_ids(self, db, tmp_path):
        """depends_on: [1] should resolve to sprint 1's actual ID."""
        plan = [
            {"title": "Sprint 1"},
            {"title": "Sprint 2", "depends_on": [1]},
        ]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        s1_id = sprints[0]["id"]
        s2_deps = json.loads(sprints[1]["depends_on"])
        assert s2_deps == [s1_id]

    def test_depends_on_chain(self, db, tmp_path):
        """Sprint 3 depends on Sprint 2, which depends on Sprint 1."""
        plan = [
            {"title": "Sprint 1"},
            {"title": "Sprint 2", "depends_on": [1]},
            {"title": "Sprint 3", "depends_on": [2]},
        ]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        s1_id, s2_id = sprints[0]["id"], sprints[1]["id"]
        assert json.loads(sprints[1]["depends_on"]) == [s1_id]
        assert json.loads(sprints[2]["depends_on"]) == [s2_id]

    def test_acceptance_criteria_creates_contract(self, db, tmp_path):
        plan = [{"title": "A", "acceptance_criteria": ["test passes"]}]
        build_id = self._run_create(db, plan, tmp_path)
        sprints = db.get_sprints(build_id)
        contracts = db.get_contracts(sprints[0]["id"])
        assert len(contracts) == 1

    def test_missing_title_exits(self, db, tmp_path):
        plan = [{"description": "no title"}]
        with pytest.raises(SystemExit):
            self._run_create(db, plan, tmp_path)

    def test_empty_plan_exits(self, db, tmp_path):
        with pytest.raises(SystemExit):
            self._run_create(db, [], tmp_path)

    def test_updates_total_sprints(self, db, tmp_path):
        plan = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        build_id = self._run_create(db, plan, tmp_path)
        b = db.get_build(build_id)
        assert b["total_sprints"] == 3
