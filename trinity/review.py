"""Validate the complete v0.1 section 5.4 consultant decision shape."""

import json


FIELDS = {
    "decision", "assessment", "concerns", "recommendations",
    "requires_supervisor_action", "requires_user_approval", "suggested_next_review_seconds",
}


def validate_review(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("review must contain exactly the seven decision fields")
    if value["decision"] not in ("CONTINUE", "STEER", "REPLAN", "ESCALATE"):
        raise ValueError("unknown decision")
    if not isinstance(value["assessment"], str) or not value["assessment"].strip():
        raise ValueError("assessment must be nonempty text")
    for key in ("concerns", "recommendations"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError(f"{key} must be a list of strings")
    for key in ("requires_supervisor_action", "requires_user_approval"):
        if type(value[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    interval = value["suggested_next_review_seconds"]
    if type(interval) is not int or interval <= 0:
        raise ValueError("suggested review interval must be a positive integer")
    return value
