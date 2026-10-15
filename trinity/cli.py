"""Single-operation JSON interface to the offline coordinator."""

import argparse
import hashlib
import json
import os
import sqlite3

from .coordinator import Coordinator, Refused
from .authority import authorize, role_for
from .herdr import Herdr, HerdrLauncher
from .team import Provisioner, summary as team_summary
from . import recovery, work
from .store import Store
from .transcript import CodexTranscripts
from .config import adjust_config, resolve_config, render_summary
from .intake import QUESTIONS, missing_context, proposal, render_proposal, repository_root
from .launch import launch_run


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    root = Parser(prog="trinity")
    root.add_argument("--db", required=True)
    root.add_argument("--transcript-root")
    commands = root.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("init")
    configure.add_argument("objective")
    configure.add_argument("--preset", default="balanced")
    configure.add_argument("--config", dest="config_file")
    configure.add_argument("--success-criteria", default="[]")
    for role in ("worker", "supervisor", "consultant"):
        configure.add_argument("--" + role)
    configure.add_argument("--interval-s", type=float)
    configure.add_argument("--json", action="store_true")
    propose = commands.add_parser("propose", help="record a task-aware run proposal; launches nothing")
    propose.add_argument("objective", nargs="?")
    propose.add_argument("--repo", help="repository the team works in (default: git checkout of the current directory)")
    propose.add_argument("--criterion", action="append", default=[], help="success criterion; repeat for more")
    propose.add_argument("--constraint", action="append", default=[], help="constraint; repeat for more")
    propose.add_argument("--preset", default="balanced")
    propose.add_argument("--config", dest="config_file")
    for role in ("worker", "supervisor", "consultant"):
        propose.add_argument("--" + role)
    propose.add_argument("--interval-s", type=float)
    propose.add_argument("--json", action="store_true")
    launch = commands.add_parser("launch", help="launch the team for an explicit Trinity invocation")
    launch.add_argument("objective", nargs="?")
    launch.add_argument("--repo", help="repository the team works in (default: git checkout of the current directory)")
    launch.add_argument("--criterion", action="append", default=[], help="success criterion; repeat for more")
    launch.add_argument("--constraint", action="append", default=[], help="constraint; repeat for more")
    launch.add_argument("--preset", default="balanced")
    launch.add_argument("--config", dest="config_file")
    for role in ("worker", "supervisor", "consultant"):
        launch.add_argument("--" + role)
    launch.add_argument("--interval-s", type=float)
    launch.add_argument("--json", action="store_true")
    show = commands.add_parser("proposal", help="show the run proposal (read-only)")
    show.add_argument("--json", action="store_true")
    adjust = commands.add_parser("adjust", help="change a not-yet-approved proposal; bumps its revision")
    adjust.add_argument("--objective")
    adjust.add_argument("--criterion", action="append", help="replaces all success criteria; repeat for more")
    adjust.add_argument("--constraint", action="append", help="replaces all constraints; repeat for more")
    adjust.add_argument("--no-constraints", action="store_true")
    adjust.add_argument("--preset")
    for role in ("worker", "supervisor", "consultant"):
        adjust.add_argument("--" + role)
    adjust.add_argument("--interval-s", type=float)
    adjust.add_argument("--json", action="store_true")
    commands.add_parser("cancel", help="close a proposal before approval; launches nothing")
    start = commands.add_parser("start", help="user approval: run from your own shell pane")
    start.add_argument("--user-approved", action="store_true")
    start.add_argument("--proposal", type=int, dest="proposal_revision", help="the proposal revision you reviewed")
    commands.add_parser("provision", help="create or reconcile this run's three role sessions")
    commands.add_parser("team", help="show team setup state (read-only)")
    planner = commands.add_parser("plan", help="supervisor: record bounded units for the approved objective")
    planner.add_argument("--units", help="optional JSON list of {id, task, evidence, boundaries}; default: one unit from the proposal")
    commands.add_parser("dispatch", help="supervisor: send the next unit to the worker exactly once")
    ack = commands.add_parser("ack", help="worker: acknowledge the dispatched unit")
    ack.add_argument("unit_id")
    blocker = commands.add_parser("report-blocker", help="worker: report that the unit cannot proceed")
    blocker.add_argument("unit_id")
    blocker.add_argument("text")
    commands.add_parser("reconcile", help="after a restart or reconnect: compare the run with live Herdr; sends nothing")
    resolve = commands.add_parser("resolve-unit", help="user only: decide an uncertain unit")
    resolve.add_argument("unit_id")
    decision = resolve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--requeue", dest="decision", action="store_const", const="requeue")
    decision.add_argument("--abandon", dest="decision", action="store_const", const="abandon")
    commands.add_parser("controls", help="what each control does and who may use it now (read-only)")
    progress = commands.add_parser("progress", help="what is running, what it is doing, and what happens next (read-only)")
    progress.add_argument("--json", action="store_true")
    commands.add_parser("stop")
    commands.add_parser("history")
    commands.add_parser("config")
    init = commands.add_parser("init-run")
    init.add_argument("objective")
    init.add_argument("--success-criteria", default="[]")
    init.add_argument("--interval-s", type=float, default=300)
    bind = commands.add_parser("bind-identity")
    bind.add_argument("role", choices=("worker", "supervisor", "consultant"))
    bind.add_argument("pane_id")
    plan = commands.add_parser("plan-units")
    plan.add_argument("units")
    commands.add_parser("admit-next-unit")
    complete = commands.add_parser("record-completion")
    complete.add_argument("unit_id")
    complete.add_argument("report")
    settlement = complete.add_mutually_exclusive_group(required=True)
    settlement.add_argument("--settled", dest="settled", action="store_true")
    settlement.add_argument("--unsettled", dest="settled", action="store_false")
    checkpoint = commands.add_parser("request-checkpoint", aliases=["review"])
    checkpoint.add_argument("reason")
    review = commands.add_parser("record-review")
    review.add_argument("decision_json", nargs="?")
    review.add_argument("--from-transcript", action="store_true")
    steer = commands.add_parser("record-steer", aliases=["steer"])
    steer.add_argument("text")
    steer.add_argument("--new-units")
    commands.add_parser("pause")
    resume = commands.add_parser("resume")
    resume.add_argument("--user-approved", action="store_true")
    commands.add_parser("status")
    commands.add_parser("whoami")
    preview = commands.add_parser("transcript-preview")
    preview.add_argument("--after", type=float)
    return root


def dispatch(coordinator, args, adapter, caller, launcher=None):
    def configure():
        configuration = resolve_config(args.preset, file=args.config_file, interval_s=args.interval_s,
                                       overrides={r: getattr(args, r) for r in ("worker", "supervisor", "consultant") if getattr(args, r) is not None})
        return coordinator.init_run(args.objective, json.loads(args.success_criteria),
                                    configuration["checkpoints"]["interval_seconds"],
                                    bootstrap_identity=caller, configuration=configuration)
    def target_session():
        identity = adapter.agent_identity(args.pane_id)
        if identity is None:
            raise Refused("unknown_identity")
        return identity["session_id"]

    def observations(state):
        result = {}
        for identity in state["identities"]:
            observed = adapter.agent_identity(identity["pane_id"])
            if observed is None:
                raise Refused("unknown_identity")
            result[identity["id"]] = observed
        return result

    def provenance(state):
        consultant = next((i for i in state["identities"] if i["id"] == "consultant"), None)
        if consultant is None:
            raise Refused("consultant_unbound")
        observed = adapter.agent_identity(consultant["pane_id"])
        if observed is None:
            raise Refused("unknown_identity")
        if any(observed[k] != consultant[k] for k in ("pane_id", "session_id")):
            raise Refused("stale_identity")
        return {**observed, "channel": "relay", "sha256": hashlib.sha256(args.decision_json.encode("utf-8")).hexdigest()}

    def record_review():
        if args.from_transcript:
            return coordinator.record_review(transcript_source=CodexTranscripts(args.transcript_root), identity_observer=adapter.agent_identity)
        return coordinator.record_review(args.decision_json, provenance=provenance)

    def propose():
        configuration = resolve_config(args.preset, file=args.config_file, interval_s=args.interval_s,
                                       overrides={r: getattr(args, r) for r in ("worker", "supervisor", "consultant") if getattr(args, r) is not None})
        return coordinator.init_run(args.objective.strip(), [c.strip() for c in args.criterion],
                                    configuration["checkpoints"]["interval_seconds"], bootstrap_identity=caller,
                                    configuration=configuration, repository=repository_root(args.repo),
                                    constraints=[c.strip() for c in args.constraint])

    def launch_team():
        configuration = resolve_config(args.preset, file=args.config_file, interval_s=args.interval_s,
                                       overrides={r: getattr(args, r) for r in ("worker", "supervisor", "consultant") if getattr(args, r) is not None})
        return launch_run(coordinator, launcher, adapter, {
            "objective": args.objective, "repository": repository_root(args.repo),
            "success_criteria": args.criterion, "constraints": args.constraint, "configuration": configuration,
        }, approval_mode=configuration["launch"]["approval_mode"])

    def adjust():
        overrides = {r: getattr(args, r) for r in ("worker", "supervisor", "consultant") if getattr(args, r) is not None}
        reconfigure = None
        if args.preset is not None or overrides or args.interval_s is not None:
            reconfigure = lambda current: adjust_config(current, preset=args.preset, overrides=overrides, interval_s=args.interval_s)

        def validate(project):
            missing = [m for m in missing_context(project["objective"], project["repository"], project["success_criteria"])]
            if missing:
                raise Refused("missing_context", {"missing": missing, "questions": {m: QUESTIONS[m] for m in missing}})
        constraints = [] if args.no_constraints else args.constraint
        return coordinator.adjust(objective=args.objective, success_criteria=args.criterion, constraints=constraints,
                                  reconfigure=reconfigure, validate=validate)

    operations = {
        "init": configure,
        "propose": propose,
        "launch": launch_team,
        "adjust": adjust,
        "cancel": coordinator.cancel,
        "provision": lambda: Provisioner(coordinator, launcher, adapter).provision(),
        "plan": lambda: work.plan(coordinator, json.loads(args.units) if args.units is not None else None),
        "dispatch": lambda: work.Dispatcher(coordinator, launcher, adapter, args.db).dispatch(),
        "ack": lambda: work.acknowledge(coordinator, args.unit_id),
        "report-blocker": lambda: work.report_blocker(coordinator, args.unit_id, args.text),
        "start": lambda: coordinator.start(approval_source="user_shell" if args.user_approved else None,
                                           proposal_revision=args.proposal_revision,
                                           approver={k: caller[k] for k in ("kind", "pane_id") if k in caller}),
        "stop": coordinator.stop,
        "init-run": lambda: coordinator.init_run(args.objective, json.loads(args.success_criteria), args.interval_s, bootstrap_identity=caller),
        "bind-identity": lambda: coordinator.bind_identity(args.role, args.pane_id, target_session),
        "plan-units": lambda: coordinator.plan_units(json.loads(args.units)),
        "admit-next-unit": lambda: coordinator.admit_next_unit(observations),
        "record-completion": lambda: coordinator.record_completion(args.unit_id, json.loads(args.report), args.settled),
        "request-checkpoint": lambda: coordinator.request_checkpoint(args.reason, caller=caller),
        "review": lambda: coordinator.request_checkpoint(args.reason, caller=caller),
        "record-review": record_review,
        "record-steer": lambda: coordinator.record_steer(args.text, json.loads(args.new_units) if args.new_units is not None else None),
        "steer": lambda: coordinator.record_steer(args.text, json.loads(args.new_units) if args.new_units is not None else None),
        "pause": coordinator.pause,
        "resume": lambda: coordinator.resume(user_approved=args.user_approved, identity_observation=lambda state: {
            i["id"]: adapter.agent_identity(i["pane_id"]) for i in state["identities"]}),
        "reconcile": lambda: recovery.reconcile(coordinator, adapter),
        "resolve-unit": lambda: recovery.resolve_unit(coordinator, args.unit_id, args.decision, caller),
        "status": coordinator.status,
    }
    return operations[args.command]()


def main(argv=None, *, runner=None, environ=None, launch_runner=None):
    store = None
    try:
        args = parser().parse_args(argv)
        if args.command == "record-review" and (args.from_transcript == (args.decision_json is not None)):
            raise ValueError("provide decision text or --from-transcript, exclusively")
        if args.command in ("propose", "launch"):
            # Refuse before touching the store: no placeholder run, no empty database.
            args.repo = args.repo if args.repo is not None else os.getcwd()
            missing = missing_context(args.objective, args.repo, args.criterion)
            if missing:
                raise Refused("missing_context", {"missing": missing, "questions": {m: QUESTIONS[m] for m in missing}})
        readonly = args.command in ("status", "whoami", "transcript-preview", "history", "config", "proposal", "team", "progress", "controls")
        adapter = Herdr(runner=runner, environ=environ)
        if not readonly and not os.path.exists(args.db):
            # Leave no empty database behind: refuse before the store creates the file.
            if args.command not in ("init", "init-run", "propose", "launch"):
                raise Refused("run_not_initialized")
            if args.command in ("init", "propose", "launch"):
                resolve_config(args.preset, file=args.config_file, interval_s=args.interval_s,
                               overrides={r: getattr(args, r) for r in ("worker", "supervisor", "consultant") if getattr(args, r) is not None})
            if adapter.current_caller()["kind"] != "agent":
                raise Refused("bootstrap_requires_agent")
        store = Store.open_readonly(args.db) if readonly else Store(args.db)
        caller = {}

        def check(state):
            caller.clear()
            caller.update(adapter.current_caller())
            if args.command == "start":
                if caller["kind"] != "user" or not args.user_approved:
                    raise Refused("user_approval_required")
                role = "user"
            elif args.command == "stop":
                role = role_for(caller, state)
                if role not in ("supervisor", "user"):
                    raise Refused("authority_denied")
            else:
                authority_command = {"init": "init-run", "propose": "init-run",
                                     "launch": ("provision" if state and state.get("team") else "init-run"),
                                     "adjust": "adjust-proposal", "plan": "plan-work",
                                     "review": "request-checkpoint", "steer": "record-steer"}.get(args.command, args.command)
                role = authorize(authority_command, caller, state, user_approved=getattr(args, "user_approved", False))
            caller["role"] = role
            if args.command == "bind-identity" and args.role == "supervisor":
                raise Refused("supervisor_rebind_forbidden")

        def guard(state):
            check(state)
            store.event("caller_authorized", {"command": args.command, "caller": caller.copy()})

        if args.command == "status":
            result = Coordinator(store).status()
        elif args.command == "history":
            result = Coordinator(store).history()
        elif args.command == "config":
            result = Coordinator(store).configuration()
        elif args.command == "progress":
            state = Coordinator(store).status()
            result = work.progress(state, adapter, os.path.abspath(args.db))
            result["controls"] = recovery.controls(state)
        elif args.command == "controls":
            result = recovery.controls(Coordinator(store).status())
        elif args.command == "team":
            result = team_summary(Coordinator(store).status())
        elif args.command == "proposal":
            result = proposal(Coordinator(store).status(), os.path.abspath(args.db))
        elif args.command == "transcript-preview":
            result = Coordinator(store).transcript_preview(CodexTranscripts(args.transcript_root), after=args.after)
        elif args.command == "whoami":
            caller.update(adapter.current_caller())
            if caller["kind"] == "unknown":
                raise Refused("unknown_caller")
            result = {**caller, "role": role_for(caller, store.load())}
        else:
            check(store.load())
            if launch_runner is None and runner is not None:
                # A test's fake read runner also guards launches: no real subprocess.
                launch_runner = lambda command, timeout: runner(command)
            launcher = HerdrLauncher(runner=launch_runner, environ=environ)
            result = dispatch(Coordinator(store, guard=guard), args, adapter, caller, launcher)
        if args.command in ("propose", "adjust", "cancel"):
            result = proposal(result, os.path.abspath(args.db))
        if args.command == "init" and not args.json:
            print(render_summary(result["configuration"], result["project"]["objective"]))
        elif args.command in ("propose", "adjust", "proposal") and not args.json:
            print(render_proposal(result))
        elif args.command == "launch" and result.get("status") == "configured" and not args.json:
            print(render_proposal(result))
        elif args.command == "progress" and not args.json:
            print(work.render_progress(result))
        elif args.command == "controls":
            print(recovery.render_controls(result))
        else:
            print(json.dumps({"ok": True, "result": result}, sort_keys=True, allow_nan=False))
        return 0
    except Refused as error:
        refusal = {"ok": False, "reason": error.reason}
        if error.detail is not None:
            refusal["detail"] = error.detail
        print(json.dumps(refusal, sort_keys=True))
        return 3
    except (ValueError, TypeError) as error:
        print(json.dumps({"ok": False, "reason": "usage", "detail": str(error)}))
        return 2
    except (sqlite3.Error, OSError) as error:
        print(json.dumps({"ok": False, "reason": "store_unavailable", "detail": str(error)}))
        return 3
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
