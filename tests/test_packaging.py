import os
from pathlib import Path
import subprocess
import sys
import venv

from trinity.coordinator import Coordinator
from trinity.store import Store


ROOT = Path(__file__).resolve().parents[1]


def test_distribution_metadata_matches_release_name():
    metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "herdr-trinity"' in metadata
    assert 'version = "1.0.0"' in metadata
    assert 'readme = "README.md"' in metadata
    assert 'requires-python = ">=3.10"' in metadata
    assert "dependencies = []" in metadata


def test_installed_console_entrypoint_runs_help_and_reads_persisted_state(tmp_path):
    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                            "--wheel-dir", str(wheel_dir), str(ROOT)], capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("herdr_trinity-*.whl"))

    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    command = scripts / ("trinity.exe" if os.name == "nt" else "trinity")
    installed = subprocess.run([sys.executable, "-m", "pip", "--python", str(python), "install",
                                "--no-deps", str(wheel)],
                               capture_output=True, text=True, timeout=120)
    assert installed.returncode == 0, installed.stderr
    help_result = subprocess.run([str(command), "--help"], capture_output=True, text=True, timeout=15)
    assert help_result.returncode == 0, help_result.stderr
    assert "launch" in help_result.stdout

    database = tmp_path / "run.sqlite3"
    store = Store(database)
    Coordinator(store).init_run("Persisted package smoke test", [])
    store.close()
    status = subprocess.run([str(command), "--db", str(database), "status"], capture_output=True, text=True, timeout=15)
    assert status.returncode == 0, status.stderr
    assert "Persisted package smoke test" in status.stdout


def test_readme_documents_uv_install_and_upgrade():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install herdr-trinity" in readme
    assert "uv tool upgrade herdr-trinity" in readme
    assert "Python 3.10" in readme
    assert "Herdr" in readme


def test_skill_install_instructions_reference_repository_skill():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "skills/trinity/SKILL.md" in readme
    assert "CODEX_HOME" in readme and "$HOME/.claude/skills" in readme


def test_ci_workflow_tests_supported_python_endpoints_and_builds_package():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '"3.10"' in workflow and '"3.15"' in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m build" in workflow and "python -m twine check" in workflow


def test_release_workflow_only_publishes_reviewed_version_tags():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "push:" in workflow and "tags:" in workflow and '"v[0-9]*"' in workflow
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in workflow
