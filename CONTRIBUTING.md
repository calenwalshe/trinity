# Contributing

Thanks for helping improve Trinity. Contributions are reviewed and merged into
the public `main` branch through pull requests.

## Development setup

Use Python 3.10 or newer and `uv`:

```sh
uv sync --extra test
uv run pytest -q
uv build
```

Keep changes focused. Add or update tests for behavior changes, and update
public documentation when a command, configuration option, or installation
step changes. Do not include credentials, local SQLite run state, agent
transcripts, or private machine configuration.

## Pull requests

1. Branch from the latest public `main`.
2. Explain the user-visible behavior and any compatibility impact.
3. Run the test suite and package build locally.
4. Open a pull request against `main` and respond to any maintainer feedback.

Pull requests must pass the required CI checks before merge. Maintainer review
is welcome but is not a merge requirement.

## Distribution

The public GitHub `main` branch is the canonical distribution. Users install
Trinity directly from the repository with the command in `README.md`; no
package-index account, release upload, or version tag is required.
