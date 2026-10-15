"""Approved-run role provisioning and identity reconciliation.

Every external step is bracketed by two transactions: the intent is durable
before Herdr is called, and the observed outcome is durable afterwards. A retry
first reconciles what an interrupted attempt may have created, so it never
launches a second agent for the same role. Only panes this run created (inside
its own workspace, under its own agent names) are ever reused.
"""

import copy
from uuid import uuid4

from .clock import timestamp
from .coordinator import Refused
from .herdr import LaunchError


# Logical alias → launch profile. Evidence is recorded in
# docs/trinity-redesign/runtime-capabilities.md §7; anything absent is unsupported.
PROFILES = {
    "luna": {"kind": "codex", "args": ["-m", "gpt-6-luna"]},
    "sol": {"kind": "codex", "args": ["-m", "gpt-6.1-sol"]},
    "astra": {"kind": "codex", "args": ["-m", "gpt-6-astra"]},
    "opus": {"kind": "claude", "args": ["--model", "opus"]},
    "fable": {"kind": "claude", "args": ["--model", "fable"]},
}
# The consultant never edits files or directs other agents.
ROLE_ARGS = {("codex", "consultant"): ["-s", "read-only"], ("claude", "consultant"): ["--permission-mode", "plan"]}
REQUIRED = ("worker", "supervisor", "consultant")
START_TIMEOUT_MS = 60000
MAX_START_ATTEMPTS = 3
LEASE_SECONDS = 300  # refreshed at every step; a holder silent this long is presumed dead
NEXT = {
    "user_approval_required": "Approve the proposal from your own shell pane first (see '/trinity proposal').",
    "unsupported_launch_profile": "Adjust is closed after approval: stop this run and propose again with a supported model alias.",
    "foreign_binding": "A role was bound by hand to a session this run did not create. Stop this run and propose a fresh one.",
    "herdr_unavailable": "Herdr is not reachable from this pane. Run setup again from inside Herdr.",
    "herdr_launch_failed": "Herdr refused the step; nothing was created. Run setup again; if it repeats, report the detail.",
    "uncertain_provisioning": "An earlier step may or may not have taken effect. Run setup again: it inspects the run's own workspace before creating anything.",
    "role_start_failed": "The agent did not start. Run setup again to retry in the same run-owned pane.",
    "start_attempts_exhausted": "The agent failed to start three times. Inspect the pane in the run's workspace, then stop the run or ask for help.",
    "foreign_agent_in_pane": "A run pane now hosts an agent this run did not start. It was left untouched; stop the run or close that pane yourself.",
    "supervisor_stale": "The supervisor session that created this run is gone. Stop this run and propose a new one from the current session.",
    "duplicate_identity": "Two roles resolved to the same session. Stop this run; nothing will be dispatched.",
    "unit_in_flight": "A unit is in flight; the worker cannot be re-established until it settles or is resolved.",
    "provisioning_in_progress": "Another setup call is running for this run. Wait for it to finish.",
    "workspace_missing": "The run's workspace was closed. Run setup again to create a fresh one.",
}


def launch_plan(configuration):
    """Role → launch profile, refusing any alias or mode without verified support."""
    plan = {}
    agents = configuration.get("agents", {})
    for role in REQUIRED:
        agent = agents.get(role)
        if not isinstance(agent, dict) or agent.get("model") not in PROFILES:
            raise Refused("unsupported_launch_profile", {"role": role, "model": (agent or {}).get("model")})
        if agent.get("execution_mode", "interactive") != "interactive":
            raise Refused("unsupported_launch_profile", {"role": role, "execution_mode": agent["execution_mode"]})
        profile = PROFILES[agent["model"]]
        plan[role] = {"model": agent["model"], "kind": profile["kind"],
                      "args": profile["args"] + ROLE_ARGS.get((profile["kind"], role), [])}
    return plan


def summary(state):
    """User-facing team view; carries no session identifiers."""
    team = state.get("team")
    if team is None:
        return {"status": "not_started", "next_action": "Run setup ('/trinity setup') after approval." if state.get("approval") else NEXT["user_approval_required"]}
    roles = {role: {k: r.get(k) for k in ("status", "model", "kind", "pane_id", "name", "error") if r.get(k) is not None}
             for role, r in team["roles"].items()}
    view = {"status": team["status"], "workspace": (team.get("workspace") or {}).get("label"), "roles": roles}
    if team.get("blocker"):
        view["blocker"] = copy.deepcopy(team["blocker"])
        view["next_action"] = team["blocker"]["next_action"]
    elif team["status"] == "ready":
        view["next_action"] = "Team ready. The supervisor plans the first unit and dispatches it."
    else:
        view["next_action"] = "Setup is in progress."
    return view


class Provisioner:
    def __init__(self, coordinator, launcher, observer, now=None):
        self.coordinator = coordinator
        self.launcher = launcher
        self.observer = observer
        self.now = now or coordinator.now
        self.lease = str(uuid4())

    # -- durable helpers -------------------------------------------------
    def _txn(self, fn):
        def guarded(state):
            team = state.get("team")
            lease = (team or {}).get("lease")
            if lease is not None:
                if lease["id"] != self.lease and timestamp(self.now) - lease["at"] < LEASE_SECONDS:
                    raise Refused("provisioning_in_progress", {"next_action": NEXT["provisioning_in_progress"]})
                if lease["id"] == self.lease:
                    lease["at"] = timestamp(self.now)
            return fn(state)
        return self.coordinator.transact(guarded, configured_allowed=True)

    def _event(self, kind, data):
        self.coordinator.store.event(kind, data)

    def _block(self, code, step, detail=None):
        def block(state):
            team = state["team"]
            team["status"] = "blocked"
            team["blocker"] = {"code": code, "step": step, "next_action": NEXT.get(code, NEXT["herdr_launch_failed"]),
                               "detail": detail, "at": timestamp(self.now)}
            team.pop("lease", None)
            self._event("team_blocked", {"run_id": state["id"], **team["blocker"]})
        self._txn(block)
        raise Refused(code, {"step": step, "next_action": NEXT.get(code, NEXT["herdr_launch_failed"]), "detail": detail})

    def _role(self, state, role):
        return state["team"]["roles"][role]

    # -- entry point -----------------------------------------------------
    def provision(self):
        caller = self.observer.current_caller()
        if not caller.get("workspace_id") or not caller.get("tab_id") or not caller.get("pane_id"):
            raise Refused("herdr_unavailable", {"reason": "current Herdr workspace and tab could not be verified"})
        self._txn(lambda state: self._begin(state, caller))
        workspace = self._workspace()
        for role in REQUIRED:
            self._pane(role, workspace)
            self._agent(role, workspace)
        return self._finish()

    def _begin(self, state, caller):
        if state["status"] == "configured" or not state.get("approval"):
            raise Refused("user_approval_required", {"next_action": NEXT["user_approval_required"]})
        if state["status"] in ("completed",):
            raise Refused("run_completed")
        run8 = state["id"][:8]
        team = state.setdefault("team", {"status": "provisioning", "key": state["id"], "workspace": None, "blocker": None,
                                         "context": {"workspace_id": caller["workspace_id"], "caller_tab_id": caller["tab_id"]},
                                         "launcher": {k: caller[k] for k in ("pane_id", "session_id") if k in caller},
                                         "roles": {r: {"status": "pending", "name": f"trinity-{run8}-{r}", "pane_id": None,
                                                       "attempts": 0} for r in REQUIRED}})
        if team.get("context", {}).get("workspace_id") != caller["workspace_id"]:
            raise Refused("caller_workspace_changed")
        lease = team.get("lease")
        if lease and lease["id"] != self.lease and timestamp(self.now) - lease["at"] < LEASE_SECONDS:
            raise Refused("provisioning_in_progress", {"next_action": NEXT["provisioning_in_progress"]})
        team["lease"] = {"id": self.lease, "at": timestamp(self.now)}
        foreign = [i["id"] for i in state["identities"] if i["id"] in REQUIRED and i.get("owner") != state["id"]
                   and not (i["id"] == "supervisor" and i.get("bootstrap") is True)]
        if foreign:
            self._fail_now(state, "foreign_binding", "preflight", {"roles": foreign})
        try:
            plan = launch_plan(state["configuration"])
        except Refused as error:
            self._fail_now(state, error.reason, "preflight", error.detail)
        for role, profile in plan.items():
            team["roles"][role].update(model=profile["model"], kind=profile["kind"], args=profile["args"])
        if team["status"] != "ready":
            team["status"] = "provisioning"
        team["blocker"] = None
        self._event("provisioning_started", {"run_id": state["id"], "plan": plan})

    def _fail_now(self, state, code, step, detail):
        team = state["team"]
        team["status"] = "blocked"
        team["blocker"] = {"code": code, "step": step, "next_action": NEXT[code], "detail": detail, "at": timestamp(self.now)}
        team.pop("lease", None)
        self._event("team_blocked", {"run_id": state["id"], **team["blocker"]})
        raise Refused(code, {"step": step, "next_action": NEXT[code], "detail": detail})

    def _external(self, step, call):
        try:
            return call()
        except LaunchError as error:
            if error.code == "herdr_unavailable":
                self._block("herdr_unavailable", step, error.detail)
            return error

    # -- workspace -------------------------------------------------------
    def _workspace(self):
        state = self.coordinator.status()
        workspace = state["team"]["workspace"]
        repository = state["project"].get("repository")
        if repository is None:
            self._block("herdr_launch_failed", "workspace", {"reason": "run has no repository"})
        workspace_id = state["team"]["context"]["workspace_id"]
        if workspace and workspace["status"] == "created":
            listed = self._external("tab", lambda: self.launcher.list_tabs(workspace_id))
            if isinstance(listed, LaunchError):
                self._block("uncertain_provisioning", "tab", {"code": listed.code})
            if any(tab.get("tab_id") == workspace["tab_id"] for tab in listed):
                return workspace
            if self._txn(self._forget_workspace) == "unit_in_flight":
                self._block("unit_in_flight", "tab")
            workspace = None
        if workspace and workspace["status"] in ("requested", "uncertain"):
            listed = self._external("tab", lambda: self.launcher.list_tabs(workspace_id))
            if isinstance(listed, LaunchError):
                self._block("uncertain_provisioning", "tab", {"code": listed.code})
            matches = [tab for tab in listed if tab.get("label") == workspace["label"]]
            if len(matches) > 1:
                self._block("uncertain_provisioning", "tab", {"label": workspace["label"], "matches": len(matches)})
            if len(matches) == 1:
                return self._adopt_tab(matches[0])
            workspace = None
        return self._create_tab(repository)

    def _forget_workspace(self, state):
        if self.coordinator.inflight(state):
            return "unit_in_flight"
        team = state["team"]
        self._event("tab_missing", {"run_id": state["id"], "tab_id": team["workspace"]["tab_id"]})
        team["generation"] = team.get("generation", 1) + 1
        team["workspace"] = None
        for role in REQUIRED:
            self._reset_role(state, role, "workspace_missing")

    def _reset_role(self, state, role, reason):
        record = self._role(state, role)
        if record["pane_id"] is not None or record["status"] == "ready":
            self._event("role_reset", {"role": role, "pane_id": record["pane_id"], "reason": reason})
        record.update(status="pending", pane_id=None)
        state["identities"] = [i for i in state["identities"] if i["id"] != role]
        state["team"]["status"] = "provisioning"

    def _create_tab(self, repository):
        def intend(state):
            team = state["team"]
            generation = team.get("generation", 1)
            label = f"trinity-{state['id'][:8]}" + (f"-{generation}" if generation > 1 else "")
            team["workspace"] = {"status": "requested", "label": label, "cwd": repository,
                                  "workspace_id": team["context"]["workspace_id"],
                                  "caller_tab_id": team["context"]["caller_tab_id"]}
            self._event("tab_requested", {"run_id": state["id"], "label": label,
                                           "workspace_id": team["context"]["workspace_id"]})
            return label
        label = self._txn(intend)
        workspace_id = self.coordinator.status()["team"]["context"]["workspace_id"]
        created = self._external("tab", lambda: self.launcher.create_tab(workspace_id, label, repository))
        if isinstance(created, LaunchError):
            def failed(state):
                state["team"]["workspace"]["status"] = "uncertain" if created.ambiguous else "failed"
            self._txn(failed)
            self._block("uncertain_provisioning" if created.ambiguous else "herdr_launch_failed", "tab",
                        {"code": created.code, **(created.detail or {})})

        def record(state):
            workspace = state["team"]["workspace"]
            workspace.update(status="created", tab_id=created["tab_id"], root_pane=created["pane_id"])
            worker = self._role(state, "worker")
            worker.update(status="pane_created", pane_id=created["pane_id"])
            self._event("tab_created", {"run_id": state["id"], **workspace})
            return copy.deepcopy(workspace)
        return self._txn(record)

    def _adopt_tab(self, tab):
        workspace_id = self.coordinator.status()["team"]["context"]["workspace_id"]
        panes = self._external("tab", lambda: self.launcher.list_panes(workspace_id))
        if isinstance(panes, LaunchError):
            self._block("uncertain_provisioning", "tab", {"code": panes.code})
        panes = [pane for pane in panes if pane.get("tab_id") == tab.get("tab_id")]
        if len(panes) != 1:
            self._block("uncertain_provisioning", "tab", {"tab_id": tab.get("tab_id"), "panes": len(panes)})

        def adopt(state):
            workspace = state["team"]["workspace"]
            workspace.update(status="created", tab_id=tab["tab_id"], root_pane=panes[0]["pane_id"])
            self._role(state, "worker").update(status="pane_created", pane_id=panes[0]["pane_id"])
            self._event("tab_adopted", {"run_id": state["id"], "tab_id": tab["tab_id"], "panes": len(panes)})
            return copy.deepcopy(workspace)
        return self._txn(adopt)

    # -- panes -----------------------------------------------------------
    def _pane(self, role, workspace):
        state = self.coordinator.status()
        record = self._role(state, role)
        if record["pane_id"] is not None:
            return
        if record["status"] in ("requested", "uncertain"):
            panes = self._external(f"{role}:pane", lambda: self.launcher.list_panes(workspace["workspace_id"]))
            if isinstance(panes, LaunchError):
                self._block("uncertain_provisioning", f"{role}:pane", {"code": panes.code})
            panes = [pane for pane in panes if pane.get("tab_id") == workspace["tab_id"]]
            assigned = {r["pane_id"] for r in state["team"]["roles"].values() if r["pane_id"]}
            unassigned = [p["pane_id"] for p in panes if p.get("pane_id") not in assigned]
            if len(unassigned) > 1:
                self._block("uncertain_provisioning", f"{role}:pane", {"unassigned_panes": len(unassigned)})
            if len(unassigned) == 1:
                def adopt(state):
                    self._role(state, role).update(status="pane_created", pane_id=unassigned[0])
                    self._event("pane_adopted", {"role": role, "pane_id": unassigned[0]})
                self._txn(adopt)
                return

        def intend(state):
            self._role(state, role)["status"] = "requested"
            self._event("pane_requested", {"role": role, "workspace_id": workspace["workspace_id"]})
        self._txn(intend)
        # Herdr's ratio is the fraction retained by the existing pane; 0.65
        # keeps the worker left at 65% and leaves 35% for the right column.
        parent_role, direction, ratio = {"supervisor": ("worker", "right", 0.65),
                                         "consultant": ("supervisor", "down", 0.5)}[role]
        parent = self._role(self.coordinator.status(), parent_role)["pane_id"]
        created = self._external(f"{role}:pane", lambda: self.launcher.split_pane(
            parent, direction=direction, ratio=ratio, cwd=state["project"]["repository"]))
        if isinstance(created, LaunchError):
            def failed(state):
                self._role(state, role)["status"] = "uncertain" if created.ambiguous else "failed"
            self._txn(failed)
            self._block("uncertain_provisioning" if created.ambiguous else "herdr_launch_failed", f"{role}:pane",
                        {"code": created.code})

        def record_pane(state):
            self._role(state, role).update(status="pane_created", pane_id=created)
            self._event("pane_created", {"role": role, "pane_id": created})
        self._txn(record_pane)

    # -- agents ----------------------------------------------------------
    def _agent(self, role, workspace, depth=0):
        state = self.coordinator.status()
        record = self._role(state, role)
        pane = self.observer.pane_info(record["pane_id"])
        if pane is None or pane.get("workspace_id") != workspace["workspace_id"]:
            def reset(state):
                if role == "worker" and self.coordinator.inflight(state):
                    return False
                self._reset_role(state, role, "pane_missing")
                return True
            if depth:
                self._block("uncertain_provisioning", f"{role}:agent", {"reason": "new pane not observed"})
            if not self._txn(reset):
                self._block("unit_in_flight", f"{role}:agent")
            self._pane(role, workspace)
            return self._agent(role, workspace, depth + 1)
        info = self.observer.agent_info(record["pane_id"])
        if info is not None:
            if info.get("name") != record["name"]:
                self._block("foreign_agent_in_pane", f"{role}:agent", {"pane_id": record["pane_id"]})
            return self._bind(role, info)
        if record["attempts"] >= MAX_START_ATTEMPTS:
            self._block("start_attempts_exhausted", f"{role}:agent", {"pane_id": record["pane_id"]})

        def intend(state):
            record = self._role(state, role)
            record["status"] = "starting"
            record["attempts"] += 1
            self._event("agent_start_requested", {"role": role, "pane_id": record["pane_id"], "attempt": record["attempts"]})
        self._txn(intend)
        started = self._external(f"{role}:agent", lambda: self.launcher.start_agent(
            record["name"], record["kind"], record["pane_id"], record["args"], START_TIMEOUT_MS))
        if isinstance(started, LaunchError):
            def failed(state):
                record = self._role(state, role)
                record["status"] = "uncertain" if started.ambiguous else "failed"
                record["error"] = started.code
            self._txn(failed)
            self._block("uncertain_provisioning" if started.ambiguous else "role_start_failed", f"{role}:agent",
                        {"code": started.code})
        # Readiness is what Herdr observes in the pane now, not the launch reply.
        info = self.observer.agent_info(record["pane_id"])
        if info is None or info.get("name") != record["name"]:
            def unobserved(state):
                self._role(state, role)["status"] = "uncertain"
            self._txn(unobserved)
            self._block("uncertain_provisioning", f"{role}:agent", {"reason": "started agent not observed"})
        return self._bind(role, info)

    def _bind(self, role, info):
        identity = self.observer.agent_identity(info["pane_id"])
        if identity is None:
            self._block("uncertain_provisioning", f"{role}:agent", {"reason": "no session identity observed"})

        def bind(state):
            record = self._role(state, role)
            current = next((i for i in state["identities"] if i["id"] == role), None)
            if current and {k: current[k] for k in ("pane_id", "session_id")} == identity:
                record["status"] = "ready"
                return
            if role == "worker" and current and self.coordinator.inflight(state):
                return "unit_in_flight"
            bound = {"id": role, **identity, "owner": state["id"], "provisioned": True}
            state["identities"] = [i for i in state["identities"] if i["id"] != role] + [bound]
            record.update(status="ready", error=None)
            record.pop("error")
            self._event("identity_bound", {**bound, "previous_session": current["session_id"] if current else None})
        if self._txn(bind) == "unit_in_flight":
            self._block("unit_in_flight", f"{role}:agent")

    # -- readiness -------------------------------------------------------
    def _finish(self):
        state = self.coordinator.status()
        observed = {}
        for identity in state["identities"]:
            observed[identity["id"]] = self.observer.agent_identity(identity["pane_id"])
        supervisor = next((i for i in state["identities"] if i["id"] == "supervisor"), None)
        if supervisor is None or observed.get("supervisor") != {k: supervisor[k] for k in ("pane_id", "session_id")}:
            self._block("supervisor_stale", "readiness")
        for identity in state["identities"]:
            if observed.get(identity["id"]) != {k: identity[k] for k in ("pane_id", "session_id")}:
                self._block("uncertain_provisioning", "readiness", {"role": identity["id"], "reason": "identity changed during setup"})
        sessions = [i["session_id"] for i in state["identities"]]
        panes = [i["pane_id"] for i in state["identities"]]
        if len(set(sessions)) != len(sessions) or len(set(panes)) != len(panes):
            self._block("duplicate_identity", "readiness")

        def ready(state):
            team = state["team"]
            if any(team["roles"][r]["status"] != "ready" for r in REQUIRED):
                return False
            changed = team["status"] != "ready"
            team.update(status="ready", blocker=None)
            # Identity blockers are resolved by the re-observation above.
            self.coordinator.clear_blockers(state, lambda b: b["code"] in ("stale_identity", "role_missing"))
            team.pop("lease", None)
            if changed:
                self._event("team_ready", {"run_id": state["id"], "roles": {r: team["roles"][r]["pane_id"] for r in REQUIRED}})
            return True
        if not self._txn(ready):
            self._block("uncertain_provisioning", "readiness", {"reason": "a role is not ready"})
        return summary(self.coordinator.status())


def reconcile_team(state, observer):
    """Compare a ready team with live Herdr; returns (code, detail) or None. Pure."""
    if not state.get("team"):
        return None
    for identity in sorted(state["identities"], key=lambda i: i["id"] != "supervisor"):
        observed = observer(identity["pane_id"])
        expected = {k: identity[k] for k in ("pane_id", "session_id")}
        if observed is None:
            return ("supervisor_stale" if identity["id"] == "supervisor" else "role_missing"), {"role": identity["id"]}
        if observed != expected:
            return ("supervisor_stale" if identity["id"] == "supervisor" else "stale_identity"), {"role": identity["id"]}
    return None
