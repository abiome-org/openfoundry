from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.errors import (
    IntegrityError,
    ValidationError,
)
from openfoundry.executors import (
    MODULE_EXECUTION_CAPABILITIES,
)
from openfoundry.lineage import LineageEdge
from openfoundry.modules import (
    extract_module_package,
    load_manifest,
    validate_contract,
)
from openfoundry.sdk import ProtocolRequest

if TYPE_CHECKING:
    from openfoundry.factory import Factory


class EvaluationService:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    @staticmethod
    def extract_uncertainty(outputs: dict[str, Any]) -> dict[str, dict[str, float]]:
        """Evaluator-reported spread per metric, recorded as evidence.

        Convention: an inline output named `<stage>.uncertainty` holding
        `{metric: {stat: number}}` with finite values (e.g. std, n, ci95).
        Anything else yields no uncertainty rather than a failure.
        """
        raw: Any = None
        for key, value in outputs.items():
            if key.lower().endswith(".uncertainty"):
                raw = value
        if not isinstance(raw, dict):
            return {}
        uncertainty: dict[str, dict[str, float]] = {}
        for metric, stats in raw.items():
            if not isinstance(metric, str) or not isinstance(stats, dict):
                return {}
            converted: dict[str, float] = {}
            for stat, number in stats.items():
                if (
                    not isinstance(stat, str)
                    or isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    return {}
                converted[stat] = float(number)
            uncertainty[metric] = converted
        return uncertainty

    @staticmethod
    def _compatibility_equal(expected: Any, actual: Any, tolerance: dict[str, Any]) -> bool:
        if isinstance(expected, bool) or isinstance(actual, bool):
            return bool(expected == actual)
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return math.isclose(
                float(expected),
                float(actual),
                abs_tol=float(tolerance.get("absolute", 0.0)),
                rel_tol=float(tolerance.get("relative", 0.0)),
            )
        if isinstance(expected, list) and isinstance(actual, list):
            return len(expected) == len(actual) and all(
                EvaluationService._compatibility_equal(left, right, tolerance)
                for left, right in zip(expected, actual, strict=True)
            )
        if isinstance(expected, dict) and isinstance(actual, dict):
            return expected.keys() == actual.keys() and all(
                EvaluationService._compatibility_equal(expected[key], actual[key], tolerance)
                for key in expected
            )
        return bool(expected == actual)

    def _evaluate_model_compatibility(
        self,
        run_id: str,
        run_resource: dict[str, Any],
        run_result: dict[str, Any],
        model_package: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]], int]:
        package_spec = model_package["spec"]
        adapter = package_spec["adapters"]["inferenceReference"]
        admission = run_resource["spec"]["extensions"]
        try:
            adapter_admission = admission["inferenceAdapter"]
            source_digest = adapter_admission["sourceDigest"]
            state = run_result["spec"]["outputs"][adapter["stateOutput"]]
        except (KeyError, TypeError) as exc:
            raise IntegrityError(
                "model package adapter does not match admitted run outputs"
            ) from exc
        binding = self.factory._resource_by_uri("Binding", run_resource["spec"]["bindingRef"])
        resolved = self.factory._resolve_executor(
            str(binding["spec"]["executor"]), binding, self.factory._executor_config(binding)
        )
        self.factory._require_executor(resolved, MODULE_EXECUTION_CAPABILITIES)
        source_manifest = self.factory.local_store.read_manifest(source_digest)
        if not ArtifactBuilder(self.factory.local_store).verify(source_manifest):
            raise IntegrityError("admitted compatibility adapter source failed verification")
        if source_manifest.digest != adapter_admission.get("packageDigest"):
            raise IntegrityError("compatibility adapter source differs from run admission")
        self.factory.lineage.add(
            LineageEdge(
                f"artifact:{source_digest}",
                f"run:{run_id}/compatibility",
                "used",
                "entity",
                "activity",
                run_id=run_id,
            )
        )
        failures: list[dict[str, Any]] = []
        vectors = package_spec["compatibilityVectors"]
        with tempfile.TemporaryDirectory(dir=self.factory.paths.packages) as temporary_name:
            temporary = Path(temporary_name)
            archive = temporary / "archive"
            ArtifactBuilder(self.factory.local_store).restore(source_manifest, archive)
            code_root = extract_module_package(archive / "payload", temporary / "source")
            manifest, code_root = load_manifest(code_root / Path(adapter["module"]).name, code_root)
            environment = self.factory._prepare_module_environment(
                resolved.executor, manifest, code_root
            )
            if environment["digest"] != adapter_admission["environment"]["digest"]:
                raise IntegrityError("compatibility adapter environment differs from run admission")
            signatures = package_spec["signatures"]
            state = self.factory._resolve_model_state(state, temporary / "state")
            validate_contract(signatures["state"], state, "model package state")
            for index, vector in enumerate(vectors):
                validate_contract(signatures["input"], vector["inputs"], "model package input")
                request = ProtocolRequest.model_validate(
                    {
                        "operation": adapter["operation"],
                        "inputs": vector["inputs"],
                        "state": state,
                        "config": adapter["config"],
                        "context": {
                            "runId": run_id,
                            "compatibilityVector": vector["name"],
                            "inference": {
                                "method": vector["method"],
                                "seed": vector.get("seed"),
                            },
                        },
                    }
                )
                result = self.factory._execute_module(
                    manifest,
                    code_root,
                    request,
                    self.factory.paths.runs / run_id / "evaluations" / "compatibility" / str(index),
                    executor=resolved.executor,
                    executor_config=resolved.config,
                    environment=environment,
                )
                validate_contract(signatures["output"], result.outputs, "model package output")
                for output, expected in vector["expected"].items():
                    if output not in result.outputs or not self._compatibility_equal(
                        expected,
                        result.outputs.get(output),
                        vector.get("tolerances", {}).get(output, {}),
                    ):
                        failures.append(
                            {"kind": "compatibility", "vector": vector["name"], "output": output}
                        )
        return not failures, failures, len(vectors)

    def evaluate(self, subject: str) -> dict[str, Any]:
        run_id = subject.removeprefix("run/")
        run_status = self.factory.run_status(run_id)
        run_resource = self.factory._run_resource(run_id)
        run_result = self.factory._run_result(run_id, run_status["status"])
        outputs = run_result["spec"]["outputs"]
        passing = {
            key: value
            for key, value in outputs.items()
            if key.lower().endswith((".passed", ".pass")) and isinstance(value, bool)
        }
        failures = []
        if run_status["status"].get("state") != "Succeeded":
            failures.append({"kind": "run", "message": "source run did not succeed"})
        if not passing:
            failures.append({"kind": "protocol", "message": "no evaluator pass result found"})
        metric_scores: dict[str, Any] = {}
        for reference in run_resource["spec"]["extensions"].get("evaluationRefs", []):
            suite = self.factory._resource_by_uri("EvaluationSpec", reference)
            for metric in suite["spec"]["metrics"]:
                value = outputs.get(metric["output"])
                metric_scores[metric["name"]] = value
                if not isinstance(value, (bool, int, float)):
                    failures.append(
                        {"kind": "metric", "metric": metric["name"], "message": "missing value"}
                    )
        model_package_ref = run_resource["spec"]["extensions"].get("modelPackageRef")
        if model_package_ref:
            model_package = self.factory._resource_by_uri("ModelPackage", model_package_ref)
            compatibility_passed, compatibility_failures, vector_count = (
                self._evaluate_model_compatibility(run_id, run_resource, run_result, model_package)
            )
            failures.extend(compatibility_failures)
        else:
            explicit = {
                key: value
                for key, value in outputs.items()
                if key.lower().endswith((".compatibilitypassed", ".compatibility_passed"))
                and isinstance(value, bool)
            }
            compatibility_passed = bool(explicit) and all(explicit.values())
            vector_count = 0
            if not compatibility_passed:
                failures.append(
                    {"kind": "compatibility", "message": "no model compatibility evidence found"}
                )
        passed = not failures and all(passing.values()) and compatibility_passed
        resource = self.factory.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "EvaluationResult",
                "metadata": {
                    "name": f"evaluation-{run_id}",
                    "namespace": self.factory.namespace,
                },
                "spec": {
                    "evaluationRef": f"run/{run_id}",
                    "scores": {
                        **passing,
                        **metric_scores,
                        "compatibilityPassed": compatibility_passed,
                        "passed": passed,
                    },
                    "provenance": {
                        "runId": run_id,
                        "runRef": self.factory._resource_uri(run_resource),
                        "runResultRef": self.factory._resource_uri(run_result),
                        "runStatusVersion": run_status["statusVersion"],
                    },
                    "uncertainty": EvaluationService.extract_uncertainty(outputs),
                    "failures": failures,
                    "extensions": {
                        "passed": passed,
                        "compatibilityPassed": compatibility_passed,
                        "compatibilityVectors": vector_count,
                        "evaluationRefs": run_resource["spec"]["extensions"].get(
                            "evaluationRefs", []
                        ),
                        "modelPackageRef": model_package_ref,
                        "runId": run_id,
                    },
                },
            },
            _system=True,
        )
        metadata = resource["metadata"]
        self.factory.events.append(
            type="EvaluationCompleted",
            source=f"openfoundry://{self.factory.namespace}",
            subject=f"run/{run_id}",
            resource_uid=metadata["uid"],
            revision=metadata["revision"],
            actor=self.factory.actor,
            run_id=run_id,
            data={"passed": passed, "failures": len(failures)},
            dataschema="https://schemas.openfoundry.dev/events/evaluation-completed/v1",
        )
        self.factory.lineage.add(
            LineageEdge(
                f"run:{run_id}",
                self.factory._resource_uri(resource),
                "generated",
                "activity",
                "entity",
                run_id=run_id,
            )
        )
        return resource

    def create_experiment(
        self,
        *,
        name: str,
        baseline_ref: str,
        candidate_ref: str,
        metric: str,
        direction: str,
    ) -> dict[str, Any]:
        if direction not in {"maximize", "minimize"}:
            raise ValidationError("experiment direction must be maximize or minimize")
        baseline = self.factory._evaluation_result(baseline_ref)
        candidate = self.factory._evaluation_result(candidate_ref)
        baseline_evaluations = baseline["spec"].get("extensions", {}).get("evaluationRefs", [])
        candidate_evaluations = candidate["spec"].get("extensions", {}).get("evaluationRefs", [])
        if baseline_evaluations != candidate_evaluations:
            raise ValidationError("experiment subjects use different evaluation revisions")
        try:
            baseline_score = baseline["spec"]["scores"][metric]
            candidate_score = candidate["spec"]["scores"][metric]
        except KeyError as exc:
            raise ValidationError("experiment metric is missing or non-numeric") from exc
        if (
            not isinstance(baseline_score, (int, float))
            or isinstance(baseline_score, bool)
            or not math.isfinite(float(baseline_score))
            or not isinstance(candidate_score, (int, float))
            or isinstance(candidate_score, bool)
            or not math.isfinite(float(candidate_score))
        ):
            raise ValidationError("experiment metric is missing or non-numeric")
        baseline_value = float(baseline_score)
        candidate_value = float(candidate_score)
        delta = candidate_value - baseline_value
        decision = (
            "tie"
            if delta == 0
            else "candidate"
            if (delta > 0) == (direction == "maximize")
            else "baseline"
        )
        return self.factory.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Experiment",
                "metadata": {"name": name, "namespace": self.factory.namespace},
                "spec": {
                    "baselineRef": self.factory._resource_uri(baseline),
                    "candidateRef": self.factory._resource_uri(candidate),
                    "evaluationRefs": baseline_evaluations,
                    "metric": metric,
                    "direction": direction,
                    "decision": decision,
                    "delta": delta,
                    "extensions": {},
                },
            },
            _system=True,
        )
