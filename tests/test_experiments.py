import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from openfoundry.api import create_app
from openfoundry.candidate_review import review, write_review
from openfoundry.cli import app
from openfoundry.config import ProjectPaths
from openfoundry.errors import IntegrityError, OperationCanceled, ValidationError
from openfoundry.executors import LocalExecutor
from openfoundry.experiment_definition import initialize, read_definition
from openfoundry.factory import Factory
from typer.testing import CliRunner

TRAIN = """import argparse, json, time
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--data")
p.add_argument("--output")
p.add_argument("--offset", type=float)
p.add_argument("--sleep", type=float)
a = p.parse_args()
time.sleep(a.sleep)
rows = json.loads(Path(a.data).read_text())
bias = sum(row["y"] - row["x"] for row in rows) / len(rows) + a.offset
Path(a.output).write_text(json.dumps({"bias": bias}))
"""
EVALUATE = """import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--data")
p.add_argument("--model")
p.add_argument("--output")
p.add_argument("--examples")
a = p.parse_args()
model = json.loads(Path(a.model).read_text())
rows = json.loads(Path(a.data).read_text())
results = [{"id": str(i), "input": row["x"], "expected": row["y"],
            "prediction": row["x"] + model["bias"],
            "score": int(abs(row["x"] + model["bias"] - row["y"]) < 1e-6)}
           for i, row in enumerate(rows)]
metrics = {"accuracy": sum(row["score"] for row in results) / len(results),
           "passed": True, "compatibilityPassed": isinstance(model["bias"], (float, int))}
Path(a.output).write_text(json.dumps(metrics))
Path(a.examples).write_text(json.dumps(results))
"""


def project(tmp_path, *, sleep=0):
    root = tmp_path / "experiment project"
    definition = root / "experiment.yaml"
    initialize(
        definition,
        name="regression",
        objective="Predict unseen values accurately.",
        source="src",
        actor="local-user",
    )
    (root / "src").mkdir()
    (root / "src/train.py").write_text(TRAIN)
    (root / "src/evaluate.py").write_text(EVALUATE)
    (root / "data.json").write_text(json.dumps([{"x": 1, "y": 3}, {"x": 2, "y": 4}]))
    recipe = yaml.safe_load(definition.read_text())
    recipe["data"] = {
        "samples": {
            "source": "data.json",
            "rights": {"license": "CC0-1.0", "trainingAllowed": True},
        }
    }
    recipe["train"]["command"] = [
        "python3",
        "train.py",
        "--data",
        "{inputs[samples]}",
        "--output",
        "{output}/model.json",
        "--offset",
        "{parameters[offset]}",
        "--sleep",
        "{parameters[sleep]}",
    ]
    recipe["train"]["artifacts"] = {"model": "model.json"}
    recipe["evaluate"]["command"] = [
        "python3",
        "evaluate.py",
        "--data",
        "{inputs[samples]}",
        "--model",
        "{inputs[model]}",
        "--output",
        "{output}/metrics.json",
        "--examples",
        "{output}/examples.json",
    ]
    recipe["evaluate"]["examples"] = "examples.json"
    recipe["candidates"] = {
        "baseline": {"rationale": "Existing model.", "parameters": {"offset": 1, "sleep": sleep}},
        "candidate": {
            "rationale": "Remove the fitted model's bias.",
            "parameters": {"offset": 0, "sleep": sleep},
        },
    }
    definition.write_text(yaml.safe_dump(recipe))
    return ProjectPaths(root), definition


def wait_until(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("experiment did not reach the expected state")


def test_script_candidates_review_export_and_reproduce_pinned_inputs(tmp_path):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        baseline = factory.experiments.run(definition, "baseline")
        candidate = factory.experiments.run(definition, "candidate")
        assert baseline["state"] == candidate["state"] == "succeeded"
        assert not baseline["scores"]["passed"]
        assert candidate["scores"]["passed"]
        report = review(factory.experiments, candidate["runId"], details=True)
        assert report["comparison"]["decision"] == "candidate"
        assert report["comparison"]["metrics"][0]["delta"] == 1
        assert report["changes"]["parameters"] == [{"name": "offset", "before": 1, "after": 0}]
        assert report["examples"]["total"] == 2
        assert report["candidate"]["measurement"]["wallSeconds"] > 0
        summary = review(factory.experiments, candidate["runId"])
        assert summary["comparison"] == report["comparison"]
        assert summary["examples"]["total"] == 2
        assert "items" not in summary["examples"]
        assert "reproduce" not in summary["candidate"]
        assert len(json.dumps(summary)) < 4096
        html = tmp_path / "review.html"
        report["objective"] = "<script>bad()</script>"
        write_review(report, html)
        assert "<script>bad()" not in html.read_text()
        assert "&lt;script&gt;" in html.read_text()
        exported = factory.experiments.export(candidate["runId"], tmp_path / "model")
        assert json.loads(
            (Path(exported["destination"]) / "artifacts/model/payload").read_text()
        ) == {"bias": 2.0}
        with pytest.raises(ValidationError, match="already exists"):
            factory.experiments.export(candidate["runId"], tmp_path / "model")
        (paths.root / "src/train.py").write_text("raise RuntimeError('new source must not run')")
        (paths.root / "data.json").write_text('[{"x": 1, "y": 999}]')
        factory.add_data(
            str(paths.root / "data.json"),
            name="regression-samples",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        reproduced = factory.experiments.reproduce(candidate["runId"])
        assert reproduced["scores"] == candidate["scores"]
        assert reproduced["reproduces"] == candidate["runId"]
        assert len(factory.experiments.list("regression")) == 3


def test_pending_cancellation_is_durable_and_idempotent(tmp_path):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        operation = factory.experiments.prepare(definition, "baseline")
        assert factory.experiments.status(operation["id"])["phase"] == "pending"
        assert factory.experiments.list("regression")[0]["candidate"] == "baseline"
        canceled = factory.run_control.request(operation["id"], "Try a different experiment")
        repeated = factory.run_control.request(operation["id"], "Repeated request")
        assert canceled == repeated
        assert canceled["state"] == "canceled"
        assert not (paths.runs / operation["id"] / "stages").exists()


def heldout_project(tmp_path):
    paths, definition = project(tmp_path)
    recipe = yaml.safe_load(definition.read_text())
    recipe["data"]["heldout"] = {
        "source": "heldout.json",
        "rights": {"license": "CC0-1.0", "trainingAllowed": False},
    }
    (paths.root / "heldout.json").write_text('[{"x": 10, "y": 12}]')
    recipe["train"]["inputs"] = ["samples"]
    recipe["evaluate"]["inputs"] = ["heldout", "model"]
    recipe["evaluate"]["command"] = [
        argument.replace("{inputs[samples]}", "{inputs[heldout]}")
        for argument in recipe["evaluate"]["command"]
    ]
    definition.write_text(yaml.safe_dump(recipe))
    return paths, definition


def test_evaluation_only_data_runs_reproduces_and_retains_rights(tmp_path):
    paths, definition = heldout_project(tmp_path)
    with Factory(paths) as factory:
        run = factory.experiments.run(definition, "candidate")
        assert run["scores"]["accuracy"] == 1
        assert factory.experiments.reproduce(run["id"])["scores"] == run["scores"]
        stage = paths.runs / run["id"] / "stages"
        assert set(json.loads((stage / "train/request.json").read_text())["inputs"]) == {"samples"}
        assert set(json.loads((stage / "evaluate/request.json").read_text())["inputs"]) == {
            "heldout",
            "model",
        }
        heldout = factory.find_resource("DatasetSnapshot", "regression-heldout")
        assert heldout["spec"]["rights"]["trainingAllowed"] is False
        release = factory.create_release(run["id"], name="heldout-model", intended_use="Testing")
        explanations = factory.publishing.promotion_decision(release).explanations
        assert any(item["rule"] == "rights" and item["effect"] == "allow" for item in explanations)


@pytest.mark.parametrize("use", ["training", "evaluation"])
def test_dataset_permissions_are_checked_for_actual_use(tmp_path, use):
    paths, definition = heldout_project(tmp_path)
    recipe = yaml.safe_load(definition.read_text())
    if use == "training":
        recipe["train"]["inputs"].append("heldout")
    else:
        recipe["data"]["heldout"]["rights"]["evaluationAllowed"] = False
    definition.write_text(yaml.safe_dump(recipe))
    with Factory(paths) as factory:
        with pytest.raises(ValidationError, match=f"rights do not allow {use}"):
            factory.experiments.prepare(definition, "candidate")
        assert factory.operations.list() == []


def test_revoked_evaluation_data_cannot_start_a_queued_run(tmp_path):
    paths, definition = heldout_project(tmp_path)
    with Factory(paths) as factory:
        queued = factory.experiments.prepare(definition, "candidate")
        factory.revoke_data("regression-heldout", reason="Evaluation permission withdrawn")
        with pytest.raises(ValidationError, match="current rights do not allow evaluation"):
            factory.execute_run_operation(queued["id"])
        assert not (paths.runs / queued["id"] / "stages").exists()


def test_shared_dataset_requires_both_uses_through_release(tmp_path):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        run = factory.experiments.run(definition, "candidate")
        factory.add_data(
            str(paths.root / "data.json"),
            name="regression-samples",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True, "evaluationAllowed": False},
        )
        release = factory.create_release(run["id"], name="shared-model", intended_use="Testing")
        explanations = factory.publishing.promotion_decision(release).explanations
        assert any(item["rule"] == "rights" and item["effect"] == "deny" for item in explanations)
        recipe = yaml.safe_load(definition.read_text())
        recipe["data"]["samples"]["rights"]["evaluationAllowed"] = False
        definition.write_text(yaml.safe_dump(recipe))
        with pytest.raises(ValidationError, match="rights do not allow evaluation"):
            factory.experiments.prepare(definition, "candidate")
        assert len(factory.operations.list()) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_cancel_interrupted_launch_respects_executor_recovery(tmp_path, monkeypatch, ambiguous):
    paths, definition = project(tmp_path)

    def interrupted(_executor, plan):
        if ambiguous:
            (plan.run_dir / "execution.json").write_text(
                json.dumps({"id": "unknown", "state": "launching"})
            )
        raise SystemExit("controller interrupted during launch")

    with Factory(paths) as factory:
        operation = factory.experiments.prepare(definition, "baseline")
        with monkeypatch.context() as patch:
            patch.setattr(LocalExecutor, "submit", interrupted)
            with pytest.raises(SystemExit, match="controller interrupted"):
                factory.execute_run_operation(operation["id"])
    receipt = paths.runs / operation["id"] / "stages/train/controller-execution.json"
    assert json.loads(receipt.read_text())["state"] == "launching"
    with Factory(paths) as restarted:
        if ambiguous:
            with pytest.raises(IntegrityError, match="indeterminate"):
                restarted.run_control.request(operation["id"], "Stop after interruption")
            assert restarted.operations.get(operation["id"])["state"] == "running"
        else:
            result = restarted.run_control.request(operation["id"], "Stop after interruption")
            assert result["state"] == "canceled"
            assert restarted.run_status(operation["id"])["status"]["state"] == "Canceled"
    assert not (receipt.parent.parent / "evaluate").exists()


def test_controller_interruption_and_rights_change_do_not_prevent_cancellation(
    tmp_path, monkeypatch
):
    paths, definition = project(tmp_path, sleep=30)
    with Factory(paths) as factory:
        operation = factory.experiments.prepare(definition, "baseline")
    worker = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openfoundry.run_worker",
            "--project",
            str(paths.root),
            "--operation",
            operation["id"],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    receipt = paths.runs / operation["id"] / "stages/train/controller-execution.json"
    try:
        wait_until(
            lambda: receipt.exists() and json.loads(receipt.read_text()).get("state") == "submitted"
        )
        worker.kill()
        worker.wait(timeout=10)
        with Factory(paths) as factory:
            factory.operations.request_cancel(
                operation["id"], actor=factory.actor, reason="Recorded before restart"
            )
            factory.revoke_data("regression-samples", reason="Source rights changed")
        with Factory(paths) as restarted:
            with monkeypatch.context() as patch:

                def unavailable(_executor, _execution_id):
                    raise RuntimeError("provider temporarily unavailable")

                patch.setattr(LocalExecutor, "cancel", unavailable)
                with pytest.raises(RuntimeError, match="provider temporarily"):
                    restarted.execute_run_operation(operation["id"])
                assert restarted.operations.get(operation["id"])["state"] == "running"
            result = restarted.execute_run_operation(operation["id"])
            assert result["state"] == "canceled"
            assert restarted.run_status(operation["id"])["status"]["state"] == "Canceled"
        assert not (paths.runs / operation["id"] / "stages/evaluate").exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)


def test_detached_run_can_be_canceled_through_authenticated_api(tmp_path):
    paths, definition = project(tmp_path, sleep=30)
    with Factory(paths) as factory:
        operation = factory.experiments.run(definition, "baseline", detach=True)
        token = factory.secrets.get("local-api-token", "api-authentication").decode()
    wait_until(
        lambda: (paths.runs / operation["id"] / "stages/train/controller-execution.json").exists()
    )
    with TestClient(create_app(paths)) as client:
        response = client.post(
            f"/v1/operations/{operation['id']}/cancel",
            json={"reason": "Stop the experiment"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["cancelRequest"]["reason"] == "Stop the experiment"
    with Factory(paths) as factory:
        wait_until(lambda: factory.operations.get(operation["id"])["state"] == "canceled")
        assert factory.operations.get(operation["id"])["error"] is None


def test_finalization_serializes_against_cancellation(tmp_path):
    paths, _ = project(tmp_path)
    with Factory(paths) as factory:
        first = factory.operations.create("run", {"actor": factory.actor})
        factory.operations.request_cancel(first["id"], actor=factory.actor, reason="Stop")
        with pytest.raises(OperationCanceled):
            factory.operations.advance(first["id"], state="finalizing")
        second = factory.operations.create("run", {"actor": factory.actor})
        finalized = factory.operations.advance(second["id"], state="finalizing")
        assert (
            factory.operations.request_cancel(second["id"], actor=factory.actor, reason="Too late")
            == finalized
        )


def test_definition_and_cli_discovery(tmp_path):
    paths, definition = project(tmp_path)
    assert read_definition(definition).primaryMetric == "accuracy"
    runner = CliRunner()
    result = runner.invoke(
        app, ["--project", str(paths.root), "--output", "json", "experiment", "schema"]
    )
    assert result.exit_code == 0, result.output
    assert "candidates" in json.loads(result.output)["properties"]
    recipe = yaml.safe_load(definition.read_text())
    recipe["primaryMetric"] = "missing"
    definition.write_text(yaml.safe_dump(recipe))
    with pytest.raises(ValidationError, match="invalid experiment"):
        read_definition(definition)


def test_cli_initialization_respects_project_and_returns_structured_errors(tmp_path):
    runner = CliRunner()
    root = tmp_path / "chosen project"
    arguments = [
        "--project",
        str(root),
        "--output",
        "json",
        "experiment",
        "init",
        "--name",
        "example",
        "--objective",
        "Classify accurately",
    ]
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert (root / "experiment.yaml").is_file()
    assert json.loads(result.output)["project"] == str(root)
    invalid = runner.invoke(
        app,
        [
            "--output",
            "json",
            "experiment",
            "init",
            str(tmp_path / "invalid.yaml"),
            "--name",
            "../bad",
            "--objective",
            "Classify accurately",
        ],
    )
    assert invalid.exit_code == 1
    assert json.loads(invalid.output)["error"]["code"] == "validation_error"
    assert not (tmp_path / "invalid.yaml").exists()


def test_finalization_resumes_after_evaluation_publication_failure(tmp_path, monkeypatch):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        operation = factory.experiments.prepare(definition, "candidate")
        with monkeypatch.context() as patch:

            def interrupted(_subject):
                raise OSError("controller interrupted before publishing evaluation")

            patch.setattr(factory, "evaluate", interrupted)
            with pytest.raises(OSError, match="controller interrupted"):
                factory.execute_run_operation(operation["id"])
        assert factory.operations.get(operation["id"])["state"] == "finalizing"
        receipt = paths.runs / operation["id"] / "stages/train/controller-execution.json"
        execution = json.loads(receipt.read_text())["executionId"]
        factory.execute_run_operation(operation["id"])
        assert factory.experiments.status(operation["id"])["scores"]["passed"]
        assert json.loads(receipt.read_text())["executionId"] == execution
        factory.execute_run_operation(operation["id"])
        assert len(factory.resources.list(kind="EvaluationResult")) == 1


def test_changed_evaluation_evidence_requires_review_and_has_source_diff(tmp_path):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        baseline = factory.experiments.run(definition, "baseline")
        (paths.root / "src/evaluate.py").write_text(EVALUATE + "\n# Revised evaluator\n")
        (paths.root / "data.json").write_text('[{"x":1,"y":4},{"x":2,"y":5}]')
        candidate = factory.experiments.run(definition, "candidate")
        report = review(factory.experiments, candidate["id"], baseline["id"], details=True)
        assert report["comparison"]["decision"] == "review"
        assert report["comparison"]["reasons"] == [
            "evaluation source changed",
            "evaluation data or arguments changed",
        ]
        assert report["changes"]["source"]["total"] == 2
        assert "Revised evaluator" in report["changes"]["source"]["items"][0]["diff"]
        assert report["examples"]["items"][0]["delta"] is None


def test_api_experiments_and_export_deliver_usable_source(tmp_path):
    paths, definition = project(tmp_path)
    with (paths.root / ".gitignore").open("a") as stream:
        stream.write(".env\n")
    (paths.root / "src/.env").write_text("SECRET=not-source")
    (paths.root / "src/module.yaml").write_text("existing trainer configuration\n")
    with Factory(paths) as factory:
        token = factory.secrets.get("local-api-token", "api-authentication").decode()
    with TestClient(create_app(paths)) as client:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/experiment-schema", headers=headers).status_code == 200
        response = client.post(
            "/v1/experiment-runs",
            headers=headers,
            json={"definition": str(definition), "candidate": "candidate", "detach": False},
        )
        assert response.status_code == 200, response.text
        identity = response.json()["id"]
        assert client.get(f"/v1/experiment-runs/{identity}", headers=headers).json()["scores"][
            "passed"
        ]
        assert len(client.get("/v1/experiment-runs?name=regression", headers=headers).json()) == 1
        report = client.get(f"/v1/experiment-runs/{identity}/review", headers=headers).json()
        assert report["baseline"] is None
        exported = client.post(
            f"/v1/experiment-runs/{identity}/export",
            headers=headers,
            json={"destination": str(tmp_path / "export")},
        )
        assert exported.status_code == 200, exported.text
        assert (tmp_path / "export/source/train/train.py").read_text() == TRAIN
        assert (
            tmp_path / "export/source/train/module.yaml"
        ).read_text() == "existing trainer configuration\n"
        assert not (tmp_path / "export/source/train/.env").exists()
        reproduction = client.post(
            f"/v1/experiment-runs/{identity}/reproduce", headers=headers, json={"detach": False}
        )
        assert reproduction.status_code == 200, reproduction.text
        assert reproduction.json()["scores"] == response.json()["scores"]
        invalid = client.post(
            "/v1/experiment-runs",
            headers=headers,
            json={"definition": str(definition), "candidate": "candidate", "detatch": True},
        )
        assert invalid.status_code == 422


@pytest.mark.parametrize("artifact", ["model", "source"])
def test_directory_model_passes_between_scripts_and_exports(tmp_path, artifact):
    paths, definition = project(tmp_path)
    recipe = yaml.safe_load(definition.read_text())
    recipe["train"]["artifacts"] = {artifact: "model.json"}
    recipe["evaluate"]["command"] = [
        argument.replace("{inputs[model]}", "{inputs[" + artifact + "]}")
        for argument in recipe["evaluate"]["command"]
    ]
    definition.write_text(yaml.safe_dump(recipe))
    source = TRAIN.replace(
        'Path(a.output).write_text(json.dumps({"bias": bias}))',
        "Path(a.output).mkdir()\n"
        '(Path(a.output) / "payload").write_text("model metadata")\n'
        '(Path(a.output) / "weights.json").write_text(json.dumps({"bias": bias}))',
    )
    (paths.root / "src/train.py").write_text(source)
    (paths.root / "src/evaluate.py").write_text(
        EVALUATE.replace(
            "Path(a.model).read_text()", '(Path(a.model) / "weights.json").read_text()'
        )
    )
    with Factory(paths) as factory:
        candidate = factory.experiments.run(definition, "candidate")
        assert candidate["scores"]["passed"]
        exported = factory.experiments.export(candidate["id"], tmp_path / "directory-model")
        model = Path(exported["destination"]) / exported["artifacts"][artifact]["path"]
        assert json.loads((model / "weights.json").read_text()) == {"bias": 2.0}
        assert {path.name for path in model.iterdir()} == {"weights.json", "payload"}
        assert (Path(exported["destination"]) / "source/train/train.py").read_text() == source


def test_failed_candidate_can_be_saved_without_passing_selection(tmp_path):
    paths, definition = project(tmp_path)
    with Factory(paths) as factory:
        baseline = factory.experiments.run(definition, "baseline")
        release = factory.create_release(baseline["runId"], name="baseline", intended_use="test")
        manifest = release["spec"]["extensions"]["manifest"]
        assert manifest["evaluations"]
        assert manifest["assessment"]["evaluation_passed"] is False
        with pytest.raises(IntegrityError, match="evaluation"):
            factory.promote_release("baseline")
        assert factory.show_release("baseline")["release"] == release
