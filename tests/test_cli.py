import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from openfoundry.cli import app
from openfoundry.config import ProjectPaths
from openfoundry.factory import Factory
from typer.testing import CliRunner


def test_cli_help_version_and_bootstrap_plan(tmp_path):
    runner = CliRunner()
    assert runner.invoke(app, ["--version"]).stdout.strip() == "2.0.0"
    assert runner.invoke(app, ["--help"]).exit_code == 0
    root = tmp_path / "project"
    root.mkdir()
    (root / "openfoundry.yaml").write_text(
        """apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata: {name: cli-test, namespace: local/cli-test}
spec: {owners: [local-user], extensions: {}}
"""
    )
    result = runner.invoke(
        app,
        ["--project", str(root), "--output", "json", "bootstrap", "--plan"],
    )
    assert result.exit_code == 0, result.output
    assert "initialize-database" in result.stdout
    capabilities = runner.invoke(
        app, ["--project", str(root), "--output", "json", "agent", "capabilities"]
    )
    assert capabilities.exit_code == 0
    assert json.loads(capabilities.stdout)["catalogVersion"] == 3
    context = runner.invoke(app, ["--project", str(root), "--output", "json", "agent", "context"])
    assert context.exit_code == 0
    assert json.loads(context.stdout)["bootstrapPlan"]["actions"]


def _full_project(tmp_path):
    root = tmp_path / "full-project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "openfoundry.yaml").write_text(
        """apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata: {name: cli-full, namespace: local/cli-full}
spec: {owners: [local-user], extensions: {}}
"""
    )
    shutil.copytree("modules", root / "modules")
    shutil.copytree("workloads", root / "workloads")
    (root / "bindings").mkdir()
    shutil.copy("bindings/local.yaml", root / "bindings/local.yaml")
    (root / "numbers.jsonl").write_text(Path("data/fixtures/numbers.jsonl").read_text())
    (root / "rights.yaml").write_text("license: CC0-1.0\ntrainingAllowed: true\n")
    return root


def test_cli_complete_local_lifecycle(tmp_path):
    if sys.platform == "darwin":
        pytest.skip("deny-stage lifecycle needs user namespaces; unavailable on macOS")
    root = _full_project(tmp_path)
    runner = CliRunner()

    def invoke(*arguments):
        result = runner.invoke(
            app, ["--project", str(root), "--output", "json", *map(str, arguments)]
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    assert invoke("bootstrap")["ready"]
    assert invoke("doctor")["ready"]
    assert invoke("agent", "context", "--limit", "2", "--max-bytes", "16384")["readiness"]["ready"]
    assert "Project" in invoke("schema", "list")["kinds"]
    assert invoke("schema", "show", "Project")["x-openfoundry-kind"] == "Project"
    assert invoke("schema", "validate", root / "openfoundry.yaml")["kind"] == "Project"
    assert (
        invoke(
            "store",
            "add",
            "secondary",
            "--driver",
            "filesystem",
            "--endpoint",
            ".openfoundry/secondary",
        )["kind"]
        == "ArtifactStore"
    )
    assert len(invoke("store", "list")) == 1
    assert (
        invoke(
            "data",
            "add",
            root / "numbers.jsonl",
            "--name",
            "example-numbers",
            "--mode",
            "copy",
            "--rights",
            root / "rights.yaml",
        )["kind"]
        == "DatasetSnapshot"
    )
    assert invoke("data", "verify", "example-numbers")["valid"]
    assert len(invoke("data", "list")) == 1
    manifest = root / "modules/examples/statistical/module.yaml"
    validated_module = invoke("module", "validate", manifest)[0]
    assert validated_module["valid"]
    assert invoke("module", "test", manifest)[0]["passed"] == 1
    scaffold = root / "modules/new-model"
    assert invoke("module", "init", scaffold)["valid"]
    assert invoke("module", "test", scaffold / "module.yaml")[0]["passed"] == 1
    assert {item["name"] for item in invoke("executor", "list")["providers"]} >= {"local"}
    assert invoke(
        "executor",
        "preflight",
        root / "bindings/local.yaml",
        "--workload",
        root / "workloads/example-statistical.yaml",
    )["ready"]
    plan = invoke("sync", "push", "dataset/example-numbers", "--to", "secondary", "--plan")
    assert not plan["mutates"]
    assert invoke("sync", "push", "dataset/example-numbers", "--to", "secondary")["committed"]
    run = invoke(
        "run",
        root / "workloads/example-statistical.yaml",
        "--binding",
        root / "bindings/local.yaml",
    )
    assert run["state"] == "Succeeded"
    run_id = run["runId"]
    run_status = invoke("runs", "status", run_id)
    assert run_status["status"]["state"] == "Succeeded"
    run_resource = invoke("resource", "list", "--kind", "Run")[0]
    assert invoke("runs", "list") == [
        {
            "runId": run_id,
            "state": "Succeeded",
            "workload": run_resource["spec"]["workloadRef"],
            "createdAt": run_resource["metadata"]["createdAt"],
        }
    ]
    rendered = runner.invoke(app, ["--project", str(root), "runs", "list"])
    assert rendered.exit_code == 0, rendered.output
    header, row = rendered.stdout.splitlines()
    assert header.split() == ["runId", "state", "workload", "createdAt"]
    assert row.split()[:2] == [run_id, "Succeeded"]
    evaluation = invoke("evaluate", f"run/{run_id}")
    assert evaluation["spec"]["scores"]["passed"]
    evaluation_ref = (
        f"openfoundry://local/cli-full/evaluationresult/{evaluation['metadata']['name']}"
        f"@{evaluation['metadata']['revision']}"
    )
    invalid_experiment = runner.invoke(
        app,
        [
            "--project",
            str(root),
            "--output",
            "json",
            "experiment",
            "create",
            "self-comparison",
            "--baseline",
            evaluation_ref,
            "--candidate",
            evaluation_ref,
            "--metric",
            "passed",
            "--direction",
            "sideways",
        ],
    )
    assert invalid_experiment.exit_code == 1
    assert json.loads(invalid_experiment.stdout)["error"]["code"] == "validation_error"
    evidence = invoke("release", "evidence", f"run/{run_id}")
    assert set(evidence["subjects"]) == {
        run["outputs"]["train.model"],
        *run_status["execution"]["digests"]["modules"].values(),
    }
    vulnerability_report = root / "vulnerability-report.yaml"
    vulnerability_report.write_text(
        yaml.safe_dump(
            {
                **evidence,
                "scanner": {"name": "test-scanner", "version": "1"},
                "databaseRevision": "test-db-1",
            }
        )
    )
    release = invoke(
        "release",
        "create",
        run_id,
        "--name",
        "release-one",
        "--intended-use",
        "test",
        "--promote",
        "--vulnerability-report",
        vulnerability_report,
    )
    assert release["kind"] == "Release"
    assert invoke("release", "list") == [
        {
            "name": "release-one",
            "revision": release["metadata"]["revision"],
            "evaluationPassed": True,
            "aliases": ["candidate"],
            "createdAt": release["metadata"]["createdAt"],
        }
    ]
    assert invoke("release", "promote", "release-one", "--alias", "stable")["version"] == 1
    assert invoke("release", "show", "release/release-one")["aliases"] == ["candidate", "stable"]
    assert invoke("lineage", "show", f"run:{run_id}/stage:train")
    assert invoke("resource", "list", "--kind", "Release")[0]["metadata"]["name"] == "release-one"
    deployment = {
        "apiVersion": "openfoundry.dev/v1alpha1",
        "kind": "DeploymentSpec",
        "metadata": {"name": "edge-one", "namespace": "local/cli-full"},
        "spec": {
            "releaseRef": "release/release-one",
            "extensions": {"form": "edge"},
        },
    }
    deployment_path = root / "deployment.yaml"
    deployment_path.write_text(yaml.safe_dump(deployment))
    assert invoke("deploy", deployment_path)["state"] == "packaged"
    assert invoke("deployment", "status", "edge-one")["status"]["state"] == "packaged"
    [deployment] = invoke("deployment", "list")
    assert (deployment["name"], deployment["release"], deployment["state"]) == (
        "edge-one",
        f"openfoundry://local/cli-full/release/release-one@{release['metadata']['revision']}",
        "packaged",
    )
    revoked = invoke("data", "revoke", "example-numbers", "--reason", "test withdrawal")
    assert revoked["spec"]["rights"]["trainingAllowed"] is False
    assert revoked["spec"]["rights"]["revoked"] is True

    operations = invoke("operation", "list")
    assert len(operations) == 1
    assert operations[0]["kind"] == "run"
    assert operations[0]["state"] == "succeeded"
    api_token = invoke("admin", "token", "create", "--actor", "reader", "--scope", "read")
    assert api_token["actor"] == "reader"
    assert api_token["token"] not in repr(invoke("admin", "token", "list"))
    assert invoke("admin", "token", "revoke", api_token["tokenId"])["revoked"]
    assert (
        invoke("admin", "secret", "set", "example", "--purpose", "test", "--value", "x")["version"]
        == 1
    )
    assert (
        invoke(
            "admin",
            "secret",
            "set",
            "example",
            "--purpose",
            "test",
            "--value",
            "replacement",
            "--expected-version",
            "1",
        )["version"]
        == 2
    )
    assert "example" in {item["name"] for item in invoke("admin", "secret", "list")}
    backup = invoke("admin", "backup", root.parent / "factory.openfoundry-backup")
    assert backup["integrity"]
    restored = root.parent / "restored"
    restored.mkdir()
    shutil.copy(root / "openfoundry.yaml", restored / "openfoundry.yaml")
    restoration = runner.invoke(
        app,
        [
            "--project",
            str(restored),
            "--output",
            "json",
            "admin",
            "restore",
            backup["path"],
            "--expected-key-id",
            backup["keyId"],
        ],
    )
    assert restoration.exit_code == 0, restoration.output
    assert json.loads(restoration.stdout)["keyId"] == backup["keyId"]


def test_catalog_and_command_tree_match_and_support_focused_help():
    from typer.main import get_command

    def leaves(command, path=()):
        children = getattr(command, "commands", {})
        if children:
            return {
                leaf for name, child in children.items() for leaf in leaves(child, (*path, name))
            }
        return {path}

    runner = CliRunner()
    result = runner.invoke(app, ["--output", "json", "agent", "capabilities"])
    assert result.exit_code == 0, result.output
    catalog = json.loads(result.stdout)
    advertised = set()
    for action in catalog["actions"]:
        words = action["interfaces"]["cli"].split()[1:]
        path = []
        for word in words:
            if "<" in word or word.startswith(("[", "-")):
                break
            path.append(word)
        advertised.add(tuple(path))
        help_result = runner.invoke(app, [*path, "--help"])
        assert help_result.exit_code == 0, (action["action"], help_result.output)
    assert advertised == leaves(get_command(app))
    focused = runner.invoke(app, ["--output", "json", "agent", "capabilities", "release.promote"])
    assert focused.exit_code == 0, focused.output
    action = json.loads(focused.stdout)["actions"]
    assert len(action) == 1
    assert "--alias" in action[0]["interfaces"]["cli"]
    unknown = runner.invoke(app, ["--output", "json", "agent", "capabilities", "does-not-exist"])
    assert unknown.exit_code == 1
    assert json.loads(unknown.stdout)["error"]["code"] == "not_found"


def test_cli_secret_stdin(tmp_path):
    root = _full_project(tmp_path)
    runner = CliRunner()
    prefix = ["--project", str(root), "--output", "json"]
    assert runner.invoke(app, [*prefix, "bootstrap"]).exit_code == 0
    secret = runner.invoke(
        app,
        [*prefix, "admin", "secret", "set", "sample", "--purpose", "test", "--value-stdin"],
        input="private-value\n",
    )
    assert secret.exit_code == 0, secret.output
    assert "private-value" not in secret.output
    with Factory(ProjectPaths(root)) as factory:
        assert factory.secrets.get("sample", "test") == b"private-value"
    events = runner.invoke(app, [*prefix, "event", "list"])
    assert events.exit_code == 0
    assert isinstance(json.loads(events.stdout), list)
