---
name: trinity
description: Start and coordinate a three-role Trinity run only when the user explicitly invokes Trinity.
---

# Trinity

Start a run only when the user explicitly invokes Trinity, such as `/trinity`,
“start Trinity for this task,” or a direct request to launch the Trinity team.
Ordinary discussion, brainstorming, or a mention of Trinity is not an invocation.

## Before launching

Gather the task details from the user's explicit request and current conversation:

- A concrete objective, described in at least two words.
- The repository to work in. Resolve it to the root of an existing Git checkout.
- At least one checkable success criterion.
- Any constraints the user supplied.

If the objective, repository, or criteria are missing or unclear, ask for the
missing information first. Do not run a Trinity command, create a run database,
or open Herdr panes until the required context is complete.

## Start the team

From inside Herdr, run the installed `trinity` command with the repository's
ignored local state path. Pass one `--criterion` per success criterion and one
`--constraint` per constraint. For example:

```sh
mkdir -p .trinity
trinity --db .trinity/run.sqlite3 launch "Build spaced repetition quizzes" \
  --repo "$PWD" \
  --criterion "A learner can complete a quiz and see the score" \
  --criterion "Results are saved for the next session"
```

An explicit invocation authorizes starting the Trinity run. By default, the
command records that authorization, opens a dedicated tab in the caller's
current workspace, starts worker, supervisor, and consultant in the agreed
three-pane layout, and submits the task context to the supervisor. Report the
command's result to the user. A submitted prompt is not evidence that the
supervisor accepted the task or completed it.

If the user's configuration sets `launch.approval_mode` to `manual`, include
the configured file with `--config PATH`. The command then records a proposal
and starts no agents. Show the proposal and its `start_command`; the user must
run that command from their own shell pane to approve the run. Never approve it
from an agent pane.

Starting a Trinity run does not authorize publishing, deployment, credential
use, payments, notifications, or other external actions. Those actions still
need their own authorization.
