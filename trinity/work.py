"""Bounded work planning, one-shot worker dispatch and the progress view.

Dispatch follows the same two-transaction shape as team setup: a dispatch
record is durable before the prompt is submitted, and the outcome is recorded
afterwards. A Herdr receipt only ever means "submitted"; acceptance and
completion come from the worker through the coordinator.
"""

import copy
import hashlib
import re
from uuid import uuid4

from .clock import timestamp
from .coordinator import Refused
from .herdr import LaunchError
from .intake import invocation
from .team import reconcile_team, summary as team_summary


UNIT_KEYS = {"id", "task", "boundaries", "evidence", "notes"}
FORBIDDEN = {"objective", "success_criteria", "constraints", "permissions", "autonomy", "configuration", "approval"}
STANDARD_BOUNDARIES = [
    "Stay inside the approved objective and repository.",
    "Do not change objectives, permissions or scope; ask the supervisor instead.",
    "No push, deploy, publish, notifications, payments or credential changes.",
]
STAGES = {"pending": "queued", "completed": "completed", "uncertain": "uncertain", "blocked": "blocked",
          "cancelled": "cancelled", "abandoned": "abandoned"}
STAGE_TEXT = {
    "queued": "queued — not sent yet",
    "admitted": "admitted by the coordinator — no delivery recorded (direct admit-next-unit)",
    "sending": "sending — submission in progress",
    "sent": "sent — delivery receipt only; the worker has not acknowledged it",
    "accepted": "accepted — the worker acknowledged it and is working",
    "completed": "completed — settled report recorded",
    "uncertain": "uncertain — delivery or settlement is unknown; dispatch is blocked until resolved",
    "blocked": "blocked — the worker reported a blocker",
    "cancelled": "cancelled", "abandoned": "abandoned by user decision",
}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_units(state, values=None):
    """Validated units tied to the approved objective; derives one unit when none are given."""
    project = state["project"]
    if values is None:
        values = [{"id": "u1", "task": project["objective"], "evidence": list(project["success_criteria"]),
                   "boundaries": list(project.get("constraints", []))}]
    if not isinstance(values, list) or not values:
        raise Refused("invalid_units")
    units, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise Refused("invalid_units")
        if set(value) & FORBIDDEN:
            raise Refused("unit_scope_violation", {"keys": sorted(set(value) & FORBIDDEN)})
        if set(value) - UNIT_KEYS:
            raise Refused("invalid_units", {"unknown_keys": sorted(set(value) - UNIT_KEYS)})
        if not isinstance(value.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", value["id"]) or value["id"] in seen:
            raise Refused("invalid_units", {"reason": "id"})
        seen.add(value["id"])
        if not isinstance(value.get("task"), str) or not value["task"].strip():
            raise Refused("unscoped_unit", {"unit_id": value["id"], "missing": "task"})
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not evidence or any(not isinstance(e, str) or not e.strip() for e in evidence):
            raise Refused("unscoped_unit", {"unit_id": value["id"], "missing": "evidence"})
        boundaries = value.get("boundaries", [])
        if not isinstance(boundaries, list) or any(not isinstance(b, str) for b in boundaries):
            raise Refused("invalid_units", {"unit_id": value["id"], "reason": "boundaries"})
        assignment = {"id": value["id"], "task": value["task"].strip(), "evidence": evidence,
                      "boundaries": boundaries + [b for b in STANDARD_BOUNDARIES if b not in boundaries]}
        if isinstance(value.get("notes"), str):
            assignment["notes"] = value["notes"]
        units.append(assignment)
    return units


def assignment_text(state, unit, dispatch_id, db_path):
    a = unit["assignment"]
    cli = invocation(db_path)
    lines = [
        f"[Trinity run {state['id'][:8]} · unit {unit['id']} · dispatch {dispatch_id[:8]}] You are the Trinity worker.",
        f"Task: {a['task']}",
        f"Approved objective (context only; do not change it): {state['project']['objective']}",
        f"Repository: {state['project'].get('repository')}",
        "Boundaries:", *[f"- {b}" for b in a["boundaries"]],
        "Evidence required in your report:", *[f"- {e}" for e in a["evidence"]],
        "Protocol (run these yourself; each is checked against your pane identity):",
        f"1. Acknowledge now: {cli} ack {unit['id']}",
        f"2. When finished and every command you started has exited: {cli} record-completion {unit['id']} "
        "'{\"summary\": \"...\", \"evidence\": [...], \"changed_files\": [...], \"tests\": [...]}' --settled",
        f"3. If you cannot proceed: {cli} report-blocker {unit['id']} \"WHY\"",
        "Do only this unit, then stop and wait.",
    ]
    return "\n".join(lines)


def _record_planned(coordinator, units, state):
    if state["status"] not in ("active", "planning"):
        raise Refused("already_planned")
    if not state.get("approval"):
        raise Refused("user_approval_required")
    objective = sha(state["project"]["objective"])
    state["units"] = [{"id": u["id"], "assignment": copy.deepcopy(u), "status": "pending",
                       "objective_sha256": objective, "approval_revision": state["approval"].get("revision")} for u in units]
    state["execution"]["current_phase"] = "execution"
    coordinator.store.event("units_planned", {"unit_ids": [u["id"] for u in units]})
    coordinator.store.event("work_planned", {"objective_sha256": objective, "units": units})
    coordinator._state(state, "running")
    return state


def plan(coordinator, values=None):
    def plan_work(state):
        return _record_planned(coordinator, bounded_units(state, values), state)
    return coordinator.transact(plan_work)


class Dispatcher:
    def __init__(self, coordinator, launcher, observer, db_path):
        self.coordinator = coordinator
        self.launcher = launcher
        self.observer = observer
        self.db_path = db_path

    def _prepare(self, state):
        c = self.coordinator
        if not state.get("approval"):
            raise Refused("user_approval_required")
        team = state.get("team")
        if not team or team["status"] != "ready":
            raise Refused("team_not_ready", {"team": team_summary(state)})
        open_dispatch = next((d for d in state.get("dispatches", []) if d["status"] in ("sending", "uncertain")), None)
        if open_dispatch:
            raise Refused("dispatch_uncertain", {"dispatch_id": open_dispatch["id"], "unit_id": open_dispatch["unit_id"],
                                                 "next_action": "Resolve the uncertain dispatch first ('/trinity progress')."})
        problem = reconcile_team(state, self.observer.agent_identity)
        if problem:
            code, detail = problem
            team["status"] = "blocked"
            team["blocker"] = {"code": code, "step": "dispatch", "detail": detail, "at": timestamp(c.now),
                               "next_action": "Run setup again ('/trinity setup') to re-establish the role before any dispatch."
                               if code != "supervisor_stale" else "The supervisor session is gone. Stop this run and propose a new one."}
            c.store.event("team_blocked", {"run_id": state["id"], **team["blocker"]})
            raise Refused(code, {**detail, "next_action": team["blocker"]["next_action"]})
        foreign = [i["id"] for i in state["identities"] if i["id"] != "supervisor" and i.get("owner") != state["id"]]
        if foreign:
            raise Refused("foreign_binding", {"roles": foreign})
        pending = next((u for u in state["units"] if u["status"] == "pending"), None)
        if pending is not None and not isinstance(pending.get("assignment", {}).get("task"), str):
            raise Refused("unscoped_unit", {"unit_id": pending["id"], "next_action": "Plan units with 'plan' (task and evidence required)."})
        observation = {i["id"]: {k: i[k] for k in ("pane_id", "session_id")} for i in state["identities"]}
        unit = c._admit(state, observation)
        unit = next(u for u in state["units"] if u["id"] == unit["id"])
        worker = next(i for i in state["identities"] if i["id"] == "worker")
        dispatch_id = str(uuid4())
        text = assignment_text(state, unit, dispatch_id, self.db_path)
        record = {"id": dispatch_id, "unit_id": unit["id"], "status": "sending", "target": worker["pane_id"],
                  "message_sha256": sha(text), "prepared_at": timestamp(c.now)}
        state.setdefault("dispatches", []).append(record)
        unit["dispatch_id"] = dispatch_id
        c.store.event("dispatch_prepared", record)
        return {"record": record, "text": text}

    def _settle(self, dispatch_id, outcome, error=None):
        c = self.coordinator

        def settle(state):
            record = next(d for d in state["dispatches"] if d["id"] == dispatch_id)
            if record["status"] != "sending":
                return record  # already reconciled by another path
            unit = next(u for u in state["units"] if u["id"] == record["unit_id"])
            record["settled_at"] = timestamp(c.now)
            if outcome == "sent":
                record["status"] = "sent"
                c.store.event("dispatch_sent", {"dispatch_id": dispatch_id, "unit_id": unit["id"],
                                                "meaning": "submitted to Herdr; not acceptance or completion"})
            elif outcome == "not_sent":
                record.update(status="not_sent", error=error)
                if unit["status"] in ("dispatched", "uncertain"):
                    unit["status"] = "pending"
                    unit.pop("dispatch_id", None)
                    state["execution"]["active_work_unit"] = None
                c.store.event("dispatch_not_sent", {"dispatch_id": dispatch_id, "unit_id": unit["id"], "error": error})
            else:
                record.update(status="uncertain", error=error)
                if unit["status"] == "dispatched":
                    unit["status"] = "uncertain"
                c.store.event("dispatch_uncertain", {"dispatch_id": dispatch_id, "unit_id": unit["id"], "error": error})
                c.block(state, "dispatch_uncertain", RESOLVE_UNCERTAIN, unit_id=unit["id"], dispatch_id=dispatch_id)
            return record
        return c.transact(settle, stopped_allowed=True)

    def dispatch(self):
        prepared = self.coordinator.transact(self._prepare)
        record = prepared["record"]
        try:
            self.launcher.prompt(record["target"], prepared["text"])
        except LaunchError as error:
            outcome = "uncertain" if error.ambiguous else "not_sent"
            self._settle(record["id"], outcome, error.code)
            raise Refused("dispatch_" + outcome, {"unit_id": record["unit_id"], "dispatch_id": record["id"], "code": error.code,
                                                  "next_action": RESOLVE_UNCERTAIN if outcome == "uncertain" else
                                                  "Nothing was delivered; the unit is queued again. Retry dispatch when the worker is idle."})
        settled = self._settle(record["id"], "sent")
        return {"unit_id": record["unit_id"], "dispatch_id": record["id"], "status": settled["status"],
                "meaning": "Submitted to the worker. Not yet acknowledged; watch '/trinity status'."}


RESOLVE_UNCERTAIN = ("Delivery is unknown. Do not resend. Check the worker pane: if the worker acknowledges, work continues; "
                     "otherwise the user decides with 'resolve-unit UNIT --requeue' or '--abandon' from their own shell pane.")


def acknowledge(coordinator, unit_id):
    def ack(state):
        unit = next((u for u in state["units"] if u["id"] == unit_id), None)
        if unit is None or unit["status"] not in ("dispatched", "uncertain") or "dispatch_id" not in unit:
            raise Refused("no_dispatched_unit")
        record = next(d for d in state["dispatches"] if d["id"] == unit["dispatch_id"])
        if unit.get("accepted_at"):
            return unit
        unit["accepted_at"] = timestamp(coordinator.now)
        if record["status"] in ("sending", "uncertain"):
            # The worker's own acknowledgement is delivery evidence.
            unit["status"] = "dispatched"
            record["status"] = "acknowledged"
            remaining = coordinator.clear_blockers(state, lambda b: b.get("dispatch_id") == record["id"])
            if state["status"] == "blocked" and not remaining:
                coordinator._state(state, "running")
        else:
            record["status"] = "acknowledged"
        coordinator.store.event("unit_accepted", {"unit_id": unit_id, "dispatch_id": record["id"]})
        return unit
    return coordinator.transact(ack)


def report_blocker(coordinator, unit_id, text):
    if not isinstance(text, str) or not text.strip():
        raise Refused("invalid_blocker")

    def blocked(state):
        unit = coordinator.inflight(state)
        if unit is None or unit["id"] != unit_id:
            raise Refused("no_dispatched_unit")
        unit["status"] = "blocked"
        unit["blocker"] = text
        state["execution"]["active_work_unit"] = None
        record = next((d for d in state.get("dispatches", []) if d["id"] == unit.get("dispatch_id")), None)
        if record is not None and record["status"] in ("sending", "sent", "uncertain"):
            # The bound worker's report proves the assignment arrived.
            record["status"] = "acknowledged"
        coordinator.clear_blockers(state, lambda b: b.get("unit_id") == unit_id and b["code"] in ("dispatch_uncertain", "unit_uncertain"))
        coordinator.store.event("unit_blocked", {"unit_id": unit_id, "text": text})
        coordinator.block(state, "worker_blocked",
                          "Supervisor: read the blocker, then request a review or steer, or resume to continue with the next unit.",
                          unit_id=unit_id)
        coordinator.promote_checkpoint(state)
        return unit
    return coordinator.transact(blocked)


def stage(state, unit):
    if unit["status"] == "dispatched":
        record = next((d for d in state.get("dispatches", []) if d["id"] == unit.get("dispatch_id")), None)
        if unit.get("accepted_at"):
            return "accepted"
        if record is None:
            return "admitted"  # legacy admit-next-unit path: no delivery was recorded
        return {"sending": "sending", "sent": "sent", "acknowledged": "accepted"}.get(record["status"], "uncertain")
    return STAGES.get(unit["status"], unit["status"])


def _next_action(state, view, db_path):
    status = state["status"]
    if status == "configured":
        from .intake import proposal
        return "Waiting for your approval. In your own shell pane run: " + proposal(state, db_path)["start_command"]
    if status == "cancelled":
        return "Nothing to do: the proposal was cancelled."
    if status == "stopped":
        return "Stopped. No further work will be dispatched; external agents were not terminated."
    if state.get("team") is None or view["team"]["status"] != "ready":
        return view["team"]["next_action"]
    if view["blockers"]:
        return view["blockers"][0]["next_action"]
    checkpoint = next((c for c in state["checkpoints"] if c["status"] != "closed"), None)
    if checkpoint:
        return {"pending": "Checkpoint due: waiting for the current unit to settle before the consultant reviews.",
                "awaiting_review": "Checkpoint: waiting for the consultant's review (supervisor records it with record-review --from-transcript).",
                "awaiting_steer": "Review asked for changes: the supervisor records a steer before work continues.",
                "escalated": "Escalated to you: review the consultant's concerns, then run 'resume --user-approved' in your own shell pane."}[checkpoint["status"]]
    if status == "paused":
        if view.get("active_unit"):
            return (f"Paused. Unit {view['active_unit']['id']} is still in flight and is not interrupted; "
                    "no new unit starts until '/trinity resume'.")
        return "Paused. No new unit starts until '/trinity resume'."
    if not state["units"]:
        return "Team ready. The supervisor plans the work next ('plan')."
    active = view.get("active_unit")
    if active:
        if active["stage"] in ("sending", "sent", "admitted"):
            return f"Waiting for the worker to acknowledge unit {active['id']}."
        return f"The worker is on unit {active['id']}; waiting for its settled report."
    if status == "completed" or not any(u["status"] == "pending" for u in state["units"]):
        return "All units are settled. Review the evidence, then accept the result or steer."
    return "Ready for the next unit: the supervisor dispatches it ('dispatch')."


def progress(state, observer=None, db_path=None):
    """Read-only answer to 'is it running and what is it doing?'."""
    view = {"run_id": state["id"], "status": state["status"], "objective": state["project"]["objective"],
            "repository": state["project"].get("repository"), "team": team_summary(state)}
    if observer is not None and state.get("team"):
        for role, record in view["team"].get("roles", {}).items():
            if record.get("pane_id"):
                info = observer.agent_info(record["pane_id"])
                record["observed"] = info.get("agent_status", "unknown") if info else "not observed"
    units = []
    for unit in state["units"]:
        entry = {"id": unit["id"], "stage": stage(state, unit)}
        task = unit.get("assignment", {}).get("task")
        if task:
            entry["task"] = task
        if unit.get("blocker"):
            entry["blocker"] = unit["blocker"]
        units.append(entry)
    view["units"] = units
    active = next((u for u in units if u["stage"] in ("sending", "sent", "accepted", "admitted", "uncertain")), None)
    if active:
        view["active_unit"] = active
    reports = [u for u in state["units"] if u.get("report") is not None]
    evidence = {}
    if reports:
        evidence["latest_report"] = {"unit_id": reports[-1]["id"], "settled": reports[-1].get("settled"), "report": reports[-1]["report"]}
    if state["reviews"]:
        decision = state["reviews"][-1]["decision"]
        evidence["latest_review"] = {"decision": decision["decision"], "assessment": decision["assessment"]}
    view["latest_evidence"] = evidence
    view["blockers"] = copy.deepcopy(state["recovery"].get("blockers", []))
    checkpoint = next((c for c in state["checkpoints"] if c["status"] != "closed"), None)
    view["checkpoint"] = ({"status": checkpoint["status"], "reason": checkpoint["reason"]} if checkpoint else
                          {"status": "none", "next_review_after_active_seconds": state["next_review_active_seconds"],
                           "active_seconds": round(state["active_seconds"], 1)})
    view["next_action"] = _next_action(state, view, db_path)
    return view


def render_progress(view):
    team = view["team"]
    lines = [f"Trinity run {view['run_id'][:8]} — {view['status']}", f"Objective: {view['objective']}"]
    roles = team.get("roles", {})
    described = []
    for role, record in sorted(roles.items(), key=lambda item: ("worker", "consultant").index(item[0])
                               if item[0] in ("worker", "consultant") else 9):
        text = f"{role} {record.get('model', '?')} {record.get('status')}"
        if record.get("pane_id"):
            text += f" in {record['pane_id']}"
        if record.get("observed"):
            text += f" [observed: {record['observed']} — a hint, not proof]"
        described.append(text)
    lines.append(f"Team: {team['status']}" + (f" (workspace {team['workspace']})" if team.get("workspace") else "")
                 + (" — " + "; ".join(described) if described else ""))
    if view.get("active_unit"):
        unit = view["active_unit"]
        lines.append(f"Active unit {unit['id']}: {STAGE_TEXT.get(unit['stage'], unit['stage'])}")
    if view["units"]:
        lines.append("Units: " + " · ".join(f"{u['id']} {u['stage']}" for u in view["units"]))
    evidence = view["latest_evidence"]
    if evidence.get("latest_report"):
        r = evidence["latest_report"]
        lines.append(f"Latest report ({r['unit_id']}, {'settled' if r['settled'] else 'unsettled'}): {r['report'].get('summary', r['report'])}")
    if evidence.get("latest_review"):
        lines.append(f"Latest review: {evidence['latest_review']['decision']} — {evidence['latest_review']['assessment']}")
    if not evidence:
        lines.append("Latest evidence: none yet")
    checkpoint = view["checkpoint"]
    lines.append(f"Checkpoint: {checkpoint['status']}" + (f" ({checkpoint['reason']})" if checkpoint.get("reason") else
                 f" — next review after {checkpoint['next_review_after_active_seconds']} s active ({checkpoint['active_seconds']} s so far)"))
    lines.append("Blockers: " + ("; ".join(f"{b['code']}" + (f" ({b['unit_id']})" if b.get("unit_id") else "") for b in view["blockers"]) or "none"))
    lines.append(f"Next: {view['next_action']}")
    if view.get("controls"):
        lines.append("Controls: " + " · ".join(f"{name}: {c['who']}" for name, c in view["controls"].items() if c["available"]))
    return "\n".join(lines)
