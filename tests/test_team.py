import copy
import json

import pytest

from trinity import team as team_module
from trinity.cli import main
from trinity.store import Store


@pytest.fixture
def run(tmp_path, capsys, fake):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    path = tmp_path / "run.sqlite3"

    def default_caller():
        if path.exists():
            store = Store.open_readonly(path)
            try:
                state = store.load()
                if state.get("team"):
                    supervisor = next((i for i in state["identities"] if i["id"] == "supervisor"), None)
                    if supervisor:
                        return supervisor["pane_id"]
            finally:
                store.close()
        return "w1:p1"

    def invoke(*args, env="1"):
        code = main(["--db", str(path), *args], runner=fake, environ={"HERDR_ENV": env}, launch_runner=fake)
        result = json.loads(capsys.readouterr().out)
        fake.caller = default_caller()
        return code, result

    assert invoke("propose", "Add CSV export to reports", "--repo", str(repo), "--criterion", "Tests pass", "--json")[0] == 0
    return path, invoke


def approve(invoke, fake):
    fake.caller = "w1:p9"
    assert invoke("start", "--user-approved", "--proposal", "1")[0] == 0
    fake.caller = "w1:p1"


def snapshot(path):
    store = Store.open_readonly(path)
    try:
        return store.load(), store.events()
    finally:
        store.close()


def untouched(fake):
    # Pre-existing panes (the stand-in for wA1) are never launch targets.
    for command in fake.launches():
        assert not any(p in command for p in ("w1:p1", "w1:p2", "w1:p3", "w1:p9")), command


def test_provisioning_is_refused_before_approval(run, fake):
    path, invoke = run
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "user_approval_required"
    assert fake.launches() == []
    assert "team" not in snapshot(path)[0]


def test_approved_run_gets_dedicated_observed_roles(run, fake):
    path, invoke = run
    approve(invoke, fake)
    code, result = invoke("provision")
    assert code == 0 and result["result"]["status"] == "ready"
    view = json.dumps(result)
    assert "session" not in view
    state, events = snapshot(path)
    roles = {i["id"]: i for i in state["identities"]}
    assert set(roles) == {"supervisor", "worker", "consultant"}
    for role in ("worker", "supervisor", "consultant"):
        assert roles[role]["owner"] == state["id"] and roles[role]["provisioned"] is True
        assert fake.agents[roles[role]["pane_id"]]["name"] == f"trinity-{state['id'][:8]}-{role}"
        assert roles[role]["session_id"] == fake.agents[roles[role]["pane_id"]]["agent_session"]["value"]
    starts = fake.launches("agent start")
    assert len(starts) == 3 and len(fake.launches("workspace create")) == 0 and len(fake.launches("tab create")) == 1
    worker_start, supervisor_start, consultant_start = starts
    assert worker_start[-2:] == ["-m", "gpt-6-luna"]
    assert supervisor_start[-2:] == ["--model", "opus"]
    assert consultant_start[-4:] == ["-m", "gpt-6-astra", "-s", "read-only"]
    kinds = [e["kind"] for e in events]
    for kind in ("provisioning_started", "tab_requested", "tab_created", "pane_created", "agent_start_requested", "team_ready"):
        assert kind in kinds
    assert state["team"]["workspace"]["label"] == f"trinity-{state['id'][:8]}"
    untouched(fake)


def test_three_roles_use_current_workspace_layout(run, fake):
    path, invoke = run
    approve(invoke, fake)
    code, result = invoke("provision")
    assert code == 0, result
    state, _ = snapshot(path)
    workspace = state["team"]["workspace"]
    assert workspace["workspace_id"] == "w1"
    assert workspace["caller_tab_id"] == "w1:t1"
    assert workspace["tab_id"] != "w1:t1"
    roles = {role: state["team"]["roles"][role]["pane_id"] for role in ("worker", "supervisor", "consultant")}
    assert len(set(roles.values())) == 3
    assert all(fake.panes[pane]["tab_id"] == workspace["tab_id"] for pane in roles.values())
    assert fake.panes[roles["worker"]]["root"] is True
    assert fake.panes[roles["supervisor"]]["split_from"] == roles["worker"]
    assert fake.panes[roles["supervisor"]]["direction"] == "right"
    assert fake.panes[roles["supervisor"]]["ratio"] == "0.65"
    assert fake.panes[roles["consultant"]]["split_from"] == roles["supervisor"]
    assert fake.panes[roles["consultant"]]["direction"] == "down"
    assert fake.panes[roles["consultant"]]["ratio"] == "0.5"
    assert len(fake.launches("workspace create")) == 0 and len(fake.launches("tab create")) == 1
    assert "--no-focus" in fake.launches("tab create")[0]
    assert all("--no-focus" in command for command in fake.launches("pane split"))


def test_launch_preserves_preexisting_panes(run, fake):
    path, invoke = run
    approve(invoke, fake)
    existing_panes = copy.deepcopy(fake.panes)
    existing_agents = copy.deepcopy(fake.agents)
    assert invoke("provision")[0] == 0
    assert {pane: fake.panes[pane] for pane in existing_panes} == existing_panes
    assert {pane: fake.agents[pane] for pane in existing_agents} == existing_agents
    assert len(fake.launches("workspace create")) == 0


def test_bootstrap_supervisor_is_replaced_by_run_owned_supervisor(run, fake):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    state, _ = snapshot(path)
    supervisor = next(identity for identity in state["identities"] if identity["id"] == "supervisor")
    assert supervisor["pane_id"] != "w1:p1"
    assert supervisor["owner"] == state["id"] and supervisor["provisioned"] is True
    assert len(fake.launches("agent start")) == 3


def test_ambiguous_split_adopts_existing_run_pane(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["pane split"] = ["timeout_after"]
    assert invoke("provision")[1]["reason"] == "uncertain_provisioning"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches("pane split")) == 2


def test_reprovisioning_a_ready_team_launches_nothing(run, fake):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    before = len(fake.launches())
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches()) == before


def test_definite_start_failure_blocks_then_retry_only_restarts_that_role(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["agent start"] = [None, ("error", "pane_not_at_prompt")]
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "role_start_failed"
    assert result["detail"]["step"] == "supervisor:agent" and "retry" in result["detail"]["next_action"]
    state, _ = snapshot(path)
    assert state["team"]["status"] == "blocked" and state["team"]["roles"]["worker"]["status"] == "ready"
    assert "consultant" not in [i["id"] for i in state["identities"]]
    code, team = invoke("team", env="0")
    assert team["result"]["status"] == "blocked" and team["result"]["blocker"]["code"] == "role_start_failed"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches("agent start")) == 4 and len(fake.launches("workspace create")) == 0


def test_ambiguous_start_is_reconciled_without_a_second_agent(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["agent start"] = ["timeout_after"]
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "uncertain_provisioning"
    assert snapshot(path)[0]["team"]["roles"]["worker"]["status"] == "uncertain"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches("agent start")) == 3  # worker once; supervisor and consultant once


def test_ambiguous_tab_creation_is_adopted_by_label(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["tab create"] = ["timeout_after"]
    assert invoke("provision")[1]["reason"] == "uncertain_provisioning"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches("tab create")) == 1
    assert len([tab for tab in fake.tabs.values() if tab["label"].startswith("trinity-")]) == 1
    assert "tab_adopted" in [e["kind"] for e in snapshot(path)[1]]


def test_workspace_timeout_before_effect_is_retried_once(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["tab create"] = ["timeout_before"]
    assert invoke("provision")[1]["reason"] == "uncertain_provisioning"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len([tab for tab in fake.tabs.values() if tab["label"].startswith("trinity-")]) == 1


def test_ambiguous_tab_creation_adopts_the_single_unassigned_pane(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["tab create"] = ["timeout_after"]
    assert invoke("provision")[1]["reason"] == "uncertain_provisioning"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    assert len(fake.launches("tab create")) == 1


def test_herdr_unavailable_is_definite_and_recoverable(run, fake):
    path, invoke = run
    approve(invoke, fake)
    fake.faults["tab create"] = ["oserror"]
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "herdr_unavailable"
    assert invoke("provision")[1]["result"]["status"] == "ready"


def test_foreign_agent_in_run_pane_is_never_replaced(run, fake):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    state, _ = snapshot(path)
    pane = next(i["pane_id"] for i in state["identities"] if i["id"] == "consultant")
    fake.agents[pane].update(name="someone-else", agent_session={"value": "other"})
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "foreign_agent_in_pane"
    assert fake.agents[pane]["name"] == "someone-else"
    assert len(fake.launches("agent start")) == 3


def test_restarted_own_agent_is_rebound_with_audit(run, fake):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    state, _ = snapshot(path)
    pane = next(i["pane_id"] for i in state["identities"] if i["id"] == "consultant")
    fake.agents[pane]["agent_session"] = {"value": "restarted"}
    assert invoke("provision")[1]["result"]["status"] == "ready"
    state, events = snapshot(path)
    assert next(i for i in state["identities"] if i["id"] == "consultant")["session_id"] == "restarted"
    rebound = [e for e in events if e["kind"] == "identity_bound" and e["data"].get("previous_session")]
    assert rebound and rebound[-1]["data"]["session_id"] == "restarted"


def test_closed_role_pane_is_replaced_inside_the_run_workspace(run, fake):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    state, _ = snapshot(path)
    pane = next(i["pane_id"] for i in state["identities"] if i["id"] == "consultant")
    del fake.panes[pane], fake.agents[pane]
    assert invoke("provision")[1]["result"]["status"] == "ready"
    state, events = snapshot(path)
    assert next(i for i in state["identities"] if i["id"] == "consultant")["pane_id"] != pane
    assert "role_reset" in [e["kind"] for e in events]
    untouched(fake)


@pytest.mark.parametrize("change", ["missing_profile", "execution_mode"])
def test_unsupported_launch_configuration_blocks_without_launch(tmp_path, capsys, fake, monkeypatch, change):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    path = tmp_path / "run.sqlite3"

    def invoke(*args):
        code = main(["--db", str(path), *args], runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake)
        return code, json.loads(capsys.readouterr().out)

    extra = []
    if change == "missing_profile":
        monkeypatch.delitem(team_module.PROFILES, "astra")
    else:
        file = tmp_path / "config.json"
        file.write_text(json.dumps({"trinity": {"version": 1, "checkpoints": {"interval_seconds": 300}, "agents": {
            "worker": {"model": "luna", "execution_mode": "batch"}, "supervisor": {"model": "opus"}, "consultant": {"model": "astra"}}}}))
        extra = ["--config", str(file)]
    assert invoke("propose", "Add CSV export", "--repo", str(repo), "--criterion", "Tests pass", "--json", *extra)[0] == 0
    approve(invoke, fake)
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "unsupported_launch_profile"
    assert fake.launches() == []
    assert snapshot(path)[0]["team"]["status"] == "blocked"


def test_hand_bound_session_is_not_commandeered(run, fake):
    path, invoke = run
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    approve(invoke, fake)
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "foreign_binding"
    assert fake.launches() == []


def test_stale_supervisor_blocks_readiness(run, fake):
    path, invoke = run
    approve(invoke, fake)

    target = None
    observed = 0
    def flaky(command, timeout=None):
        nonlocal target, observed
        if command[1:3] == ["agent", "start"] and command[3].endswith("-supervisor"):
            target = command[command.index("--pane") + 1]
        if target and command[1:4] == ["agent", "get", target]:
            observed += 1
            if observed == 3:
                fake.agents[target]["agent_session"]["value"] = "replaced"
        return fake(command, timeout)
    code = main(["--db", str(path), "provision"], runner=flaky, environ={"HERDR_ENV": "1"}, launch_runner=flaky)
    assert code == 3
    # A replaced supervisor can no longer write at all, so the team never reads ready.
    state, _ = snapshot(path)
    assert state["team"]["status"] != "ready"
    assert not any(e["kind"] == "team_ready" for e in snapshot(path)[1])


@pytest.mark.parametrize("caller", ["worker", "consultant", "user", "unrelated"])
def test_only_the_supervisor_provisions(run, fake, caller):
    path, invoke = run
    approve(invoke, fake)
    assert invoke("provision")[0] == 0
    roles = {i["id"]: i["pane_id"] for i in snapshot(path)[0]["identities"]}
    fake.caller = {"user": "w1:p9", "unrelated": "w1:p2"}.get(caller) or roles[caller]
    expected = "unbound_caller" if caller == "unrelated" else "authority_denied"
    assert invoke("provision")[1]["reason"] == expected


def test_concurrent_setup_is_refused_by_lease(run, fake):
    path, invoke = run
    approve(invoke, fake)
    store = Store(path)
    with store.transaction():
        state = store.load()
        state["team"] = {"status": "provisioning", "workspace": None, "blocker": None, "key": state["id"],
                         "lease": {"id": "other", "at": 9e18}, "roles": {}}
        store.save(state)
    store.close()
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "provisioning_in_progress"
    assert fake.launches() == []
