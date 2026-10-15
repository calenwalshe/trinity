#!/usr/bin/env python3
"""ledger — find prior agent work across Claude Code and Codex sessions.

One line per Trinity run or standalone session, grep-friendly:

    <when>  <agent>  <n_prompts>  <cwd>  <title>

so an agent orienting itself can pipe this straight into grep:

    ledger | grep -i voice
    ledger --here
    ledger search "turnstile"
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from trinity_groups import project_rows

DB_PATH = Path(__file__).resolve().parent / "sessions.db"


def ago(ts):
    """Compact relative age: 3h, 5d, 2w."""
    if not ts:
        return "?"
    d = max(0, time.time() - ts)
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{int(d // 3600)}h"
    if d < 86400 * 14:
        return f"{int(d // 86400)}d"
    return f"{int(d // (86400 * 7))}w"


def connect():
    if not DB_PATH.exists():
        sys.exit(f"no index at {DB_PATH} — run build_index.py first")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def emit(rows, show_path=False, expand=False):
    """Print rows as fixed-ish columns. Stays one line per run/session so that
    grep, awk and a reading agent all see the same record."""
    for r in rows:
        title = (r["ai_title"] or r["first_prompt"] or "").replace("\n", " ").strip()
        if r["agent"] == "trinity":
            print("  ".join((f"{ago(r['last_ts']):>4}", "trinity",
                f"{len(r['children'])} sessions", r['start_cwd'],
                f"[{r['session_id']}] {r['status']} {title[:100]}")))
            if show_path:
                print(f"      {r['jsonl_path']}")
            if expand:
                for child in r['children']:
                    session = child['session']
                    detail = (f"{session['agent']} {session['n_user_msgs']}p (whole session)"
                              if session else 'not indexed')
                    print(f"      {child['role']}: {child['session_id']}  {detail}")
                    if show_path and session:
                        print(f"          {session['jsonl_path']}")
            continue
        line = "  ".join((
            f"{ago(r['last_ts']):>4}",
            f"{r['agent']:<6}",
            f"{r['n_user_msgs']:>3}p",
            r["start_cwd"] or "-",
            title[:100],
        ))
        if show_path:
            line += f"\n      {r['jsonl_path']}"
        print(line)


BASE = """SELECT session_id, jsonl_path, start_cwd, ai_title, first_prompt,
                 last_ts, n_user_msgs, agent FROM sessions"""


def cmd_list(conn, args):
    where, params = [], []
    if args.agent:
        where.append("agent = ?")
        params.append(args.agent)
    if args.dir:
        # Match the session's own cwd OR any directory it touched, so work that
        # drifted into a folder is findable from that folder.
        where.append("""(start_cwd LIKE ? OR session_id IN (
                           SELECT session_id FROM session_paths WHERE path LIKE ?))""")
        d = os.path.abspath(os.path.expanduser(args.dir)).rstrip("/")
        params += [d + "%", d + "%"]
    if not args.all:
        # Default to sessions that did real work; one-turn probes are noise.
        where.append("n_user_msgs >= ?")
        params.append(args.min_prompts)
    sql = BASE + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY last_ts DESC"
    emit(project_rows(conn, conn.execute(sql, params), args), args.paths, args.expand)


def cmd_search(conn, args):
    """Full-text over titles and every human-authored prompt, newest first."""
    q = " ".join(args.query)
    try:
        sql = f"""{BASE} WHERE session_id IN (
                     SELECT session_id FROM sessions_fts WHERE sessions_fts MATCH ?
                     UNION
                     SELECT session_id FROM user_prompts_fts WHERE user_prompts_fts MATCH ?)
                  ORDER BY last_ts DESC"""
        rows = list(conn.execute(sql, (q, q)))
        prompt_paths = {}
        for match in conn.execute(
            "SELECT session_id, file_path FROM user_prompts_fts "
            "WHERE user_prompts_fts MATCH ? ORDER BY rowid",
            (q,),
        ):
            prompt_paths.setdefault(match["session_id"], match["file_path"])
        rows = [
            {**dict(row), "jsonl_path": prompt_paths.get(row["session_id"], row["jsonl_path"])}
            for row in rows
        ]
    except sqlite3.OperationalError:
        # No FTS5 (or a query its parser rejects) — fall back to LIKE.
        like = f"%{q}%"
        sql = f"""{BASE} WHERE ai_title LIKE ? OR first_prompt LIKE ? OR last_prompt LIKE ?
                  OR session_id IN (SELECT session_id FROM user_prompts WHERE prompt LIKE ?)
                  ORDER BY last_ts DESC"""
        rows = list(conn.execute(sql, (like, like, like, like)))
    if args.agent:
        rows = [row for row in rows if row['agent'] == args.agent]
    emit(project_rows(conn, rows, args, query=q), args.paths, args.expand)


def cmd_dirs(conn, args):
    """Which project directories have the most recorded work."""
    sql = """SELECT sp.path, COUNT(DISTINCT sp.session_id) n, MAX(s.last_ts) last
             FROM session_paths sp JOIN sessions s USING (session_id)
             WHERE sp.source = 'cwd' AND sp.path LIKE ?
             GROUP BY sp.path ORDER BY last DESC LIMIT ?"""
    pref = os.path.abspath(os.path.expanduser(args.under)).rstrip("/") + "%"
    for r in conn.execute(sql, (pref, args.limit)):
        print(f"{ago(r['last']):>4}  {r['n']:>4} sessions  {r['path']}")


def cmd_stats(conn, _args):
    for r in conn.execute(
        "SELECT agent, COUNT(*) n, SUM(n_user_msgs) p FROM sessions GROUP BY agent"
    ):
        print(f"{r['agent']:<8} {r['n']:>7} sessions  {r['p'] or 0:>8} prompts")
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"{'total':<8} {total:>7} sessions")


def main():
    ap = argparse.ArgumentParser(
        prog="ledger", description="find prior Claude/Codex work")
    ap.add_argument("--db", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("-n", "--limit", type=int, default=40)
        p.add_argument("-a", "--agent", choices=("claude", "codex"))
        p.add_argument("--raw", action="store_true", help="show individual agent sessions")
        p.add_argument("--expand", action="store_true", help="show each Trinity run's role sessions")
        p.add_argument("--trinity-dir", action="append", default=[],
                       help="also read run databases from this directory (repeatable)")
        p.add_argument("--paths", action="store_true",
                       help="also print the transcript path")

    p = sub.add_parser("list", help="recent sessions (default)")
    common(p)
    p.add_argument("-d", "--dir", help="restrict to a directory subtree")
    p.add_argument("--here", action="store_true", help="shorthand for --dir .")
    p.add_argument("--all", action="store_true", help="include one-turn probes")
    p.add_argument("--min-prompts", type=int, default=2)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("search", help="full-text over titles and all human prompts")
    common(p)
    p.add_argument("query", nargs="+")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("dirs", help="directories ranked by recorded work")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--under", default="~/work")
    p.set_defaults(fn=cmd_dirs)

    p = sub.add_parser("stats", help="row counts per agent")
    p.set_defaults(fn=cmd_stats)

    # Bare `ledger` and `ledger --here` behave as `ledger list`. A global
    # option given before the subcommand (e.g. --db) must stay ahead of it.
    argv = sys.argv[1:]
    head = []
    while len(argv) >= 2 and argv[0] == "--db":
        head += argv[:2]
        argv = argv[2:]
    if not argv or argv[0].startswith("-"):
        argv = ["list"] + argv
    args = ap.parse_args(head + argv)
    if getattr(args, "here", False):
        args.dir = os.getcwd()

    global DB_PATH
    if args.db:
        DB_PATH = Path(args.db)
    args.trinity_cache = DB_PATH.with_suffix(".trinity-runs.json")
    conn = connect()
    try:
        args.fn(conn, args)
    except BrokenPipeError:
        pass  # piped into head/grep
    finally:
        conn.close()


if __name__ == "__main__":
    main()
