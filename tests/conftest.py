import copy
import json
import subprocess

import pytest

from trinity.cli import main
from trinity.store import Store


class LiveHerdr:
    """Only replaces the subprocess boundary; JSON parsing stays real."""
    def __init__(self):
        self.caller = "w1:p1"
        self.agents = {
            "w1:p1": {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude", "agent_session": {"value": "supervisor-1"}},
            "w1:p2": {"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "codex", "agent_session": {"value": "worker-1"}},
            "w1:p3": {"pane_id": "w1:p3", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "codex", "agent_session": {"value": "consultant-1"}},
            "w1:p9": {"pane_id": "w1:p9", "workspace_id": "w1", "tab_id": "w1:t1", "agent": None, "agent_session": None},
        }
        self.calls = []

    def __call__(self, command):
        self.calls.append(command)
        if command == ["herdr", "pane", "current", "--current"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": {"pane": copy.deepcopy(self.agents[self.caller])}}), "")
        if command[:3] == ["herdr", "agent", "get"] and len(command) == 4:
            data = self.agents.get(command[3])
            return subprocess.CompletedProcess(command, 0 if data else 1, json.dumps({"result": {"agent": copy.deepcopy(data)}}), "")
        raise AssertionError(f"Forbidden Herdr operation: {command}")


@pytest.fixture
def live():
    return LiveHerdr()


class FakeHerdr(LiveHerdr):
    """Stateful in-memory Herdr for launch tests. Never runs a subprocess.

    ``faults[op]`` holds a queue of injected outcomes for an operation such as
    ``"agent start"``: ``("error", code)`` fails with no effect,
    ``"timeout_before"`` times out with no effect, ``"timeout_after"`` takes
    effect and then times out, ``"oserror"`` means the binary never ran.
    """

    def __init__(self):
        super().__init__()
        self.workspaces = {"w1": {"workspace_id": "w1", "label": "main"}}
        self.tabs = {"w1:t1": {"tab_id": "w1:t1", "workspace_id": "w1", "label": "1"}}
        self.panes = {p: {"pane_id": p, "workspace_id": "w1", "tab_id": "w1:t1"} for p in self.agents}
        for data in self.agents.values():
            data["workspace_id"] = "w1"
        self.faults = {}
        self.prompts = []
        self.serial = 10

    def _id(self, prefix):
        self.serial += 1
        return f"{prefix}{self.serial}"

    @staticmethod
    def ok(command, data):
        return subprocess.CompletedProcess(command, 0, json.dumps({"id": "fake", "result": data}), "")

    def __call__(self, command, timeout=None):
        op = " ".join(command[1:3])
        fault = self.faults.get(op, []).pop(0) if self.faults.get(op) else None
        if fault == "oserror":
            self.calls.append(command)
            raise FileNotFoundError("herdr")
        if fault == "timeout_before":
            self.calls.append(command)
            raise subprocess.TimeoutExpired(command, timeout or 1)
        if isinstance(fault, tuple):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 1, json.dumps({"id": "fake", "error": {"code": fault[1], "message": "injected"}}), "")
        result = self.handle(command)
        if fault == "timeout_after":
            raise subprocess.TimeoutExpired(command, timeout or 1)
        return result

    def handle(self, command):
        args = command[1:]
        if args[:2] == ["agent", "get"]:
            self.calls.append(command)
            target = args[2]
            data = self.agents.get(target) or next((a for a in self.agents.values() if a.get("name") == target), None)
            if data is not None and data.get("agent") is None:
                data = None
            return subprocess.CompletedProcess(command, 0 if data else 1, json.dumps({"result": {"agent": copy.deepcopy(data)}}), "")
        if args[:2] == ["pane", "get"]:
            self.calls.append(command)
            data = self.panes.get(args[2])
            if data is None:
                return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "pane_not_found", "message": ""}}), "")
            return self.ok(command, {"type": "pane_info", "pane": {**data, **{k: v for k, v in self.agents.get(args[2], {}).items() if k != "name"}}})
        if args[:2] == ["pane", "list"]:
            self.calls.append(command)
            workspace = args[args.index("--workspace") + 1]
            return self.ok(command, {"type": "pane_list", "panes": [copy.deepcopy(p) for p in self.panes.values() if p["workspace_id"] == workspace]})
        if args[:2] == ["workspace", "list"]:
            self.calls.append(command)
            return self.ok(command, {"type": "workspace_list", "workspaces": [copy.deepcopy(w) for w in self.workspaces.values()]})
        if args[:2] == ["workspace", "create"]:
            self.calls.append(command)
            workspace, pane = self._id("ws"), None
            pane = f"{workspace}:p1"
            self.workspaces[workspace] = {"workspace_id": workspace, "label": args[args.index("--label") + 1]}
            self.panes[pane] = {"pane_id": pane, "workspace_id": workspace, "cwd": args[args.index("--cwd") + 1]}
            return self.ok(command, {"type": "workspace_created", "workspace": self.workspaces[workspace], "tab": {}, "root_pane": self.panes[pane]})
        if args[:2] == ["tab", "create"]:
            self.calls.append(command)
            workspace = args[args.index("--workspace") + 1]
            if workspace not in self.workspaces:
                return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "workspace_not_found", "message": ""}}), "")
            tab_id, pane = f"{workspace}:{self._id('t')}", f"{workspace}:{self._id('p')}"
            self.tabs[tab_id] = {"tab_id": tab_id, "workspace_id": workspace, "label": args[args.index("--label") + 1]}
            self.panes[pane] = {"pane_id": pane, "workspace_id": workspace, "tab_id": tab_id,
                                "cwd": args[args.index("--cwd") + 1], "root": True}
            return self.ok(command, {"type": "tab_created", "tab": self.tabs[tab_id], "root_pane": self.panes[pane]})
        if args[:2] == ["tab", "list"]:
            self.calls.append(command)
            workspace = args[args.index("--workspace") + 1]
            return self.ok(command, {"type": "tab_list", "tabs": [copy.deepcopy(t) for t in self.tabs.values()
                                                                       if t["workspace_id"] == workspace]})
        if args[:2] == ["pane", "split"]:
            self.calls.append(command)
            parent = args[2]
            if parent not in self.panes:
                return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "pane_not_found", "message": ""}}), "")
            pane_id = f"{self.panes[parent]['workspace_id']}:{self._id('p')}"
            self.panes[pane_id] = {"pane_id": pane_id, "workspace_id": self.panes[parent]["workspace_id"],
                                   "tab_id": self.panes[parent]["tab_id"], "cwd": args[args.index("--cwd") + 1],
                                   "split_from": parent, "direction": args[args.index("--direction") + 1],
                                   "ratio": args[args.index("--ratio") + 1], "root": False}
            return self.ok(command, {"type": "pane_split", "pane": self.panes[pane_id]})
        if args[:2] == ["agent", "start"]:
            self.calls.append(command)
            name, pane = args[2], args[args.index("--pane") + 1]
            if pane not in self.panes or (self.agents.get(pane) or {}).get("agent"):
                return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "pane_not_at_prompt", "message": ""}}), "")
            kind = args[args.index("--kind") + 1]
            argv = args[args.index("--") + 1:] if "--" in args else []
            self.agents[pane] = {"pane_id": pane, "name": name, "agent": kind,
                                 "workspace_id": self.panes[pane]["workspace_id"], "tab_id": self.panes[pane]["tab_id"],
                                 "agent_session": {"value": f"{name}-session-{self._id('')}"}, "argv": argv}
            return self.ok(command, {"type": "agent_started", "agent": copy.deepcopy(self.agents[pane]), "argv": argv})
        if args[:2] == ["agent", "prompt"]:
            self.calls.append(command)
            target, text = args[2], args[3]
            data = self.agents.get(target)
            if data is None or data.get("agent") is None:
                return subprocess.CompletedProcess(command, 1, json.dumps({"error": {"code": "agent_not_found", "message": ""}}), "")
            self.prompts.append((target, text))
            return self.ok(command, {"type": "agent_prompted", "agent": copy.deepcopy(data)})
        return super().__call__(command)

    def launches(self, op=None):
        verbs = (["workspace", "create"], ["tab", "create"], ["pane", "split"], ["agent", "start"], ["agent", "prompt"])
        return [c for c in self.calls if c[1:3] in verbs and (op is None or " ".join(c[1:3]) == op)]


@pytest.fixture
def fake():
    return FakeHerdr()


@pytest.fixture
def team(tmp_path, capsys, fake):
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    path = tmp_path / "run.sqlite3"

    def default_caller():
        if path.exists():
            store = Store.open_readonly(path)
            try:
                state = store.load()
                if state.get("team"):
                    supervisor = next((i for i in state["identities"] if i["id"] == "supervisor"), None)
                    if supervisor:
                        return supervisor["pane_id"]
            finally:
                store.close()
        return "w1:p1"

    def invoke(*args, env="1", caller=None, text=False):
        if caller is not None:
            fake.caller = caller
        code = main(["--db", str(path), *args], runner=fake, environ={"HERDR_ENV": env}, launch_runner=fake)
        output = capsys.readouterr().out
        fake.caller = default_caller()
        return code, output if text else json.loads(output)

    def setup(*extra, provision=True):
        assert invoke("propose", "Add CSV export to reports", "--repo", str(repo), "--criterion", "Tests pass",
                      "--constraint", "No new dependencies", "--json", *extra)[0] == 0
        assert invoke("start", "--user-approved", "--proposal", "1", caller="w1:p9")[0] == 0
        if provision:
            assert invoke("provision")[0] == 0

    def role(name):
        store = Store.open_readonly(path)
        try:
            return next(i["pane_id"] for i in store.load()["identities"] if i["id"] == name)
        finally:
            store.close()

    return path, invoke, setup, role
