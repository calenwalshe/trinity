import hashlib
import json

import pytest

from trinity.cli import main
from trinity.coordinator import Coordinator
from trinity.store import Store


REVIEW = json.dumps({"decision": "CONTINUE", "assessment": "Evidence checked", "concerns": [],
                     "recommendations": [], "requires_supervisor_action": False,
                     "requires_user_approval": False, "suggested_next_review_seconds": 300}, indent=2)
MUTATIONS = [
    ["init-run", "Build"], ["bind-identity", "worker", "w1:p2"],
    ["plan-units", '[{"id":"u3"}]'], ["admit-next-unit"],
    ["record-review", REVIEW], ["record-steer", "Simplify"], ["pause"], ["resume"],
    ["request-checkpoint", "milestone"], ["record-completion", "u1", "{}", "--settled"],
]


@pytest.fixture
def session(tmp_path, capsys, live):
    path = tmp_path / "run.sqlite3"

    def invoke(*args, env="1"):
        code = main(["--db", str(path), *args], runner=live, environ={"HERDR_ENV": env})
        return code, json.loads(capsys.readouterr().out)

    assert invoke("init-run", "Build")[0] == 0
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    assert invoke("bind-identity", "consultant", "w1:p3")[0] == 0
    assert invoke("plan-units", '[{"id":"u1"},{"id":"u2"}]')[0] == 0
    return path, invoke


@pytest.mark.parametrize("args", MUTATIONS)
@pytest.mark.parametrize("caller", ["consultant", "unknown", "stale"])
def test_cli_denies_every_mutation_without_authority(session, live, args, caller):
    path, invoke = session
    store = Store(path)
    before, events = store.load(), store.events()
    live.caller = "w1:p3" if caller == "consultant" else "w1:p1"
    if caller == "stale":
        live.agents["w1:p1"]["agent_session"]["value"] = "replaced"
    code, result = invoke(*args, env="0" if caller == "unknown" else "1")
    assert code == 3 and result["reason"] in {"authority_denied", "unknown_caller", "unbound_caller"}
    assert store.load() == before and store.events() == events
    store.close()


def test_cli_bootstrap_atomic_binding_and_whoami(tmp_path, capsys, live):
    path = tmp_path / "run.sqlite3"
    base = ["--db", str(path)]
    assert main(base + ["init-run", "Build"], runner=live, environ={"HERDR_ENV": "1"}) == 0
    capsys.readouterr()
    store = Store(path)
    assert store.load()["identities"] == [{"id": "supervisor", "bootstrap": True, "pane_id": "w1:p1", "session_id": "supervisor-1"}]
    bootstrap = [e for e in store.events() if e["kind"] in ("run_initialized", "identity_bound")]
    assert all(e["data"]["bootstrap"] for e in bootstrap)
    assert main(base + ["whoami"], runner=live, environ={"HERDR_ENV": "1"}) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"kind": "agent", "pane_id": "w1:p1", "session_id": "supervisor-1",
                                                              "workspace_id": "w1", "tab_id": "w1:t1", "role": "supervisor"}
    store.close()


@pytest.mark.parametrize("kind", ["user", "unknown", "missing_session", "mismatched_session"])
def test_cli_refuses_bootstrap_without_verified_agent(tmp_path, capsys, live, kind):
    path = tmp_path / "run.sqlite3"
    live.caller = "w1:p9" if kind == "user" else "w1:p1"
    if kind == "missing_session":
        live.agents[live.caller]["agent_session"] = None
    runner = live
    if kind == "mismatched_session":
        def runner(command):
            result = live(command)
            if command[:3] == ["herdr", "agent", "get"]:
                data = json.loads(result.stdout)
                data["result"]["agent"]["agent_session"]["value"] = "replaced"
                result.stdout = json.dumps(data)
            return result
    code = main(["--db", str(path), "init-run", "Build"], runner=runner, environ={"HERDR_ENV": "0" if kind == "unknown" else "1"})
    assert code == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "bootstrap_requires_agent"
    store = Store(path)
    assert store.load() is None and store.events() == []
    store.close()


def test_cli_admission_observes_live_sessions_and_rejects_supplied_identity(session, live):
    _, invoke = session
    live.agents["w1:p2"]["agent_session"]["value"] = "new-worker"
    assert invoke("admit-next-unit") == (3, {"ok": False, "reason": "stale_identity"})
    assert invoke("admit-next-unit", '{}')[0] == 2
    assert invoke("bind-identity", "worker", "w1:p2", "invented-session")[0] == 2


def test_cli_bind_cannot_rebind_supervisor_or_bind_unknown_target(session, live):
    _, invoke = session
    assert invoke("bind-identity", "supervisor", "w1:p2") == (3, {"ok": False, "reason": "supervisor_rebind_forbidden"})
    assert invoke("bind-identity", "worker", "w1:p404") == (3, {"ok": False, "reason": "unknown_identity"})
    assert invoke("request-checkpoint", "architecture")[0] == 0
    assert invoke("bind-identity", "worker", "w1:p2") == (3, {"ok": False, "reason": "checkpoint_open"})


def test_cli_review_relay_hashes_exact_text_and_requires_live_consultant(session, live):
    path, invoke = session
    assert invoke("request-checkpoint", "milestone")[0] == 0
    live.agents["w1:p3"]["agent_session"]["value"] = "new-consultant"
    assert invoke("record-review", REVIEW) == (3, {"ok": False, "reason": "stale_identity"})
    live.agents["w1:p3"]["agent_session"]["value"] = "consultant-1"
    assert invoke("record-review", REVIEW)[0] == 0
    store = Store(path)
    assert store.load()["reviews"][0]["provenance"] == {"pane_id": "w1:p3", "session_id": "consultant-1", "channel": "relay", "sha256": hashlib.sha256(REVIEW.encode()).hexdigest()}
    store.close()


def test_cli_worker_checkpoint_and_human_approval_are_audited(session, live):
    path, invoke = session
    live.caller = "w1:p2"
    assert invoke("request-checkpoint", "blocked")[0] == 0
    live.caller = "w1:p1"
    decision = json.loads(REVIEW) | {"decision": "ESCALATE", "requires_user_approval": True}
    assert invoke("record-review", json.dumps(decision))[0] == 0
    for caller in ["w1:p1", "w1:p2", "w1:p3"]:
        live.caller = caller
        assert invoke("resume", "--user-approved") == (3, {"ok": False, "reason": "user_approval_requires_user_caller"})
    live.caller = "w1:p9"
    assert invoke("resume")[0] == 3
    assert invoke("resume", "--user-approved")[0] == 0
    assert invoke("pause")[0] == 0
    assert invoke("resume")[0] == 0
    store = Store(path)
    event = next(e for e in store.events() if e["kind"] == "checkpoint_requested")
    assert event["data"]["caller"] == {"kind": "agent", "pane_id": "w1:p2", "session_id": "worker-1",
                                        "workspace_id": "w1", "tab_id": "w1:t1", "role": "worker"}
    assert any(e["kind"] == "caller_authorized" and e["data"]["caller"] == {"kind": "user", "pane_id": "w1:p9",
                                                                               "workspace_id": "w1", "tab_id": "w1:t1",
                                                                               "role": "user"} for e in store.events())
    store.close()


def test_cli_unknown_can_read_status_but_whoami_is_refused(session, live):
    _, invoke = session
    assert invoke("status", env="0")[0] == 0
    assert invoke("whoami", env="0") == (3, {"ok": False, "reason": "unknown_caller"})


@pytest.mark.parametrize("args", [
    ["init-run", "Build"], ["bind-identity", "worker", "w1:p2"],
    ["plan-units", '[{"id":"u3"}]'], ["admit-next-unit"],
    ["record-review", REVIEW], ["record-steer", "Simplify"],
    ["record-completion", "u1", "{}", "--settled"],
])
def test_cli_human_cannot_perform_agent_operations(session, live, args):
    path, invoke = session
    live.caller = "w1:p9"
    store = Store(path)
    before, ledger = store.load(), store.events()
    assert invoke(*args) == (3, {"ok": False, "reason": "authority_denied"})
    assert store.load() == before and store.events() == ledger
    store.close()


def test_cli_human_checkpoint_records_caller_pane(session, live):
    path, invoke = session
    live.caller = "w1:p9"
    assert invoke("request-checkpoint", "user intervention")[0] == 0
    store = Store(path)
    event = next(e for e in store.events() if e["kind"] == "checkpoint_requested")
    assert event["data"]["caller"] == {"kind": "user", "pane_id": "w1:p9", "workspace_id": "w1",
                                        "tab_id": "w1:t1", "role": "user"}
    store.close()


def test_cli_rechecks_caller_inside_transaction_before_mutation(session, live, capsys):
    path, _ = session
    store = Store(path)
    before, ledger = store.load(), store.events()
    count = 0

    def runner(command):
        nonlocal count
        if command == ["herdr", "pane", "current", "--current"]:
            count += 1
            if count == 2:
                live.agents["w1:p1"]["agent_session"]["value"] = "replacement"
        return live(command)

    assert main(["--db", str(path), "pause"], runner=runner, environ={"HERDR_ENV": "1"}) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == "unbound_caller"
    assert store.load() == before and store.events() == ledger
    store.close()
