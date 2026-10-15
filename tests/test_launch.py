import json

from trinity.cli import main
from trinity.store import Store


def invoke(fake, capsys, db, *args):
    code = main(["--db", str(db), *args], runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake)
    return code, json.loads(capsys.readouterr().out)


def read(path):
    store = Store.open_readonly(path)
    try:
        return store.load(), store.events()
    finally:
        store.close()


def test_launch_records_invocation_approval_and_ready_team(tmp_path, capsys, fake):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    db = tmp_path / "run.sqlite3"
    code, result = invoke(fake, capsys, db, "launch", "Add practice quizzes to the lesson page", "--repo", str(repo),
                          "--criterion", "A quiz can be completed and scored", "--constraint", "No external services", "--json")
    assert code == 0
    assert result["result"]["team"]["status"] == "ready"
    state, events = read(db)
    assert state["approval"]["source"] == "invocation"
    assert {identity["id"] for identity in state["identities"] if identity.get("owner") == state["id"]} == {
        "worker", "supervisor", "consultant"}
    assert state["launch_delivery"]["status"] == "submitted"
    event = next(event for event in events if event["kind"] == "launch_prompt_submitted")
    assert "does not establish acceptance" in event["data"]["meaning"]
    supervisor = next(identity for identity in state["identities"] if identity["id"] == "supervisor")
    assert fake.prompts == [(supervisor["pane_id"], fake.prompts[0][1])]
    assert "Add practice quizzes" in fake.prompts[0][1]


def test_manual_approval_policy_keeps_shell_gate(tmp_path, capsys, fake):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"trinity": {"version": 1, "agents": {
        "worker": {"model": "luna"}, "supervisor": {"model": "opus"}, "consultant": {"model": "astra"}},
        "checkpoints": {"interval_seconds": 300}, "launch": {"approval_mode": "manual"}}}))
    db = tmp_path / "run.sqlite3"
    code, result = invoke(fake, capsys, db, "launch", "Add practice quizzes to the lesson page", "--repo", str(repo),
                          "--criterion", "A quiz can be completed and scored", "--config", str(config), "--json")
    assert code == 0 and result["result"]["status"] == "configured"
    state, _ = read(db)
    assert "approval" not in state and fake.launches() == []
    fake.caller = "w1:p9"
    assert invoke(fake, capsys, db, "start", "--user-approved", "--proposal", "1")[0] == 0


def test_missing_context_creates_no_run_or_herdr_resources(tmp_path, capsys, fake):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    db = tmp_path / "run.sqlite3"
    code, result = invoke(fake, capsys, db, "launch", "Add practice quizzes to the lesson page", "--repo", str(repo))
    assert code == 3 and result["reason"] == "missing_context"
    assert result["detail"]["missing"] == ["success_criteria"]
    assert not db.exists() and fake.launches() == []
