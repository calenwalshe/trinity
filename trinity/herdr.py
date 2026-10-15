"""Live Herdr access with bounded subprocess calls.

``Herdr`` only reads. ``HerdrLauncher`` is the single place that creates
workspaces, tabs and agents or submits prompts; every argument it sends is
validated, and every outcome is classified as definite or ambiguous.
"""

import json
import os
import re
import subprocess


def run(command, timeout=10):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


class Herdr:
    def __init__(self, runner=None, environ=None):
        self.runner = runner or run
        self.environ = os.environ if environ is None else environ

    def _read(self, arguments, entity):
        if self.environ.get("HERDR_ENV") != "1":
            return None
        try:
            result = self.runner(["herdr", *arguments])
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)["result"][entity]
            return data if isinstance(data, dict) else None
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _identity(data):
        if not data or not isinstance(data.get("agent"), str) or not data["agent"].strip():
            return None
        session = data.get("agent_session")
        pane = data.get("pane_id")
        value = session.get("value") if isinstance(session, dict) else None
        if not isinstance(pane, str) or not pane or not isinstance(value, str) or not value:
            return None
        return {"pane_id": pane, "session_id": value}

    def agent_info(self, name_or_pane):
        """Raw agent record for a pane or name, or None when no agent is detected there."""
        data = self._read(["agent", "get", name_or_pane], "agent")
        if self._identity(data) and name_or_pane in (data.get("pane_id"), data.get("name")):
            return data
        return None

    def pane_info(self, pane_id):
        data = self._read(["pane", "get", pane_id], "pane")
        return data if data and data.get("pane_id") == pane_id else None

    def agent_identity(self, name_or_pane):
        data = self._read(["agent", "get", name_or_pane], "agent")
        identity = self._identity(data)
        if identity and name_or_pane in (data.get("pane_id"), data.get("name")):
            return identity
        return None

    def current_caller(self):
        data = self._read(["pane", "current", "--current"], "pane")
        if not data or not isinstance(data.get("pane_id"), str) or not data["pane_id"]:
            return {"kind": "unknown"}
        context = {key: data[key] for key in ("workspace_id", "tab_id")
                   if isinstance(data.get(key), str) and data[key]}
        if data.get("agent") is None:
            return {"kind": "user", "pane_id": data["pane_id"], **context}
        identity = self._identity(data)
        if identity and self.agent_identity(identity["pane_id"]) == identity:
            return {"kind": "agent", **identity, **context}
        return {"kind": "unknown"}


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,95}")
# Herdr error codes after which nothing was created or delivered. Any other
# failure is ambiguous and must be reconciled before a retry.
DEFINITE_ERRORS = {"agent_blocked", "agent_not_found", "pane_not_found", "workspace_not_found", "not_found",
                   "invalid_params", "invalid_request", "unsupported_agent_kind", "pane_not_at_prompt"}


class LaunchError(Exception):
    def __init__(self, code, ambiguous, detail=None):
        self.code = code
        self.ambiguous = ambiguous
        self.detail = detail
        super().__init__(code)


def safe_id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise LaunchError("invalid_argument", False, {"value": str(value)[:100]})
    return value


def safe_path(value):
    if not isinstance(value, str) or not os.path.isabs(value) or "\0" in value or not os.path.isdir(value):
        raise LaunchError("invalid_argument", False, {"cwd": str(value)[:200]})
    return value


class HerdrLauncher:
    """Mutating Herdr operations. Tests always inject a fake runner."""

    def __init__(self, runner=None, environ=None):
        self.runner = runner or run
        self.environ = os.environ if environ is None else environ

    def _call(self, arguments, timeout):
        if self.environ.get("HERDR_ENV") != "1":
            raise LaunchError("herdr_unavailable", False)
        try:
            result = self.runner(["herdr", *arguments], timeout)
        except subprocess.TimeoutExpired as error:
            raise LaunchError("timeout", True) from error
        except OSError as error:
            # The process never started, so nothing reached Herdr.
            raise LaunchError("herdr_unavailable", False, {"error": str(error)[:200]}) from error
        except subprocess.SubprocessError as error:
            raise LaunchError("subprocess_error", True) from error
        try:
            data = json.loads(result.stdout or result.stderr or "")
        except ValueError:
            data = None
        if result.returncode != 0:
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) and isinstance(error.get("code"), str) else "herdr_error"
            raise LaunchError(code, code not in DEFINITE_ERRORS, {"message": str((error or {}).get("message", ""))[:300]})
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise LaunchError("unparseable_response", True)
        return data["result"]

    def create_workspace(self, label, cwd):
        result = self._call(["workspace", "create", "--label", safe_id(label), "--cwd", safe_path(cwd), "--no-focus"], 15)
        try:
            return {"workspace_id": safe_id(result["workspace"]["workspace_id"]), "pane_id": safe_id(result["root_pane"]["pane_id"])}
        except (KeyError, TypeError, LaunchError) as error:
            raise LaunchError("unparseable_response", True) from error

    def create_tab(self, workspace_id, label, cwd):
        result = self._call(["tab", "create", "--workspace", safe_id(workspace_id), "--label", safe_id(label),
                             "--cwd", safe_path(cwd), "--no-focus"], 15)
        try:
            return {"tab_id": safe_id(result["tab"]["tab_id"]),
                    "pane_id": safe_id(result["root_pane"]["pane_id"])}
        except (KeyError, TypeError, LaunchError) as error:
            raise LaunchError("unparseable_response", True) from error

    def split_pane(self, pane_id, *, direction, ratio, cwd):
        if direction not in ("right", "down") or type(ratio) not in (int, float) or not 0 < ratio < 1:
            raise LaunchError("invalid_argument", False)
        result = self._call(["pane", "split", safe_id(pane_id), "--direction", direction,
                             "--ratio", str(ratio), "--cwd", safe_path(cwd), "--no-focus"], 15)
        try:
            return safe_id(result["pane"]["pane_id"])
        except (KeyError, TypeError, LaunchError) as error:
            raise LaunchError("unparseable_response", True) from error

    def start_agent(self, name, kind, pane_id, args, timeout_ms):
        if kind not in ("codex", "claude") or any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9._=-]+", a) for a in args):
            raise LaunchError("invalid_argument", False)
        if type(timeout_ms) is not int or not 3000 < timeout_ms <= 300000:
            raise LaunchError("invalid_argument", False)
        command = ["agent", "start", safe_id(name), "--kind", kind, "--pane", safe_id(pane_id), "--timeout", str(timeout_ms)]
        result = self._call(command + (["--", *args] if args else []), timeout_ms / 1000 + 15)
        agent = result.get("agent")
        if not isinstance(agent, dict):
            raise LaunchError("unparseable_response", True)
        return agent

    def list_workspaces(self):
        result = self._call(["workspace", "list"], 10)
        values = result.get("workspaces")
        if not isinstance(values, list):
            raise LaunchError("unparseable_response", True)
        return [w for w in values if isinstance(w, dict)]

    def list_tabs(self, workspace_id):
        result = self._call(["tab", "list", "--workspace", safe_id(workspace_id)], 10)
        values = result.get("tabs")
        if not isinstance(values, list):
            raise LaunchError("unparseable_response", True)
        return [tab for tab in values if isinstance(tab, dict)]

    def list_panes(self, workspace_id):
        result = self._call(["pane", "list", "--workspace", safe_id(workspace_id)], 10)
        values = result.get("panes")
        if not isinstance(values, list):
            raise LaunchError("unparseable_response", True)
        return [p for p in values if isinstance(p, dict)]

    def prompt(self, target, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise LaunchError("invalid_argument", False)
        result = self._call(["agent", "prompt", safe_id(target), text], 30)
        return result.get("agent") if isinstance(result.get("agent"), dict) else {}
