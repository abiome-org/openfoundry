from __future__ import annotations

import fcntl
import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from openfoundry.canonical import portable_relative_path, sha256_digest
from openfoundry.errors import IntegrityError, OperationCanceled, ValidationError
from openfoundry.schema_registry import default_registry


class RunState(StrEnum):
    DRAFT = "Draft"
    VALIDATED = "Validated"
    ADMITTED = "Admitted"
    RUNNING = "Running"
    RECOVERING = "Recovering"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELED = "Canceled"


_TRANSITIONS = {
    RunState.DRAFT: {RunState.VALIDATED, RunState.CANCELED},
    RunState.VALIDATED: {RunState.ADMITTED, RunState.FAILED, RunState.CANCELED},
    RunState.ADMITTED: {RunState.RUNNING, RunState.CANCELED},
    RunState.RUNNING: {RunState.RECOVERING, RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELED},
    RunState.RECOVERING: {RunState.RUNNING, RunState.FAILED, RunState.CANCELED},
}


DataUse = Literal["training", "evaluation"]


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str
    needs: list[str] = Field(default_factory=list)
    module: str
    operation: str = "run"
    data_use: DataUse = Field(default="training", alias="dataUse")
    config: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)


def dataset_uses(stages: list[Stage]) -> dict[str, list[DataUse]]:
    uses: dict[str, set[DataUse]] = {}
    for stage in stages:
        for reference in stage.inputs.values():
            if reference.startswith("dataset/"):
                uses.setdefault(reference, set()).add(stage.data_use)
    return {reference: sorted(purposes) for reference, purposes in uses.items()}


class AdmittedWorkload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stages: list[Stage]
    source_digest: str
    binding_digest: str | None = None
    module_digests: dict[str, str] = Field(default_factory=dict)
    environments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_revisions: dict[str, str] = Field(default_factory=dict)
    reference_revisions: dict[str, str] = Field(default_factory=dict)
    model_package_ref: str | None = None
    evaluation_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def graph(self) -> AdmittedWorkload:
        names = [x.name for x in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("stage names must be unique")
        known = set(names)
        for stage in self.stages:
            if stage.name in stage.needs or not set(stage.needs) <= known:
                raise ValueError(f"invalid dependency for {stage.name}")
            for reference in stage.inputs.values():
                producer, separator, output = reference.partition(".")
                if separator and producer in known:
                    producer_stage = next(item for item in self.stages if item.name == producer)
                    if producer not in stage.needs:
                        raise ValueError(
                            f"stage {stage.name} input references {producer} without a dependency"
                        )
                    if output not in producer_stage.outputs:
                        raise ValueError(f"stage input references undeclared output: {reference}")
        self.topological_order()
        return self

    def topological_order(self) -> list[str]:
        pending = {s.name: set(s.needs) for s in self.stages}
        result = []
        while pending:
            ready = sorted(k for k, v in pending.items() if not v)
            if not ready:
                raise ValueError("workload graph contains a cycle")
            for name in ready:
                result.append(name)
                pending.pop(name)
            for needs in pending.values():
                needs.difference_update(ready)
        return result

    @property
    def digest(self) -> str:
        value = self.model_dump(mode="json", exclude={"binding_digest", "environments"})
        # Preserve admission digests recorded before per-stage data uses existed.
        for stage in value["stages"]:
            if stage["data_use"] == "training":
                del stage["data_use"]
        value["environmentDigests"] = {
            stage: descriptor["digest"] for stage, descriptor in sorted(self.environments.items())
        }
        return sha256_digest(value)


def project_workload(resource: Any) -> AdmittedWorkload:
    value = default_registry.validate_as(resource, "WorkloadSpec")
    spec = value["spec"]
    try:
        stages = [Stage.model_validate(stage) for stage in spec["graph"]["stages"]]
        for stage in stages:
            portable_relative_path(stage.module, f"stage {stage.name!r} module")
        admitted = AdmittedWorkload(
            stages=stages,
            source_digest="pending",
            model_package_ref=spec.get("modelPackageRef"),
            evaluation_refs=spec.get("evaluationRefs", []),
        )
    except PydanticValidationError as exc:
        raise ValidationError(
            "workload semantic validation failed",
            details={"errors": exc.error_count()},
        ) from exc
    desired = {
        "apiVersion": value["apiVersion"],
        "kind": value["kind"],
        "metadata": {
            "name": value["metadata"]["name"],
            "namespace": value["metadata"].get("namespace"),
        },
        "spec": spec,
    }
    return admitted.model_copy(update={"source_digest": sha256_digest(desired)})


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def initialize(self, spec: AdmittedWorkload) -> None:
        if not self.path.exists():
            self._write(
                {
                    "version": 0,
                    "state": "Draft",
                    "history": [],
                    "stages": {},
                    "digests": self._digests(spec),
                }
            )

    @staticmethod
    def _digests(spec: AdmittedWorkload) -> dict[str, Any]:
        return {
            "workload": spec.digest,
            "workloadManifest": spec.source_digest,
            "binding": spec.binding_digest,
            "modules": spec.module_digests,
            "environments": spec.environments,
            "inputs": spec.input_revisions,
            "references": spec.reference_revisions,
            "modelPackage": spec.model_package_ref,
        }

    def verify(self, spec: AdmittedWorkload) -> dict[str, Any]:
        value = self.read()
        if value.get("digests") != self._digests(spec):
            raise IntegrityError("run state admission evidence differs from the admitted workload")
        stages = value.get("stages")
        known = {stage.name for stage in spec.stages}
        if not isinstance(stages, dict) or not set(stages) <= known:
            raise IntegrityError("run state contains an unknown stage")
        for name, stage in stages.items():
            _verify_stage_record(name, stage)
        if value.get("state") == RunState.SUCCEEDED.value:
            _verify_succeeded(spec, stages)
        return value

    def _write(self, value: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def read(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.path.read_text())
        return value

    def transition(
        self, expected: RunState, target: RunState, reason: str | None = None
    ) -> dict[str, Any]:
        lock = self.path.with_suffix(".lock")
        with lock.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            value = self.read()
            if value["state"] != expected.value:
                raise RuntimeError("state compare-and-set conflict")
            if target not in _TRANSITIONS.get(expected, set()):
                raise ValueError("invalid state transition")
            value["version"] += 1
            value["state"] = target.value
            value["history"].append(
                {
                    "from": expected.value,
                    "to": target.value,
                    "reason": reason,
                    "version": value["version"],
                }
            )
            self._write(value)
            return value


def _verify_stage_record(name: str, stage: Any) -> None:
    if not isinstance(stage, dict) or stage.get("status") not in {"succeeded", "failed"}:
        raise IntegrityError(f"run state for stage {name!r} is invalid")
    attempt = stage.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IntegrityError(f"run state attempt for stage {name!r} is invalid")
    if stage["status"] == "succeeded" and not isinstance(stage.get("outputs"), dict):
        raise IntegrityError(f"run state outputs for stage {name!r} are invalid")


def _verify_succeeded(spec: AdmittedWorkload, stages: dict[str, Any]) -> None:
    known = {stage.name for stage in spec.stages}
    if set(stages) != known or any(stage["status"] != "succeeded" for stage in stages.values()):
        raise IntegrityError("succeeded run state does not contain every successful stage")
    for stage in spec.stages:
        if not set(stage.outputs) <= set(stages[stage.name]["outputs"]):
            raise IntegrityError(
                f"succeeded run state is missing declared outputs for stage {stage.name!r}"
            )


class WorkloadRunner:
    def __init__(self, spec: AdmittedWorkload, store: StateStore) -> None:
        self.spec = spec
        self.store = store
        store.initialize(spec)

    def run(self, execute: Any, verify: Any = lambda outputs: True) -> dict[str, Any]:
        value = self.store.read()
        for name in self.spec.topological_order():
            stage = next(x for x in self.spec.stages if x.name == name)
            old = value["stages"].get(name)
            if old and old.get("status") == "succeeded":
                if verify(old.get("outputs", {})):
                    continue
                raise IntegrityError(
                    f"succeeded stage {name!r} output evidence failed verification"
                )
            try:
                outputs = execute(stage)
            except OperationCanceled:
                raise
            except Exception:
                value["stages"][name] = {"status": "failed", "attempt": 1}
                self.store._write(value)
                raise
            value["stages"][name] = {"status": "succeeded", "attempt": 1, "outputs": outputs}
            self.store._write(value)
        return value
