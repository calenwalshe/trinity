"""Phase 1 caller permissions; all identities come from the Herdr adapter."""

from .coordinator import Refused


PERMISSIONS = {
    "supervisor": {"init-run", "bind-identity", "plan-units", "admit-next-unit",
                   "record-review", "record-steer", "pause", "resume", "request-checkpoint",
                   "adjust-proposal", "cancel", "provision", "plan-work", "dispatch", "reconcile"},
    "worker": {"record-completion", "request-checkpoint", "ack", "report-blocker"},
    "consultant": set(),
    "launcher": {"provision", "reconcile"},
    "user": {"pause", "resume", "request-checkpoint", "cancel", "reconcile", "resolve-unit"},
}


def role_for(caller, state):
    if caller["kind"] != "agent":
        return caller["kind"]
    for identity in (state or {}).get("identities", []):
        if all(identity[k] == caller.get(k) for k in ("pane_id", "session_id")):
            return identity["id"]
    launcher = ((state or {}).get("team") or {}).get("launcher")
    if launcher and all(launcher.get(k) == caller.get(k) for k in ("pane_id", "session_id")):
        return "launcher"
    return "unbound"


def authorize(command, caller, state, *, user_approved=False):
    if user_approved and caller["kind"] == "agent":
        raise Refused("user_approval_requires_user_caller")
    if state is None and command == "init-run":
        if caller["kind"] != "agent":
            raise Refused("bootstrap_requires_agent")
        return "supervisor"
    role = role_for(caller, state)
    if role == "unknown":
        raise Refused("unknown_caller")
    if role == "unbound":
        raise Refused("unbound_caller")
    if command not in PERMISSIONS.get(role, set()):
        raise Refused("authority_denied")
    return role
