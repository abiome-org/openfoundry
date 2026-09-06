import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import yaml
from _wheels import build_wheel, lock_for
from openfoundry.cli import app
from openfoundry.config import ProjectPaths
from openfoundry.experiment_definition import initialize
from openfoundry.factory import Factory
from test_experiments import project
from typer.testing import CliRunner


def test_cli_uses_configured_owner_and_respects_explicit_project(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    initialize(
        root / "experiment.yaml", name="owned", objective="Test", source="src", actor="alice"
    )
    runner = CliRunner()
    prefix = ["--project", str(root), "--output", "json"]
    with Factory(ProjectPaths(root)) as factory:
        assert factory.actor == "alice"
    result = runner.invoke(app, [*prefix, "data", "list"])
    assert result.exit_code == 0, result.output
    context = runner.invoke(app, [*prefix, "agent", "context"])
    assert json.loads(context.stdout)["project"]["actor"] == "alice"
    denied = runner.invoke(
        app,
        [
            *prefix,
            "--actor",
            "bob",
            "experiment",
            "run",
            "experiment.yaml",
            "--candidate",
            "baseline",
        ],
    )
    assert denied.exit_code == 1
    assert "bob" in denied.stdout
    nested = root / "nested"
    nested.mkdir()
    explicit = runner.invoke(app, ["--project", str(nested), "--output", "json", "bootstrap"])
    assert explicit.exit_code == 1
    assert "no openfoundry.yaml in project directory" in explicit.stdout
    monkeypatch.chdir(nested)
    discovered = runner.invoke(app, ["--output", "json", "data", "list"])
    assert discovered.exit_code == 0, discovered.output


def test_installed_defaults_run_uncommitted_scripts_without_openfoundry_in_worker(
    tmp_path, monkeypatch
):
    paths, definition = project(tmp_path)
    template = Path("templates/project/policies/default.yaml").read_text()
    (paths.root / "policies/local.yaml").write_text(
        template.replace("__OPENFOUNDRY_PROJECT_NAMESPACE__", "local/regression")
    )
    wheelhouse = tmp_path / "wheels"
    _, digest = build_wheel(wheelhouse)
    (paths.root / "src/requirements.lock").write_bytes(lock_for("openfoundrytiny", "1.0", digest))
    recipe = yaml.safe_load(definition.read_text())
    for stage in ("train", "evaluate"):
        recipe[stage]["dependencies"] = "requirements.lock"
    recipe["provider"] = {"dependencyWheelhouse": str(wheelhouse), "dependencyIndex": False}
    definition.write_text(yaml.safe_dump(recipe))
    isolated = tmp_path / "python"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(isolated)
    monkeypatch.setenv("PATH", f"{isolated / 'bin'}{os.pathsep}{os.environ['PATH']}")
    missing_openfoundry = subprocess.run(
        [str(isolated / "bin/python3"), "-I", "-c", "import openfoundry"], capture_output=True
    )
    assert missing_openfoundry.returncode != 0
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "openfoundry",
            "--project",
            str(paths.root),
            "--output",
            "json",
            "experiment",
            "run",
            str(definition),
            "--candidate",
            "candidate",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    run = json.loads(completed.stdout)
    assert run["scores"]["accuracy"] == 1
    with Factory(paths) as factory:
        evidence = factory._run_resource(run["id"])["spec"]["extensions"]
        assert evidence["worktree"]["dirty"] is True
        assert evidence["worktree"]["policy"] == "archive"
        assert evidence["worktree"]["commit"] is None
        assert set(evidence["moduleDigests"]) == {"train", "evaluate"}
        reproduced = factory.experiments.reproduce(run["id"])
        assert reproduced["scores"] == run["scores"]
