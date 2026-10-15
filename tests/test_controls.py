import contextlib
import io
import json
import time

import pytest

from trinity.cli import main
from trinity.store import Store

UNITS = json.dumps([{"id": "u1", "task": "First", "evidence": ["e1"]}, {"id": "u2", "task": "Second", "evidence": ["e2"]}])
REPORT = json.dumps({"summary": "done"})
REVIEW = {"decision": "CONTINUE", "assessment": "Evidence checked", "concerns": [], "recommendations": [],
          "requires_supervisor_action": False, "requires_user_approval": False, "suggested_next_review_seconds": 300}


def load(path):
    store = Store.open_readonly(path)
    try:
        return store.load(), store.events()
    finally:
        store.close()


def files(path):
    return {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}


@pytest.fixture
def running(team):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    return team


def test_controls_table_tracks_state_and_authority(team):
    path, invoke, setup, role = team
    assert invoke("propose", "Add CSV export", "--repo", str(path.parent / "project"), "--criterion", "Tests pass", "--json")[0] == 0
    code, text = invoke("controls", env="0", text=True)
    assert code == 0 and "✓ start — you only" in text and "✓ cancel" in text and "· pause" in text
    assert invoke("start", "--user-approved", "--proposal", "1", caller="w1:p9")[0] == 0
    assert invoke("provision")[0] == 0
    assert invoke("plan")[0] == 0
    controls = invoke("progress", "--json", env="0")[1]["result"]["controls"]
    assert controls["pause"]["available"] and controls["stop"]["available"] and not controls["start"]["available"]
    assert "not terminated" in controls["stop"]["effect"] and controls["resolve-unit"]["who"] == "you only"
    assert not controls["resume"]["available"]


@pytest.mark.parametrize("verb,args,callers", [
    ("pause", [], {"worker": "authority_denied", "consultant": "authority_denied"}),
    ("reconcile", [], {"worker": "authority_denied", "consultant": "authority_denied"}),
    ("resolve-unit", ["u1", "--requeue"], {"supervisor": "authority_denied", "worker": "authority_denied"}),
    ("dispatch", [], {"worker": "authority_denied", "user": "authority_denied"}),
    ("steer", ["direction"], {"user": "authority_denied", "consultant": "authority_denied"}),
])
def test_control_authority_by_caller(running, verb, args, callers):
    path, invoke, setup, role = running
    for caller, reason in callers.items():
        pane = {"user": "w1:p9"}.get(caller) or role(caller)
        before = load(path)
        assert invoke(verb, *args, caller=pane)[1]["reason"] == reason
        assert load(path) == before


@pytest.mark.parametrize("caller", ["supervisor", "worker", "consultant"])
def test_agents_cannot_self_approve_user_operations(running, caller):
    path, invoke, setup, role = running
    pane = role(caller)
    assert invoke("resume", "--user-approved", caller=pane)[1]["reason"] == "user_approval_requires_user_caller"


def test_pause_racing_a_dispatch_blocks_only_new_work(running, fake):
    path, invoke, setup, role = running
    original = fake.handle

    def pause_during_send(command):
        if command[1:3] == ["agent", "prompt"] and not fake.prompts:
            # The user pauses while the prompt is in flight.
            with contextlib.redirect_stdout(io.StringIO()):
                assert main(["--db", str(path), "pause"], runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake) == 0
        return original(command)
    fake.handle = pause_during_send
    code, result = invoke("dispatch")
    fake.handle = original
    assert code == 0 and result["result"]["status"] == "sent"
    state, _ = load(path)
    assert state["status"] == "paused" and state["units"][0]["status"] == "dispatched"
    assert invoke("dispatch")[1]["reason"] == "paused"
    assert len(fake.prompts) == 1
    assert "not interrupted" in invoke("progress", "--json", env="0")[1]["result"]["next_action"]


def test_checkpoint_waits_for_settlement_then_requires_valid_review(team, fake):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    assert invoke("dispatch")[0] == 0
    assert invoke("review", "milestone", caller="w1:p9")[0] == 0
    state, _ = load(path)
    assert state["checkpoints"][0]["status"] == "pending"
    assert invoke("record-review", json.dumps(REVIEW))[1]["reason"] == "unit_in_flight"
    assert invoke("record-completion", "u1", REPORT, "--settled", caller=role("worker"))[0] == 0
    assert load(path)[0]["status"] == "awaiting_review"
    for verb in (["dispatch"], ["resume"]):
        assert invoke(*verb)[1]["reason"] == "review_required"
    assert invoke("resume", caller="w1:p9")[1]["reason"] == "review_required"
    assert invoke("record-review", '{"decision": "CONTINUE"}')[1]["reason"] == "invalid_review"
    assert invoke("dispatch")[1]["reason"] == "review_required"
    assert invoke("record-review", json.dumps(REVIEW))[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"


def test_escalation_needs_the_user_pane(running, fake):
    path, invoke, setup, role = running
    assert invoke("review", "architecture")[0] == 0
    escalate = {**REVIEW, "decision": "ESCALATE", "requires_user_approval": True}
    assert invoke("record-review", json.dumps(escalate))[0] == 0
    assert invoke("resume")[1]["reason"] == "user_approval_required"
    assert invoke("resume", caller="w1:p9")[1]["reason"] == "user_approval_required"
    assert "own shell pane" in invoke("progress", "--json", env="0")[1]["result"]["next_action"]
    assert invoke("resume", "--user-approved", caller="w1:p9")[0] == 0
    assert invoke("dispatch")[0] == 0


def test_resume_rechecks_live_identity(running, fake):
    path, invoke, setup, role = running
    assert invoke("pause")[0] == 0
    fake.agents[role("consultant")]["agent_session"] = {"value": "replaced"}
    code, result = invoke("resume")
    assert code == 3 and result["reason"] == "stale_identity" and "setup" in result["detail"]["next_action"]
    state, _ = load(path)
    assert state["team"]["status"] == "blocked" and state["recovery"]["blockers"][0]["code"] == "stale_identity"
    assert invoke("dispatch")[1]["reason"] in ("paused", "team_not_ready")
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert load(path)[0]["recovery"]["blockers"] == []
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[0] == 0


def test_restart_with_uncertain_dispatch_needs_user_decision(running, fake):
    path, invoke, setup, role = running
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[1]["reason"] == "dispatch_uncertain"
    # A new coordinator process (every CLI call is one) reattaches.
    code, report = invoke("reconcile")
    assert code == 0 and report["result"]["safe_to_continue"] is False
    assert report["result"]["blockers"][0]["code"] in ("dispatch_uncertain", "unit_uncertain")
    assert invoke("resume")[1]["reason"] == "unsettled_unit"
    assert invoke("dispatch")[1]["reason"] in ("dispatch_uncertain", "blocked")
    assert len(fake.prompts) == 1
    assert invoke("resolve-unit", "u1", "--requeue")[1]["reason"] == "authority_denied"
    code, resolved = invoke("resolve-unit", "u1", "--requeue", caller="w1:p9")
    assert code == 0 and resolved["result"]["status"] == "pending"
    state, events = load(path)
    assert state["recovery"]["blockers"] == [] and state["status"] == "blocked"
    assert any(e["kind"] == "unit_resolved" and e["data"]["by"] == {"kind": "user", "pane_id": "w1:p9"} for e in events)
    assert invoke("resume")[0] == 0
    assert len(fake.prompts) == 1  # nothing resent until an explicit dispatch
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u1"
    assert len(fake.prompts) == 2


def test_abandon_keeps_evidence_and_moves_on(running, fake):
    path, invoke, setup, role = running
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[0] == 3
    assert invoke("resolve-unit", "u1", "--abandon", caller="w1:p9")[0] == 0
    assert invoke("resume")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"
    state, _ = load(path)
    assert state["units"][0]["status"] == "abandoned" and state["dispatches"][0]["status"] == "resolved_abandon"


def test_interrupted_sender_is_marked_uncertain_on_reconcile(running, fake):
    path, invoke, setup, role = running
    store = Store(path)
    with store.transaction():
        state = store.load()
        unit = state["units"][0]
        unit.update(status="dispatched", dispatch_id="d-crashed")
        state["execution"]["active_work_unit"] = "u1"
        state["dispatches"] = [{"id": "d-crashed", "unit_id": "u1", "status": "sending", "target": "x",
                                "message_sha256": "0", "prepared_at": time.time() - 600}]
        store.save(state)
    store.close()
    assert invoke("dispatch")[1]["reason"] == "dispatch_uncertain"
    report = invoke("reconcile")[1]["result"]
    assert any("interrupted mid-send" in c for c in report["changes"])
    state, events = load(path)
    assert state["units"][0]["status"] == "uncertain" and state["status"] == "blocked"
    count = len(events)
    again = invoke("reconcile")[1]["result"]
    assert again["changes"] == [] and len(load(path)[1]) == count + 1  # only the caller audit event
    assert fake.prompts == []


def test_reconcile_records_stale_roles_and_supervisor_loss(running, fake):
    path, invoke, setup, role = running
    del fake.agents[role("worker")]
    report = invoke("reconcile")[1]["result"]
    assert report["team"]["status"] == "blocked" and report["team"]["blocker"]["code"] == "role_missing"
    assert report["safe_to_continue"] is False
    fake.agents[role("supervisor")]["agent_session"]["value"] = "gone"
    # The stale supervisor can no longer act; the user can still reconcile and see why.
    assert invoke("reconcile")[1]["reason"] == "unbound_caller"
    report = invoke("reconcile", caller="w1:p9")[1]["result"]
    assert report["team"]["blocker"]["code"] == "supervisor_stale"
    assert "propose a new one" in report["next_action"]


def test_stop_is_terminal_and_does_not_claim_termination(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    assert invoke("stop", caller="w1:p9")[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["status"] == "stopped" and "not terminated" in view["next_action"]
    assert view["units"][0]["stage"] == "uncertain"
    for verb in (["dispatch"], ["resume"], ["provision"], ["plan"]):
        assert invoke(*verb)[1]["reason"] == "run_stopped"
    assert role("worker") in fake.agents  # nothing was killed


def test_readonly_commands_are_byte_stable(running, fake):
    path, invoke, setup, role = running
    assert invoke("dispatch")[0] == 0
    before = files(path)
    for verb in ("status", "history", "config", "proposal", "team", "progress", "controls"):
        assert invoke(verb, env="0", text=True)[0] == 0
    assert files(path) == before


def test_failure_reason_survives_a_restart(running, fake):
    path, invoke, setup, role = running
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[0] == 3
    # A fresh process sees the same diagnosis from durable state alone.
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["blockers"][0]["code"] == "dispatch_uncertain" and "Do not resend" in view["next_action"]
