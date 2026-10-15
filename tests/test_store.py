import sqlite3

import pytest

from trinity.store import Store


def test_store_transactions_are_durable_and_ledger_is_append_only(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with store.transaction():
        store.save({"id": "run-1", "status": "planning"})
        store.event("initialized", {"objective": "Build"})
    before = store.events()
    store.close()
    store = Store(path)
    assert store.load() == {"id": "run-1", "status": "planning"}
    assert store.events() == before
    assert before[0]["seq"] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.connection.execute("DELETE FROM events")
    store.close()


def test_failed_transaction_rolls_back_state_and_event_together(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.save({"id": "run-1", "status": "running"})
            store.event("started", {})
            raise RuntimeError("crash")
    assert store.load() is None
    assert store.events() == []
    store.close()


@pytest.mark.parametrize("wal_present", [False, True])
def test_readonly_store_works_without_writable_directory_and_preserves_files(tmp_path, wal_present, monkeypatch):
    path = tmp_path / "state ?#.sqlite3"
    writer = Store(path)
    with writer.transaction():
        writer.save({"status": "running", "identities": []})
        writer.event("initialized", {})
    if not wal_present:
        writer.close()
    assert path.with_name(path.name + "-wal").exists() == wal_present
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    statements, connections = [], []
    connect = sqlite3.connect

    def traced(database, **kwargs):
        connections.append((database, kwargs))
        result = connect(database, **kwargs)
        result.set_trace_callback(statements.append)
        return result

    monkeypatch.setattr(sqlite3, "connect", traced)
    for p in tmp_path.iterdir():
        p.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert hasattr(Store, "open_readonly"), "read-only store opener is missing"
        reader = Store.open_readonly(path)
        assert reader.load()["status"] == "running"
        assert reader.events()[0]["kind"] == "initialized"
        assert all(s.startswith("SELECT") for s in statements)
        assert connections[0][0].startswith("file:") and "mode=ro" in connections[0][0]
        assert connections[0][1]["uri"] is True
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.connection.execute("DELETE FROM runs")
        reader.close()
        after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        assert after.keys() == before.keys()
        # WAL read marks are shared coordination memory, not durable state.
        assert {k: v for k, v in after.items() if not k.endswith("-shm")} == {
            k: v for k, v in before.items() if not k.endswith("-shm")
        }
    finally:
        tmp_path.chmod(0o755)
        for p in tmp_path.iterdir():
            p.chmod(0o644)
        writer.close()


def test_readonly_missing_database_does_not_create_it(tmp_path):
    assert hasattr(Store, "open_readonly"), "read-only store opener is missing"
    with pytest.raises(sqlite3.OperationalError):
        Store.open_readonly(tmp_path / "missing.sqlite3")
    assert list(tmp_path.iterdir()) == []
