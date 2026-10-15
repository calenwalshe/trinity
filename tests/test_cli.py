import json
import subprocess
import sys
import os

from trinity.cli import main
from trinity.coordinator import Coordinator
from trinity.store import Store


def test_package_entrypoint_persists_across_processes(tmp_path):
    base = [sys.executable, "-m", "trinity", "--db", str(tmp_path / "run.sqlite3")]
    store = Store(tmp_path / "run.sqlite3")
    snapshot = Coordinator(store).init_run("Build", [])
    store.close()
    reopened = subprocess.run(base + ["status"], capture_output=True, text=True, timeout=10, env={**os.environ, "HERDR_ENV": "0"})
    assert reopened.returncode == 0, reopened.stderr
    assert json.loads(reopened.stdout)["result"] == snapshot


def test_cli_lifecycle_is_json_and_refusals_have_exit_three(tmp_path, capsys, live):
    base = ["--db", str(tmp_path / "run.sqlite3")]

    def invoke(*args):
        code = main(base + list(args), runner=live, environ={"HERDR_ENV": "1"})
        return code, json.loads(capsys.readouterr().out)

    assert invoke("init-run", "Build", "--success-criteria", '["Tests pass"]')[0] == 0
    assert invoke("bind-identity", "worker", "w1:p2")[0] == 0
    assert invoke("plan-units", '[{"id":"u1","task":"Build"}]')[0] == 0
    code, result = invoke("admit-next-unit")
    assert code == 0 and result["result"]["id"] == "u1"
    assert invoke("admit-next-unit") == (3, {"ok": False, "reason": "unit_in_flight"})
    live.caller = "w1:p2"
    assert invoke("record-completion", "u1", '{"tests":["pass"]}', "--settled")[0] == 0
    assert invoke("status")[1]["result"]["status"] == "completed"


def test_cli_usage_and_bad_json_have_exit_two(tmp_path, capsys, live):
    assert main(["--db", str(tmp_path / "run.sqlite3"), "unknown"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "usage"
    base = ["--db", str(tmp_path / "run.sqlite3")]
    assert main(base + ["init-run", "Build"], runner=live, environ={"HERDR_ENV": "1"}) == 0
    capsys.readouterr()
    assert main(base + ["plan-units", "{"], runner=live, environ={"HERDR_ENV": "1"}) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "usage"


def test_cli_requires_explicit_settlement(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "run.sqlite3"), "record-completion", "u1", "{}"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "usage"


def test_cli_lists_launch_without_removing_manual_commands():
    from trinity.cli import parser
    help_text = parser().format_help()
    assert "launch" in help_text and "propose" in help_text and "start" in help_text
