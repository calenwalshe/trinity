"""Regressions for the 08-01 adversarial review findings."""

import contextlib
import io
import json
import os
import subprocess

import pytest

from trinity.cli import main
from trinity.store import Store

UNITS = json.dumps([{"id": "u1", "task": "First", "evidence": ["e1"]}, {"id": "u2", "task": "Second", "evidence": ["e2"]}])
REVIEW = {"decision": "CONTINUE", "assessment": "ok", "concerns": [], "recommendations": [],
          "requires_supervisor_action": False, "requires_user_approval": False, "suggested_next_review_seconds": 300}


def load(path):
    store = Store.open_readonly(path)
    try:
        return store.load()
    finally:
        store.close()


@pytest.fixture
def running(team):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    return team


@pytest.mark.parametrize("target", ["w1:p2", "w1:p1"])
def test_identities_cannot_be_hand_rebound_once_a_team_exists(running, fake, target):
    path, invoke, setup, role = running
    for which in ("worker", "consultant"):
        assert invoke("bind-identity", which, target)[1]["reason"] == "team_managed_identities"
    assert invoke("dispatch")[0] == 0
    assert fake.prompts[0][0] == role("worker") and fake.prompts[0][0] != target


def test_worker_blocker_after_ambiguous_send_is_recoverable(running, fake):
    path, invoke, setup, role = running
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[1]["reason"] == "dispatch_uncertain"
    assert invoke("report-blocker", "u1", "cannot", caller=role("worker"))[0] == 0
    state = load(path)
    assert state["dispatches"][0]["status"] == "acknowledged"
    assert [b["code"] for b in state["recovery"]["blockers"]] == ["worker_blocked"]
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"
    assert len(fake.prompts) == 2


def test_pending_checkpoint_is_promoted_when_the_unit_leaves_flight(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    assert invoke("ack", "u1", caller=role("worker"))[0] == 0
    assert invoke("review", "please check")[0] == 0
    assert load(path)["checkpoints"][0]["status"] == "pending"
    assert invoke("report-blocker", "u1", "cannot", caller=role("worker"))[0] == 0
    state = load(path)
    assert state["checkpoints"][0]["status"] == "awaiting_review" and state["status"] == "blocked"
    assert invoke("resume")[1]["reason"] == "review_required"
    assert invoke("record-review", json.dumps(REVIEW))[0] == 0
    # The worker's blocker still needs its own resolution: no silent "running".
    assert load(path)["status"] == "blocked"
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"


def test_resolve_unit_promotes_a_pending_checkpoint(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    assert invoke("review", "milestone", caller="w1:p9")[0] == 0
    assert invoke("record-completion", "u1", '{"background": "still running"}', "--unsettled", caller=role("worker"))[0] == 0
    assert load(path)["checkpoints"][0]["status"] == "pending"
    assert invoke("resolve-unit", "u1", "--abandon", caller="w1:p9")[0] == 0
    assert load(path)["checkpoints"][0]["status"] == "awaiting_review"
    assert invoke("record-review", json.dumps(REVIEW))[0] == 0
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"


def test_legacy_plan_units_is_refused_on_task_aware_runs(team, fake):
    path, invoke, setup, role = team
    setup()
    bad = '[{"id": "u1", "objective": "deploy to prod", "permissions": "all"}]'
    assert invoke("plan-units", bad)[1]["reason"] == "use_plan"
    assert load(path)["units"] == []


def test_refusals_leave_no_empty_database(tmp_path, capsys, fake):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for name, args in (("a", ["start", "--user-approved"]), ("b", ["dispatch"]),
                       ("c", ["propose", "Add CSV export", "--repo", str(repo), "--criterion", "ok", "--preset", "nope"]),
                       ("d", ["init", "Build", "--worker", "unknown"])):
        db = tmp_path / f"{name}.sqlite3"
        assert main(["--db", str(db), *args], runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake) == 3
        capsys.readouterr()
        assert not os.path.exists(db), name


def test_mismatched_completion_while_paused_keeps_the_pause(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    assert invoke("pause")[0] == 0
    assert invoke("record-completion", "u2", "{}", "--settled", caller=role("worker"))[1]["reason"] == "completion_mismatch"
    state = load(path)
    assert state["status"] == "paused" and state["resume_to"] == "blocked"
    assert state["recovery"]["blockers"][0]["code"] == "completion_mismatch"


def test_stop_during_definite_send_failure_is_not_uncertain(running, fake):
    path, invoke, setup, role = running
    original = fake.handle

    def stop_then_refuse(command):
        if command[1:3] != ["agent", "prompt"]:
            return original(command)
        fake.caller = "w1:p9"
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--db", str(path), "stop"], runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake) == 0
            fake.caller = role("supervisor")
        return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "agent_blocked", "message": ""}}), "")
    fake.handle = stop_then_refuse
    code, result = invoke("dispatch")
    fake.handle = original
    assert code == 3 and result["reason"] == "dispatch_not_sent"
    state = load(path)
    assert state["status"] == "stopped"
    assert state["units"][0]["status"] == "pending" and state["dispatches"][0]["status"] == "not_sent"


def test_workspace_is_not_rebuilt_under_an_in_flight_unit(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    tab_id = load(path)["team"]["workspace"]["tab_id"]
    del fake.tabs[tab_id]
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "unit_in_flight"
    assert len(fake.launches("workspace create")) == 0
