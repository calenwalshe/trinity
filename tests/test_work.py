import json

import pytest

from trinity.cli import main
from trinity.store import Store

UNITS = json.dumps([{"id": "u1", "task": "Add the export button", "evidence": ["button test passes"]},
                    {"id": "u2", "task": "Write CSV serializer", "evidence": ["serializer tests pass"], "boundaries": ["src/ only"]}])
REPORT = json.dumps({"summary": "done", "evidence": ["tests pass"]})


def state_of(path):
    store = Store.open_readonly(path)
    try:
        return store.load(), [e["kind"] for e in store.events()]
    finally:
        store.close()


def test_default_plan_is_one_bounded_unit_tied_to_the_approved_objective(team):
    path, invoke, setup, _ = team
    setup()
    code, result = invoke("plan")
    assert code == 0
    state, events = state_of(path)
    unit = state["units"][0]
    assert unit["assignment"]["task"] == "Add CSV export to reports"
    assert unit["assignment"]["evidence"] == ["Tests pass"]
    assert "No new dependencies" in unit["assignment"]["boundaries"]
    assert any("No push" in b for b in unit["assignment"]["boundaries"])
    assert unit["objective_sha256"] and unit["approval_revision"] == 1
    assert state["status"] == "running" and "work_planned" in events
    assert state["project"]["objective"] == "Add CSV export to reports"


@pytest.mark.parametrize("units,reason", [
    ("[]", "invalid_units"),
    ('[{"id": "u1", "task": "x"}]', "unscoped_unit"),
    ('[{"id": "u1", "evidence": ["e"]}]', "unscoped_unit"),
    ('[{"id": "u1", "task": "x", "evidence": ["e"], "objective": "something else"}]', "unit_scope_violation"),
    ('[{"id": "u1", "task": "x", "evidence": ["e"], "permissions": "all"}]', "unit_scope_violation"),
    ('[{"id": "u1", "task": "x", "evidence": ["e"]}, {"id": "u1", "task": "y", "evidence": ["e"]}]', "invalid_units"),
])
def test_unscoped_or_objective_changing_plans_are_refused(team, units, reason):
    path, invoke, setup, _ = team
    setup()
    assert invoke("plan", "--units", units)[1]["reason"] == reason
    state, _ = state_of(path)
    assert state["units"] == [] and state["status"] == "active"


def test_planning_requires_approval(team):
    path, invoke, setup, _ = team
    assert invoke("propose", "Add CSV export", "--repo", str(path.parent / "project"), "--criterion", "Tests pass", "--json")[0] == 0
    assert invoke("plan")[1]["reason"] == "run_not_started"
    assert invoke("dispatch")[1]["reason"] == "run_not_started"


def test_dispatch_requires_a_ready_team(team, fake):
    path, invoke, setup, _ = team
    setup(provision=False)
    assert invoke("plan")[0] == 0
    code, result = invoke("dispatch")
    assert code == 3 and result["reason"] == "team_not_ready"
    assert fake.prompts == []


def test_one_shot_dispatch_receipt_ack_completion_and_next_unit(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    code, result = invoke("dispatch")
    assert code == 0 and result["result"]["status"] == "sent" and result["result"]["unit_id"] == "u1"
    assert "Not yet acknowledged" in result["result"]["meaning"]
    worker = role("worker")
    assert len(fake.prompts) == 1 and fake.prompts[0][0] == worker
    text = fake.prompts[0][1]
    for expected in ["unit u1", "Add the export button", "button test passes", "ack u1", "record-completion u1",
                     "report-blocker u1", "--settled", "do not change it", "No push"]:
        assert expected in text, expected
    state, events = state_of(path)
    assert events.index("dispatch_prepared") < events.index("dispatch_sent")
    assert state["dispatches"][0]["status"] == "sent" and state["units"][0]["status"] == "dispatched"
    code, view = invoke("progress", "--json", env="0")
    assert view["result"]["active_unit"]["stage"] == "sent"
    assert "acknowledge" in view["result"]["next_action"]
    # Duplicate invocation cannot send a second prompt.
    assert invoke("dispatch")[1]["reason"] == "unit_in_flight"
    assert len(fake.prompts) == 1
    assert invoke("ack", "u1", caller=role("consultant"))[1]["reason"] == "authority_denied"
    assert invoke("ack", "u1", caller=worker)[0] == 0
    assert invoke("progress", "--json", env="0")[1]["result"]["active_unit"]["stage"] == "accepted"
    assert invoke("record-completion", "u1", REPORT, "--settled", caller=worker)[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert [u["stage"] for u in view["units"]] == ["completed", "queued"]
    assert view["latest_evidence"]["latest_report"]["report"]["summary"] == "done"
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"
    assert len(fake.prompts) == 2


@pytest.mark.parametrize("fault,reason", [(("error", "agent_blocked"), "dispatch_not_sent"), ("oserror", "dispatch_not_sent")])
def test_definite_send_failure_requeues_without_delivery(team, fake, fault, reason):
    path, invoke, setup, _ = team
    setup()
    assert invoke("plan")[0] == 0
    fake.faults["agent prompt"] = [fault]
    code, result = invoke("dispatch")
    assert code == 3 and result["reason"] == reason and "queued again" in result["detail"]["next_action"]
    state, events = state_of(path)
    assert state["units"][0]["status"] == "pending" and state["dispatches"][0]["status"] == "not_sent"
    assert invoke("dispatch")[0] == 0
    assert len(fake.prompts) == 1


def test_ambiguous_send_blocks_until_worker_evidence(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    fake.faults["agent prompt"] = ["timeout_after"]
    code, result = invoke("dispatch")
    assert code == 3 and result["reason"] == "dispatch_uncertain" and "Do not resend" in result["detail"]["next_action"]
    state, events = state_of(path)
    assert state["status"] == "blocked" and state["units"][0]["status"] == "uncertain"
    assert state["recovery"]["blockers"][0]["code"] == "dispatch_uncertain"
    assert invoke("dispatch")[1]["reason"] in ("dispatch_uncertain", "blocked")
    assert invoke("progress", "--json", env="0")[1]["result"]["active_unit"]["stage"] == "uncertain"
    assert len(fake.prompts) == 1
    # The worker's acknowledgement is delivery evidence and unblocks the run.
    assert invoke("ack", "u1", caller=role("worker"))[0] == 0
    state, _ = state_of(path)
    assert state["status"] == "running" and state["recovery"]["blockers"] == []
    assert state["dispatches"][0]["status"] == "acknowledged"


def test_ambiguous_send_settled_by_completion_report(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[0] == 3
    assert invoke("record-completion", "u1", REPORT, "--settled", caller=role("worker"))[0] == 0
    state, _ = state_of(path)
    assert state["status"] == "running" and state["recovery"]["blockers"] == []
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"


def test_stale_worker_blocks_dispatch_visibly(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan")[0] == 0
    fake.agents[role("worker")]["agent_session"] = {"value": "replaced"}
    code, result = invoke("dispatch")
    assert code == 3 and result["reason"] == "stale_identity" and "setup" in result["detail"]["next_action"]
    assert fake.prompts == []
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["team"]["status"] == "blocked" and "setup" in view["next_action"]
    state, _ = state_of(path)
    assert state["units"][0]["status"] == "pending"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert invoke("dispatch")[0] == 0


def test_missing_worker_blocks_dispatch(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan")[0] == 0
    del fake.agents[role("worker")]
    assert invoke("dispatch")[1]["reason"] == "role_missing"
    assert fake.prompts == []


def test_due_checkpoint_blocks_dispatch(team, fake):
    path, invoke, setup, _ = team
    setup("--interval-s", "0.000001")
    assert invoke("plan")[0] == 0
    code, result = invoke("dispatch")
    assert code == 3 and result["reason"] == "review_required"
    assert fake.prompts == []
    assert "consultant" in invoke("progress", "--json", env="0")[1]["result"]["next_action"]


def test_worker_blocker_is_recorded_and_supervisor_can_continue(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    assert invoke("dispatch")[0] == 0
    assert invoke("report-blocker", "u1", "Missing API key", caller=role("worker"))[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["status"] == "blocked" and view["units"][0]["blocker"] == "Missing API key"
    assert view["blockers"][0]["code"] == "worker_blocked" and "Supervisor" in view["next_action"]
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"


def test_progress_is_readonly_and_answers_what_is_happening(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan")[0] == 0
    assert invoke("dispatch")[0] == 0
    fake.agents[role("worker")]["agent_status"] = "working"
    before = {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}
    code, text = invoke("progress", text=True)
    assert code == 0
    for expected in ["running", "Add CSV export to reports", "Team: ready", "Active unit u1: sent — delivery receipt only",
                     "observed: working — a hint, not proof", "Next: Waiting for the worker to acknowledge unit u1"]:
        assert expected in text, expected
    assert "session" not in text
    assert before == {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}


def test_progress_before_approval_names_the_user_command(team):
    path, invoke, setup, _ = team
    assert invoke("propose", "Add CSV export", "--repo", str(path.parent / "project"), "--criterion", "Tests pass", "--json")[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["status"] == "configured" and view["team"]["status"] == "not_started"
    assert view["next_action"].endswith("start --user-approved --proposal 1")
    assert "own shell pane" in view["next_action"]


def test_legacy_admit_shows_admitted_not_sent(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan")[0] == 0
    assert invoke("admit-next-unit")[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["active_unit"]["stage"] == "admitted" and fake.prompts == []
