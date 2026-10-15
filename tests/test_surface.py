import json

import pytest

from trinity.cli import main
from trinity.coordinator import Coordinator, Refused
from trinity.store import Store


@pytest.fixture
def surface(tmp_path, capsys, live):
    path = tmp_path / "surface.sqlite3"

    def invoke(*args, env="1", text=False):
        code = main(["--db", str(path), *args], runner=live, environ={"HERDR_ENV": env})
        output = capsys.readouterr().out
        return code, output if text else json.loads(output)

    return path, invoke


def test_init_summary_configured_gate_and_user_start(surface, live):
    path, invoke = surface
    code, summary = invoke("init", "Build an app", "--preset", "economy", text=True)
    assert code == 0
    assert all(s in summary for s in ["Build an app", "luna", "sol", "astra", "900", "configured"])
    assert invoke("status", env="0")[1]["result"]["status"] == "configured"
    assert invoke("admit-next-unit") == (3, {"ok": False, "reason": "run_not_started"})
    assert invoke("plan-units", '[{"id":"u1"}]')[0] == 3
    live.caller = "w1:p9"
    assert invoke("start", "--user-approved")[0] == 0
    assert invoke("status")[1]["result"]["status"] == "active"
    live.caller = "w1:p1"
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    assert invoke("plan-units", '[{"id":"u1"}]')[0] == 0
    assert invoke("admit-next-unit")[1]["result"]["id"] == "u1"
    store = Store.open_readonly(path)
    kinds = [e["kind"] for e in store.events()]
    assert "run_configured" in kinds and "run_started" in kinds
    store.close()


@pytest.mark.parametrize("caller,env,approved", [
    ("w1:p1", "1", True), ("w1:p2", "1", True), ("w1:p3", "1", True),
    ("w1:p1", "0", True), ("w1:p9", "1", False),
])
def test_start_requires_real_user_caller_and_explicit_approval(surface, live, caller, env, approved):
    path, invoke = surface
    assert invoke("init", "Build", "--json")[0] == 0
    live.caller = caller
    args = ["start", "--user-approved"] if approved else ["start"]
    assert invoke(*args, env=env) == (3, {"ok": False, "reason": "user_approval_required"})
    store = Store.open_readonly(path)
    assert store.load()["status"] == "configured" and store.load()["units"] == []
    assert not any(e["kind"] == "run_started" for e in store.events())
    store.close()


def test_init_json_overrides_and_warning_are_persisted(surface):
    path, invoke = surface
    code, result = invoke("init", "Build", "--worker", "sol", "--supervisor", "astra", "--consultant", "fable", "--interval-s", "1", "--json")
    assert code == 0
    config = result["result"]["configuration"]
    assert config["interval_warning"] == "outside_spec_bounds"
    assert config["agents"]["consultant"] == {"model": "fable", "provider": "anthropic"}
    assert invoke("config", env="0")[1]["result"]["agents"] == config["agents"]


def test_cli_init_file_and_invalid_alias(surface, tmp_path):
    path, invoke = surface
    assert invoke("init", "Build", "--worker", "unknown", "--json") == (3, {"ok": False, "reason": "unknown_model_alias"})
    assert invoke("init", "Build", "--preset", "brainstorm", "--json") == (3, {"ok": False, "reason": "preset_requires_phase_2"})
    file = tmp_path / "preset.json"
    file.write_text(json.dumps({"trinity": {"version": 1, "agents": {r: {"model": m} for r, m in zip(
        ("worker", "supervisor", "consultant"), ("luna", "opus", "astra"))}, "checkpoints": {"interval_seconds": 120}}}))
    assert invoke("init", "Build", "--config", str(file), "--json")[0] == 0
    assert invoke("config")[1]["result"]["checkpoints"]["interval_seconds"] == 120


def test_stop_is_terminal_and_cannot_be_bypassed_by_resume(surface, live):
    path, invoke = surface
    assert invoke("init", "Build", "--json")[0] == 0
    live.caller = "w1:p9"
    assert invoke("start", "--user-approved")[0] == 0
    live.caller = "w1:p1"
    assert invoke("stop")[1]["result"]["status"] == "stopped"
    assert invoke("admit-next-unit") == (3, {"ok": False, "reason": "run_stopped"})
    assert invoke("resume") == (3, {"ok": False, "reason": "run_stopped"})
    live.caller = "w1:p9"
    assert invoke("start", "--user-approved") == (3, {"ok": False, "reason": "run_stopped"})
    assert invoke("stop")[0] == 0
    store = Store.open_readonly(path)
    assert len([e for e in store.events() if e["kind"] == "run_stopped"]) == 1
    store.close()


@pytest.mark.parametrize("caller", ["w1:p2", "w1:p3"])
def test_worker_and_consultant_cannot_stop(surface, live, caller):
    path, invoke = surface
    assert invoke("init", "Build", "--json")[0] == 0
    for role, pane in [("worker", "w1:p2"), ("consultant", "w1:p3")]:
        assert invoke("bind-identity", role, pane)[0] == 0
    live.caller = caller
    assert invoke("stop") == (3, {"ok": False, "reason": "authority_denied"})


def test_review_steer_aliases_and_readonly_history(surface, live):
    path, invoke = surface
    assert invoke("init-run", "Build")[0] == 0
    assert invoke("plan-units", '[{"id":"u1"}]')[0] == 0
    assert invoke("bind-identity", "consultant", "w1:p3")[0] == 0
    live.caller = "w1:p9"
    assert invoke("review", "user intervention")[0] == 0
    live.caller = "w1:p1"
    decision = {"decision": "STEER", "assessment": "Simplify", "concerns": [], "recommendations": [],
                "requires_supervisor_action": True, "requires_user_approval": False, "suggested_next_review_seconds": 300}
    assert invoke("record-review", json.dumps(decision))[0] == 0
    assert invoke("steer", "Use simpler implementation")[0] == 0
    before = {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}
    history = invoke("history", env="0")[1]["result"]
    assert history["reviews"][0]["decision"]["decision"] == "STEER"
    assert history["steers"][0]["text"] == "Use simpler implementation"
    assert history["decisions"]["accepted"][0]["assessment"] == "Simplify"
    assert invoke("config", env="0")[0] == 0
    assert before == {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}
    store = Store.open_readonly(path)
    commands = [e["data"]["command"] for e in store.events() if e["kind"] == "caller_authorized"]
    assert "review" in commands and "steer" in commands
    store.close()


def test_configured_run_cannot_be_activated_by_completion_or_resume(surface, live):
    path, invoke = surface
    assert invoke("init", "Build", "--json")[0] == 0
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    live.caller = "w1:p2"
    assert invoke("record-completion", "invented", "{}", "--settled") == (3, {"ok": False, "reason": "run_not_started"})
    assert invoke("review", "blocker") == (3, {"ok": False, "reason": "run_not_started"})
    live.caller = "w1:p1"
    assert invoke("resume") == (3, {"ok": False, "reason": "run_not_started"})
    assert invoke("status")[1]["result"]["status"] == "configured"


def test_stop_marks_inflight_uncertain_and_completion_cannot_restart(surface, live):
    path, invoke = surface
    assert invoke("init-run", "Build")[0] == 0
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    assert invoke("plan-units", '[{"id":"u1"}]')[0] == 0
    assert invoke("admit-next-unit")[0] == 0
    live.caller = "w1:p9"
    assert invoke("stop")[0] == 0
    live.caller = "w1:p2"
    assert invoke("record-completion", "u1", "{}", "--settled") == (3, {"ok": False, "reason": "run_stopped"})
    state = invoke("status")[1]["result"]
    assert state["status"] == "stopped" and state["units"][0]["status"] == "uncertain"
