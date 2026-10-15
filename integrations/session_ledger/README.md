# Trinity grouping for the shared session ledger

This is the shared Claude/Codex machine ledger with an additive Trinity run view.
It uses the existing session index; it does not replace the indexer or merge
transcripts. Maintained here alongside Trinity so the integration is reviewable.

## Usage

```sh
ledger --here                       # one entry per run or standalone conversation
ledger search "word statistics"     # search run objectives/IDs and member prompts
ledger --here --expand --paths       # role identities and conversation paths
ledger --raw                        # original individual session view
ledger --trinity-dir /path/to/.trinity # include an unindexed or relocated run directory
```

List/search limits apply after grouping. A match in any member brings back its
parent run; expansion includes all recorded members, including earlier bindings
when a role was replaced. `--agent` selects runs with that agent; prompt matches
must come from the selected agent. Unrelated sessions in the same directory stay
separate. `dirs` and `stats` continue to count physical agent sessions.

## Discovery and retention

The ledger checks `.trinity/*.sqlite3` and `.trinity/*.db` in directories already
recorded in its session index. `--trinity-dir` adds explicit directories. This
version understands the deterministic coordinator's `runs` and `identity_bound`
event records. It does not infer membership from pane layout, directory sharing,
or conversation text; other runtime formats need an explicit adapter.

Run IDs identify logical runs. The same three sessions may belong to several
runs; these remain distinct entries. Copies of the same run are deduplicated by
ID, with the newest file snapshot selected. Only recorded memberships are shown;
missing indexed transcripts are labelled `not indexed`.

Observed metadata is retained in `sessions.trinity-runs.json` beside `sessions.db`
(or the corresponding basename for `--db`). It contains run objectives, status,
source paths and role/session IDs, never prompt text. Atomic replacement and a
file lock protect concurrent ledger calls; files are private (0600). Run source
files and the session database are read only. A corrupt/unwritable registry warns
and falls back to currently discoverable runs without overwriting the bad file.
`--raw` bypasses discovery and registry writes.

Retention begins when ledger first observes a run. Runs deleted or overwritten
before discovery cannot be reconstructed by this integration. Missing sources
retain their last observed status and source path, which may no longer exist.

**Historical limits:** older runs lack reliable start/end timestamps. Search and
role transcript links cover each member's whole conversation, so a reused member
can return multiple runs. Prompt counts in expansion are whole-session counts,
not per-run totals. Parent age is the source database snapshot's modification
age, not a reconstructed execution timestamp. No transcript slicing is implied.

## Installation

Copy `ledger.py` and `trinity_groups.py` together into the existing session-index
directory (normally `~/.claude/session-index/`), backing up its current `ledger.py`
first. Keep its `sessions.db` and indexer intact. Existing `~/bin/ledger` symlinks
continue to work. Rollback restores the backed-up script; retained JSON metadata
can remain without affecting the original ledger.

Run regression tests from the Trinity repository:

```sh
python3 -m pytest tests/test_session_ledger.py -q
```
