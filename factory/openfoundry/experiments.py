from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.canonical import canonical_json, sha256_digest
from openfoundry.errors import IntegrityError, NotFoundError, ValidationError
from openfoundry.experiment_definition import (
    ExperimentDefinition,
    capture_script,
    evaluation_spec,
    project_path,
    read_definition,
    resource,
    stage,
    write_yaml,
)
from openfoundry.ids import uuid7
from openfoundry.modules import extract_module_package

if TYPE_CHECKING:
    from openfoundry.factory import Factory


def launch_worker(factory: Factory, operation_id: str) -> None:
    log_path = factory.paths.state / "operations" / f"{operation_id}.log"
    with log_path.open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openfoundry.run_worker",
                "--project",
                str(factory.paths.root),
                "--operation",
                operation_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
        )


class ExperimentService:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    def prepare(self, path: str | Path, candidate: str) -> dict[str, Any]:
        self.factory._authorize("experiment.run")
        path = self.factory._project_file(path, kind="experiment")
        definition = read_definition(path)
        if candidate not in definition.candidates:
            raise ValidationError(
                "unknown candidate", details={"candidates": list(definition.candidates)}
            )
        started = time.monotonic()
        compiled = self.factory.paths.state / "experiments" / str(uuid7())
        compiled.mkdir(parents=True)
        try:
            workload, binding = self._compile(definition, path, candidate, compiled)
            operation = self.factory.create_run_operation(workload, binding)
        except BaseException:
            shutil.rmtree(compiled)
            raise
        return {
            **operation,
            "preparationSeconds": time.monotonic() - started,
            "generated": str(compiled.relative_to(self.factory.paths.root)),
        }

    def _compile(
        self, definition: ExperimentDefinition, path: Path, candidate: str, compiled: Path
    ) -> tuple[Path, Path]:
        spec = definition.candidates[candidate]
        return self._compile_params(
            definition,
            path,
            compiled,
            candidate_name=candidate,
            parameters=spec.parameters,
            from_ref=spec.from_ref,
            rationale=spec.rationale,
        )

    def _compile_params(
        self,
        definition: ExperimentDefinition,
        path: Path,
        compiled: Path,
        *,
        candidate_name: str,
        parameters: dict[str, Any],
        from_ref: str | None,
        rationale: str = "",
    ) -> tuple[Path, Path]:
        root = self.factory.paths.root
        sources = {
            name: capture_script(root, path.parent, script, compiled / name)
            for name, script in (("train", definition.train), ("evaluate", definition.evaluate))
        }
        inputs = {}
        for name, dataset in definition.data.items():
            source = project_path(root, path.parent, dataset.source)
            dataset_name = f"{definition.name}-{name}"
            self.factory.add_data(
                str(source), name=dataset_name, mode="copy", rights=dataset.rights
            )
            inputs[name] = f"dataset/{dataset_name}"
        evaluation = self.factory.apply_resource(evaluation_spec(definition))
        # The reserved base input is always supplied so one train script can
        # serve every candidate: scratch candidates receive "" and branch
        # candidates receive their resolved from reference.
        train_inputs = {**inputs, "base": ""}
        if from_ref is not None:
            train_inputs["base"] = self._resolve_branch_ref(from_ref)
            requested = set(definition.train.inputs or [*train_inputs])
            if "base" not in requested:
                raise ValidationError(
                    "candidate declares from but the train script does not accept a base input",
                    details={"candidate": candidate_name},
                )
        card = project_path(root, path.parent, definition.modelCard)
        metadata = {
            "definition": definition.model_dump(mode="json"),
            "definitionDigest": sha256_digest(definition.model_dump(mode="json")),
            "candidate": candidate_name,
            "candidateParams": parameters,
            "candidateRationale": rationale,
            "sources": sources,
            "modelCard": card.read_text() if card.is_file() else None,
        }
        scripts = [
            stage(
                "train",
                definition.train,
                (compiled / "train/openfoundry-script.yaml").relative_to(root).as_posix(),
                train_inputs,
                parameters,
            ),
            stage(
                "evaluate",
                definition.evaluate,
                (compiled / "evaluate/openfoundry-script.yaml").relative_to(root).as_posix(),
                {**inputs, **{name: f"train.{name}" for name in definition.train.artifacts}},
                parameters,
            ),
        ]
        scripts[1]["config"]["metricNames"] = list(definition.metrics)
        workload = resource(
            "WorkloadSpec",
            f"{definition.name}-{candidate_name}",
            {
                "graph": {"stages": scripts},
                "evaluationRefs": [f"evaluationspec/{evaluation['metadata']['name']}"],
                "extensions": {"experiment": metadata},
            },
        )
        binding = resource(
            "Binding",
            f"{definition.name}-execution",
            {
                "executor": definition.executor,
                "resources": definition.limits.model_dump(exclude_none=True),
                "config": definition.provider,
            },
        )
        write_yaml(compiled / "workload.yaml", workload)
        write_yaml(compiled / "binding.yaml", binding)
        write_yaml(
            compiled / "experiment.yaml", definition.model_dump(mode="json", exclude_none=True)
        )
        return compiled / "workload.yaml", compiled / "binding.yaml"

    def _resolve_branch_ref(self, reference: str) -> str:
        if not reference.startswith("run/"):
            return reference
        run_id = reference.removeprefix("run/")
        try:
            status = self.factory.run_status(run_id)
            result = self.factory._run_result(run_id, status["status"])
        except (NotFoundError, IntegrityError) as exc:
            raise ValidationError(
                "candidate from run is unavailable", details={"reference": reference}
            ) from exc
        outputs = result["spec"]["outputs"]
        for key in ("train.checkpoint", "train.model"):
            if isinstance(outputs.get(key), str):
                digest = str(outputs[key])
                return digest if digest.startswith("sha256:") else f"artifact:{digest}"
        raise ValidationError(
            "candidate from run has no branchable train output",
            details={"reference": reference},
        )

    def run(self, path: str | Path, candidate: str, *, detach: bool = False) -> dict[str, Any]:
        operation = self.prepare(path, candidate)
        if detach:
            launch_worker(self.factory, str(operation["id"]))
            return operation
        self.factory.execute_run_operation(str(operation["id"]))
        return self.status(str(operation["id"]))

    def run_search(self, path: str | Path, *, detach: bool = False) -> dict[str, Any]:
        """Expand definition.search into trials and run them sequentially.

        Trials execute one at a time in deterministic expansion order even when
        search.concurrency > 1; concurrency is recorded as the declared budget
        for schedulers that honor it. budget.maxRuns caps expansion (already
        enforced at validation). Sequential runs stop early once measured spend
        exceeds budget.maxCostUSD; detached runs launch every trial
        concurrently and cannot observe per-trial costs, so budget.maxCostUSD
        together with detach is rejected.
        """
        from openfoundry.experiment_definition import expand_search

        self.factory._authorize("experiment.search")
        project_path_value = self.factory._project_file(path, kind="experiment")
        definition = read_definition(project_path_value)
        search = definition.search
        if search is None:
            raise ValidationError("experiment definition declares no search")
        if detach and search.budget.maxCostUSD is not None:
            raise ValidationError(
                "budget.maxCostUSD requires sequential trials; rerun the search without detach",
                details={"budget": "maxCostUSD"},
            )
        template = definition.candidates[search.template]
        trials = expand_search(search, template.parameters)
        max_cost = search.budget.maxCostUSD
        runs: list[dict[str, Any]] = []
        spent = 0.0
        stopped = None
        for index, parameters in enumerate(trials):
            name = f"{search.template}-s{index:02d}"
            operation = self._prepare_params(
                project_path_value,
                definition,
                candidate_name=name,
                parameters=parameters,
                from_ref=template.from_ref,
                rationale=f"search trial {index} from {search.template}",
            )
            if detach:
                launch_worker(self.factory, str(operation["id"]))
                runs.append({**operation, "candidate": name, "parameters": parameters})
                continue
            self.factory.execute_run_operation(str(operation["id"]))
            status = self.status(str(operation["id"]))
            runs.append({**status, "parameters": parameters})
            if max_cost is not None:
                spent += self._measured_cost(str(operation["id"]))
                if spent > max_cost and index + 1 < len(trials):
                    stopped = {
                        "reason": "budget.maxCostUSD exceeded",
                        "spentUSD": spent,
                        "completedTrials": index + 1,
                        "remainingTrials": len(trials) - index - 1,
                    }
                    break
        return {
            "experiment": definition.name,
            "template": search.template,
            "trials": len(trials),
            "completed": len(runs),
            "concurrency": search.concurrency,
            "spentUSD": spent,
            "stopped": stopped,
            "runs": runs,
        }

    def _measured_cost(self, run_id: str) -> float:
        from openfoundry.candidate_review import _subject

        try:
            measurement = _subject(self, run_id).get("measurement") or {}
            return float(measurement.get("monetaryCostUSD") or 0.0)
        except (ValidationError, IntegrityError, NotFoundError, KeyError, TypeError, ValueError):
            return 0.0

    def _prepare_params(
        self,
        path: Path,
        definition: ExperimentDefinition,
        *,
        candidate_name: str,
        parameters: dict[str, Any],
        from_ref: str | None,
        rationale: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        compiled = self.factory.paths.state / "experiments" / str(uuid7())
        compiled.mkdir(parents=True)
        try:
            workload, binding = self._compile_params(
                definition,
                path,
                compiled,
                candidate_name=candidate_name,
                parameters=parameters,
                from_ref=from_ref,
                rationale=rationale,
            )
            operation = self.factory.create_run_operation(workload, binding)
        except BaseException:
            shutil.rmtree(compiled)
            raise
        return {
            **operation,
            "preparationSeconds": time.monotonic() - started,
            "generated": str(compiled.relative_to(self.factory.paths.root)),
        }

    def leaderboard(self, name: str | Path) -> dict[str, Any]:
        """Ranked trials for an experiment file, best first. Observation, not advice."""
        from openfoundry.candidate_review import _subject

        path = self.factory._project_file(name, kind="experiment")
        definition = read_definition(path)
        primary = definition.primaryMetric
        direction = definition.metrics[primary].direction
        entries: list[dict[str, Any]] = []
        for item in self.list(definition.name):
            run_id = str(item["id"])
            score = item.get("scores", {}).get(primary)
            cost = None
            if item.get("state") == "succeeded":
                try:
                    subject = _subject(self, run_id)
                    score = subject["scores"].get(primary, score)
                    measurement = subject.get("measurement") or {}
                    cost = measurement.get("monetaryCostUSD")
                except (ValidationError, IntegrityError, NotFoundError, KeyError):
                    pass
            entries.append(
                {
                    "runId": run_id,
                    "candidate": item.get("candidate"),
                    "state": item.get("state"),
                    "score": score,
                    "costUSD": cost,
                    "staleDefinition": item.get("definitionDigest")
                    != sha256_digest(definition.model_dump(mode="json")),
                }
            )
        ranked = [e for e in entries if isinstance(e["score"], (int, float))]
        unranked = [e for e in entries if not isinstance(e["score"], (int, float))]
        ranked.sort(key=lambda e: float(e["score"]), reverse=(direction == "maximize"))
        return {
            "experiment": definition.name,
            "primaryMetric": primary,
            "direction": direction,
            "entries": [*ranked, *unranked],
            "best": ranked[0] if ranked else None,
        }

    def metadata(self, run_id: str) -> dict[str, Any]:
        try:
            run = self.factory._run_resource(run_id)
        except IntegrityError:
            pending: dict[str, Any] = (
                self.factory.operations.get(run_id)["request"].get("experiment") or {}
            )
            return pending
        workload = self.factory._resource_by_uri("WorkloadSpec", run["spec"]["workloadRef"])
        metadata: dict[str, Any] = workload["spec"].get("extensions", {}).get("experiment", {})
        return metadata

    def complete(self, operation: dict[str, Any]) -> None:
        if operation["state"] not in {"succeeded", "finalizing"} or not self.metadata(
            str(operation["id"])
        ):
            return
        try:
            self.factory._evaluation_result(f"run/{operation['id']}")
        except NotFoundError:
            self.factory.evaluate(f"run/{operation['id']}")

    def status(self, run_id: str) -> dict[str, Any]:
        run_id = run_id.removeprefix("run/")
        operation = self.factory.operations.get(run_id)
        view = {key: operation[key] for key in ("id", "state", "createdAt", "updatedAt", "error")}
        view["cancelRequest"] = operation.get("cancelRequest")
        view["reproduces"] = operation["request"].get("reproduces")
        active = operation["state"] in {"pending", "running", "recovering", "finalizing"}
        view["commands"] = (
            {
                "resume": f"openfoundry operation reconcile {run_id}",
                "cancel": f"openfoundry operation cancel {run_id}",
            }
            if active
            else {"review": f"openfoundry experiment review {run_id}"}
            if operation["state"] == "succeeded"
            else {}
        )
        metadata = self.metadata(run_id)
        if metadata:
            definition = metadata["definition"]
            view.update(
                experiment=definition["name"],
                candidate=metadata["candidate"],
                objective=definition["objective"],
                definitionDigest=metadata["definitionDigest"],
            )
        try:
            status = self.factory.run_status(run_id)
        except (NotFoundError, IntegrityError):
            return {**view, "phase": operation["state"], "stages": {}}
        stages = (status["execution"] or {}).get("stages", {})
        view.update(
            {
                "runId": run_id,
                "runState": status["status"].get("state"),
                "stages": stages,
                "phase": "complete" if operation["state"] == "succeeded" else operation["state"],
            }
        )
        view["progress"] = self._progress(run_id, stages)
        try:
            evaluation = self.factory._evaluation_result(f"run/{run_id}")
            view["scores"] = evaluation["spec"]["scores"]
            view["evaluationRef"] = self.factory._resource_uri(evaluation)
        except NotFoundError:
            pass
        return view

    def list(self, name: str | None = None) -> list[dict[str, Any]]:
        results = []
        for operation in self.factory.operations.list():
            if operation["kind"] != "run":
                continue
            metadata = self.metadata(str(operation["id"]))
            if metadata and (name is None or metadata["definition"]["name"] == name):
                results.append(self.status(str(operation["id"])))
        return sorted(results, key=lambda item: item["id"], reverse=True)

    def _progress(self, run_id: str, stages: dict[str, Any]) -> dict[str, Any]:
        active = []
        for receipt in (self.factory.paths.runs / run_id).glob(
            "stages/*/controller-execution.json"
        ):
            if receipt.parent.name not in stages:
                record = json.loads(receipt.read_bytes())
                active.append({"stage": receipt.parent.name, **record})
        return {"completedStages": list(stages), "executions": active}

    def artifact_json(self, digest: str) -> Any:
        builder = ArtifactBuilder(self.factory.local_store)
        manifest = self.factory.local_store.read_manifest(digest)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "value"
            builder.restore(manifest, target)
            return json.loads((target / "payload").read_bytes())

    def export(self, run_id: str, destination: Path) -> dict[str, Any]:
        self.factory._authorize("experiment.export")
        run_id = run_id.removeprefix("run/")
        status = self.factory.run_status(run_id)
        result = self.factory._run_result(run_id, status["status"])
        metadata = self.metadata(run_id)
        if not metadata:
            raise ValidationError("run is not a script experiment")
        destination = destination.resolve()
        if destination.exists():
            raise ValidationError("export destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        builder = ArtifactBuilder(self.factory.local_store)
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            bundle = Path(temporary) / "bundle"
            bundle.mkdir()
            exported = {}
            for name, filename in metadata["definition"]["train"]["artifacts"].items():
                digest = result["spec"]["outputs"][f"train.{name}"]
                manifest = self.factory.local_store.read_manifest(digest)
                builder.restore(manifest, bundle / "artifacts" / name)
                exported[name] = {
                    "digest": digest,
                    "path": f"artifacts/{name}" + ("" if manifest.is_directory else "/payload"),
                    "originalPath": filename,
                }
            run = self.factory._run_resource(run_id)
            for name, digest in run["spec"]["extensions"]["moduleDigests"].items():
                archive = bundle / f"{name}-archive"
                builder.restore(self.factory.local_store.read_manifest(digest), archive)
                extract_module_package(archive / "payload", bundle / "source" / name)
                shutil.rmtree(archive)
            write_yaml(bundle / "experiment.yaml", metadata["definition"])
            if metadata["modelCard"]:
                (bundle / "MODEL_CARD.md").write_text(metadata["modelCard"])
            (bundle / "evidence.json").write_bytes(
                canonical_json(
                    {
                        "run": run,
                        "result": result,
                        "evaluation": self.factory._evaluation_result(f"run/{run_id}"),
                        "experiment": metadata,
                        "artifacts": exported,
                        "reproduceCommand": f"openfoundry experiment reproduce {run_id}",
                        "reproductionProject": str(self.factory.paths.root),
                    }
                )
            )
            os.rename(bundle, destination)
        return {"runId": run_id, "destination": str(destination), "artifacts": exported}

    def reproduce(self, run_id: str, *, detach: bool = False) -> dict[str, Any]:
        self.factory._authorize("experiment.reproduce")
        self.factory._authorize("workload.run")
        run_id = run_id.removeprefix("run/")
        if not self.metadata(run_id):
            raise ValidationError("run is not a script experiment")
        run = self.factory._run_resource(run_id)
        original = self.factory.operations.get(run_id)
        directory = self.factory.paths.state / "experiments" / str(uuid7())
        directory.mkdir(parents=True)
        builder = ArtifactBuilder(self.factory.local_store)
        workload = deepcopy(
            self.factory._resource_by_uri("WorkloadSpec", run["spec"]["workloadRef"])
        )
        for stage_spec in workload["spec"]["graph"]["stages"]:
            name = stage_spec["name"]
            digest = run["spec"]["extensions"]["moduleDigests"][name]
            archive = directory / f"{name}-archive"
            builder.restore(self.factory.local_store.read_manifest(digest), archive)
            extract_module_package(archive / "payload", directory / name)
            stage_spec["module"] = (
                (directory / name / Path(stage_spec["module"]).name)
                .relative_to(self.factory.paths.root)
                .as_posix()
            )
            shutil.rmtree(archive)
        workload = resource("WorkloadSpec", workload["metadata"]["name"], workload["spec"])
        binding = self.factory._resource_by_uri("Binding", run["spec"]["bindingRef"])
        binding = resource("Binding", binding["metadata"]["name"], binding["spec"])
        write_yaml(directory / "workload.yaml", workload)
        write_yaml(directory / "binding.yaml", binding)
        request = deepcopy(original["request"])
        request.update(
            {
                "actor": self.factory.actor,
                "policyDigest": self.factory.policy.digest,
                "worktree": self.factory._admission_worktree(),
                "reproduces": run_id,
                "workload": (directory / "workload.yaml")
                .relative_to(self.factory.paths.root)
                .as_posix(),
                "binding": (directory / "binding.yaml")
                .relative_to(self.factory.paths.root)
                .as_posix(),
                "workloadDigest": sha256_digest(
                    self.factory._load_resource(directory / "workload.yaml")
                ),
                "bindingDigest": sha256_digest(
                    self.factory._load_resource(directory / "binding.yaml")
                ),
            }
        )
        operation = self.factory.operations.create("run", request)
        if detach:
            launch_worker(self.factory, str(operation["id"]))
            return operation
        self.factory.execute_run_operation(str(operation["id"]))
        return self.status(str(operation["id"]))
