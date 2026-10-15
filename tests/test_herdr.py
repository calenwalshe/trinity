import json
import subprocess

import pytest

from trinity.herdr import Herdr


def response(entity, data, code=0):
    return subprocess.CompletedProcess([], code, json.dumps({"result": {entity: data}}), "")


def test_adapter_classifies_agent_user_and_unknown_without_mutating_commands():
    assert Herdr is not None, "read-only Herdr adapter is missing"
    calls = []
    pane = {"pane_id": "w1:p1", "agent": "codex", "agent_session": {"value": "s1"}}

    def runner(command):
        calls.append(command)
        if command == ["herdr", "pane", "current", "--current"]:
            return response("pane", pane)
        if command == ["herdr", "agent", "get", "w1:p1"]:
            return response("agent", pane)
        raise AssertionError(command)

    adapter = Herdr(runner=runner, environ={"HERDR_ENV": "1"})
    assert adapter.current_caller() == {"kind": "agent", "pane_id": "w1:p1", "session_id": "s1"}
    assert calls == [["herdr", "pane", "current", "--current"], ["herdr", "agent", "get", "w1:p1"]]
    pane.update(agent=None, agent_session=None)
    assert adapter.current_caller() == {"kind": "user", "pane_id": "w1:p1"}
    assert Herdr(runner=runner, environ={}).current_caller() == {"kind": "unknown"}


@pytest.mark.parametrize("bad", [
    subprocess.CompletedProcess([], 1, "{}", "failed"),
    subprocess.CompletedProcess([], 0, "{", ""),
    response("pane", {"pane_id": "w1:p1", "agent": "codex"}),
    response("pane", {"agent": None}),
    subprocess.CompletedProcess([], 0, '[]', ""),
])
def test_bad_caller_response_is_unknown(bad):
    assert Herdr is not None, "read-only Herdr adapter is missing"
    assert Herdr(runner=lambda _: bad, environ={"HERDR_ENV": "1"}).current_caller() == {"kind": "unknown"}


def test_agent_lookup_requires_matching_target_and_session():
    assert Herdr is not None, "read-only Herdr adapter is missing"
    data = {"pane_id": "w1:p2", "name": "worker", "agent": "codex", "agent_session": {"value": "s2"}}
    adapter = Herdr(runner=lambda _: response("agent", data), environ={"HERDR_ENV": "1"})
    assert adapter.agent_identity("worker") == {"pane_id": "w1:p2", "session_id": "s2"}
    assert adapter.agent_identity("w1:p1") is None
    data["agent_session"] = None
    assert adapter.agent_identity("worker") is None


def test_caller_crosscheck_rejects_replaced_session_and_transport_failure():
    assert Herdr is not None, "read-only Herdr adapter is missing"
    values = iter([
        response("pane", {"pane_id": "w1:p1", "agent": "codex", "agent_session": {"value": "old"}}),
        response("agent", {"pane_id": "w1:p1", "agent": "codex", "agent_session": {"value": "new"}}),
    ])
    assert Herdr(runner=lambda _: next(values), environ={"HERDR_ENV": "1"}).current_caller() == {"kind": "unknown"}

    def unavailable(_):
        raise OSError("Herdr unavailable")
    assert Herdr(runner=unavailable, environ={"HERDR_ENV": "1"}).current_caller() == {"kind": "unknown"}


def test_malformed_agent_type_is_not_recognized():
    pane = {"pane_id": "w1:p1", "agent": True, "agent_session": {"value": "s1"}}
    def runner(command):
        return response("pane" if command[1] == "pane" else "agent", pane)
    assert Herdr(runner=runner, environ={"HERDR_ENV": "1"}).current_caller() == {"kind": "unknown"}


def test_real_shell_shape_with_omitted_agent_field_is_user():
    # Herdr omits optional agent/session fields for a shell pane.
    pane = {"pane_id": "w1:p9", "agent_status": "unknown", "cwd": "/project", "revision": 1}
    adapter = Herdr(runner=lambda _: response("pane", pane), environ={"HERDR_ENV": "1"})
    assert adapter.current_caller() == {"kind": "user", "pane_id": "w1:p9"}


def test_current_caller_includes_workspace_and_tab():
    pane = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t7",
            "agent": "codex", "agent_session": {"value": "s1"}}
    values = iter([response("pane", pane), response("agent", pane)])
    adapter = Herdr(runner=lambda _: next(values), environ={"HERDR_ENV": "1"})
    assert adapter.current_caller() == {"kind": "agent", "pane_id": "w1:p1", "session_id": "s1",
                                        "workspace_id": "w1", "tab_id": "w1:t7"}


from trinity.herdr import HerdrLauncher, LaunchError


def launcher_with(result=None, raises=None):
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        if raises:
            raise raises
        return result
    return HerdrLauncher(runner=runner, environ={"HERDR_ENV": "1"}), calls


@pytest.mark.parametrize("outcome,code,ambiguous", [
    (subprocess.CompletedProcess([], 1, '{"error":{"code":"agent_blocked","message":"x"}}', ""), "agent_blocked", False),
    (subprocess.CompletedProcess([], 1, '{"error":{"code":"internal","message":"x"}}', ""), "internal", True),
    (subprocess.CompletedProcess([], 1, "garbage", ""), "herdr_error", True),
    (subprocess.CompletedProcess([], 0, "garbage", ""), "unparseable_response", True),
    (subprocess.TimeoutExpired([], 1), "timeout", True),
    (FileNotFoundError("herdr"), "herdr_unavailable", False),
])
def test_launcher_classifies_definite_and_ambiguous_outcomes(outcome, code, ambiguous):
    if isinstance(outcome, BaseException):
        launcher, _ = launcher_with(raises=outcome)
    else:
        launcher, _ = launcher_with(result=outcome)
    with pytest.raises(LaunchError) as error:
        launcher.prompt("ws1:p1", "hello")
    assert (error.value.code, error.value.ambiguous) == (code, ambiguous)


def test_launcher_refuses_without_herdr_and_validates_arguments(tmp_path):
    calls = []
    launcher = HerdrLauncher(runner=lambda c, t: calls.append(c), environ={})
    with pytest.raises(LaunchError) as error:
        launcher.prompt("ws1:p1", "x")
    assert error.value.code == "herdr_unavailable" and not error.value.ambiguous and calls == []
    launcher, calls = launcher_with(result=subprocess.CompletedProcess([], 0, '{"result":{}}', ""))
    for bad in [lambda: launcher.start_agent("x; rm", "codex", "ws1:p1", [], 60000),
                lambda: launcher.start_agent("n", "bash", "ws1:p1", [], 60000),
                lambda: launcher.start_agent("n", "codex", "ws1:p1", ["$(evil)"], 60000),
                lambda: launcher.start_agent("n", "codex", "ws1:p1", [], 10),
                lambda: launcher.create_workspace("label", "relative/path"),
                lambda: launcher.create_tab("ws1", "label", str(tmp_path / "missing")),
                lambda: launcher.prompt("ws1:p1", "")]:
        with pytest.raises(LaunchError) as error:
            bad()
        assert error.value.code == "invalid_argument" and not error.value.ambiguous
    assert calls == []


def test_launcher_start_agent_command_shape_and_timeout():
    agent = {"pane_id": "ws1:p1", "name": "n", "agent": "codex", "agent_session": {"value": "s"}}
    launcher, calls = launcher_with(result=subprocess.CompletedProcess([], 0, json.dumps({"result": {"type": "agent_started", "agent": agent, "argv": []}}), ""))
    assert launcher.start_agent("n", "codex", "ws1:p1", ["-m", "gpt-6-astra", "-s", "read-only"], 60000) == agent
    command, timeout = calls[0]
    assert command == ["herdr", "agent", "start", "n", "--kind", "codex", "--pane", "ws1:p1", "--timeout", "60000",
                       "--", "-m", "gpt-6-astra", "-s", "read-only"]
    assert timeout == 75


def test_split_pane_uses_requested_ratio_and_no_focus(tmp_path):
    result = subprocess.CompletedProcess([], 0, json.dumps({"result": {"pane": {"pane_id": "w1:p8"}}}), "")
    launcher, calls = launcher_with(result=result)
    assert launcher.split_pane("w1:p1", direction="right", ratio=0.35, cwd=str(tmp_path)) == "w1:p8"
    assert calls[0][0] == ["herdr", "pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.35",
                           "--cwd", str(tmp_path), "--no-focus"]
