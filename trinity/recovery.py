"""Restart/reattach reconciliation, user resolution of uncertain units, and the control table."""

from .clock import timestamp
from .coordinator import STALE_ROLE, STALE_SUPERVISOR, UNSETTLED, Refused
from .team import reconcile_team, summary as team_summary


SENDING_GRACE_SECONDS = 90  # longer than the bounded prompt call; older "sending" means the sender died


def reconcile(coordinator, observer):
    """Compare the durable run with live Herdr and persist what is wrong. Idempotent; sends nothing."""
    def check(state):
        changes = []
        now = timestamp(coordinator.now)
        for record in state.get("dispatches", []):
            if record["status"] == "sending" and now - record["prepared_at"] > SENDING_GRACE_SECONDS:
                record["status"] = "uncertain"
                unit = next(u for u in state["units"] if u["id"] == record["unit_id"])
                if unit["status"] == "dispatched":
                    unit["status"] = "uncertain"
                coordinator.store.event("dispatch_uncertain", {"dispatch_id": record["id"], "unit_id": unit["id"],
                                                               "error": "sender_interrupted"})
                changes.append(f"dispatch {record['id'][:8]} for {unit['id']} was interrupted mid-send: marked uncertain")
        for unit in state["units"]:
            if unit["status"] == "uncertain" and state["status"] != "stopped":
                before = len(state["recovery"].get("blockers", []))
                coordinator.block(state, "unit_uncertain", UNSETTLED, unit_id=unit["id"])
                if len(state["recovery"]["blockers"]) != before:
                    changes.append(f"unit {unit['id']} is uncertain: blocked until settled or resolved")
        problem = reconcile_team(state, observer.agent_identity)
        if problem:
            code, detail = problem
            action = STALE_SUPERVISOR if code == "supervisor_stale" else STALE_ROLE
            team = state["team"]
            current = team.get("blocker") or {}
            if team["status"] != "blocked" or (current.get("code"), current.get("detail")) != (code, detail):
                team["status"] = "blocked"
                team["blocker"] = {"code": code, "step": "reconcile", "detail": detail, "next_action": action, "at": now}
                coordinator.store.event("team_blocked", {"run_id": state["id"], **team["blocker"]})
                changes.append(f"{detail['role']} identity is {code}: team blocked")
        elif state.get("team") is None:
            missing = [i["id"] for i in state["identities"] if observer.agent_identity(i["pane_id"]) !=
                       {k: i[k] for k in ("pane_id", "session_id")}]
            if missing:
                changes.append("hand-bound identities no longer match: " + ", ".join(missing))
        if changes:
            coordinator.store.event("reconciled", {"changes": changes})
        blockers = state["recovery"].get("blockers", [])
        team = team_summary(state)
        safe = (not blockers and state["status"] not in ("stopped", "cancelled", "blocked")
                and (state.get("team") is None or team["status"] == "ready"))
        next_action = (blockers[0]["next_action"] if blockers else team.get("next_action") if team["status"] not in ("ready", "not_started")
                       else "Safe to continue." if safe else f"Run is {state['status']}; see '/trinity status'.")
        return {"changes": changes, "blockers": blockers, "team": team, "status": state["status"],
                "safe_to_continue": safe, "next_action": next_action}
    return coordinator.transact(check, configured_allowed=True, stopped_allowed=True)


def resolve_unit(coordinator, unit_id, decision, caller):
    """The user's explicit decision about an uncertain unit; never resends anything."""
    if decision not in ("requeue", "abandon"):
        raise Refused("invalid_resolution")

    def resolve(state):
        unit = next((u for u in state["units"] if u["id"] == unit_id), None)
        if unit is None or unit["status"] != "uncertain":
            raise Refused("unit_not_uncertain")
        record = next((d for d in state.get("dispatches", []) if d["id"] == unit.get("dispatch_id")), None)
        if record is not None:
            record["status"] = f"resolved_{decision}"
        unit["status"] = "pending" if decision == "requeue" else "abandoned"
        unit["resolution"] = {"decision": decision, "by": {k: caller.get(k) for k in ("kind", "pane_id")}, "at": timestamp(coordinator.now)}
        unit.pop("dispatch_id", None)
        state["execution"]["active_work_unit"] = None
        coordinator.clear_blockers(state, lambda b: b.get("unit_id") == unit_id)
        coordinator.promote_checkpoint(state)
        coordinator.store.event("unit_resolved", {"unit_id": unit_id, **unit["resolution"]})
        return {"unit_id": unit_id, "status": unit["status"],
                "next_action": "Resume when ready ('/trinity resume'); a requeued unit is dispatched again only by an explicit dispatch."}
    return coordinator.transact(resolve)


CONTROLS = {
    "status": ("anyone", "Read-only: run, team, unit stage, evidence, blockers and next action.", None),
    "pause": ("supervisor or you", "No new unit starts. An in-flight unit is not interrupted; its current turn finishes.", None),
    "resume": ("supervisor or you", "Re-checks live identities, checkpoints, blockers and uncertain units before work continues.",
               "An escalated checkpoint needs 'resume --user-approved' from your own shell pane."),
    "review": ("supervisor, worker or you", "Requests a consultant checkpoint: once the current unit settles, nothing new starts until a valid review is recorded.", None),
    "steer": ("supervisor (relays your direction)", "Records direction after a STEER/REPLAN review; may replace queued units.", None),
    "stop": ("supervisor or you", "Terminal for the coordinator. Agents and their processes are not terminated; an in-flight unit becomes uncertain.", None),
    "reconcile": ("supervisor or you", "Compares the durable run with live Herdr after a restart or reconnect; records problems; sends nothing.", None),
    "resolve-unit": ("you only", "Decide an uncertain unit: --requeue (queue it again) or --abandon. Nothing is resent automatically.",
                     "Run from your own shell pane."),
    "cancel": ("supervisor or you", "Before approval only: closes the proposal; nothing is launched.", None),
    "start": ("you only", "Approves the proposal revision you reviewed.", "Run from your own shell pane."),
}


def controls(state):
    status = state["status"]
    checkpoint = next((c for c in state["checkpoints"] if c["status"] != "closed"), None)
    uncertain = any(u["status"] == "uncertain" for u in state["units"])
    live = status not in ("configured", "cancelled", "stopped")
    available = {
        "status": True,
        "pause": status in ("active", "running", "checkpoint_pending", "awaiting_review", "awaiting_steer", "escalated"),
        "resume": status in ("paused", "blocked", "escalated"),
        "review": status in ("active", "running", "paused", "checkpoint_pending"),
        "steer": bool(checkpoint and checkpoint["status"] == "awaiting_steer"),
        "stop": status not in ("stopped", "cancelled"),
        "reconcile": live,
        "resolve-unit": uncertain and status != "stopped",
        "cancel": status == "configured",
        "start": status == "configured",
    }
    return {name: {"available": available[name], "who": who, "effect": effect, **({"approval": approval} if approval else {})}
            for name, (who, effect, approval) in CONTROLS.items()}


def render_controls(table):
    lines = ["Trinity controls (✓ available now):"]
    for name, control in table.items():
        mark = "✓" if control["available"] else "·"
        line = f"  {mark} {name} — {control['who']}: {control['effect']}"
        if control.get("approval"):
            line += f" {control['approval']}"
        lines.append(line)
    return "\n".join(lines)
