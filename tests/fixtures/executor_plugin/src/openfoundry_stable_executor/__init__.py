from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from openfoundry.executors import (
    EXECUTOR_API_VERSION,
    MODULE_PROTOCOL_CAPABILITIES,
    DependencyLock,
    ExecutionPlan,
    ExecutionStatus,
    Executor,
    ExecutorContext,
    ExecutorProvider,
)

_RECORD = "stable-execution.json"
_COMPLETION = "stable-completion.json"
_PLAN = "stable-plan.json"
_ATTEMPTS = "stable-submit-attempts.jsonl"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid executor record: {path.name}")
    return value


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _network_namespace_available() -> bool:
    unshare = shutil.which("unshare")
    if unshare is None or os.name != "posix":
        return False
    completed = subprocess.run(
        [unshare, "--user", "--map-root-user", "--net", "true"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


_CAPABILITIES = MODULE_PROTOCOL_CAPABILITIES | frozenset(
    {"recovery:attach"} | ({"isolation:network-deny"} if _network_namespace_available() else set())
)


class StableTestExecutor(Executor):
    def __init__(self, context: ExecutorContext) -> None:
        self.context = context
        self._directories: dict[str, Path] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    @property
    def capabilities(self) -> frozenset[str]:
        return _CAPABILITIES

    def preflight(self) -> list[str]:
        return [] if os.name == "posix" else ["test provider requires POSIX process groups"]

    def prepare_environment(
        self,
        *,
        argv: list[str],
        cwd: Path,
        dependency: DependencyLock,
        deny_network: bool = False,
    ) -> dict[str, Any]:
        if dependency.contents:
            raise RuntimeError("test provider accepts only the empty test dependency lock")
        executable = shutil.which(argv[0])
        if executable is None:
            candidate = cwd / argv[0]
            executable = str(candidate) if candidate.is_file() else None
        if executable is None:
            raise RuntimeError(f"module executable is unavailable: {argv[0]}")
        invocation = Path(executable)
        # Resolving the final symlink would discard a Python virtual environment.
        command = [str(invocation.parent.resolve() / invocation.name), *argv[1:]]
        if deny_network:
            unshare = shutil.which("unshare")
            if unshare is None or not _network_namespace_available():
                raise RuntimeError("network denial unavailable")
            command = [unshare, "--user", "--map-root-user", "--net", "--", *command]
        identity = json.dumps(
            {"command": command, "dependencyDigest": dependency.digest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return {
            "command": command,
            "dependencyDigest": dependency.digest,
            "digest": "sha256:" + hashlib.sha256(identity).hexdigest(),
            "provider": "stable-test",
        }

    def plan(
        self,
        *,
        argv: list[str],
        run_dir: Path,
        cwd: Path,
        env: dict[str, str] | None = None,
        resources: dict[str, int | float] | None = None,
        timeout: float | None = None,
        deny_network: bool = False,
        requires_result: bool = True,
        environment: dict[str, Any] | None = None,
        **_: Any,
    ) -> ExecutionPlan:
        return ExecutionPlan(
            argv=tuple(argv),
            run_dir=run_dir,
            cwd=cwd,
            env=env or {},
            resources=resources or {},
            timeout=timeout,
            deny_network=deny_network,
            metadata={
                "requiresResult": requires_result,
                "environmentDigest": (environment or {}).get("digest"),
            },
        )

    def submit(self, plan: ExecutionPlan) -> str:
        execution_id = str(uuid.uuid4())
        plan.run_dir.mkdir(parents=True, exist_ok=True)
        with (plan.run_dir / _ATTEMPTS).open("a", encoding="utf-8") as attempts:
            attempts.write(execution_id + "\n")
            attempts.flush()
            os.fsync(attempts.fileno())
        request = plan.run_dir / "request.json"
        request.touch(exist_ok=True)
        _write_json(
            plan.run_dir / _PLAN,
            {
                "argv": list(plan.argv),
                "cwd": str(plan.cwd),
                "env": plan.env,
                "timeout": plan.timeout,
                "requiresResult": bool(plan.metadata.get("requiresResult", True)),
                "environmentDigest": plan.metadata.get("environmentDigest"),
            },
        )
        _write_json(
            plan.run_dir / _RECORD,
            {"id": execution_id, "state": "launching"},
        )
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", str(plan.run_dir)],
            cwd=plan.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_json(
            plan.run_dir / _RECORD,
            {"id": execution_id, "state": "submitted", "pid": process.pid},
        )
        self._directories[execution_id] = plan.run_dir
        self._processes[execution_id] = process
        marker = self.context.state_root / "stable-test-submit-interrupted"
        if self.context.config.get("interruptSubmitOnce") and not marker.exists():
            marker.touch()
            raise KeyboardInterrupt("controller interrupted during provider submit")
        return execution_id

    def status(self, execution_id: str) -> ExecutionStatus:
        directory = self._directory(execution_id)
        marker = self.context.state_root / "stable-test-status-interrupted"
        if self.context.config.get("interruptStatusOnce") and not marker.exists():
            marker.touch()
            raise KeyboardInterrupt("controller interrupted after durable submit")
        process = self._processes.get(execution_id)
        if process is not None:
            process.poll()
        completion_path = directory / _COMPLETION
        if completion_path.exists():
            completion = _read_json(completion_path)
            return ExecutionStatus(
                str(completion["state"]),  # type: ignore[arg-type]
                str(completion["reason"]) if completion.get("reason") else None,
                int(completion["exitCode"]),
            )
        record = _read_json(directory / _RECORD)
        if record["state"] == "canceled":
            return ExecutionStatus("canceled", "canceled by controller", -signal.SIGTERM)
        pid = record.get("pid")
        if isinstance(pid, int) and _alive(pid):
            return ExecutionStatus("running")
        return ExecutionStatus("unknown", "worker disappeared before durable completion")

    def cancel(self, execution_id: str) -> None:
        directory = self._directory(execution_id)
        record = _read_json(directory / _RECORD)
        pid = record.get("pid")
        _write_json(directory / _RECORD, {**record, "state": "canceled"})
        if isinstance(pid, int) and _alive(pid):
            os.killpg(pid, signal.SIGTERM)

    def logs(self, execution_id: str) -> tuple[Path, Path]:
        directory = self._directory(execution_id)
        return directory / "stdout.log", directory / "stderr.log"

    def attach(self, execution_id: str, run_dir: Path) -> None:
        record = _read_json(run_dir / _RECORD)
        if record.get("id") != execution_id or record.get("state") not in {
            "submitted",
            "canceled",
        }:
            raise RuntimeError("execution identity does not match the run directory")
        self._directories[execution_id] = run_dir

    def recover(self, run_dir: Path) -> str | None:
        path = run_dir / _RECORD
        if not path.exists():
            return None
        record = _read_json(path)
        if record.get("state") == "launching":
            raise RuntimeError("executor launch outcome is indeterminate")
        execution_id = str(record["id"])
        self.attach(execution_id, run_dir)
        return execution_id

    def _directory(self, execution_id: str) -> Path:
        try:
            return self._directories[execution_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution: {execution_id}") from exc


def create(context: ExecutorContext) -> StableTestExecutor:
    return StableTestExecutor(context)


provider = ExecutorProvider(
    name="stable-test",
    api_version=EXECUTOR_API_VERSION,
    factory=create,
    description="Independent acceptance-test executor.",
    capabilities=_CAPABILITIES,
    config_contract={
        "type": "object",
        "properties": {
            "interruptStatusOnce": {"type": "boolean"},
            "interruptSubmitOnce": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
)


def _worker(run_dir: Path) -> int:
    plan = _read_json(run_dir / _PLAN)
    execution = _read_json(run_dir / _RECORD)
    environment = {
        key: value for key, value in os.environ.items() if key in {"HOME", "LANG", "PATH", "TZ"}
    }
    environment.update({str(key): str(value) for key, value in plan["env"].items()})
    environment.update(
        {
            "OPENFOUNDRY_REQUEST_FILE": str(run_dir / "request.json"),
            "OPENFOUNDRY_RESULT_FILE": str(run_dir / "result.json"),
            "OPENFOUNDRY_RUN_ID": str(execution["id"]),
        }
    )
    with (
        (run_dir / "stdout.log").open("wb") as stdout,
        (run_dir / "stderr.log").open("wb") as stderr,
    ):
        try:
            completed = subprocess.run(
                [str(item) for item in plan["argv"]],
                cwd=str(plan["cwd"]),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=float(plan["timeout"]) if plan.get("timeout") else None,
                check=False,
            )
            exit_code = completed.returncode
            missing_result = bool(plan["requiresResult"]) and not (run_dir / "result.json").exists()
            state = "succeeded" if exit_code == 0 and not missing_result else "failed"
            reason = "result missing" if missing_result else None
        except subprocess.TimeoutExpired:
            exit_code, state, reason = -1, "failed", "timeout"
    _write_json(
        run_dir / _COMPLETION,
        {"state": state, "exitCode": exit_code, "reason": reason, "finished": time.time()},
    )
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "worker":
        raise SystemExit("usage: executor-plugin worker RUN_DIR")
    raise SystemExit(_worker(Path(sys.argv[2])))
