import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.factory import Factory
from openfoundry.install_support import copy_starter

DOCS = Path("docs")


def _walkthrough_project(tmp_path: Path) -> Path:
    root = tmp_path / "walkthrough-project"
    root.mkdir()
    shutil.copy2("openfoundry.yaml", root / "openfoundry.yaml")
    shutil.copy2(".gitignore", root / ".gitignore")
    model_card = Path("templates/project/MODEL_CARD.md").read_text()
    (root / "MODEL_CARD.md").write_text(
        model_card.replace("__OPENFOUNDRY_PROJECT_NAME__", "openfoundry").replace(
            "__OPENFOUNDRY_PROJECT_NAMESPACE__", "local/openfoundry"
        )
    )
    (root / "bindings").mkdir()
    shutil.copy2("bindings/local.yaml", root / "bindings/local.yaml")
    copy_starter(Path.cwd(), root)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "OpenFoundry walkthrough test"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@openfoundry.invalid"], cwd=root, check=True
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "Create walkthrough fixture"], cwd=root, check=True)
    return root


def test_greenfield_model_card_to_compared_candidate(tmp_path):
    root = _walkthrough_project(tmp_path)
    model_card_path = root / "MODEL_CARD.md"
    model_card_path.write_text(
        model_card_path.read_text()
        .replace("**Status:** Draft", "**Status:** Active")
        .replace("TBD", "Defined for the affine acceptance benchmark")
    )
    workload_path = root / "workloads/example-from-scratch.yaml"
    workload = yaml.safe_load(workload_path.read_text())
    workload["spec"]["graph"]["stages"][0]["config"]["steps"] = 200
    workload_path.write_text(yaml.safe_dump(workload))
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "Define baseline"], cwd=root, check=True)

    paths = ProjectPaths(root)
    bootstrap(paths)
    with Factory(paths) as factory:
        for resource in (
            root / "model-packages/example-affine.yaml",
            root / "evaluations/example-affine.yaml",
        ):
            factory.apply_resource_file(resource)
        factory.add_data(
            root / "data/fixtures/affine.jsonl",
            name="example-affine",
            mode="copy",
            rights=yaml.safe_load((root / "data/fixtures/rights.yaml").read_text()),
        )
        baseline = factory.run(workload_path, root / "bindings/local.yaml")
        baseline_evaluation = factory.evaluate(f"run/{baseline['runId']}")

        workload["spec"]["graph"]["stages"][0]["config"]["steps"] = 500
        workload_path.write_text(yaml.safe_dump(workload))
        subprocess.run(["git", "add", str(workload_path)], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "Train candidate longer"], cwd=root, check=True)
        candidate = factory.run(workload_path, root / "bindings/local.yaml")
        candidate_evaluation = factory.evaluate(f"run/{candidate['runId']}")
        experiment = factory.create_experiment(
            name="longer-training",
            baseline_ref=f"run/{baseline['runId']}",
            candidate_ref=f"run/{candidate['runId']}",
            metric="training-loss",
            direction="minimize",
        )
        baseline_evaluation_ref = factory._resource_uri(baseline_evaluation)
        candidate_evaluation_ref = factory._resource_uri(candidate_evaluation)
        baseline_lineage = factory.lineage.by_run(baseline["runId"])
        candidate_lineage = factory.lineage.by_run(candidate["runId"])

    initial_decision = (
        "| Defined for the affine acceptance benchmark | Initial direction | "
        "Defined for the affine acceptance benchmark |"
    )
    model_card_path.write_text(
        model_card_path.read_text().replace(
            initial_decision,
            f"| 2026-09-03 | Selected longer training | {experiment['metadata']['revision']} |",
        )
    )
    assert "TBD" not in model_card_path.read_text()
    assert baseline["workloadDigest"] != candidate["workloadDigest"]
    assert (
        baseline_evaluation["spec"]["extensions"]["evaluationRefs"]
        == (candidate_evaluation["spec"]["extensions"]["evaluationRefs"])
    )
    assert (
        candidate_evaluation["spec"]["scores"]["training-loss"]
        < baseline_evaluation["spec"]["scores"]["training-loss"]
    )
    assert experiment["spec"]["decision"] == "candidate"
    assert experiment["spec"]["baselineRef"] == baseline_evaluation_ref
    assert experiment["spec"]["candidateRef"] == candidate_evaluation_ref
    assert baseline_lineage
    assert candidate_lineage
    assert experiment["metadata"]["revision"] in model_card_path.read_text()


def test_walkthrough_transcript_executes_end_to_end(tmp_path):
    content = (DOCS / "walkthrough.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- manual-test: local-lifecycle -->\s*```sh\n(?P<script>.*?)\n```",
        content,
        re.S,
    )
    assert match is not None
    assert content.count("manual-test: local-lifecycle") == 1
    root = _walkthrough_project(tmp_path)

    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    openfoundry = binary_directory / "openfoundry"
    openfoundry.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m openfoundry "$@"\n',
        encoding="utf-8",
    )
    openfoundry.chmod(0o755)
    environment = os.environ | {
        "OPENFOUNDRY_ACTOR": "local-user",
        "PATH": f"{binary_directory}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", "-c", match.group("script")],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "manual lifecycle completed: run/" in result.stdout

    with Factory(ProjectPaths(root)) as factory:
        assert factory.doctor()["ready"]
        assert factory.verify_data("example-affine")
        assert factory.find_resource("ArtifactStore", "secondary")["kind"] == "ArtifactStore"
        runs = factory.list_runs()
        assert len(runs) == 1
        run_id = runs[0]["runId"]
        assert runs[0]["state"] == "Succeeded"
        assert factory.lineage_query(f"run:{run_id}/stage:train")
        evaluations = factory.list_resources(kind="EvaluationResult")
        assert len(evaluations) == 1
        assert evaluations[0]["spec"]["scores"]["passed"]
        release = factory.create_release(
            run_id,
            name="walkthrough-v1",
            intended_use="walkthrough test",
            promote=True,
        )
        assert factory.show_release("alias/candidate")["release"] == release


def test_source_distribution_contains_the_docs_and_starter(tmp_path):
    output = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(output)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = next(output.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as package:
        members = {
            member.name.split("/", 1)[1] for member in package.getmembers() if "/" in member.name
        }
    expected = {path.as_posix() for path in DOCS.glob("*.md")}
    assert expected <= members
    assert {
        "workloads/example-from-scratch.yaml",
        "modules/examples/affine-regression/module.yaml",
        "model-packages/example-affine.yaml",
        "evaluations/example-affine.yaml",
        "data/fixtures/affine.jsonl",
    } <= members
