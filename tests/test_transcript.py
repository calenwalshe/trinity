import hashlib
import json

import pytest

from trinity.coordinator import Refused

from trinity.transcript import CodexTranscripts


def message(text, timestamp="2026-10-13T08:00:00Z", role="assistant"):
    return {"timestamp": timestamp, "type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": "output_text", "text": text}]}}


def rollout(root, rows, session="consultant-1"):
    path = root / "2026" / "10" / "13" / "rollout-test-consultant-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": session, "session_id": session}}
    path.write_text("\n".join([json.dumps(meta), *[json.dumps(r) if not isinstance(r, str) else r for r in rows]]) + "\n")
    return path


def test_selector_orders_by_timestamp_preserves_payload_and_full_message_bytes(tmp_path):
    assert CodexTranscripts is not None, "Codex transcript reader is missing"
    payload = '\t{"assessment":"__main__.py café"}  '
    full = "An explanation\nREVIEW-JSON:   " + payload + "\r\nAfterword"
    path = rollout(tmp_path, [
        message("REVIEW-JSON: {}", "2026-10-13T07:59:00Z"),
        "{broken", message(full, "2026-10-13T08:01:00Z"),
        message("REVIEW-JSON: user", "2026-10-13T09:00:00Z", role="user"),
        message("REVIEW-JSON: older", "2026-10-13T08:00:00Z"),
    ])
    before = path.read_bytes()
    result = CodexTranscripts(tmp_path).select("consultant-1", after=1791878400)
    assert result["text"] == payload
    assert result["sha256"] == hashlib.sha256(payload.encode()).hexdigest()
    assert result["message_sha256"] == hashlib.sha256(full.encode()).hexdigest()
    assert result["transcript_line"] == 4
    assert result["transcript_path"] == str(path.resolve())
    assert result["timestamp"] == 1791878460
    assert result["malformed_lines"] == 1
    assert path.read_bytes() == before
    with pytest.raises(Refused, match="no_qualifying_review"):
        CodexTranscripts(tmp_path).select("consultant-1", after=result["timestamp"])


@pytest.mark.parametrize("text,expected", [
    ('REVIEW-JSON:{}', '{}'), ('REVIEW-JSON:   {}  ', '{}  '),
    ('REVIEW-JSON:\t {}', '\t {}'),
])
def test_delimiter_removes_only_ascii_spaces(tmp_path, text, expected):
    assert CodexTranscripts is not None, "Codex transcript reader is missing"
    rollout(tmp_path, [message(text)])
    assert CodexTranscripts(tmp_path).select("consultant-1")["text"] == expected


@pytest.mark.parametrize("fault,reason", [
    ("missing", "transcript_not_found"), ("no_line", "no_qualifying_review"),
    ("no_timestamp", "transcript_timestamp_missing"), ("bad_timestamp", "invalid_transcript_timestamp"),
    ("session", "transcript_session_mismatch"), ("no_meta", "transcript_session_mismatch"),
])
def test_reader_refuses_missing_or_uncertain_source(tmp_path, fault, reason):
    assert CodexTranscripts is not None, "Codex transcript reader is missing"
    row = message("REVIEW-JSON: {}")
    if fault == "no_line":
        row = message("  REVIEW-JSON: {}")
    if fault == "no_timestamp":
        row.pop("timestamp")
    if fault == "bad_timestamp":
        row["timestamp"] = "not a time"
    if fault != "missing":
        path = rollout(tmp_path, [row], session="other" if fault == "session" else "consultant-1")
        if fault == "no_meta":
            path.write_text(json.dumps(row) + "\n")
    with pytest.raises(Refused, match=reason):
        CodexTranscripts(tmp_path).select("consultant-1")
