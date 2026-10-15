"""Task-aware intake: required run context and the user-facing run proposal."""

import hashlib
from pathlib import Path
import re
import shlex

from .config import ROLES
from .store import encode


PLACEHOLDERS = {"", "objective", "task", "the task", "tbd", "todo", "placeholder", "n/a", "na", "none",
                "?", "...", "this", "it", "same", "do it", "the objective", "current task"}
QUESTIONS = {
    "objective": "What should this Trinity run accomplish? One or two sentences in your words.",
    "repository": "Which repository should the team work in? (I could not find a git checkout for it.)",
    "success_criteria": "How will we know it is done? Name at least one checkable success criterion.",
}


def placeholder(text):
    if not isinstance(text, str):
        return True
    value = " ".join(text.split()).strip().lower()
    return value in PLACEHOLDERS or bool(re.fullmatch(r"<.*>|\{.*\}|\[.*\]|\$\{?\w+\}?", value))


def repository_root(path):
    """The git checkout containing ``path``, or None. Never creates anything."""
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        candidate = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not candidate.is_dir():
        return None
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return str(directory)
    return None


def missing_context(objective, repository, success_criteria):
    """Names of required fields that are absent or placeholders, in a stable order."""
    missing = []
    if placeholder(objective) or len(objective.split()) < 2:
        missing.append("objective")
    if repository_root(repository) is None:
        missing.append("repository")
    criteria = [c for c in (success_criteria or []) if isinstance(c, str) and not placeholder(c)]
    if not criteria or len(criteria) != len(success_criteria or []):
        missing.append("success_criteria")
    return missing


def invocation(db_path):
    """A CLI prefix that works from any directory and any shell pane."""
    package_root = str(Path(__file__).resolve().parent.parent)
    return f"PYTHONPATH={shlex.quote(package_root)} python3 -m trinity --db {shlex.quote(str(Path(db_path).resolve()))}"


def digest(state):
    content = {"project": state["project"], "configuration": state["configuration"]}
    return hashlib.sha256(encode(content).encode("utf-8")).hexdigest()


def permissions(configuration):
    autonomy = configuration.get("autonomy", {})
    level = autonomy.get("level", "bounded")
    scope = autonomy.get("require_approval_for_scope_changes", True)
    external = autonomy.get("require_approval_for_external_actions", True)
    lines = [f"Autonomy {level}: the worker makes implementation changes only inside the approved objective."]
    lines.append("Scope or objective changes need your approval." if scope else
                 "Scope changes may be made by the supervisor (configured).")
    lines.append("External actions (push, deploy, publish, notify, payments, credentials) need your approval."
                 if external else "External actions are allowed by configuration.")
    lines.append("Agents can never approve on your behalf; only your own shell pane can start or resume an escalated run.")
    return lines


def proposal(state, db_path=None):
    """A JSON-safe view of what the user is being asked to approve."""
    configuration = state["configuration"]
    checkpoints = configuration.get("checkpoints", {})
    interval = checkpoints.get("interval_seconds", configuration.get("interval_s"))
    revision = state.get("proposal", {}).get("revision")
    started = state["status"] != "configured"
    view = {
        "run_id": state["id"], "status": state["status"], "revision": revision,
        "objective": state["project"]["objective"],
        "repository": state["project"].get("repository"),
        "success_criteria": state["project"]["success_criteria"],
        "constraints": state["project"].get("constraints", []),
        "roles": {r: {**configuration["agents"][r], "launched": False} for r in ROLES} if "agents" in configuration else {},
        "mode": configuration.get("name"),
        "autonomy": configuration.get("autonomy", {}).get("level", "bounded"),
        "checkpoints": {"interval_seconds": interval,
                        "rule": "After each interval of active work, no new unit starts until the current one settles and the consultant's review is recorded."},
        "permissions": permissions(configuration),
        "approval": state.get("approval"),
        "launched": False,
    }
    if "interval_warning" in configuration:
        view["interval_warning"] = configuration["interval_warning"]
    if state["status"] == "configured":
        command = [invocation(db_path) if db_path else "python3 -m trinity --db DB", "start", "--user-approved"]
        if revision is not None:
            command += ["--proposal", str(revision)]
        view["choices"] = {
            "adjust": "Tell the assistant what to change; it re-shows the proposal with a new revision.",
            "cancel": "Say '/trinity cancel'; nothing is launched and the run is closed.",
            "start": "In your own shell pane (not an agent pane), run the start command below.",
        }
        view["start_command"] = " ".join(command)
        view["next_action"] = "Review the proposal, then adjust, cancel, or start it yourself."
    elif state["status"] == "cancelled":
        view["next_action"] = "Nothing to do: the proposal was cancelled and nothing was launched."
    elif started:
        view["next_action"] = "Approved. Team setup is next: '/trinity setup' (the assistant runs provision)."
    return view


def render_proposal(view):
    title = "Trinity run proposal"
    if view["revision"] is not None:
        title += f" (revision {view['revision']})"
    state = {"configured": "awaiting your approval — nothing has been launched",
             "cancelled": "cancelled — nothing was launched"}.get(view["status"], f"state {view['status']}")
    lines = [f"{title}: {state}", "", f"Objective: {view['objective']}"]
    if view.get("repository"):
        lines.append(f"Repository: {view['repository']}")
    lines.append("Success criteria:")
    lines.extend(f"  - {c}" for c in view["success_criteria"]) if view["success_criteria"] else lines.append("  (none)")
    if view["constraints"]:
        lines.append("Constraints:")
        lines.extend(f"  - {c}" for c in view["constraints"])
    if view["roles"]:
        lines.append("Roles (logical models; nothing launched yet):")
        notes = {"supervisor": " — this assistant session", "consultant": " — read-only reviewer", "worker": " — does the work"}
        lines.extend(f"  {r.title()}: {a['model']} ({a['provider']}){notes[r]}" for r, a in view["roles"].items())
    lines.append(f"Mode: {view['mode']} · Autonomy: {view['autonomy']}")
    lines.append(f"Checkpoints: every {view['checkpoints']['interval_seconds']} s of active work. {view['checkpoints']['rule']}")
    if "interval_warning" in view:
        lines.append(f"interval_warning: {view['interval_warning']}")
    lines.append("Permissions:")
    lines.extend(f"  - {p}" for p in view["permissions"])
    if "choices" in view:
        lines.extend(["", "Your choices:",
                      f"  adjust → {view['choices']['adjust']}",
                      f"  cancel → {view['choices']['cancel']}",
                      f"  start  → {view['choices']['start']}",
                      f"           {view['start_command']}"])
    lines.extend(["", f"Next: {view['next_action']}"])
    return "\n".join(lines)
