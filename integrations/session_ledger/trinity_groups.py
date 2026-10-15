"""Read-only Trinity run projection for the shared Claude/Codex ledger.

Directories locate candidate records; only recorded session IDs establish membership.
No transcripts, index rows, or coordinator state are modified.
"""
from contextlib import closing
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
import sqlite3

ROLES = ('supervisor', 'worker', 'consultant')


def read_run(path):
    """Return a validated coordinator snapshot, or None for unrelated/bad files."""
    try:
        uri = path.resolve().as_uri() + '?mode=ro'
        # Immutable avoids creating SQLite sidecars for settled databases. WAL
        # readers must see the live WAL instead of an older main-file snapshot.
        if not Path(str(path) + '-wal').exists():
            uri += '&immutable=1'
        with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as db:
            db.execute('BEGIN')
            row = db.execute('SELECT data FROM runs WHERE singleton=1').fetchone()
            data = json.loads(row[0]) if row else None
            if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not data['id']:
                return None
            project = data.get('project')
            if not isinstance(project, dict) or not isinstance(project.get('objective'), str):
                return None
            identities = data.get('identities', [])
            if not isinstance(identities, list):
                return None
            members = set()
            def add(identity):
                if (isinstance(identity, dict) and identity.get('id') in ROLES
                        and isinstance(identity.get('session_id'), str) and identity['session_id']):
                    members.add((identity['id'], identity['session_id']))
            for identity in identities:
                add(identity)
            # Include earlier role bindings when an agent was replaced mid-run.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'events' in tables:
                for event in db.execute("SELECT data FROM events WHERE kind='identity_bound'"):
                    add(json.loads(event[0]))
            if not members:
                return None
        updated = max(p.stat().st_mtime for p in (path, Path(str(path) + '-wal')) if p.exists())
        return dict(id=data['id'], objective=project['objective'],
                    status=str(data.get('status', 'unknown')),
                    members=[list(member) for member in sorted(members)],
                    path=str(path.resolve()), cwd=str(path.resolve().parent.parent),
                    updated=updated)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def discover(conn, extra_dirs=()):
    """Inspect only indexed project paths and explicitly supplied directories."""
    dirs = {Path(r[0]) / '.trinity' for r in conn.execute(
        "SELECT start_cwd FROM sessions WHERE start_cwd != '' "
        "UNION SELECT path FROM session_paths") if Path(r[0]).is_absolute()}
    dirs.update(Path(p).expanduser().resolve() for p in extra_dirs)
    runs = {}
    for directory in sorted(dirs):
        if not directory.is_dir():
            continue
        try:
            paths = sorted(set(directory.glob('*.sqlite3')) | set(directory.glob('*.db')))
        except OSError:
            continue
        for path in paths:
            run = read_run(path)
            if run and (run['id'] not in runs or run['updated'] > runs[run['id']]['updated']):
                runs[run['id']] = run
    return list(runs.values())


def remember(runs, cache_path):
    """Retain observed runs after their source file is replaced or removed.

    The companion registry stores identities and run metadata, never prompts.
    Lock + atomic replace prevents lost updates from simultaneous ledger calls.
    """
    cache_path = Path(cache_path)
    try:
        lock_fd = os.open(str(cache_path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            known = {}
            if cache_path.exists():
                saved = json.loads(cache_path.read_text())
                if saved.get('version') != 1 or not isinstance(saved.get('runs'), dict):
                    raise ValueError('unsupported Trinity registry format')
                known = saved['runs']
                for key, record in known.items():
                    if (not isinstance(record, dict) or record.get('id') != key
                            or not all(isinstance(record.get(field), str) for field in
                                       ('id', 'objective', 'status', 'path', 'cwd'))
                            or not isinstance(record.get('updated'), (float, int))
                            or not isinstance(record.get('members'), list)
                            or not all(isinstance(member, list) and len(member) == 2
                                       and member[0] in ROLES and isinstance(member[1], str)
                                       for member in record['members'])):
                        raise ValueError('invalid Trinity registry record')
            changed = False
            for run in runs:
                previous = known.get(run['id'])
                if previous and previous['updated'] > run['updated']:
                    continue
                if previous != run:
                    known[run['id']] = run
                    changed = True
            if changed:
                fd, temporary = tempfile.mkstemp(prefix=cache_path.name + '.', dir=cache_path.parent)
                try:
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(dict(version=1, runs=known), stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, cache_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return list(known.values())
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f'ledger: cannot retain Trinity history: {exc}', file=sys.stderr)
        return runs


def project_rows(conn, rows, args, query=None):
    """Collapse matching sessions, applying the output limit after projection."""
    rows = [dict(row) for row in rows]
    if args.raw:
        return rows[:args.limit] if args.limit >= 0 else rows
    all_sessions = {r['session_id']: dict(r) for r in conn.execute('SELECT * FROM sessions')}
    matches = {r['session_id']: r for r in rows}
    groups, represented = [], set()
    for run in remember(discover(conn, args.trinity_dir), args.trinity_cache):
        ids = {sid for _, sid in run['members']}
        member_match = bool(ids & matches.keys())
        # Run IDs/objectives are searchable even when no prompt mentions them.
        direct_match = query is not None and query.casefold() in (
            run['id'] + ' ' + run['objective']).casefold()
        if query is not None:
            qualifies = member_match or direct_match
        else:
            qualifies = member_match
            directory = getattr(args, 'dir', None)
            if directory:
                root = Path(directory).expanduser().resolve()
                qualifies = qualifies or Path(run['cwd']).is_relative_to(root)
            elif not args.agent:
                qualifies = True
        if args.agent and not any(
            all_sessions.get(sid, {}).get('agent') == args.agent for sid in ids
        ):
            qualifies = False
        if not qualifies:
            continue
        children = []
        for role, sid in run['members']:
            child = matches.get(sid) or all_sessions.get(sid)
            children.append(dict(role=role, session_id=sid, session=child))
        groups.append(dict(session_id=run['id'], agent='trinity',
                           ai_title=run['objective'], first_prompt='',
                           last_ts=run['updated'], start_cwd=run['cwd'],
                           jsonl_path=run['path'], status=run['status'], children=children))
        represented.update(ids)
    result = groups + [row for row in rows if row['session_id'] not in represented]
    result.sort(key=lambda row: row['last_ts'], reverse=True)
    return result[:args.limit] if args.limit >= 0 else result
