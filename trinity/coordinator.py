"""One-shot transactional admission gate; no agent delivery or timers."""

import copy
import math
import time
from uuid import uuid4

from .clock import accrue, timestamp
from .review import validate_review
from .store import encode


def digest(state):
    from .intake import digest as proposal_digest
    return proposal_digest(state)


RESUMABLE = {"worker_blocked", "completion_mismatch"}
UNSETTLED = ("Unit delivery or settlement is unknown. The worker can settle it with record-completion; "
             "otherwise the user decides with 'resolve-unit UNIT --requeue|--abandon' in their own shell pane.")
STALE_ROLE = "A role's live session no longer matches the run. Run setup again ('/trinity setup'), then resume."
STALE_SUPERVISOR = "The supervisor session is gone. Stop this run and propose a new one from the current session."


class Refused(Exception):
    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class Coordinator:
    def __init__(self, store, now=time.time, guard=None):
        self.store = store
        self.now = now
        self.guard = guard

    def _operation(self, operation, *, initializing=False, configured_allowed=False, stopped_allowed=False):
        error = None
        with self.store.transaction():
            state = self.store.load()
            if self.guard is not None:
                self.guard(state)
            if state is not None and initializing:
                raise Refused("run_already_initialized")
            if state is not None and state["status"] == "stopped" and not stopped_allowed:
                raise Refused("run_stopped")
            if state is not None and state["status"] == "cancelled":
                raise Refused("run_cancelled")
            if state is not None and state["status"] == "configured" and not configured_allowed:
                raise Refused("run_not_started")
            if state is None and not initializing:
                raise Refused("run_not_initialized")
            try:
                result = operation(state)
            except Refused as exc:
                # Barrier detection and uncertainty must survive a refusal.
                error = exc
                result = None
            if state is not None:
                self.store.save(state)
        if error:
            raise error
        return copy.deepcopy(result)

    def _event(self, kind, data):
        self.store.event(kind, data)

    def block(self, state, code, next_action, **detail):
        """Record an actionable blocker durably and move the run to blocked."""
        blockers = state["recovery"].setdefault("blockers", [])
        entry = {"code": code, "next_action": next_action, "at": timestamp(self.now), **detail}
        if not any(b["code"] == code and all(b.get(k) == v for k, v in detail.items()) for b in blockers):
            blockers.append(entry)
            self._event("blocker_recorded", entry)
        if state["status"] not in ("stopped", "blocked"):
            if state["status"] == "paused":
                state["resume_to"] = "blocked"
            else:
                self._state(state, "blocked")

    def clear_blockers(self, state, predicate):
        blockers = state["recovery"].get("blockers", [])
        kept = [b for b in blockers if not predicate(b)]
        for b in blockers:
            if predicate(b):
                self._event("blocker_cleared", {"code": b["code"], **{k: v for k, v in b.items() if k in ("unit_id", "dispatch_id", "role")}})
        state["recovery"]["blockers"] = kept
        return kept

    def transact(self, operation, **flags):
        """One guarded transaction around ``operation(state)`` (used by team setup and dispatch)."""
        return self._operation(operation, **flags)

    def inflight(self, state):
        return self._inflight(state)

    def _state(self, state, target):
        if state["status"] != target:
            previous = state["status"]
            state["status"] = target
            state["active_since"] = timestamp(self.now) if target == "running" else None
            self._event("state_changed", {"from": previous, "to": target})

    def _enter(self, state, target):
        if state["status"] == "paused":
            state["resume_to"] = target
        else:
            self._state(state, target)

    @staticmethod
    def _inflight(state):
        return next((u for u in state["units"] if u["status"] in ("dispatched", "uncertain")), None)

    @staticmethod
    def _checkpoint(state):
        return next((c for c in state["checkpoints"] if c["status"] != "closed"), None)

    def _tick(self, state):
        before = state["active_seconds"]
        try:
            accrue(state, timestamp(self.now))
        except ValueError as error:
            raise Refused("clock_rollback" if str(error) == "clock moved backwards" else "invalid_clock") from error
        if state["active_seconds"] != before:
            self._event("active_time_accrued", {"seconds": state["active_seconds"]})
        if state["status"] == "running" and state["active_seconds"] >= state["next_review_active_seconds"]:
            self._request(state, "scheduled")

    def _request(self, state, reason, caller=None):
        checkpoint = self._checkpoint(state)
        if checkpoint is None:
            checkpoint = {"id": str(uuid4()), "status": "pending", "reason": reason,
                          "active_seconds": state["active_seconds"], "requested_at": timestamp(self.now)}
            if caller is not None:
                checkpoint["caller"] = copy.deepcopy(caller)
            state["checkpoints"].append(checkpoint)
            self._event("checkpoint_requested", checkpoint)
            self._enter(state, "checkpoint_pending")
        self.promote_checkpoint(state)

    def promote_checkpoint(self, state):
        """A pending checkpoint becomes reviewable as soon as no unit is in flight."""
        checkpoint = self._checkpoint(state)
        if checkpoint and checkpoint["status"] == "pending" and self._inflight(state) is None:
            checkpoint["status"] = "awaiting_review"
            if state["status"] != "blocked":
                self._enter(state, "awaiting_review")

    @staticmethod
    def _units(values):
        if not isinstance(values, list) or not values:
            raise Refused("invalid_units")
        result = []
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"].strip():
                raise Refused("invalid_units")
            if value["id"] in [u["id"] for u in result]:
                raise Refused("duplicate_unit_id")
            encode(value)
            result.append({"id": value["id"], "assignment": copy.deepcopy(value), "status": "pending"})
        return result

    def init_run(self, objective, success_criteria, interval_s=300, *, bootstrap_identity=None, configuration=None,
                 repository=None, constraints=None):
        if not isinstance(objective, str) or not objective.strip():
            raise Refused("invalid_objective")
        if not isinstance(success_criteria, list) or any(not isinstance(s, str) for s in success_criteria):
            raise Refused("invalid_success_criteria")
        if type(interval_s) not in (int, float) or not math.isfinite(interval_s) or interval_s <= 0:
            raise Refused("invalid_interval")
        if constraints is not None and (not isinstance(constraints, list) or any(not isinstance(c, str) for c in constraints)):
            raise Refused("invalid_constraints")

        def initialize(_):
            state = {
                "id": str(uuid4()), "status": "configured" if configuration is not None else "planning",
                "configuration": {**copy.deepcopy(configuration or {}), "interval_s": interval_s},
                "project": {"objective": objective, "success_criteria": success_criteria, "constraints": list(constraints or [])},
                "execution": {"current_phase": "planning", "active_work_unit": None},
                "decisions": {"accepted": [], "challenged": [], "unresolved": []},
                "evidence": {"changed_files": [], "tests": [], "artifacts": []},
                "resources": {"model_usage": {}, "subscription_limits": {}},
                "recovery": {"latest_safe_snapshot": None},
                "active_seconds": 0.0, "active_since": None, "next_review_active_seconds": interval_s,
                "units": [], "checkpoints": [], "reviews": [], "steers": [], "identities": [],
            }
            if repository is not None:
                state["project"]["repository"] = repository
                state["proposal"] = {"revision": 1}
                state["proposal"]["sha256"] = digest(state)
            if bootstrap_identity is not None:
                if any(not isinstance(bootstrap_identity.get(k), str) or not bootstrap_identity[k] for k in ("pane_id", "session_id")):
                    raise Refused("invalid_identity")
                state["identities"] = [{"id": "supervisor", "bootstrap": True,
                                        **{k: bootstrap_identity[k] for k in ("pane_id", "session_id")}}]
            self.store.save(state)
            self._event("run_initialized", {"run_id": state["id"], "bootstrap": bootstrap_identity is not None})
            if bootstrap_identity is not None:
                self._event("identity_bound", {**state["identities"][0], "bootstrap": True})
            if configuration is not None:
                self._event("run_configured", {"run_id": state["id"], "configuration": state["configuration"]})
            if "proposal" in state:
                self._event("proposal_recorded", {"run_id": state["id"], **state["proposal"], "project": state["project"]})
            return state
        return self._operation(initialize, initializing=True)

    def bind_identity(self, role, pane_id, session_id):
        if role not in ("worker", "supervisor", "consultant") or not isinstance(pane_id, str) or not pane_id.strip():
            raise Refused("invalid_identity")

        def bind(state):
            if state.get("team") is not None:
                # Provisioned roles are bound only from run-owned observations.
                raise Refused("team_managed_identities")
            if self._checkpoint(state):
                raise Refused("checkpoint_open")
            if self._inflight(state):
                raise Refused("unit_in_flight")
            session = session_id() if callable(session_id) else session_id
            if not isinstance(session, str) or not session.strip():
                raise Refused("invalid_identity")
            identity = {"id": role, "pane_id": pane_id, "session_id": session}
            state["identities"] = [i for i in state["identities"] if i["id"] != role] + [identity]
            self._event("identity_bound", identity)
            return identity
        return self._operation(bind, configured_allowed=True)

    def plan_units(self, values):
        units = self._units(values)

        def plan(state):
            if "proposal" in state:
                raise Refused("use_plan", {"next_action": "Task-aware runs plan with 'plan' (bounded, objective-linked units)."})
            if state["status"] not in ("planning", "active"):
                raise Refused("already_planned")
            state["units"] = units
            state["execution"]["current_phase"] = "execution"
            self._event("units_planned", {"unit_ids": [u["id"] for u in units]})
            self._state(state, "running")
            return state
        return self._operation(plan)

    def _barrier(self, state):
        checkpoint = self._checkpoint(state)
        if checkpoint:
            return {"pending": "checkpoint_pending", "awaiting_review": "review_required",
                    "awaiting_steer": "steer_required", "escalated": "user_approval_required"}[checkpoint["status"]]
        return None

    def admit_next_unit(self, identity_observation):
        return self._operation(lambda state: self._admit(state, identity_observation))

    def _admit(self, state, identity_observation):
        self._tick(state)
        if state["status"] == "paused":
            raise Refused("paused")
        if state["status"] == "blocked":
            raise Refused("blocked")
        barrier = self._barrier(state)
        if barrier:
            raise Refused(barrier)
        if state["status"] != "running":
            raise Refused(state["status"])
        if self._inflight(state):
            raise Refused("unit_in_flight")
        if not any(i["id"] == "worker" for i in state["identities"]):
            raise Refused("worker_unbound")
        observation = identity_observation(state) if callable(identity_observation) else identity_observation
        if not isinstance(observation, dict):
            raise Refused("stale_identity")
        for identity in state["identities"]:
            expected = {k: identity[k] for k in ("pane_id", "session_id")}
            if observation.get(identity["id"]) != expected:
                raise Refused("stale_identity")
        unit = next((u for u in state["units"] if u["status"] == "pending"), None)
        if unit is None:
            raise Refused("no_pending_units")
        unit["status"] = "dispatched"  # The only dispatch mutation in the core.
        state["execution"]["active_work_unit"] = unit["id"]
        self._event("unit_dispatched", {"unit_id": unit["id"], "identities": state["identities"]})
        return unit

    def record_completion(self, unit_id, report, settled):
        if not isinstance(report, dict) or type(settled) is not bool:
            raise Refused("invalid_completion")
        encode(report)

        def complete(state):
            old = next((u for u in state["units"] if u["id"] == unit_id and u["status"] == "completed"), None)
            if old:
                if settled and old["report"] == report:
                    return old
                raise Refused("conflicting_duplicate_completion")
            self._tick(state)
            unit = self._inflight(state)
            if unit is None or unit["id"] != unit_id:
                if unit:
                    unit["status"] = "uncertain"
                self._event("completion_rejected", {"unit_id": unit_id})
                self.block(state, "completion_mismatch", UNSETTLED, unit_id=unit["id"] if unit else unit_id)
                raise Refused("completion_mismatch")
            unit["report"] = copy.deepcopy(report)
            unit["settled"] = settled
            unit["status"] = "completed" if settled else "uncertain"
            self._event("unit_completion", {"unit_id": unit_id, "report": report, "settled": settled})
            record = next((d for d in state.get("dispatches", []) if d["id"] == unit.get("dispatch_id")), None)
            if record is not None and record["status"] in ("sending", "sent", "uncertain"):
                # A completion report from the bound worker proves the assignment arrived.
                record["status"] = "acknowledged"
            if settled:
                before = len(state["recovery"].get("blockers", []))
                remaining = self.clear_blockers(state, lambda b: b.get("unit_id") == unit_id and b["code"] in (
                    "dispatch_uncertain", "unit_uncertain", "unit_unsettled", "completion_mismatch"))
                if before != len(remaining) and not remaining and state["status"] == "blocked":
                    self._state(state, "running")
            if not settled:
                self.block(state, "unit_unsettled", UNSETTLED, unit_id=unit_id)
                return unit
            state["execution"]["active_work_unit"] = None
            state["recovery"]["latest_safe_snapshot"] = {"unit_id": unit_id, "active_seconds": state["active_seconds"]}
            checkpoint = self._checkpoint(state)
            if checkpoint and checkpoint["status"] == "pending":
                self._request(state, checkpoint["reason"])
            elif not any(u["status"] == "pending" for u in state["units"]):
                self._enter(state, "completed")
            return unit
        return self._operation(complete)

    def request_checkpoint(self, reason, *, caller=None):
        if not isinstance(reason, str) or not reason.strip():
            raise Refused("invalid_checkpoint_reason")

        def request(state):
            if state["status"] in ("planning", "completed", "blocked", "escalated"):
                raise Refused(state["status"])
            self._tick(state)
            if caller is not None:
                self._event("checkpoint_request_received", {"reason": reason, "caller": caller})
            self._request(state, reason, caller)
            return self._checkpoint(state)
        return self._operation(request)

    def _ready(self, state, checkpoint):
        checkpoint["status"] = "closed"
        state["next_review_active_seconds"] = state["active_seconds"] + state["configuration"]["interval_s"]
        self._event("checkpoint_closed", {"checkpoint_id": checkpoint["id"]})
        if state["recovery"].get("blockers"):
            self._enter(state, "blocked")  # a durable blocker still needs its own resolution
        else:
            self._enter(state, "running" if any(u["status"] == "pending" for u in state["units"]) else "completed")

    def record_review(self, decision_json=None, *, provenance=None, transcript_source=None, identity_observer=None):
        def record(state):
            if self._inflight(state):
                raise Refused("unit_in_flight")
            checkpoint = self._checkpoint(state)
            if not checkpoint or checkpoint["status"] != "awaiting_review":
                raise Refused("no_review_pending")
            raw_text = decision_json
            if transcript_source is not None:
                cutoff = checkpoint.get("requested_at")
                if cutoff is None:
                    raise Refused("checkpoint_time_missing")
                consultant = next((i for i in state["identities"] if i["id"] == "consultant"), None)
                if consultant is None:
                    raise Refused("consultant_unbound")
                expected = {k: consultant[k] for k in ("pane_id", "session_id")}
                observed = identity_observer(consultant["pane_id"]) if identity_observer else None
                if observed is None:
                    raise Refused("unknown_identity")
                if observed != expected:
                    raise Refused("stale_identity")
                selected = transcript_source.select(consultant["session_id"], after=cutoff)
                raw_text = selected["text"]
                source = {**expected, "channel": "transcript", **{k: selected[k] for k in (
                    "transcript_path", "transcript_line", "sha256", "message_sha256")}}
            else:
                source = provenance(state) if callable(provenance) else provenance
            try:
                decision = validate_review(raw_text)
            except (ValueError, TypeError):
                self._event("review_rejected", {"checkpoint_id": checkpoint["id"]})
                raise Refused("invalid_review")
            item = {"id": checkpoint["id"], "decision": copy.deepcopy(decision)}
            if source is not None:
                item["provenance"] = copy.deepcopy(source)
            if isinstance(raw_text, str):
                item["raw_text"] = raw_text
            state["reviews"].append(item)
            state["decisions"]["accepted"].append(copy.deepcopy(decision))
            self._event("review_recorded", {"checkpoint_id": checkpoint["id"], **item})
            if decision["decision"] == "ESCALATE" or decision["requires_user_approval"]:
                checkpoint["status"] = "escalated"
                self._enter(state, "escalated")
            elif decision["decision"] in ("STEER", "REPLAN") or decision["requires_supervisor_action"]:
                checkpoint["status"] = "awaiting_steer"
                self._enter(state, "awaiting_steer")
            else:
                self._ready(state, checkpoint)
            return decision
        return self._operation(record)

    def record_steer(self, text, new_units=None):
        if not isinstance(text, str) or not text.strip():
            raise Refused("invalid_steer")
        units = self._units(new_units) if new_units is not None else None

        def steer(state):
            checkpoint = self._checkpoint(state)
            if not checkpoint or checkpoint["status"] != "awaiting_steer":
                raise Refused("no_steer_pending")
            if units is not None:
                if set(u["id"] for u in units) & set(u["id"] for u in state["units"]):
                    raise Refused("duplicate_unit_id")
                for old in state["units"]:
                    if old["status"] == "pending":
                        old["status"] = "cancelled"
                state["units"].extend(units)
            item = {"id": checkpoint["id"], "text": text}
            state["steers"].append(item)
            self._event("steer_recorded", item)
            self._ready(state, checkpoint)
            return item
        return self._operation(steer)

    def pause(self):
        def pause_run(state):
            if state["status"] == "paused":
                return state
            if state["status"] in ("planning", "completed", "blocked"):
                raise Refused(state["status"])
            self._tick(state)
            state["resume_to"] = state["status"]
            self._state(state, "paused")
            return state
        return self._operation(pause_run)

    def resume(self, *, user_approved=False, identity_observation=None):
        if type(user_approved) is not bool:
            raise Refused("invalid_user_approval")

        def resume_run(state):
            unit = self._inflight(state)
            if unit and unit["status"] == "uncertain":
                raise Refused("unsettled_unit", {"unit_id": unit["id"], "next_action": UNSETTLED})
            if identity_observation is not None and state["status"] != "completed":
                observation = identity_observation(state) if callable(identity_observation) else identity_observation
                stale = [i["id"] for i in state["identities"]
                         if (observation or {}).get(i["id"]) != {k: i[k] for k in ("pane_id", "session_id")}]
                if stale:
                    action = STALE_SUPERVISOR if "supervisor" in stale else STALE_ROLE
                    team = state.get("team")
                    if team is not None:
                        team["status"] = "blocked"
                        team["blocker"] = {"code": "stale_identity", "step": "resume", "detail": {"roles": stale},
                                           "next_action": action, "at": timestamp(self.now)}
                    self.block(state, "stale_identity", action, role=stale[0])
                    raise Refused("stale_identity", {"roles": stale, "next_action": action})
            hard = [b for b in state["recovery"].get("blockers", []) if b["code"] not in RESUMABLE]
            if hard:
                raise Refused(hard[0]["code"], {"next_action": hard[0]["next_action"]})
            barrier = self._barrier(state)
            if barrier:
                if barrier == "user_approval_required" and user_approved:
                    checkpoint = self._checkpoint(state)
                    decision = next(r["decision"] for r in state["reviews"] if r["id"] == checkpoint["id"])
                    self._event("user_resume_approved", {"checkpoint_id": checkpoint["id"]})
                    if decision["decision"] in ("STEER", "REPLAN") or decision["requires_supervisor_action"]:
                        checkpoint["status"] = "awaiting_steer"
                        self._enter(state, "awaiting_steer")
                        raise Refused("steer_required")
                    self._ready(state, checkpoint)
                else:
                    raise Refused(barrier)
            if state["status"] == "paused":
                self._state(state, state.pop("resume_to"))
            if state["status"] == "blocked":
                self.clear_blockers(state, lambda b: b["code"] in RESUMABLE)
                self._state(state, "running")
            elif state["status"] not in ("running", "completed"):
                raise Refused(state["status"])
            self._tick(state)
            barrier = self._barrier(state)
            if barrier:
                raise Refused(barrier)
            return state
        return self._operation(resume_run)

    def status(self):
        # A pure persisted snapshot; status never advances the execution clock.
        state = self.store.load()
        if state is None:
            raise Refused("run_not_initialized")
        return copy.deepcopy(state)

    def start(self, *, approval_source=None, proposal_revision=None, approver=None):
        if approval_source not in ("invocation", "user_shell"):
            raise Refused("invalid_approval_source" if approval_source is not None else "user_approval_required")

        def start_run(state):
            if state["status"] != "configured":
                raise Refused("run_already_started")
            proposal = state.get("proposal")
            if proposal is not None:
                if proposal_revision is None:
                    raise Refused("proposal_revision_required", {"current_revision": proposal["revision"]})
                if proposal_revision != proposal["revision"]:
                    raise Refused("proposal_changed", {"current_revision": proposal["revision"]})
            approval = {"at": timestamp(self.now), "by": copy.deepcopy(approver), "source": approval_source}
            if proposal is not None:
                approval.update(revision=proposal["revision"], sha256=proposal["sha256"])
            state["approval"] = approval
            self._event("run_approved", {"run_id": state["id"], **approval})
            self._state(state, "active")
            self._event("run_started", {"run_id": state["id"]})
            return state
        return self._operation(start_run, configured_allowed=True)

    def adjust(self, *, objective=None, success_criteria=None, constraints=None, reconfigure=None, validate=None):
        def adjust_run(state):
            if state["status"] != "configured":
                raise Refused("proposal_locked")
            if "proposal" not in state:
                raise Refused("no_proposal")
            project = state["project"]
            changed = {}
            for key, value in (("objective", objective), ("success_criteria", success_criteria), ("constraints", constraints)):
                if value is not None and value != project.get(key):
                    changed[key] = value
            if reconfigure is not None:
                current = {k: v for k, v in state["configuration"].items() if k not in ("interval_s",)}
                configuration = reconfigure(copy.deepcopy(current))
                interval = configuration["checkpoints"]["interval_seconds"]
                new = {**configuration, "interval_s": interval}
                if new != state["configuration"]:
                    changed["configuration"] = new
            if validate is not None:
                validate({**project, **{k: v for k, v in changed.items() if k != "configuration"}})
            if not changed:
                raise Refused("no_change")
            for key in ("objective", "success_criteria", "constraints"):
                if key in changed:
                    project[key] = changed[key]
            if "configuration" in changed:
                state["configuration"] = changed["configuration"]
                state["next_review_active_seconds"] = changed["configuration"]["interval_s"]
            state["proposal"]["revision"] += 1
            state["proposal"]["sha256"] = digest(state)
            self._event("proposal_adjusted", {"run_id": state["id"], "changed": sorted(changed), **state["proposal"]})
            return state
        return self._operation(adjust_run, configured_allowed=True)

    def cancel(self):
        def cancel_run(state):
            if state["status"] != "configured":
                raise Refused("not_cancellable")
            self._state(state, "cancelled")
            self._event("proposal_cancelled", {"run_id": state["id"]})
            return state
        return self._operation(cancel_run, configured_allowed=True)

    def stop(self):
        def stop_run(state):
            if state["status"] != "stopped":
                self._tick(state)
                unit = self._inflight(state)
                if unit:
                    unit["status"] = "uncertain"
                self._state(state, "stopped")
                self._event("run_stopped", {"run_id": state["id"], "unit_id": unit["id"] if unit else None})
            return state
        return self._operation(stop_run, configured_allowed=True, stopped_allowed=True)

    def history(self):
        state = self.status()
        return {key: state[key] for key in ("reviews", "decisions", "steers")}

    def configuration(self):
        result = self.status()["configuration"]
        if "version" in result:
            result.pop("interval_s", None)
        return result

    def transcript_preview(self, source, after=None):
        state = self.status()
        consultant = next((i for i in state["identities"] if i["id"] == "consultant"), None)
        if consultant is None:
            raise Refused("consultant_unbound")
        return source.select(consultant["session_id"], after=after)
