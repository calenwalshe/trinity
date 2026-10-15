# Trinity

Trinity coordinates a worker, supervisor, and independent consultant in a
Herdr workspace. It stores run state in SQLite and provides a Python CLI plus
an optional agent skill for explicit task launches.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A running [Herdr](https://herdr.dev/) session; launch commands must run inside
  a Herdr-managed pane
- A supported agent CLI for the configured worker, supervisor, and consultant

Trinity has no Python runtime dependencies. Herdr provides pane and agent
management; Trinity does not install agent configuration or credentials for you.

## Install the CLI

```sh
uv tool install herdr-trinity
trinity --help
```

Upgrade the installed release with:

```sh
uv tool upgrade herdr-trinity
```

To run the current private development checkout as an editable tool:

```sh
uv tool install --editable /path/to/trinity-dev --force
```

## Install the optional Trinity skill

Installing the CLI does not write to your agent's skill directory. Copy the
repository skill into the directory used by your agent host.

For Codex:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/trinity"
cp skills/trinity/SKILL.md "${CODEX_HOME:-$HOME/.codex}/skills/trinity/SKILL.md"
```

For Claude Code:

```sh
mkdir -p "$HOME/.claude/skills/trinity"
cp skills/trinity/SKILL.md "$HOME/.claude/skills/trinity/SKILL.md"
```

Restart or refresh the agent host if it does not discover the new skill
automatically.

## Start a run

From a Herdr-managed pane in the repository, provide a concrete objective and
at least one checkable success criterion:

```sh
mkdir -p .trinity
trinity --db .trinity/run.sqlite3 launch "Build spaced repetition quizzes" \
  --repo "$PWD" \
  --criterion "A learner can complete a quiz and see the score" \
  --criterion "Results are saved for the next session"
```

An explicit `launch` command authorizes starting the run. By default, Trinity
opens a dedicated tab in the current Herdr workspace, starts its three roles,
and sends the task context to the supervisor. It asks for missing task context
before creating run state or Herdr panes. Ordinary discussion does not start a
run.

Inspect the run with:

```sh
trinity --db .trinity/run.sqlite3 status
trinity --db .trinity/run.sqlite3 progress
```

Manual shell approval is available as a configuration opt-out with
`launch.approval_mode` set to `manual`. In that mode, `launch` records a
proposal and starts no agents; the user approves it from their own shell pane
with the returned `start_command`. The lower-level `propose` and
`start --user-approved` commands remain available for manual workflows.

Starting a run does not authorize external actions such as publishing,
deployment, credential use, payments, or notifications. Those actions require
their own authorization.

## Development

```sh
uv sync --extra test
uv run pytest -q
uv build
```

Pull requests run the test suite and build the package. Reviewed version tags
are the only release trigger. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
contribution flow and [SECURITY.md](SECURITY.md) for private vulnerability
reporting guidance.

## License

Trinity is released under the [MIT License](LICENSE).
