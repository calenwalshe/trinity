import pytest

from trinity.store import Store

from trinity.coordinator import Coordinator, Refused


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


IDENTITY = {"worker": {"pane_id": "w1:p1", "session_id": "worker-1"}}


def review(decision="CONTINUE", **changes):
    result = {
        "decision": decision, "assessment": "Evidence checked",
        "concerns": [], "recommendations": [],
        "requires_supervisor_action": decision in {"STEER", "REPLAN"},
        "requires_user_approval": decision == "ESCALATE",
        "suggested_next_review_seconds": 300,
    }
    return result | changes


@pytest.fixture
def core(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "run.sqlite3")
    coordinator = Coordinator(store, now=clock)
    coordinator.init_run("Build an app", ["Checks pass"], interval_s=10)
    coordinator.bind_identity("worker", "w1:p1", "worker-1")
    coordinator.plan_units([{"id": "u1", "task": "Build"}, {"id": "u2", "task": "Check"}])
    yield coordinator, clock, store
    store.close()


def refuse(reason, operation):
    with pytest.raises(Refused) as error:
        operation()
    assert error.value.reason == reason


def test_bootstrap_binds_supervisor_and_events_atomically(tmp_path):
    store = Store(tmp_path / "bootstrap.sqlite3")
    c = Coordinator(store, now=Clock())
    caller = {"pane_id": "w1:p9", "session_id": "supervisor-1"}
    state = c.init_run("Build", [], bootstrap_identity=caller)
    assert state["identities"] == [{"id": "supervisor", "bootstrap": True, **caller}]
    events = store.events()
    assert [e["kind"] for e in events] == ["run_initialized", "identity_bound"]
    assert all(e["data"]["bootstrap"] is True for e in events)
    store.close()


def test_start_records_invocation_approval(tmp_path):
    store = Store(tmp_path / "invocation.sqlite3")
    c = Coordinator(store, now=Clock())
    c.init_run("Build", ["Checks pass"], configuration={}, repository="/repo")
    state = c.start(approval_source="invocation", proposal_revision=1)
    assert state["status"] == "active"
    assert state["approval"]["source"] == "invocation"
    assert state["approval"]["revision"] == 1
    assert state["approval"]["sha256"] == state["proposal"]["sha256"]
    assert [e["kind"] for e in store.events()].count("run_approved") == 1
    store.close()


def test_start_rejects_unknown_approval_source(tmp_path):
    store = Store(tmp_path / "invalid-approval.sqlite3")
    c = Coordinator(store, now=Clock())
    c.init_run("Build", [], configuration={})
    refuse("invalid_approval_source", lambda: c.start(approval_source="system"))
    store.close()


def test_authority_guard_runs_under_transaction_before_state_changes(core):
    c, _, store = core
    before, ledger = c.status(), store.events()

    def deny(state):
        assert store.connection.in_transaction
        assert state == before
        raise Refused("authority_denied")

    gated = Coordinator(store, guard=deny)
    refuse("authority_denied", lambda: gated.pause())
    assert c.status() == before and store.events() == ledger


def test_binding_during_checkpoint_cannot_launder_identity(core):
    c, _, store = core
    c.request_checkpoint("architecture")
    before, ledger = c.status(), store.events()
    refuse("checkpoint_open", lambda: c.bind_identity("worker", "w1:p8", "new"))
    assert c.status() == before and store.events() == ledger


def test_review_provenance_survives_restart_and_matches_ledger(core, tmp_path):
    c, _, store = core
    c.request_checkpoint("milestone", caller={"role": "worker", "pane_id": "w1:p1"})
    provenance = {"pane_id": "w1:p3", "session_id": "consultant-1", "sha256": "a" * 64}
    c.record_review(review(), provenance=provenance)
    assert c.status()["reviews"][0]["provenance"] == provenance
    assert next(e for e in store.events() if e["kind"] == "review_recorded")["data"]["provenance"] == provenance
    assert next(e for e in store.events() if e["kind"] == "checkpoint_requested")["data"]["caller"]["role"] == "worker"
    store.close()
    reopened = Store(tmp_path / "run.sqlite3")
    assert Coordinator(reopened).status()["reviews"][0]["provenance"] == provenance
    reopened.close()


def test_new_checkpoint_records_injected_request_epoch(core):
    c, clock, store = core
    clock.advance(4)
    checkpoint = c.request_checkpoint("milestone")
    assert checkpoint["requested_at"] == 4
    event = next(e for e in store.events() if e["kind"] == "checkpoint_requested")
    assert event["data"]["requested_at"] == 4


def test_admission_is_exclusive_and_completion_finishes_plan(core):
    c, _, _ = core
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"
    refuse("unit_in_flight", lambda: c.admit_next_unit(IDENTITY))
    c.record_completion("u1", {"tests": ["pass"]}, settled=True)
    assert c.admit_next_unit(IDENTITY)["id"] == "u2"
    c.record_completion("u2", {"artifact": "done"}, settled=True)
    assert c.status()["status"] == "completed"


def test_deadline_in_flight_refuses_second_unit_until_settled_review(core):
    c, clock, _ = core
    c.admit_next_unit(IDENTITY)
    clock.advance(11)
    refuse("checkpoint_pending", lambda: c.admit_next_unit(IDENTITY))
    assert c.status()["status"] == "checkpoint_pending"
    refuse("unit_in_flight", lambda: c.record_review(review()))
    c.record_completion("u1", {"tests": ["pass"]}, settled=True)
    assert c.status()["status"] == "awaiting_review"
    refuse("review_required", lambda: c.admit_next_unit(IDENTITY))
    c.record_review(review())
    assert c.admit_next_unit(IDENTITY)["id"] == "u2"


def test_due_time_rechecked_at_admission_without_timer(core):
    c, clock, _ = core
    clock.advance(10)
    refuse("review_required", lambda: c.admit_next_unit(IDENTITY))
    assert c.status()["active_seconds"] == 10
    assert c.status()["status"] == "awaiting_review"


def test_completion_detects_deadline_without_an_admission_attempt(core):
    c, clock, _ = core
    c.admit_next_unit(IDENTITY)
    clock.advance(10)
    c.record_completion("u1", {}, settled=True)
    assert c.status()["status"] == "awaiting_review"


def test_paused_and_review_intervals_do_not_accrue_active_time(core):
    c, clock, _ = core
    clock.advance(3)
    c.pause()
    clock.advance(1000)
    c.resume()
    assert c.status()["active_seconds"] == 3
    clock.advance(7)
    refuse("review_required", lambda: c.admit_next_unit(IDENTITY))
    clock.advance(1000)
    c.record_review(review())
    assert c.status()["active_seconds"] == 10
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"


def test_duplicate_completion_has_no_state_or_ledger_effect(core):
    c, _, store = core
    c.admit_next_unit(IDENTITY)
    c.record_completion("u1", {"tests": ["pass"]}, settled=True)
    before, events = c.status(), store.events()
    c.record_completion("u1", {"tests": ["pass"]}, settled=True)
    assert c.status() == before
    assert store.events() == events


def test_wrong_unit_completion_blocks_current_unit(core):
    c, _, _ = core
    c.admit_next_unit(IDENTITY)
    refuse("completion_mismatch", lambda: c.record_completion("u2", {}, settled=True))
    assert c.status()["units"][0]["status"] == "uncertain"
    refuse("blocked", lambda: c.admit_next_unit(IDENTITY))


def test_unsettled_background_work_cannot_resume_or_admit(core):
    c, _, _ = core
    c.admit_next_unit(IDENTITY)
    c.record_completion("u1", {"background": "still running"}, settled=False)
    assert c.status()["units"][0]["status"] == "uncertain"
    refuse("unsettled_unit", c.resume)
    refuse("blocked", lambda: c.admit_next_unit(IDENTITY))


@pytest.mark.parametrize("bad", ["{", {"decision": "UNKNOWN"}, review("UNKNOWN"),
                                   review(assessment=12), review(requires_user_approval="false"),
                                   review(suggested_next_review_seconds=True)])
def test_invalid_review_blocks_resume(core, bad):
    c, _, _ = core
    c.request_checkpoint("milestone")
    refuse("invalid_review", lambda: c.record_review(bad))
    refuse("review_required", c.resume)
    refuse("review_required", lambda: c.admit_next_unit(IDENTITY))


def test_missing_review_blocks_resume_even_after_pause(core):
    c, _, _ = core
    c.request_checkpoint("milestone")
    c.pause()
    refuse("review_required", c.resume)
    assert c.status()["status"] == "paused"


@pytest.mark.parametrize("decision", ["STEER", "REPLAN"])
def test_steer_decisions_require_recorded_supervisor_steer(core, decision):
    c, _, _ = core
    c.request_checkpoint("architecture")
    c.record_review(review(decision))
    refuse("steer_required", c.resume)
    refuse("steer_required", lambda: c.admit_next_unit(IDENTITY))
    c.record_steer("Simplify the approach")
    assert c.status()["status"] == "running"
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"


def test_escalation_requires_explicit_user_resume(core):
    c, _, _ = core
    c.request_checkpoint("scope")
    c.record_review(review("ESCALATE"))
    refuse("user_approval_required", c.resume)
    refuse("user_approval_required", lambda: c.admit_next_unit(IDENTITY))
    c.resume(user_approved=True)
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"


def test_stale_identity_refuses_dispatch(core):
    c, _, _ = core
    refuse("stale_identity", lambda: c.admit_next_unit({"worker": {"pane_id": "w1:p1", "session_id": "replacement"}}))
    assert all(unit["status"] == "pending" for unit in c.status()["units"])


def test_pause_in_flight_allows_completion_but_no_more_dispatch(core):
    c, clock, _ = core
    c.admit_next_unit(IDENTITY)
    clock.advance(2)
    c.pause()
    c.record_completion("u1", {}, settled=True)
    refuse("paused", lambda: c.admit_next_unit(IDENTITY))
    assert c.status()["status"] == "paused"
    c.resume()
    assert c.admit_next_unit(IDENTITY)["id"] == "u2"


def test_reopen_preserves_identical_status_and_ledger(core, tmp_path):
    c, clock, store = core
    c.admit_next_unit(IDENTITY)
    clock.advance(3)
    c.record_completion("u1", {}, settled=False)
    before, events = c.status(), store.events()
    store.close()
    second = Store(tmp_path / "run.sqlite3")
    restored = Coordinator(second, now=clock)
    assert restored.status() == before
    assert second.events() == events
    refuse("unsettled_unit", restored.resume)
    second.close()


def test_plan_replacement_preserves_completed_units(core):
    c, _, _ = core
    c.admit_next_unit(IDENTITY)
    c.record_completion("u1", {}, settled=True)
    c.request_checkpoint("simplify")
    c.record_review(review("STEER"))
    c.record_steer("Use a single check", new_units=[{"id": "u3", "task": "New check"}])
    assert c.admit_next_unit(IDENTITY)["id"] == "u3"
    assert c.status()["units"][0]["status"] == "completed"


def test_pause_does_not_bypass_due_checkpoint(core):
    c, clock, _ = core
    clock.advance(10)
    c.pause()
    refuse("review_required", c.resume)
    c.record_review(review())
    assert c.status()["status"] == "paused"
    c.resume()
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"


def test_user_resume_does_not_bypass_required_supervisor_steer(core):
    c, _, _ = core
    c.request_checkpoint("scope and architecture")
    c.record_review(review("STEER", requires_user_approval=True))
    refuse("steer_required", lambda: c.resume(user_approved=True))
    c.record_steer("Approved bounded approach")
    assert c.admit_next_unit(IDENTITY)["id"] == "u1"


def test_clock_rollback_refuses_admission_without_mutating_state(core):
    c, clock, store = core
    clock.advance(3)
    c.admit_next_unit(IDENTITY)
    before, events = c.status(), store.events()
    clock.value = 1
    refuse("clock_rollback", lambda: c.admit_next_unit(IDENTITY))
    assert c.status() == before
    assert store.events() == events


def test_concurrent_admission_dispatches_exactly_one_unit(core, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    _, clock, store = core

    def attempt(_):
        connection = Store(tmp_path / "run.sqlite3")
        try:
            return Coordinator(connection, now=clock).admit_next_unit(IDENTITY)["id"]
        except Refused as error:
            return error.reason
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results) == ["u1", "unit_in_flight"]
    assert sum(e["kind"] == "unit_dispatched" for e in store.events()) == 1


def test_bound_consultant_identity_is_checked_too(core):
    c, _, _ = core
    c.bind_identity("consultant", "w1:p3", "consultant-1")
    refuse("stale_identity", lambda: c.admit_next_unit(IDENTITY))
    observation = IDENTITY | {"consultant": {"pane_id": "w1:p3", "session_id": "consultant-1"}}
    assert c.admit_next_unit(observation)["id"] == "u1"


def test_review_flags_cannot_weaken_the_decision_gate(core):
    c, _, _ = core
    c.request_checkpoint("architecture")
    c.record_review(review("REPLAN", requires_supervisor_action=False))
    refuse("steer_required", c.resume)


def test_rebinding_worker_cannot_replace_inflight_owner(core):
    c, _, _ = core
    c.admit_next_unit(IDENTITY)
    refuse("unit_in_flight", lambda: c.bind_identity("worker", "w1:p1", "replacement"))


def test_reopen_running_clock_checks_persisted_deadline(core, tmp_path):
    c, clock, _ = core
    clock.advance(4)
    c.admit_next_unit(IDENTITY)
    c.record_completion("u1", {}, settled=True)
    connection = Store(tmp_path / "run.sqlite3")
    restored = Coordinator(connection, now=clock)
    clock.advance(6)
    refuse("review_required", lambda: restored.admit_next_unit(IDENTITY))
    assert restored.status()["active_seconds"] == 10
    connection.close()


def test_duplicate_checkpoint_does_not_create_multiple_reviews(core):
    c, _, _ = core
    c.request_checkpoint("milestone")
    c.request_checkpoint("duplicate event")
    assert len(c.status()["checkpoints"]) == 1
    c.record_review(review())
    refuse("no_review_pending", lambda: c.record_review(review()))


def test_final_unit_due_checkpoint_requires_review_before_completion(core):
    c, clock, _ = core
    c.admit_next_unit(IDENTITY)
    c.record_completion("u1", {}, settled=True)
    c.admit_next_unit(IDENTITY)
    clock.advance(10)
    c.record_completion("u2", {}, settled=True)
    assert c.status()["status"] == "awaiting_review"
    c.record_review(review())
    assert c.status()["status"] == "completed"
