"""Exercise the installed-style CLI against synthetic index and run databases."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'integrations/session_ledger/ledger.py'


@pytest.fixture
def ledger_fixture(tmp_path):
    project = tmp_path / 'project'
    runs = project / '.trinity'
    runs.mkdir(parents=True)
    index = tmp_path / 'sessions.db'
    with sqlite3.connect(index) as db:
        db.executescript('''
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY, jsonl_path TEXT,
                start_cwd TEXT, ai_title TEXT, first_prompt TEXT, last_prompt TEXT,
                last_ts REAL, n_user_msgs INTEGER, agent TEXT);
            CREATE TABLE session_paths(session_id TEXT,path TEXT,source TEXT);
            CREATE TABLE user_prompts(session_id TEXT,file_path TEXT,prompt TEXT);
            CREATE VIRTUAL TABLE sessions_fts USING fts5(
                session_id UNINDEXED, ai_title, first_prompt, last_prompt);
            CREATE VIRTUAL TABLE user_prompts_fts USING fts5(
                session_id UNINDEXED,file_path UNINDEXED,prompt);
        ''')
        for i, (sid, agent, title) in enumerate([
            ('worker-session', 'codex', 'implementation'),
            ('supervisor-session', 'claude', 'planning'),
            ('consultant-session', 'codex', 'independent assessment'),
            ('unrelated-session', 'codex', 'unrelated conversation'),
        ]):
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',
                (sid, f'/transcripts/{sid}.jsonl', str(project), title, '', '', 100+i, 4, agent))
            db.execute('INSERT INTO session_paths VALUES(?,?,?)', (sid, str(project), 'cwd'))
            db.execute('INSERT INTO sessions_fts VALUES(?,?,?,?)', (sid, title, '', ''))
        db.execute('INSERT INTO user_prompts VALUES(?,?,?)',
                   ('consultant-session', '/transcripts/review-segment.jsonl', 'hidden nebula concern'))
        db.execute('INSERT INTO user_prompts_fts VALUES(?,?,?)',
                   ('consultant-session', '/transcripts/review-segment.jsonl', 'hidden nebula concern'))

    def make_run(name='run-1', **overrides):
        data = dict(id=name, status='completed', project={'objective': 'Build word statistics'},
            identities=[{'id': role, 'session_id': f'{role}-session', 'pane_id': f'p{i}'}
                        for i, role in enumerate(('worker', 'supervisor', 'consultant'))])
        data.update(overrides)
        path = runs / f'{name}.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE runs(singleton INTEGER PRIMARY KEY, data TEXT)')
            db.execute('INSERT INTO runs VALUES(1,?)', (json.dumps(data),))
            db.execute('CREATE TABLE events(seq INTEGER PRIMARY KEY, kind TEXT, data TEXT)')
        return path

    def invoke(*args):
        result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(index), *args],
                                capture_output=True, text=True, cwd=project)
        assert result.returncode == 0, result.stderr
        return result.stdout

    return make_run, invoke, index, project


def test_one_entry_per_run_preserves_unrelated_conversation(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make()
    output = run('--here')
    assert len(output.splitlines()) == 2
    assert 'trinity' in output and 'run-1' in output
    assert 'unrelated conversation' in output
    assert 'implementation' not in output


def test_reused_trio_is_two_runs_and_limit_applies_after_grouping(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make('run-1')
    make('run-2')
    output = run('-n', '2')
    assert len(output.splitlines()) == 2
    assert 'run-1' in output and 'run-2' in output


def test_search_member_returns_whole_run_and_matching_transcript(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make()
    output = run('search', 'nebula', '--expand', '--paths')
    assert 'trinity' in output and 'run-1' in output
    for role in ('worker', 'supervisor', 'consultant'):
        assert role in output
    assert '/transcripts/review-segment.jsonl' in output
    assert 'unrelated conversation' not in output


def test_search_run_objective_and_id(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make()
    assert 'run-1' in run('search', 'word statistics')
    assert 'run-1' in run('search', 'run-1')


def test_raw_bypasses_grouping(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make()
    output = run('--raw')
    assert len(output.splitlines()) == 4
    assert 'implementation' in output and 'trinity' not in output


def test_unknown_member_is_explicit_and_run_without_members_does_not_hide_sessions(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make(identities=[{'id': 'worker', 'session_id': 'missing-session'}])
    output = run('--expand')
    assert 'missing-session' in output and 'not indexed' in output
    assert 'unrelated conversation' in output


def test_malformed_run_is_ignored_without_writes(ledger_fixture):
    make, run, index, project = ledger_fixture
    path = make(project=[])
    before = {p: p.read_bytes() for p in (index, path)}
    output = run()
    assert len(output.splitlines()) == 4
    assert before == {p: p.read_bytes() for p in before}
    assert not list(project.rglob('*-wal'))


def test_like_fallback_and_agent_filter(ledger_fixture):
    make, run, index, _ = ledger_fixture
    make()
    with sqlite3.connect(index) as db:
        db.execute('DROP TABLE sessions_fts')
        db.execute('DROP TABLE user_prompts_fts')
    assert 'run-1' in run('search', 'nebula')
    assert run('search', 'nebula', '-a', 'claude') == ''
    assert 'run-1' in run('search', 'planning', '-a', 'claude')


def test_historical_identity_and_duplicate_run_snapshot(ledger_fixture):
    make, run, _, _ = ledger_fixture
    path = make(identities=[{'id': 'worker', 'session_id': 'replacement'}])
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO events VALUES(1,?,?)',
                   ('identity_bound', json.dumps({'id': 'consultant', 'session_id': 'consultant-session'})))
    (path.parent / 'copy.sqlite3').write_bytes(path.read_bytes())
    output = run('search', 'nebula')
    assert len(output.splitlines()) == 1 and 'run-1' in output


def test_explicit_trinity_directory_for_unindexed_project(ledger_fixture, tmp_path):
    make, run, _, _ = ledger_fixture
    path = make()
    moved = tmp_path / 'elsewhere'
    moved.mkdir()
    path.rename(moved / path.name)
    assert 'run-1' in run('--trinity-dir', str(moved))


def test_valid_run_and_index_remain_byte_identical(ledger_fixture):
    make, run, index, _ = ledger_fixture
    path = make()
    before = {p: p.read_bytes() for p in (index, path)}
    assert 'run-1' in run('--expand', '--paths')
    assert before == {p: p.read_bytes() for p in before}
    assert not path.with_name(path.name + '-wal').exists()


def test_live_wal_snapshot_is_visible(ledger_fixture):
    make, run, _, _ = ledger_fixture
    path = make()
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        data = json.loads(db.execute('SELECT data FROM runs').fetchone()[0])
        data['project']['objective'] = 'Fresh WAL objective'
        db.execute('UPDATE runs SET data=?', (json.dumps(data),))
        db.commit()
        assert 'Fresh WAL objective' in run('search', 'Fresh WAL')
    finally:
        db.close()


def test_no_members_or_unrelated_database_cannot_claim_sessions(ledger_fixture):
    make, run, _, project = ledger_fixture
    make(identities=[])
    (project / '.trinity/unrelated.sqlite3').write_text('not sqlite')
    assert len(run().splitlines()) == 4


def test_directory_filter_does_not_include_other_runs(ledger_fixture, tmp_path):
    make, run, _, _ = ledger_fixture
    make()
    other = tmp_path / 'other'
    other.mkdir()
    assert run('--dir', str(other)) == ''


def test_raw_search_keeps_original_session_and_path(ledger_fixture):
    make, run, _, _ = ledger_fixture
    make()
    output = run('search', 'nebula', '--raw', '--paths')
    assert 'independent assessment' in output
    assert '/transcripts/review-segment.jsonl' in output
    assert 'trinity' not in output


def test_run_survives_source_database_removal(ledger_fixture):
    make, run, _, _ = ledger_fixture
    path = make()
    assert 'run-1' in run()
    path.unlink()
    output = run('search', 'nebula', '--expand')
    assert 'run-1' in output and 'consultant-session' in output


def test_overwritten_current_database_keeps_both_run_ids(ledger_fixture):
    make, run, _, _ = ledger_fixture
    path = make()
    assert 'run-1' in run()
    with sqlite3.connect(path) as db:
        data = json.loads(db.execute('SELECT data FROM runs').fetchone()[0])
        data['id'] = 'run-2'
        db.execute('UPDATE runs SET data=?', (json.dumps(data),))
    output = run()
    assert 'run-1' in output and 'run-2' in output


def test_registry_is_private_and_unchanged_when_nothing_changes(ledger_fixture):
    make, run, index, _ = ledger_fixture
    make()
    run()
    registry = index.with_suffix('.trinity-runs.json')
    before = registry.stat()
    run()
    assert registry.stat().st_mtime_ns == before.st_mtime_ns
    assert registry.stat().st_mode & 0o777 == 0o600


def test_corrupt_registry_does_not_break_live_results(ledger_fixture):
    make, run, index, _ = ledger_fixture
    make()
    registry = index.with_suffix('.trinity-runs.json')
    registry.write_text('{"version":1,"runs":{"invalid":null}}')
    assert 'run-1' in run()
    assert registry.read_text() == '{"version":1,"runs":{"invalid":null}}'
