from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openfoundry.canonical import canonical_json, sha256_digest
from openfoundry.errors import ConflictError, IntegrityError, ValidationError
from openfoundry.executors import ExecutionPlan, Executor, ResolvedExecutor
from openfoundry.modules import ModuleManifest, validate_contract, validate_contract_schema
from openfoundry.sdk import ProtocolRequest
from openfoundry.workloads import AdmittedWorkload, Stage


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextlib.contextmanager
def _operation_lease(path: Path) -> Iterator[None]:
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConflictError("run operation is already executing") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_execution_record(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("wb") as output:
        output.write(canonical_json(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _execution_plan_digest(
    plan: ExecutionPlan, *, request_digest: str, environment_digest: str
) -> str:
    return sha256_digest(
        {
            "plan": {
                "argv": plan.argv,
                "runDir": str(plan.run_dir),
                "cwd": str(plan.cwd),
                "env": plan.env,
                "resources": plan.resources,
                "timeout": plan.timeout,
                "denyNetwork": plan.deny_network,
                "metadata": plan.metadata,
            },
            "request": request_digest,
            "environment": environment_digest,
        }
    )


@dataclass(frozen=True)
class _CapturedSource:
    stage: Stage
    is_inference: bool
    manifest: ModuleManifest
    code_root: Path
    package_digest: str
    artifact_digest: str
    expected_environment: dict[str, Any] | None


@dataclass(frozen=True)
class _RunContext:
    run_id: str
    run_dir: Path
    recovering: bool
    spec: AdmittedWorkload
    stages: list[Stage]
    run_resource: dict[str, Any]
    executor: ResolvedExecutor
    admitted_modules: dict[str, tuple[ModuleManifest, Path, str]]
    pinned_inputs: dict[str, dict[str, Any]]
    pinned_references: dict[str, dict[str, Any]]
    outputs: dict[str, Any]


def _write_module_request(run_dir: Path, request: ProtocolRequest, recovering: bool) -> None:
    request_path = run_dir / "request.json"
    request_bytes = canonical_json(request.model_dump(mode="json"))
    if recovering and request_path.exists():
        if request_path.read_bytes() != request_bytes:
            raise IntegrityError("recovered module request differs from the admitted request")
        return
    request_path.write_bytes(request_bytes)


def _recovered_execution(
    executor: Executor, run_dir: Path, record: dict[str, Any], plan_digest: str
) -> str:
    if record.get("planDigest") != plan_digest:
        raise IntegrityError("recovered executor plan differs from the admitted plan")
    if record.get("state") == "submitted" and isinstance(record.get("executionId"), str):
        execution_id = str(record["executionId"])
        executor.attach(execution_id, run_dir)
        return execution_id
    if record.get("state") != "launching":
        raise IntegrityError("recovered executor record is invalid")
    recovered = executor.recover(run_dir)
    if recovered is None:
        raise IntegrityError("executor launch outcome is indeterminate")
    _write_execution_record(
        run_dir / "controller-execution.json",
        {"version": 1, "state": "submitted", "planDigest": plan_digest, "executionId": recovered},
    )
    return recovered


def _ensure_execution(
    executor: Executor, run_dir: Path, plan: ExecutionPlan, plan_digest: str, recovering: bool
) -> str:
    execution_record = run_dir / "controller-execution.json"
    if recovering and execution_record.exists():
        record = json.loads(execution_record.read_text())
        return _recovered_execution(executor, run_dir, record, plan_digest)
    _write_execution_record(
        execution_record, {"version": 1, "state": "launching", "planDigest": plan_digest}
    )
    execution_id = executor.submit(plan)
    _write_execution_record(
        execution_record,
        {
            "version": 1,
            "state": "submitted",
            "planDigest": plan_digest,
            "executionId": execution_id,
        },
    )
    return execution_id


def _finite_tolerance(tolerance: Any) -> bool:
    return isinstance(tolerance, dict) and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
        for value in tolerance.values()
    )


def _validate_package_signatures(package_spec: dict[str, Any]) -> None:
    signatures = package_spec["signatures"]
    for name in ("input", "output", "state"):
        contract = signatures[name]
        validate_contract_schema(contract, f"model package {name}")
        if contract.get("type") != "object":
            raise ValidationError(
                f"model package {name} contract must describe an object for openfoundry.module/v1"
            )
    for vector in package_spec["compatibilityVectors"]:
        validate_contract(signatures["input"], vector["inputs"], "model package input")
        validate_contract(signatures["output"], vector["expected"], "model package output")
        if not all(_finite_tolerance(item) for item in vector.get("tolerances", {}).values()):
            raise ValidationError("model package tolerances must be finite and non-negative")


def _training_stage(training: dict[str, Any], stages: list[Stage]) -> Stage:
    workload_stages = {stage.name: stage for stage in stages}
    if training["stage"] not in workload_stages:
        raise ValidationError("ModelPackage trainingReference references an unknown workload stage")
    training_stage = workload_stages[training["stage"]]
    if training_stage.data_use != "training":
        raise ValidationError("ModelPackage trainingReference must declare training data use")
    if training["operation"] != training_stage.operation or any(
        training_stage.config.get(key) != value for key, value in training["config"].items()
    ):
        raise ValidationError("ModelPackage trainingReference does not match the workload stage")
    return training_stage


def _pin_checkpoint(resource: dict[str, Any], name: str, uri: str) -> dict[str, Any]:
    components = resource["spec"].get("components", {})
    if not isinstance(components, dict) or "module-state" not in components:
        raise IntegrityError(f"checkpoint has no module-state component: {name}")
    artifacts = {"checkpoint": str(resource["spec"]["artifactRef"])}
    artifacts.update({role: str(digest) for role, digest in components.items()})
    return {
        "kind": "checkpoint",
        "uri": uri,
        "artifacts": artifacts,
        "runRef": resource["spec"]["runRef"],
    }


def _check_metric_names(evaluation_specs: list[dict[str, Any]]) -> None:
    metric_names = [
        metric["name"] for suite in evaluation_specs for metric in suite["spec"]["metrics"]
    ]
    reserved_scores = {"compatibilityPassed", "passed"}
    if len(metric_names) != len(set(metric_names)) or reserved_scores.intersection(metric_names):
        raise ValidationError("evaluation metric names must be unique and not reserved")
