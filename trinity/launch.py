"""High-level, explicit Trinity invocation flow."""

import hashlib
from typing import TypedDict

from .clock import timestamp
from .coordinator import Refused
from .herdr import LaunchError
from .intake import QUESTIONS, missing_context, proposal
from .team import Provisioner, summary as team_summary


class LaunchRequest(TypedDict):
    objective: str
    repository: str
    success_criteria: list[str]
    constraints: list[str]
    configuration: dict


def launch_run(coordinator, launcher, observer, request: LaunchRequest, *, approval_mode: str) -> dict:
    """Validate and execute one explicit invocation, or leave it configured for shell approval."""
    objective = request.get("objective")
    repository = request.get("repository")
    criteria = request.get("success_criteria")
    constraints = request.get("constraints", [])
    configuration = request.get("configuration")
    missing = missing_context(objective, repository, criteria)
    if missing:
        raise Refused("missing_context", {"missing": missing, "questions": {key: QUESTIONS[key] for key in missing}})
    if approval_mode not in ("invocation", "manual") or not isinstance(configuration, dict):
        raise Refused("invalid_config")
    if not isinstance(constraints, list) or any(not isinstance(value, str) for value in constraints):
        raise Refused("invalid_context")
    caller = observer.current_caller()
    if caller.get("kind") != "agent" or not caller.get("pane_id") or not caller.get("session_id"):
        raise Refused("bootstrap_requires_agent")
    state = coordinator.init_run(
        objective.strip(), [value.strip() for value in criteria],
        configuration["checkpoints"]["interval_seconds"],
        bootstrap_identity={"pane_id": caller["pane_id"], "session_id": caller["session_id"]},
        configuration=configuration, repository=repository, constraints=[value.strip() for value in constraints])
    if approval_mode == "manual":
        db_path = coordinator.store.connection.execute("PRAGMA database_list").fetchone()[2]
        return proposal(state, db_path)

    coordinator.start(approval_source="invocation", proposal_revision=state["proposal"]["revision"],
                      approver={"kind": "invocation", "pane_id": caller["pane_id"]})
    Provisioner(coordinator, launcher, observer).provision()
    state = coordinator.status()
    supervisor = next((identity for identity in state["identities"] if identity["id"] == "supervisor"), None)
    if supervisor is None or observer.agent_identity(supervisor["pane_id"]) != {
            "pane_id": supervisor["pane_id"], "session_id": supervisor["session_id"]}:
        raise Refused("supervisor_stale")
    prompt = _initial_prompt(state)
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def record_intent(current):
        delivery = current.get("launch_delivery")
        if delivery:
            if delivery.get("status") == "submitted":
                return delivery
            raise Refused("supervisor_prompt_uncertain", {"status": delivery.get("status")})
        current["launch_delivery"] = {"status": "requested", "pane_id": supervisor["pane_id"],
                                      "prompt_sha256": prompt_hash, "requested_at": timestamp(coordinator.now)}
        coordinator.store.event("launch_prompt_requested", {"run_id": current["id"], "pane_id": supervisor["pane_id"],
                                                              "prompt_sha256": prompt_hash})
        return current["launch_delivery"]

    delivery = coordinator.transact(record_intent, configured_allowed=True)
    if delivery["status"] == "submitted":
        state = coordinator.status()
        return _launch_result(state, team_summary(state), delivery)
    try:
        launcher.prompt(supervisor["pane_id"], prompt)
    except LaunchError as error:
        status = "uncertain" if error.ambiguous else "not_sent"

        def record_failure(current):
            current["launch_delivery"].update(status=status, error=error.code,
                                               settled_at=timestamp(coordinator.now))
            coordinator.store.event(f"launch_prompt_{status}", {"run_id": current["id"],
                                                                  "pane_id": supervisor["pane_id"],
                                                                  "error": error.code})

        coordinator.transact(record_failure, configured_allowed=True, stopped_allowed=True)
        raise Refused("supervisor_prompt_" + status, {"code": error.code})

    def record_receipt(current):
        receipt = {"status": "submitted", "pane_id": supervisor["pane_id"],
                   "submitted_at": timestamp(coordinator.now),
                   "meaning": "Submitted to Herdr; does not establish acceptance."}
        current["launch_delivery"].update(receipt)
        coordinator.store.event("launch_prompt_submitted", {"run_id": current["id"], **receipt})
        return current["launch_delivery"]

    receipt = coordinator.transact(record_receipt, configured_allowed=True)
    state = coordinator.status()
    return _launch_result(state, team_summary(state), receipt)


def _initial_prompt(state):
    project = state["project"]
    criteria = "\n".join(f"- {item}" for item in project["success_criteria"])
    constraints = project.get("constraints", [])
    constraint_text = "\n".join(f"- {item}" for item in constraints) if constraints else "- None provided."
    return ("A user explicitly invoked Trinity for this task. Take ownership as supervisor, review the approved scope, "
            "create a bounded plan, and coordinate the worker and consultant. Ask the user if required information is "
            "missing. External actions still require their own authorization.\n\n"
            f"Objective: {project['objective']}\nRepository: {project['repository']}\n"
            f"Success criteria:\n{criteria}\nConstraints:\n{constraint_text}\n\n"
            "Starting the team does not mean the user has accepted your plan or results.")


def _launch_result(state, team, delivery):
    return {"run_id": state["id"], "status": team["status"], "approval": state.get("approval"),
            "team": team, "prompt_delivery": {key: delivery[key] for key in ("status", "meaning") if key in delivery}}
