"""Exercise the complete model-development loop, including controller recovery."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from openfoundry.candidate_review import review, write_review
from openfoundry.config import ProjectPaths
from openfoundry.factory import Factory
from openfoundry.tracking import track
from prepare import prepare


def interrupted_run(factory, definition):
    operation = factory.experiments.prepare(definition, "character-svm")
    identity = operation["id"]
    receipt = factory.paths.runs / identity / "stages/train/controller-execution.json"
    with (factory.paths.state / "operations" / f"{identity}.log").open("ab") as log:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openfoundry.run_worker",
                "--project",
                str(factory.paths.root),
                "--operation",
                identity,
            ],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if receipt.exists() and json.loads(receipt.read_text()).get("state") == "submitted":
                    break
                if worker.poll() is not None:
                    raise RuntimeError("controller stopped before the recovery exercise")
                time.sleep(0.02)
            else:
                raise TimeoutError("training did not start")
            original = json.loads(receipt.read_text())["executionId"]
            worker.kill()
            worker.wait(timeout=10)
            factory.execute_run_operation(identity)
            recovered = json.loads(receipt.read_text())["executionId"]
            if recovered != original:
                raise RuntimeError("recovery replayed the training stage")
            return factory.experiments.status(identity)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)


def predict_export(exported, root):
    destination = Path(exported["destination"])
    environment = root / ".venv/inference"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / "bin/python"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(destination / "source/train/requirements.lock"),
        ],
        check=True,
    )
    output = subprocess.check_output(
        [
            str(python),
            str(destination / "source/train/predict.py"),
            str(destination / exported["artifacts"]["model"]["path"]),
            "Are we still meeting for lunch tomorrow?",
            "WIN a free prize! Call now to claim!",
        ],
        text=True,
    )
    predictions = json.loads(output)
    if len(predictions) != 2 or not set(predictions).issubset({"ham", "spam"}):
        raise RuntimeError("exported model did not produce valid predictions")
    return predictions


def run(destination, archive=None, tracking_uri=None):
    started = time.monotonic()
    provenance = prepare(destination, archive)
    paths = ProjectPaths(Path(destination).resolve())
    definition = paths.root / "experiment.yaml"
    with Factory(paths) as factory:
        baseline = factory.experiments.run(definition, "baseline")
        first_baseline = time.monotonic() - started
        word = factory.experiments.run(definition, "word-svm")
        character = interrupted_run(factory, definition)
        runs = [baseline, word, character]
        reports = [
            review(factory.experiments, item["id"], baseline["id"], details=True) for item in runs
        ]
        acceptable = [report for report in reports if report["comparison"]["acceptable"]]
        if not acceptable:
            raise RuntimeError("no candidate met the declared acceptance criteria")
        winner = max(acceptable, key=lambda report: report["candidate"]["scores"]["spam_f1"])
        reproduced = factory.experiments.reproduce(winner["candidate"]["runId"])
        if reproduced["scores"] != winner["candidate"]["scores"]:
            raise RuntimeError("reproduction changed evaluation results")
        write_review(winner, paths.root / "review.html")
        exported = factory.experiments.export(winner["candidate"]["runId"], paths.root / "model")
        factory.create_release(
            winner["candidate"]["runId"], name="v1", intended_use="Classify SMS messages."
        )
        selection = factory.promote_release("v1")
        tracking = None
        if tracking_uri:
            tracking = track(factory.experiments, winner["candidate"]["runId"], tracking_uri)
            repeated = track(factory.experiments, winner["candidate"]["runId"], tracking_uri)
            if repeated["trackingRunId"] != tracking["trackingRunId"]:
                raise RuntimeError("tracking export created a duplicate run")
        predictions = predict_export(exported, paths.root)
        results = {
            "dataset": provenance,
            "timeToFirstBaselineSeconds": first_baseline,
            "totalWallSeconds": time.monotonic() - started,
            "intentionalControllerInterruptions": 1,
            "unplannedOperatorInterventions": 0,
            "reproduction": {"sameScores": True, "runId": reproduced["id"]},
            "candidates": [report["candidate"] for report in reports],
            "selected": winner["candidate"]["name"],
            "review": "review.html",
            "export": exported,
            "exportPredictions": predictions,
            "release": selection,
            "tracking": tracking,
        }
        (paths.root / "results.json").write_text(json.dumps(results, indent=2))
        return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--tracking-uri")
    args = parser.parse_args()
    print(json.dumps(run(args.destination, args.archive, args.tracking_uri), indent=2))
