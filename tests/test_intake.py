import json

import pytest

from trinity.cli import main
from trinity.store import Store


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


@pytest.fixture
def intake(tmp_path, capsys, live):
    path = tmp_path / "run.sqlite3"

    def invoke(*args, env="1", text=False):
        code = main(["--db", str(path), *args], runner=live, environ={"HERDR_ENV": env})
        output = capsys.readouterr().out
        return code, output if text else json.loads(output)

    return path, invoke


def propose(invoke, repo, *extra, text=False):
    return invoke("propose", "Add CSV export to the report page", "--repo", str(repo),
                  "--criterion", "Export button downloads a valid CSV", "--criterion", "Tests pass",
                  "--constraint", "No new dependencies", *extra, *([] if text else ["--json"]), text=text)


def events(path):
    store = Store.open_readonly(path)
    try:
        return store.events(), store.load()
    finally:
        store.close()


def test_propose_captures_task_repo_and_renders_proposal(intake, repo):
    path, invoke = intake
    code, text = propose(invoke, repo / "src", text=True)
    assert code == 0
    for expected in ["revision 1", "nothing has been launched", "Add CSV export", str(repo), "Tests pass",
                     "No new dependencies", "Worker: luna (openai)", "Supervisor: opus", "Consultant: astra",
                     "Autonomy: bounded", "every 300", "need your approval", "adjust →", "cancel →",
                     "start --user-approved --proposal 1", "own shell pane"]:
        assert expected in text, expected
    log, state = events(path)
    assert state["status"] == "configured"
    assert state["project"] == {"objective": "Add CSV export to the report page", "repository": str(repo),
                                "success_criteria": ["Export button downloads a valid CSV", "Tests pass"],
                                "constraints": ["No new dependencies"]}
    assert state["proposal"]["revision"] == 1 and len(state["proposal"]["sha256"]) == 64
    assert state["identities"] == [{"id": "supervisor", "bootstrap": True, "pane_id": "w1:p1", "session_id": "supervisor-1"}]
    assert "proposal_recorded" in [e["kind"] for e in log]
    assert not any(e["kind"] in ("run_started", "run_approved") for e in log)


@pytest.mark.parametrize("args,missing", [
    (["propose", "--criterion", "Tests pass"], ["objective"]),
    (["propose", "TBD", "--criterion", "Tests pass"], ["objective"]),
    (["propose", "<objective>", "--criterion", "Tests pass"], ["objective"]),
    (["propose", "Add CSV export"], ["success_criteria"]),
    (["propose", "Add CSV export", "--criterion", "todo"], ["success_criteria"]),
    (["propose"], ["objective", "success_criteria"]),
])
def test_missing_context_asks_only_for_missing_fields_and_creates_nothing(intake, repo, args, missing):
    path, invoke = intake
    code, result = invoke(*args, "--repo", str(repo))
    assert code == 3 and result["reason"] == "missing_context"
    assert result["detail"]["missing"] == missing
    assert set(result["detail"]["questions"]) == set(missing)
    assert not path.exists()


def test_missing_repository_is_refused_without_a_database(intake, tmp_path):
    path, invoke = intake
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    code, result = invoke("propose", "Add CSV export", "--criterion", "Tests pass", "--repo", str(plain))
    assert code == 3 and result["detail"]["missing"] == ["repository"]
    assert not path.exists()


def test_adjust_bumps_revision_and_stale_approval_is_refused(intake, repo, live):
    path, invoke = intake
    assert propose(invoke, repo)[0] == 0
    code, view = invoke("adjust", "--worker", "sol", "--interval-s", "600", "--criterion", "CSV opens in Excel", "--json")
    assert code == 0 and view["result"]["revision"] == 2
    assert view["result"]["roles"]["worker"]["model"] == "sol"
    assert view["result"]["success_criteria"] == ["CSV opens in Excel"]
    assert view["result"]["checkpoints"]["interval_seconds"] == 600
    assert invoke("adjust", "--worker", "sol") == (3, {"ok": False, "reason": "no_change"})
    assert invoke("adjust", "--criterion", "todo")[1]["reason"] == "missing_context"
    live.caller = "w1:p9"
    assert invoke("start", "--user-approved", "--proposal", "1") == (
        3, {"ok": False, "reason": "proposal_changed", "detail": {"current_revision": 2}})
    assert invoke("start", "--user-approved")[1]["reason"] == "proposal_revision_required"
    code, started = invoke("start", "--user-approved", "--proposal", "2")
    assert code == 0 and started["result"]["status"] == "active"
    log, state = events(path)
    assert state["approval"]["revision"] == 2
    assert state["approval"]["sha256"] == state["proposal"]["sha256"]
    assert state["approval"]["by"] == {"kind": "user", "pane_id": "w1:p9"}
    assert [e["kind"] for e in log].count("run_approved") == 1
    live.caller = "w1:p1"
    assert invoke("adjust", "--worker", "luna") == (3, {"ok": False, "reason": "proposal_locked"})


@pytest.mark.parametrize("caller", ["w1:p1", "w1:p9"])
def test_cancel_closes_proposal_without_launch(intake, repo, live, caller):
    path, invoke = intake
    assert propose(invoke, repo)[0] == 0
    live.caller = caller
    code, view = invoke("cancel")
    assert code == 0 and view["result"]["status"] == "cancelled"
    live.caller = "w1:p9"
    assert invoke("start", "--user-approved", "--proposal", "1") == (3, {"ok": False, "reason": "run_cancelled"})
    assert invoke("cancel") == (3, {"ok": False, "reason": "run_cancelled"})
    log, state = events(path)
    assert state["status"] == "cancelled" and "approval" not in state
    assert "proposal_cancelled" in [e["kind"] for e in log]


@pytest.mark.parametrize("caller", ["w1:p2", "w1:p3"])
def test_worker_and_consultant_cannot_adjust_or_cancel(intake, repo, live, caller):
    _, invoke = intake
    assert propose(invoke, repo)[0] == 0
    for role, pane in (("worker", "w1:p2"), ("consultant", "w1:p3")):
        assert invoke("bind-identity", role, pane)[0] == 0
    live.caller = caller
    assert invoke("adjust", "--worker", "sol")[1]["reason"] == "authority_denied"
    assert invoke("cancel")[1]["reason"] == "authority_denied"


@pytest.mark.parametrize("caller,env,flags", [
    ("w1:p1", "1", ["--user-approved", "--proposal", "1"]),
    ("w1:p2", "1", ["--user-approved", "--proposal", "1"]),
    ("w1:p1", "0", ["--user-approved", "--proposal", "1"]),
    ("w1:p9", "1", ["--proposal", "1"]),
])
def test_agent_or_flagless_approval_cannot_start(intake, repo, live, caller, env, flags):
    path, invoke = intake
    assert propose(invoke, repo)[0] == 0
    live.caller = caller
    assert invoke("start", *flags, env=env) == (3, {"ok": False, "reason": "user_approval_required"})
    log, state = events(path)
    assert state["status"] == "configured" and "approval" not in state


def test_user_cannot_propose_and_agent_cannot_propose_twice(intake, repo, live):
    path, invoke = intake
    live.caller = "w1:p9"
    assert propose(invoke, repo) == (3, {"ok": False, "reason": "bootstrap_requires_agent"})
    assert not path.exists()
    live.caller = "w1:p1"
    assert propose(invoke, repo)[0] == 0
    assert propose(invoke, repo) == (3, {"ok": False, "reason": "run_already_initialized"})


def test_proposal_view_is_readonly(intake, repo):
    path, invoke = intake
    assert propose(invoke, repo)[0] == 0
    before = {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}
    code, text = invoke("proposal", env="0", text=True)
    assert code == 0 and "revision 1" in text
    assert invoke("proposal", "--json", env="0")[1]["result"]["start_command"].endswith("--proposal 1")
    assert before == {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*")}


def test_direct_init_and_start_remain_compatible(intake, live):
    path, invoke = intake
    assert invoke("init", "Build", "--json")[0] == 0
    live.caller = "w1:p9"
    code, result = invoke("start", "--user-approved")
    assert code == 0 and result["result"]["status"] == "active"
    assert result["result"]["approval"]["by"] == {"kind": "user", "pane_id": "w1:p9"}
