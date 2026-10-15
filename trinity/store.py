"""Transactional SQLite snapshots and an immutable event ledger."""

from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


class Store:
    @classmethod
    def open_readonly(cls, path):
        path = Path(path).resolve()
        uri = path.as_uri() + "?mode=ro"
        # A checkpointed WAL database without sidecars otherwise needs a
        # writable directory to initialize them, even with mode=ro.
        if not Path(str(path) + "-wal").exists():
            uri += "&immutable=1"
        store = cls.__new__(cls)
        store.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
        return store

    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        """)
        for table in ("units", "checkpoints", "reviews", "steers", "identities"):
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def load(self):
        row = self.connection.execute("SELECT data FROM runs WHERE singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state):
        self.connection.execute(
            "INSERT INTO runs VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE SET data=excluded.data",
            (encode(state),),
        )
        # The aggregate snapshot and its queryable entity tables commit together.
        for table in ("units", "checkpoints", "reviews", "steers", "identities"):
            for entity in state.get(table, []):
                self.connection.execute(
                    f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (entity["id"], encode(entity)),
                )

    def event(self, kind, data):
        self.connection.execute("INSERT INTO events(kind, data) VALUES (?, ?)", (kind, encode(data)))

    def events(self):
        return [
            {"seq": seq, "kind": kind, "data": json.loads(data)}
            for seq, kind, data in self.connection.execute("SELECT seq,kind,data FROM events ORDER BY seq")
        ]

    def close(self):
        self.connection.close()
