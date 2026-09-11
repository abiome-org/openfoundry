from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openfoundry.canonical import sha256_digest
from openfoundry.errors import CapabilityError, IntegrityError
from openfoundry.executors.base import (
    DEPLOYMENT_PROTOCOL_CAPABILITIES,
    MODULE_PROTOCOL_CAPABILITIES,
    DependencyLock,
    ExecutionPlan,
    ExecutionStatus,
    Executor,
)

_DEFAULT_LOG_BYTES = 1024 * 1024
_PYTHON_NAME = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_ENVIRONMENT_RECORD = "openfoundry-environment.json"
_INHERITED_LAYERS = "openfoundry-inherited-layers.pth"
_TOOL_ENVIRONMENT = {"HOME", "LANG", "PATH", "TZ"}
_TOOL_PASSTHROUGH = {
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_CREDENTIAL_URL = re.compile(r"://[^/@\s]+@")
_INVENTORY_SCRIPT = """
import hashlib, importlib.metadata, json, platform, sys
items = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    if not name:
        continue
    record = distribution.read_text("RECORD")
    items.append(
        {
            "name": name.lower().replace("_", "-"),
            "version": distribution.version,
            "recordDigest": (
                "sha256:" + hashlib.sha256(record.encode()).hexdigest()
                if record is not None
                else None
            ),
        }
    )
print(
    json.dumps(
        {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "cacheTag": sys.implementation.cache_tag,
            "distributions": sorted(
                items,
                key=lambda item: (
                    str(item["name"]),
                    str(item["version"]),
                    str(item["recordDigest"]),
                ),
            ),
        }
    )
)
"""
_SITE_SCRIPT = """
import json, sys
print(
    json.dumps(
        [
            entry
            for entry in sys.path
            if entry.endswith(("site-packages", "dist-packages"))
        ]
    )
)
"""


def _tool_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _TOOL_ENVIRONMENT or key in _TOOL_PASSTHROUGH or key.startswith("PIP_")
    }


def _redact(text: str) -> str:
    return _CREDENTIAL_URL.sub("://***@", text)


def _completed_status(
    directory: Path, record: dict[str, Any], value: dict[str, Any]
) -> ExecutionStatus:
    code = int(value["exitCode"])
    reason = str(value["reason"])
    if reason.startswith("signal:"):
        return ExecutionStatus("canceled", reason, code)
    if code == 0 and (
        not record.get("requiresResult", True) or (directory / "result.json").exists()
    ):
        return ExecutionStatus("succeeded", exit_code=code)
    return ExecutionStatus("failed", reason, code)


_LIMITS = {
    "cpuSeconds": resource.RLIMIT_CPU,
    "addressSpaceBytes": resource.RLIMIT_AS,
    "processes": resource.RLIMIT_NPROC,
    "fileSizeBytes": resource.RLIMIT_FSIZE,
}

# First-class compute/budget fields carried in Binding.resources for
# accounting and scheduling. Local enforces only POSIX rlimits; it
# provides no accelerators and performs no preemption itself.
_COMPUTE_ACCOUNTING = frozenset({"accelerators", "memoryBytes", "costLimitUSD", "preemptible"})


class LocalExecutor(Executor):
    def __init__(
        self,
        *,
        limits: dict[str, Any] | None = None,
        environment_root: Path | None = None,
        dependency_wheelhouse: Path | None = None,
        dependency_index: bool = True,
    ) -> None:
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._dirs: dict[str, Path] = {}
        self.limits = limits or {}
        self.environment_root = environment_root
        self.dependency_wheelhouse = dependency_wheelhouse
        self.dependency_index = dependency_index

    @property
    def capabilities(self) -> frozenset[str]:
        caps = {
            "environment:dependency-lock-realization",
            "environment:executable-drift-detection",
            "environment:python-distribution-inventory",
            "bounded-logs",
            "process-group",
            "recovery:attach",
            "rlimit",
            "compute:cpu",
            "accounting:wall-cpu-cost",
            *MODULE_PROTOCOL_CAPABILITIES,
            *DEPLOYMENT_PROTOCOL_CAPABILITIES,
        }
        if self._network_namespace_available():
            caps.update({"network-namespace", "isolation:network-deny"})
        return frozenset(caps)

    @staticmethod
    def _find_executable(name: str) -> Path | None:
        value = shutil.which(name)
        if value is None:
            return None
        try:
            return Path(value).resolve(strict=True)
        except OSError:
            return None

    @staticmethod
    def _locate_executable(name: str, cwd: Path) -> tuple[Path, Path] | None:
        if "/" in name:
            candidate = Path(cwd) / name
        else:
            found = shutil.which(name)
            if found is None:
                return None
            candidate = Path(found)
        candidate = Path(os.path.abspath(candidate))
        try:
            invocation = candidate.parent.resolve(strict=True) / candidate.name
            resolved = invocation.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_file():
            return None
        return invocation, resolved

    @staticmethod
    def _inprocess_python_inventory() -> dict[str, Any]:
        distributions = []
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata["Name"]
            if not name:
                continue
            record = distribution.read_text("RECORD")
            distributions.append(
                {
                    "name": name.lower().replace("_", "-"),
                    "version": distribution.version,
                    "recordDigest": (
                        "sha256:" + hashlib.sha256(record.encode()).hexdigest()
                        if record is not None
                        else None
                    ),
                }
            )
        return {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "cacheTag": sys.implementation.cache_tag,
            "distributions": sorted(
                distributions,
                key=lambda item: (
                    str(item["name"]),
                    str(item["version"]),
                    str(item["recordDigest"]),
                ),
            ),
        }

    @staticmethod
    def _interpreter_json(python: Path, script: str, purpose: str) -> Any:
        completed = subprocess.run(
            [str(python), "-I", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env=_tool_environment(),
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"module interpreter {purpose} failed: {completed.stderr[-500:]}")
        return json.loads(completed.stdout)

    def _python_runtime(self, invocation: Path, resolved: Path) -> dict[str, Any] | None:
        controller = Path(sys.executable)
        try:
            same_controller = invocation.parent == controller.parent.resolve(
                strict=True
            ) and resolved == controller.resolve(strict=True)
        except OSError:
            same_controller = False
        if same_controller:
            return self._inprocess_python_inventory()
        if _PYTHON_NAME.fullmatch(invocation.name):
            value = self._interpreter_json(invocation, _INVENTORY_SCRIPT, "inventory")
            return value if isinstance(value, dict) else None
        return None

    def _realize_dependency_lock(
        self, invocation: Path, resolved: Path, dependency: DependencyLock
    ) -> tuple[Path, Path, dict[str, Any]]:
        if self.environment_root is None:
            raise CapabilityError(
                "local executor has no environment cache root for dependency lock realization",
                details={"dependencyDigest": dependency.digest},
            )
        if not _PYTHON_NAME.fullmatch(invocation.name):
            raise CapabilityError(
                "dependency lock realization requires a Python interpreter entry point",
                details={"dependencyDigest": dependency.digest, "executable": invocation.name},
            )
        interpreter_digest = "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
        layers = self._interpreter_json(invocation, _SITE_SCRIPT, "site inspection")
        if not isinstance(layers, list) or not all(isinstance(item, str) for item in layers):
            raise CapabilityError("module interpreter site inspection returned no layers")
        base_digest = sha256_digest(
            {
                "interpreter": str(invocation),
                "layers": layers,
                "runtime": self._python_runtime(invocation, resolved),
            }
        )
        options = {
            "index": self.dependency_index,
            "wheelhouse": (
                str(self.dependency_wheelhouse) if self.dependency_wheelhouse is not None else None
            ),
        }
        key = sha256_digest(
            {
                "format": _ENVIRONMENT_RECORD,
                "lockDigest": dependency.digest,
                "interpreter": interpreter_digest,
                "baseEnvironment": base_digest,
                "options": options,
            }
        ).removeprefix("sha256:")
        root = self.environment_root
        root.mkdir(parents=True, exist_ok=True)
        final = root / key
        record_path = final / _ENVIRONMENT_RECORD
        with (root / f"{key}.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if not record_path.is_file():
                self._build_environment(invocation, resolved, dependency, final, options, layers)
            record = json.loads(record_path.read_text())
        if record.get("lockDigest") != dependency.digest:
            raise IntegrityError("realized environment does not match the dependency lock")
        located = self._locate_executable(str(final / "bin" / "python3"), final)
        if located is None:
            raise IntegrityError("realized environment has no interpreter")
        realized_invocation, realized_resolved = located
        realization = {
            "strategy": "venv",
            "layering": "interpreter-site-packages",
            "lockDigest": dependency.digest,
            "interpreterDigest": interpreter_digest,
            "options": options,
            "environment": str(final),
            "createdAt": record.get("createdAt"),
        }
        return realized_invocation, realized_resolved, realization

    def _build_environment(
        self,
        invocation: Path,
        resolved: Path,
        dependency: DependencyLock,
        final: Path,
        options: dict[str, Any],
        layers: list[str],
    ) -> None:
        if final.exists():
            shutil.rmtree(final)
        staging = final.parent / f".{final.name}.staging-{uuid.uuid4().hex}"
        try:
            self._run_tool(
                [str(invocation), "-m", "venv", str(staging)],
                purpose="environment creation",
                log=None,
            )
            python = staging / "bin" / "python3"
            if not python.exists():
                python = staging / "bin" / "python"
            lock_file = staging / "requirements.lock"
            lock_file.write_bytes(dependency.contents)
            command = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-warn-script-location",
            ]
            if not self.dependency_index:
                command.append("--no-index")
            if self.dependency_wheelhouse is not None:
                command.extend(["--find-links", str(self.dependency_wheelhouse)])
            command.extend(["-r", str(lock_file)])
            self._run_tool(command, purpose="dependency installation", log=staging / "pip.log")
            site_directories = self._interpreter_json(python, _SITE_SCRIPT, "site inspection")
            if not isinstance(site_directories, list) or not site_directories:
                raise CapabilityError("realized environment has no site directory")
            realized_site = [
                entry for entry in site_directories if entry.startswith(str(staging) + os.sep)
            ]
            if not realized_site:
                raise CapabilityError("realized environment site directory is not local")
            (Path(realized_site[0]) / _INHERITED_LAYERS).write_text(
                "".join(
                    f"import site; site.addsitedir({entry!r})\n"
                    for entry in layers
                    if not entry.startswith(str(staging))
                )
            )
            (staging / _ENVIRONMENT_RECORD).write_text(
                json.dumps(
                    {
                        "format": "openfoundry.local-environment/v1",
                        "lockDigest": dependency.digest,
                        "interpreter": {
                            "path": str(resolved),
                            "digest": (
                                "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
                            ),
                        },
                        "layering": "interpreter-site-packages",
                        "inheritedLayers": layers,
                        "options": options,
                        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
            os.replace(staging, final)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _run_tool(command: list[str], *, purpose: str, log: Path | None) -> None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=_tool_environment(),
            timeout=1800,
        )
        output = _redact(completed.stdout + completed.stderr)
        if log is not None:
            log.write_text(output)
        if completed.returncode != 0:
            raise CapabilityError(
                f"dependency lock {purpose} failed",
                details={"exitCode": completed.returncode, "output": output[-2000:]},
            )

    @classmethod
    def _network_namespace_available(cls, unshare: Path | None = None) -> bool:
        unshare = unshare or cls._find_executable("unshare")
        if unshare is None or os.name != "posix":
            return False
        result = subprocess.run(
            [str(unshare), "--user", "--map-root-user", "--net", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def preflight(self) -> list[str]:
        issues = [] if os.name == "posix" else ["POSIX resource limits unavailable"]
        if unknown := sorted(
            set(self.limits) - set(_LIMITS) - {"timeoutSeconds"} - set(_COMPUTE_ACCOUNTING)
        ):
            issues.append(f"unsupported local resource limits: {', '.join(unknown)}")
        accelerators = self.limits.get("accelerators")
        if isinstance(accelerators, dict) and float(accelerators.get("count", 0) or 0) > 0:
            issues.append("local executor provides no accelerators")
        return issues

    def prepare_environment(
        self,
        *,
        argv: list[str],
        cwd: Path,
        dependency: DependencyLock,
        deny_network: bool = False,
    ) -> dict[str, Any]:
        executable = argv[0]
        located = self._locate_executable(executable, cwd)
        if located is None:
            raise RuntimeError(f"module executable is unavailable: {executable}")
        invocation, resolved = located
        if resolved.read_bytes().startswith(b"#!"):
            raise RuntimeError("script entry points must declare their interpreter explicitly")
        realization: dict[str, Any] | None = None
        if dependency.contents:
            invocation, resolved, realization = self._realize_dependency_lock(
                invocation, resolved, dependency
            )
        executable_bytes = resolved.read_bytes()
        executables = [
            {
                "role": "module",
                "path": str(invocation),
                "target": str(resolved),
                "digest": "sha256:" + hashlib.sha256(executable_bytes).hexdigest(),
            }
        ]
        runtime: dict[str, Any] = {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": self._python_runtime(invocation, resolved),
        }
        wrapper: list[str] = []
        if deny_network:
            unshare = self._find_executable("unshare")
            if unshare is None or not self._network_namespace_available(unshare):
                raise RuntimeError("network denial unavailable")
            wrapper = [str(unshare), "--user", "--map-root-user", "--net", "--"]
            executables.insert(
                0,
                {
                    "role": "network-isolation",
                    "path": str(unshare),
                    "digest": "sha256:" + hashlib.sha256(unshare.read_bytes()).hexdigest(),
                },
            )
        descriptor: dict[str, Any] = {
            "requestedCommand": list(argv),
            "command": [str(invocation), *argv[1:]],
            "wrapper": wrapper,
            "dependencyDigest": dependency.digest,
            "executables": executables,
            "runtime": runtime,
            "realization": realization,
            "environmentPolicy": {
                "inherited": ["HOME", "LANG", "PATH", "TZ"],
                "additional": [],
            },
        }
        descriptor["digest"] = sha256_digest(
            {
                "requestedCommand": descriptor["requestedCommand"],
                "dependencyDigest": dependency.digest,
                "executables": [
                    {"role": item["role"], "digest": item["digest"]} for item in executables
                ],
                "runtime": runtime,
                "realization": (
                    None
                    if realization is None
                    else {
                        "strategy": realization["strategy"],
                        "layering": realization["layering"],
                        "lockDigest": realization["lockDigest"],
                        "interpreterDigest": realization["interpreterDigest"],
                        "index": realization["options"]["index"],
                        "wheelhouse": realization["options"]["wheelhouse"] is not None,
                    }
                ),
                "environmentPolicy": descriptor["environmentPolicy"],
            }
        )
        return descriptor

    def plan(
        self,
        *,
        argv: list[str],
        run_dir: Path,
        cwd: Path,
        env: dict[str, str] | None = None,
        resources: dict[str, Any] | None = None,
        timeout: float | None = None,
        deny_network: bool = False,
        requires_result: bool = True,
        environment: dict[str, Any] | None = None,
        **_: Any,
    ) -> ExecutionPlan:
        command = tuple(argv)
        if deny_network:
            wrapper = tuple((environment or {}).get("wrapper", ()))
            if not wrapper:
                raise RuntimeError("network denial unavailable")
            command = (*wrapper, *command)
        merged = {**self.limits, **(resources or {})}
        return ExecutionPlan(
            command,
            run_dir,
            cwd,
            env or {},
            dict(merged),
            timeout or self.limits.get("timeoutSeconds"),
            deny_network,
            {
                "requiresResult": requires_result,
                "environmentDigest": (environment or {}).get("digest"),
                "executables": (environment or {}).get("executables", []),
                "argvDigest": sha256_digest(list(command)),
                "logByteLimit": _DEFAULT_LOG_BYTES,
            },
        )

    def submit(self, plan: ExecutionPlan) -> str:
        execution_id = str(uuid.uuid4())
        plan.run_dir.mkdir(parents=True, exist_ok=True)
        execution_path = plan.run_dir / "execution.json"
        record = {
            "id": execution_id,
            "state": "launching",
            "started": time.time(),
            "timeout": plan.timeout,
            "requiresResult": bool(plan.metadata.get("requiresResult", True)),
            "environmentDigest": plan.metadata.get("environmentDigest"),
            "argvDigest": plan.metadata["argvDigest"],
        }
        self._write_execution(execution_path, record)
        request, result = plan.run_dir / "request.json", plan.run_dir / "result.json"
        if not request.exists():
            request.write_text("{}")
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TZ"}}
        env.update(plan.env)
        env.update(
            {
                "OPENFOUNDRY_REQUEST_FILE": str(request),
                "OPENFOUNDRY_RESULT_FILE": str(result),
                "OPENFOUNDRY_RUN_ID": execution_id,
            }
        )
        limits = plan.resources
        completion = plan.run_dir / "completion.json"
        worker = [
            sys.executable,
            "-m",
            "openfoundry.executors.local_worker",
            "--completion",
            str(completion),
            *(["--timeout", str(plan.timeout)] if plan.timeout else []),
            *(
                [
                    item
                    for executable in plan.metadata.get("executables", [])
                    for item in (
                        "--attested-executable",
                        str(executable["path"]),
                        str(executable["digest"]),
                    )
                ]
            ),
            *(
                ["--environment-digest", str(plan.metadata["environmentDigest"])]
                if plan.metadata.get("environmentDigest")
                else []
            ),
            "--argv-digest",
            str(plan.metadata["argvDigest"]),
            "--stdout-log",
            str(plan.run_dir / "stdout.log"),
            "--stderr-log",
            str(plan.run_dir / "stderr.log"),
            "--max-log-bytes",
            str(plan.metadata["logByteLimit"]),
            "--",
            *plan.argv,
        ]

        def setup() -> None:
            os.setsid()
            for key, kind in _LIMITS.items():
                if key in limits:
                    resource.setrlimit(kind, (int(limits[key]), int(limits[key])))

        stdout = (plan.run_dir / "worker.log").open("ab")
        try:
            process = subprocess.Popen(
                worker, cwd=plan.cwd, env=env, stdout=stdout, stderr=stdout, preexec_fn=setup
            )
        finally:
            stdout.close()
        self._write_execution(
            execution_path,
            {
                **record,
                "state": "submitted",
                "pid": process.pid,
                "identity": self._identity(process.pid),
                "started": time.time(),
            },
        )
        self._processes[execution_id] = process
        self._dirs[execution_id] = plan.run_dir
        return execution_id

    @staticmethod
    def _write_execution(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _identity(pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[21]
        except (OSError, IndexError):
            return None

    def _record(self, execution_id: str) -> tuple[Path, dict[str, Any]]:
        directory = self._dirs.get(execution_id)
        if directory is None:
            raise KeyError(execution_id)
        return directory, json.loads((directory / "execution.json").read_text())

    def status(self, execution_id: str) -> ExecutionStatus:
        directory, record = self._record(execution_id)
        completion = directory / "completion.json"
        if completion.exists():
            return _completed_status(directory, record, json.loads(completion.read_text()))
        process = self._processes.get(execution_id)
        process_code = process.poll() if process else None
        alive = self._identity(record["pid"]) == record["identity"]
        if alive and process_code is None:
            timeout = record.get("timeout")
            if process is not None and timeout and time.time() - record["started"] > timeout:
                self.cancel(execution_id)
                return ExecutionStatus("failed", "timeout")
            return ExecutionStatus("running")
        return ExecutionStatus(
            "failed",
            "signal" if process_code is not None and process_code < 0 else "nonzero-exit",
            process_code,
        )

    def cancel(self, execution_id: str) -> None:
        directory, record = self._record(execution_id)
        if self._identity(record["pid"]) != record["identity"]:
            return
        completion = directory / "completion.json"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if completion.exists():
                return
            if self._identity(record["pid"]) != record["identity"]:
                break
            time.sleep(0.05)
        if self._identity(record["pid"]) == record["identity"]:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(record["pid"], signal.SIGKILL)
        if not completion.exists():
            temporary = completion.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "exitCode": -signal.SIGTERM,
                        "reason": "signal:SIGTERM",
                        "finished": time.time(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            os.replace(temporary, completion)

    def logs(self, execution_id: str) -> tuple[Path, Path]:
        directory, _ = self._record(execution_id)
        return directory / "stdout.log", directory / "stderr.log"

    def attach(self, execution_id: str, run_dir: Path) -> None:
        record = json.loads((run_dir / "execution.json").read_text())
        completed = (run_dir / "completion.json").is_file()
        submitted_identity = (
            record.get("state") == "submitted"
            and isinstance(record.get("pid"), int)
            and isinstance(record.get("identity"), str)
        )
        if str(record["id"]) != execution_id or not (submitted_identity or completed):
            raise RuntimeError("execution identity does not match the run directory")
        self._dirs[execution_id] = run_dir

    def recover(self, run_dir: Path) -> str | None:
        path = run_dir / "execution.json"
        if not path.exists():
            return None
        record = json.loads((run_dir / "execution.json").read_text())
        if record.get("state") != "submitted" and not (run_dir / "completion.json").is_file():
            raise IntegrityError("local execution launch outcome is indeterminate")
        execution_id = str(record["id"])
        self.attach(execution_id, run_dir)
        return execution_id

    def reconcile(self, run_dir: Path) -> str:
        execution_id = self.recover(run_dir)
        if execution_id is None:
            raise KeyError(run_dir)
        return execution_id
