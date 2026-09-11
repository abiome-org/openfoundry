from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ExecutionState = Literal["pending", "running", "succeeded", "failed", "canceled", "unknown"]

MODULE_PROTOCOL_CAPABILITIES = frozenset(
    {
        "protocol:openfoundry.module/v1",
        "transport:module-source",
        "transport:request-result",
        "transport:artifacts",
    }
)
MODULE_EXECUTION_CAPABILITIES = MODULE_PROTOCOL_CAPABILITIES | frozenset({"isolation:network-deny"})
DEPLOYMENT_PROTOCOL_CAPABILITIES = frozenset({"protocol:openfoundry.deployment/v1"})


@dataclass(frozen=True)
class ExecutionPlan:
    argv: tuple[str, ...]
    run_dir: Path
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None
    deny_network: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionStatus:
    state: ExecutionState
    reason: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class DependencyLock:
    relative_path: str
    digest: str
    contents: bytes = field(repr=False)


class Executor(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> frozenset[str]: ...
    @abstractmethod
    def preflight(self) -> list[str]: ...
    @abstractmethod
    def plan(
        self, *, argv: list[str], run_dir: Path, cwd: Path, **kwargs: Any
    ) -> ExecutionPlan: ...
    @abstractmethod
    def submit(self, plan: ExecutionPlan) -> str: ...
    @abstractmethod
    def status(self, execution_id: str) -> ExecutionStatus: ...
    @abstractmethod
    def cancel(self, execution_id: str) -> None: ...
    @abstractmethod
    def logs(self, execution_id: str) -> tuple[Path, Path]: ...

    def read_logs(self, execution_id: str, *, tail_bytes: int = 4096) -> tuple[str, str]:
        if tail_bytes < 1:
            raise ValueError("tail_bytes must be positive")

        def tail(path: Path) -> str:
            if not path.exists():
                return ""
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - tail_bytes))
                decoded = stream.read(tail_bytes).decode(errors="replace")
            return decoded.encode()[-tail_bytes:].decode(errors="ignore")

        stdout, stderr = self.logs(execution_id)
        return tail(stdout), tail(stderr)

    def attach(self, execution_id: str, run_dir: Path) -> None:
        del execution_id, run_dir

    def recover(self, run_dir: Path) -> str | None:
        del run_dir
        raise RuntimeError("executor cannot identify an interrupted submission")

    def prepare_environment(
        self,
        *,
        argv: list[str],
        cwd: Path,
        dependency: DependencyLock,
        deny_network: bool = False,
    ) -> dict[str, Any]:
        del argv, cwd, dependency, deny_network
        raise RuntimeError("executor cannot attest the declared module environment")
