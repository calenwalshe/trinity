"""E2E-01/E2E-03 acceptance: the whole /trinity journey on a fake Herdr and temporary databases.

Each boundary asserts a durable state or event, never only a return code.
"""

import json
from datetime import datetime, timezone

from trinity.store import Store

UNITS = json.dumps([{"id": "u1", "task": "Add the export button", "evidence": ["button test passes"]},
                    {"id": "u2", "task": "Write the CSV serializer", "evidence": ["serializer tests pass"]}])
REVIEW = {"decision": "CONTINUE", "assessment": "u1 evidence checked", "concerns": [], "recommendations": [],
          "requires_supervisor_action": False, "requires_user_approval": False, "suggested_next_review_seconds": 300}


def durable(path):
    store = Store.open_readonly(path)
    try:
        return store.load(), [e["kind"] for e in store.events()]
    finally:
        store.close()


def write_transcript(root, session, text):
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    rows = [{"type": "session_meta", "payload": {"id": session}, "timestamp": stamp},
            {"type": "response_item", "timestamp": stamp,
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}]
    (root / f"rollout-2026-10-13-{session}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_full_journey_request_to_result(team, fake, tmp_path, capsys):
    path, invoke, setup, role = team
    repo = str(path.parent / "project")

    # 1. Request intake: a missing criterion is asked for, and no run exists yet.
    code, asked = invoke("propose", "Add CSV export to reports", "--repo", repo)
    assert code == 3 and asked["detail"]["missing"] == ["success_criteria"] and not path.exists()
    code, text = invoke("propose", "Add CSV export to reports", "--repo", repo, "--criterion", "Tests pass", text=True)
    assert code == 0 and "nothing has been launched" in text and "--proposal 1" in text
    assert invoke("adjust", "--consultant", "astra", "--interval-s", "600", "--json")[1]["result"]["revision"] == 2
    state, events = durable(path)
    assert state["status"] == "configured" and "proposal_adjusted" in events and fake.launches() == []

    # 2. One approval, from the user's pane, for the reviewed revision.
    assert invoke("start", "--user-approved", "--proposal", "2", caller="w1:p1")[1]["reason"] == "user_approval_required"
    assert invoke("start", "--user-approved", "--proposal", "2", caller="w1:p9")[0] == 0
    state, events = durable(path)
    assert state["approval"]["revision"] == 2 and "run_approved" in events and fake.launches() == []

    # 3. Dedicated team ready.
    assert invoke("progress", "--json", env="0")[1]["result"]["team"]["status"] == "not_started"
    assert invoke("provision")[1]["result"]["status"] == "ready"
    state, events = durable(path)
    assert state["team"]["status"] == "ready" and "team_ready" in events
    assert {i["id"] for i in state["identities"] if i.get("owner") == state["id"]} == {"worker", "supervisor", "consultant"}

    # 4. First bounded unit dispatched once.
    assert invoke("plan", "--units", UNITS)[0] == 0
    assert invoke("dispatch")[1]["result"]["status"] == "sent"
    assert invoke("dispatch")[1]["reason"] == "unit_in_flight"
    assert len(fake.prompts) == 1 and fake.prompts[0][0] == role("worker")
    state, events = durable(path)
    assert state["dispatches"][0]["status"] == "sent" and "dispatch_sent" in events

    # 5. Progress is visible and truthful.
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["active_unit"] == {"id": "u1", "stage": "sent", "task": "Add the export button"}
    assert invoke("ack", "u1", caller=role("worker"))[0] == 0
    assert invoke("progress", "--json", env="0")[1]["result"]["active_unit"]["stage"] == "accepted"

    # 6. Checkpoint and consultant review (lossless transcript channel).
    assert invoke("review", "after first unit", caller="w1:p9")[0] == 0
    assert invoke("record-completion", "u1", json.dumps({"summary": "button added", "tests": ["pass"]}), "--settled",
                  caller=role("worker"))[0] == 0
    assert durable(path)[0]["status"] == "awaiting_review"
    assert invoke("dispatch")[1]["reason"] == "review_required"
    consultant = next(i for i in durable(path)[0]["identities"] if i["id"] == "consultant")
    transcripts = tmp_path / "transcripts"
    write_transcript(transcripts, consultant["session_id"], "REVIEW-JSON: " + json.dumps(REVIEW))
    import trinity.cli as cli
    code = cli.main(["--db", str(path), "--transcript-root", str(transcripts), "record-review", "--from-transcript"],
                    runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake)
    assert code == 0 and json.loads(capsys.readouterr().out)["result"]["decision"] == "CONTINUE"
    state, events = durable(path)
    assert state["reviews"][0]["provenance"]["channel"] == "transcript" and "checkpoint_closed" in events

    # 7. Controlled continuation and result.
    assert invoke("pause", caller="w1:p9")[0] == 0
    assert invoke("dispatch")[1]["reason"] == "paused"
    assert invoke("resume", caller="w1:p9")[0] == 0
    assert invoke("dispatch")[1]["result"]["unit_id"] == "u2"
    assert invoke("record-completion", "u2", json.dumps({"summary": "serializer done"}), "--settled", caller=role("worker"))[0] == 0
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["status"] == "completed" and [u["stage"] for u in view["units"]] == ["completed", "completed"]
    assert view["latest_evidence"]["latest_report"]["unit_id"] == "u2"
    assert "accept the result" in view["next_action"]
    assert len(fake.prompts) == 2
    for command in fake.launches():
        assert not any(p in command for p in ("w1:p2", "w1:p3")), command


def test_failure_journey_never_shows_false_ready_or_duplicate_delivery(team, fake):
    path, invoke, setup, role = team
    setup(provision=False)

    # Launch failure: blocked with a next action, never ready.
    fake.faults["agent start"] = [("error", "pane_not_at_prompt")]
    code, result = invoke("provision")
    assert code == 3 and result["reason"] == "role_start_failed"
    view = invoke("progress", "--json", env="0")[1]["result"]
    assert view["team"]["status"] == "blocked" and view["next_action"] == result["detail"]["next_action"]
    assert invoke("plan")[0] == 0
    assert invoke("dispatch")[1]["reason"] == "team_not_ready" and fake.prompts == []
    assert invoke("provision")[1]["result"]["status"] == "ready"

    # Stale identity blocks dispatch until setup re-establishes it.
    fake.agents[role("worker")]["agent_session"] = {"value": "restarted"}
    assert invoke("dispatch")[1]["reason"] == "stale_identity" and fake.prompts == []
    assert invoke("provision")[1]["result"]["status"] == "ready"

    # Ambiguous delivery, then a restart: no duplicate send.
    fake.faults["agent prompt"] = ["timeout_after"]
    assert invoke("dispatch")[1]["reason"] == "dispatch_uncertain"
    assert invoke("reconcile")[1]["result"]["safe_to_continue"] is False
    assert invoke("dispatch")[1]["reason"] in ("dispatch_uncertain", "blocked")
    assert invoke("resume")[1]["reason"] == "unsettled_unit"
    assert len(fake.prompts) == 1

    # The bound worker's settled report is the evidence that resolves the uncertain delivery.
    assert invoke("record-completion", "u1", json.dumps({"summary": "done"}), "--settled", caller=role("worker"))[0] == 0
    state, _ = durable(path)
    assert state["status"] in ("running", "completed")


def test_missing_consultant_review_blocks_continuation(team, fake, tmp_path, capsys):
    path, invoke, setup, role = team
    setup()
    assert invoke("plan", "--units", UNITS)[0] == 0
    assert invoke("dispatch")[0] == 0
    assert invoke("review", "checkpoint", caller=role("worker"))[0] == 0
    assert invoke("record-completion", "u1", json.dumps({"summary": "done"}), "--settled", caller=role("worker"))[0] == 0
    for attempt in (["dispatch"], ["resume"], ["admit-next-unit"]):
        assert invoke(*attempt)[1]["reason"] == "review_required"
    import trinity.cli as cli
    empty = tmp_path / "no-transcripts"
    empty.mkdir()
    code = cli.main(["--db", str(path), "--transcript-root", str(empty), "record-review", "--from-transcript"],
                    runner=fake, environ={"HERDR_ENV": "1"}, launch_runner=fake)
    assert code == 3 and json.loads(capsys.readouterr().out)["reason"] == "transcript_not_found"
    assert durable(path)[0]["status"] == "awaiting_review" and len(fake.prompts) == 1
