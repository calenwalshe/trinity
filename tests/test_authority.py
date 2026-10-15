import pytest

from trinity.coordinator import Refused

from trinity.authority import authorize


STATE = {"identities": [
    {"id": "supervisor", "pane_id": "w1:p1", "session_id": "s1"},
    {"id": "worker", "pane_id": "w1:p2", "session_id": "s2"},
    {"id": "consultant", "pane_id": "w1:p3", "session_id": "s3"},
]}
COMMANDS = [
    ("init-run", {"supervisor"}), ("bind-identity", {"supervisor"}),
    ("plan-units", {"supervisor"}), ("admit-next-unit", {"supervisor"}),
    ("record-review", {"supervisor"}), ("record-steer", {"supervisor"}),
    ("pause", {"supervisor", "user"}), ("resume", {"supervisor", "user"}),
    ("request-checkpoint", {"supervisor", "worker", "user"}),
    ("record-completion", {"worker"}),
]


@pytest.mark.parametrize("command,allowed", COMMANDS)
@pytest.mark.parametrize("role", ["supervisor", "worker", "consultant", "user", "unknown", "unbound"])
def test_role_map_allows_only_designated_roles(command, allowed, role):
    assert authorize is not None, "caller authority is missing"
    caller = {"kind": role, "pane_id": "w1:p9"}
    if role in ("supervisor", "worker", "consultant"):
        binding = next(i for i in STATE["identities"] if i["id"] == role)
        caller = {"kind": "agent", **{k: binding[k] for k in ("pane_id", "session_id")}}
    elif role == "unbound":
        caller = {"kind": "agent", "pane_id": "w1:p1", "session_id": "replaced"}
    if role in allowed:
        assert authorize(command, caller, STATE) == role
    else:
        with pytest.raises(Refused):
            authorize(command, caller, STATE)


@pytest.mark.parametrize("kind", ["user", "unknown"])
def test_bootstrap_requires_verified_agent(kind):
    assert authorize is not None, "caller authority is missing"
    with pytest.raises(Refused, match="bootstrap_requires_agent"):
        authorize("init-run", {"kind": kind}, None)
    assert authorize("init-run", {"kind": "agent", "pane_id": "w1:p1", "session_id": "s1"}, None) == "supervisor"


@pytest.mark.parametrize("binding", STATE["identities"])
def test_user_approval_refused_even_from_supervisor(binding):
    assert authorize is not None, "caller authority is missing"
    caller = {"kind": "agent", **binding}
    with pytest.raises(Refused, match="user_approval_requires_user_caller"):
        authorize("resume", caller, STATE, user_approved=True)
    assert authorize("resume", {"kind": "user", "pane_id": "w1:p9"}, STATE, user_approved=True) == "user"
