import hashlib
import json

import pytest

from trinity.cli import main
from trinity.coordinator import Coordinator
from trinity.store import Store


REVIEW = '{"decision":"CONTINUE","assessment":"__main__.py café","concerns":[],"recommendations":[],"requires_supervisor_action":false,"requires_user_approval":false,"suggested_next_review_seconds":120}  '


@pytest.fixture
def review_run(tmp_path, capsys, live):
    db = tmp_path / "run.sqlite3"
    root = tmp_path / "sessions"
    root.mkdir()
    file = root / "rollout-test-consultant-1.jsonl"
    full_message = "REVIEW-JSON:  " + REVIEW
    rows = [{"type": "session_meta", "payload": {"id": "consultant-1"}},
            {"timestamp": "2026-10-13T08:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": full_message}]}}]
    file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    store = Store(db)
    c = Coordinator(store, now=lambda: 1791878400)
    c.init_run("Build", [])
    for role, pane, session in [("supervisor", "w1:p1", "supervisor-1"), ("worker", "w1:p2", "worker-1"), ("consultant", "w1:p3", "consultant-1")]:
        c.bind_identity(role, pane, session)
    c.plan_units([{"id": "u1"}])
    c.request_checkpoint("milestone")

    def invoke(*args, env="1"):
        code = main(["--db", str(db), "--transcript-root", str(root), *args], runner=live, environ={"HERDR_ENV": env})
        return code, json.loads(capsys.readouterr().out)

    yield store, c, file, full_message, invoke
    store.close()


def test_cli_records_transcript_payload_and_both_hashes(review_run):
    store, c, file, full, invoke = review_run
    assert invoke("record-review", "--from-transcript")[0] == 0
    item = c.status()["reviews"][0]
    assert item["raw_text"] == REVIEW
    assert item["provenance"] == {
        "pane_id": "w1:p3", "session_id": "consultant-1", "channel": "transcript",
        "transcript_path": str(file.resolve()), "transcript_line": 2,
        "sha256": hashlib.sha256(REVIEW.encode()).hexdigest(),
        "message_sha256": hashlib.sha256(full.encode()).hexdigest(),
    }
    assert c.status()["status"] == "running"
    assert next(e for e in store.events() if e["kind"] == "review_recorded")["data"]["raw_text"] == REVIEW


@pytest.mark.parametrize("fault,reason", [
    ("no_file", "transcript_not_found"), ("old", "no_qualifying_review"),
    ("invalid_json", "invalid_review"), ("session", "transcript_session_mismatch"),
    ("live_session", "stale_identity"), ("legacy", "checkpoint_time_missing"),
])
def test_transcript_refusals_preserve_checkpoint_and_never_approve(review_run, live, fault, reason):
    store, c, file, full, invoke = review_run
    if fault == "no_file":
        file.unlink()
    elif fault == "old":
        file.write_text(file.read_text().replace("08:00:01Z", "08:00:00Z"))
    elif fault == "invalid_json":
        # Latest invalid review must not fall back to the preceding valid one.
        row = {"timestamp": "2026-10-13T08:00:02Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"text": "REVIEW-JSON: {broken"}]}}
        with file.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
    elif fault == "session":
        file.write_text(file.read_text().replace('"id": "consultant-1"', '"id": "wrong"'))
    elif fault == "live_session":
        live.agents["w1:p3"]["agent_session"]["value"] = "replacement"
    elif fault == "legacy":
        state = c.status()
        state["checkpoints"][0].pop("requested_at", None)
        with store.transaction():
            store.save(state)
    assert invoke("record-review", "--from-transcript") == (3, {"ok": False, "reason": reason})
    assert c.status()["status"] == "awaiting_review" and c.status()["reviews"] == []
    assert not any(e["kind"] == "checkpoint_closed" for e in store.events())


def test_preview_is_archival_readonly_and_open_to_unknown_caller(review_run):
    store, c, file, full, invoke = review_run
    before, ledger = c.status(), store.events()
    code, data = invoke("transcript-preview", env="0")
    assert code == 0 and data["result"]["text"] == REVIEW
    assert data["result"]["timestamp"] == 1791878401
    assert invoke("transcript-preview", "--after", "1791878401", env="0") == (3, {"ok": False, "reason": "no_qualifying_review"})
    assert c.status() == before and store.events() == ledger


def test_record_transcript_takes_no_caller_text_and_worker_cannot_relay(review_run, live):
    store, c, _, _, invoke = review_run
    assert invoke("record-review", REVIEW, "--from-transcript")[0] == 2
    assert invoke("record-review")[0] == 2
    live.caller = "w1:p2"
    assert invoke("record-review", "--from-transcript") == (3, {"ok": False, "reason": "authority_denied"})
    assert c.status()["reviews"] == []


def test_screen_relay_is_explicitly_marked(review_run):
    _, c, _, _, invoke = review_run
    assert invoke("record-review", REVIEW)[0] == 0
    assert c.status()["reviews"][0]["provenance"]["channel"] == "relay"
