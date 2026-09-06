from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.errors import ConflictError, IntegrityError, OperationCanceled, ValidationError
from openfoundry.workloads import RunState, StateStore

if TYPE_CHECKING:
    from openfoundry.factory import Factory


class RunControl:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    def check(self, operation_id: str) -> None:
        if self.factory.operations.get(operation_id).get("cancelRequest"):
            raise OperationCanceled("run cancellation requested")

    def request(self, operation_id: str, reason: str) -> dict[str, Any]:
        from openfoundry.factory import _operation_lease

        self.factory._authorize("operation.cancel")
        operation = self.factory.operations.get(operation_id)
        if operation["kind"] != "run" or operation["request"]["actor"] != self.factory.actor:
            raise ValidationError("only the run's actor can cancel its operation")
        if not reason.strip() or len(reason) > 1024:
            raise ValidationError("cancellation reason must contain 1 to 1024 characters")
        requested = self.factory.operations.request_cancel(
            operation_id, actor=self.factory.actor, reason=reason
        )
        if not requested.get("cancelRequest") or requested["state"] == "canceled":
            return requested
        lease = self.factory.paths.state / "operations" / f"{operation_id}.lock"
        try:
            with _operation_lease(lease):
                return self.stop(operation_id)
        except ConflictError:
            return self.factory.operations.get(operation_id)

    def stop(self, operation_id: str) -> dict[str, Any]:
        operation = self.factory.operations.get(operation_id)
        if operation["state"] in {"succeeded", "failed", "canceled", "finalizing"}:
            return operation
        run_dir = self.factory.paths.runs / operation_id
        try:
            run = self.factory._run_resource(operation_id)
        except IntegrityError:
            if list(run_dir.glob("stages/*/controller-execution.json")):
                raise IntegrityError(
                    "cannot identify the admitted executor for cancellation"
                ) from None
            run = None
        if run is not None:
            self._stop_executions(run, run_dir)
            store = StateStore(run_dir / "state.json")
            state = RunState(store.read()["state"])
            if state == RunState.SUCCEEDED:
                raise ConflictError("run finished before cancellation could be confirmed")
            if state not in {RunState.CANCELED, RunState.FAILED}:
                store.transition(state, RunState.CANCELED, operation["cancelRequest"]["reason"])
            self.factory._settle_run(
                run,
                operation_id,
                {
                    "state": "Canceled",
                    "reason": operation["cancelRequest"]["reason"],
                    "outputs": {},
                },
                operation["cancelRequest"]["reason"],
            )
        return self.factory.operations.advance(
            operation_id, state="canceled", result={"runId": operation_id, "state": "Canceled"}
        )

    def _stop_executions(self, run: dict[str, Any], directory: Path) -> None:
        binding = self.factory._resource_by_uri("Binding", run["spec"]["bindingRef"])
        executor = self.factory._resolve_executor(
            binding["spec"]["executor"], binding, self.factory._executor_config(binding)
        ).executor
        for path in sorted(directory.glob("stages/*/controller-execution.json")):
            record = json.loads(path.read_bytes())
            if record.get("state") == "submitted":
                execution_id = str(record["executionId"])
                executor.attach(execution_id, path.parent)
            elif record.get("state") == "launching":
                recovered = executor.recover(path.parent)
                if recovered is None:
                    continue
                execution_id = recovered
            else:
                raise IntegrityError("invalid execution receipt during cancellation")
            status = executor.status(execution_id)
            if status.state in {"pending", "running"}:
                executor.cancel(execution_id)
                status = executor.status(execution_id)
            if status.state in {"pending", "running", "unknown"}:
                raise IntegrityError("executor has not confirmed cancellation")
