import json
import shutil

import pytest

from trinity.cli import main
from trinity.coordinator import Coordinator
from trinity.store import Store


@pytest.mark.parametrize("wal_present", [False, True])
def test_status_and_whoami_use_readonly_view_with_or_without_wal(tmp_path, live, capsys, wal_present):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "run.sqlite3"
    writer = Store(path)
    c = Coordinator(writer)
    c.init_run("Build", [], bootstrap_identity={"pane_id": "w1:p1", "session_id": "supervisor-1"})
    snapshot = c.status()
    if not wal_present:
        writer.close()
    view = tmp_path / "readonly"
    shutil.copytree(source, view)
    assert (view / "run.sqlite3-wal").exists() == wal_present
    before = {p.name: p.read_bytes() for p in view.iterdir()}
    for p in view.iterdir():
        p.chmod(0o444)
    view.chmod(0o555)
    try:
        base = ["--db", str(view / "run.sqlite3")]
        assert main(base + ["status"], environ={}) == 0
        assert json.loads(capsys.readouterr().out)["result"] == snapshot
        assert main(base + ["whoami"], runner=live, environ={"HERDR_ENV": "1"}) == 0
        assert json.loads(capsys.readouterr().out)["result"]["role"] == "supervisor"
        assert main(base + ["history"], environ={}) == 0
        assert json.loads(capsys.readouterr().out)["result"]["reviews"] == []
        assert main(base + ["config"], environ={}) == 0
        assert json.loads(capsys.readouterr().out)["result"] == {"interval_s": 300}
        assert {p.name: p.read_bytes() for p in view.iterdir()} == before
    finally:
        view.chmod(0o755)
        for p in view.iterdir():
            p.chmod(0o644)
        writer.close()
