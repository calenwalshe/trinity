"""Read-only Codex rollout source. Other transcript formats can supply select()."""

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from .coordinator import Refused


def epoch(value):
    if not isinstance(value, str):
        raise Refused("transcript_timestamp_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.timestamp()
    except (ValueError, OverflowError) as error:
        raise Refused("invalid_transcript_timestamp") from error


class CodexTranscripts:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path.home() / ".codex" / "sessions"

    def select(self, session_id, after=None):
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise Refused("transcript_session_mismatch")
        if after is not None and (type(after) not in (int, float) or not math.isfinite(after)):
            raise Refused("invalid_review_cutoff")
        paths = sorted(self.root.rglob(f"rollout-*-{session_id}.jsonl"))
        if not paths:
            raise Refused("transcript_not_found")
        if len(paths) != 1:
            raise Refused("ambiguous_transcript")
        path = paths[0].resolve()
        # Refuse symlink escapes from a configured transcript root.
        if not path.is_relative_to(self.root.resolve()):
            raise Refused("transcript_session_mismatch")
        malformed = 0
        metadata_seen = False
        selected = None
        try:
            with path.open("rb") as stream:
                for number, raw in enumerate(stream, 1):
                    try:
                        row = json.loads(raw)
                    except (ValueError, UnicodeError):
                        malformed += 1
                        continue
                    if not isinstance(row, dict):
                        malformed += 1
                        continue
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if row.get("type") == "session_meta":
                        ids = [payload[k] for k in ("id", "session_id") if k in payload]
                        if not ids or any(i != session_id for i in ids):
                            raise Refused("transcript_session_mismatch")
                        metadata_seen = True
                    if row.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "assistant":
                        continue
                    content = payload.get("content")
                    if not isinstance(content, list):
                        continue
                    texts = [part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
                    full = "".join(texts)
                    for text in texts:
                        for line in text.split("\n"):
                            if not line.startswith("REVIEW-JSON:"):
                                continue
                            stamp = epoch(row.get("timestamp"))
                            if after is not None and stamp <= after:
                                continue
                            suffix = line[len("REVIEW-JSON:"):].lstrip(" ")
                            if suffix.endswith("\r"):
                                suffix = suffix[:-1]
                            candidate = {"text": suffix, "timestamp": stamp,
                                         "transcript_path": str(path), "transcript_line": number,
                                         "sha256": hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
                                         "message_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest()}
                            if selected is None or stamp >= selected["timestamp"]:
                                selected = candidate
        except (OSError, UnicodeError) as error:
            raise Refused("transcript_unreadable") from error
        if not metadata_seen:
            raise Refused("transcript_session_mismatch")
        if selected is None:
            raise Refused("no_qualifying_review")
        return {**selected, "malformed_lines": malformed}
