"""Tests for the orchestrator database layer."""
import json

import pytest


class TestBuilds:
    def test_create_build(self, db):
        b = db.create_build("Test feature")
        assert b["id"]
        assert b["prompt"] == "Test feature"
        assert b["status"] == "planning"
        assert b["current_sprint"] == 0
        assert b["total_sprints"] == 0
        assert b["created_at"]
        assert b["updated_at"]

    def test_get_build(self, db):
        b = db.create_build("Test feature")
        fetched = db.get_build(b["id"])
        assert fetched["id"] == b["id"]
        assert fetched["prompt"] == "Test feature"

    def test_get_nonexistent_build(self, db):
        assert db.get_build("nonexistent") is None

    def test_update_build(self, db):
        b = db.create_build("Test feature")
        updated = db.update_build(b["id"], status="building",
                                  project_path="/tmp/project")
        assert updated["status"] == "building"
        assert updated["project_path"] == "/tmp/project"

    def test_update_build_ignores_none(self, db):
        b = db.create_build("Test feature")
        db.update_build(b["id"], project_path="/tmp/project")
        # Updating with None shouldn't overwrite
        updated = db.update_build(b["id"], project_path=None)
        assert updated["project_path"] == "/tmp/project"

    def test_list_builds(self, db):
        db.create_build("Build 1")
        db.create_build("Build 2")
        all_builds = db.list_builds()
        assert len(all_builds) == 2

    def test_list_builds_by_status(self, db):
        db.create_build("Build 1")
        db.create_build("Build 2")
        planning = db.list_builds(status="planning")
        assert len(planning) == 2
        building = db.list_builds(status="building")
        assert len(building) == 0


class TestSprints:
    def test_create_sprint(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1", "First sprint")
        assert s["id"]
        assert s["build_id"] == b["id"]
        assert s["sprint_number"] == 1
        assert s["title"] == "Sprint 1"
        assert s["status"] == "pending"
        assert s["attempts"] == 0
        assert s["max_attempts"] == 3

    def test_create_sprint_with_deps(self, build):
        db, b = build
        s1 = db.create_sprint(b["id"], 1, "Sprint 1")
        s2 = db.create_sprint(b["id"], 2, "Sprint 2",
                              depends_on=json.dumps([s1["id"]]))
        deps = json.loads(s2["depends_on"])
        assert s1["id"] in deps

    def test_get_sprints_ordered(self, build):
        db, b = build
        db.create_sprint(b["id"], 3, "Sprint 3")
        db.create_sprint(b["id"], 1, "Sprint 1")
        db.create_sprint(b["id"], 2, "Sprint 2")
        sprints = db.get_sprints(b["id"])
        numbers = [s["sprint_number"] for s in sprints]
        assert numbers == [1, 2, 3]

    def test_update_sprint_allows_none(self, build):
        """update_sprint should allow setting fields to None (e.g. negotiation_phase)."""
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        db.update_sprint(s["id"], negotiation_phase="evaluator_reviewing")
        updated = db.update_sprint(s["id"], negotiation_phase=None)
        assert updated["negotiation_phase"] is None

    def test_increment_attempts(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        assert s["attempts"] == 0
        updated = db.increment_sprint_attempts(s["id"])
        assert updated["attempts"] == 1
        updated = db.increment_sprint_attempts(s["id"])
        assert updated["attempts"] == 2


class TestContracts:
    def test_create_contract(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        c = db.create_contract(s["id"], "generator",
                               {"tests": ["unit test"]})
        assert c["id"]
        assert c["sprint_id"] == s["id"]
        assert c["version"] == 1
        assert c["status"] == "proposed"
        assert json.loads(c["criteria"]) == {"tests": ["unit test"]}

    def test_contract_auto_version(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        c1 = db.create_contract(s["id"], "generator", {"v": 1})
        c2 = db.create_contract(s["id"], "generator", {"v": 2})
        assert c1["version"] == 1
        assert c2["version"] == 2

    def test_get_latest_contract(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        db.create_contract(s["id"], "generator", {"v": 1})
        db.create_contract(s["id"], "generator", {"v": 2})
        latest = db.get_latest_contract(s["id"])
        assert latest["version"] == 2

    def test_update_contract(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        c = db.create_contract(s["id"], "generator", {"v": 1})
        updated = db.update_contract(c["id"], status="approved")
        assert updated["status"] == "approved"


class TestMessages:
    def test_send_and_list(self, build):
        db, b = build
        msg = db.send_message(b["id"], "generator", "update",
                              "Sprint done", to_agent="evaluator")
        assert msg["id"]
        assert msg["body"] == "Sprint done"

        msgs = db.list_messages(b["id"], to_agent="evaluator")
        assert len(msgs) == 1
        assert msgs[0]["body"] == "Sprint done"

    def test_broadcast_included(self, build):
        """Messages with to_agent=None should appear in any agent's list."""
        db, b = build
        db.send_message(b["id"], "pm", "steering", "All agents: pause")
        msgs = db.list_messages(b["id"], to_agent="generator")
        assert len(msgs) == 1

    def test_filter_by_sprint(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        db.send_message(b["id"], "gen", "update", "msg1", sprint_id=s["id"])
        db.send_message(b["id"], "gen", "update", "msg2")  # no sprint
        msgs = db.list_messages(b["id"], sprint_id=s["id"])
        assert len(msgs) == 1
        assert msgs[0]["body"] == "msg1"


class TestAgentLogs:
    def test_create_and_list(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        log = db.create_agent_log(b["id"], "generator",
                                  sprint_id=s["id"],
                                  session_id="tmux:s1:w1",
                                  log_path="/tmp/test.log")
        assert log["agent"] == "generator"
        assert log["session_id"] == "tmux:s1:w1"

        logs = db.get_agent_logs(b["id"], sprint_id=s["id"])
        assert len(logs) == 1


class TestMergeQueue:
    def test_create_merge_entry(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        entry = db.create_merge_entry(b["id"], s["id"],
                                      "sprint/abc", "main")
        assert entry["status"] == "pending"
        assert entry["source_branch"] == "sprint/abc"
        assert entry["target_branch"] == "main"
        assert entry["attempts"] == 0

    def test_get_pending_merges(self, build):
        db, b = build
        s1 = db.create_sprint(b["id"], 1, "Sprint 1")
        s2 = db.create_sprint(b["id"], 2, "Sprint 2")
        db.create_merge_entry(b["id"], s1["id"], "sprint/1", "main")
        db.create_merge_entry(b["id"], s2["id"], "sprint/2", "main")
        pending = db.get_pending_merges(b["id"])
        assert len(pending) == 2

    def test_update_merge_entry(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        entry = db.create_merge_entry(b["id"], s["id"], "sprint/1", "main")
        updated = db.update_merge_entry(entry["id"], status="merging")
        assert updated["status"] == "merging"
