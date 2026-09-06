from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from openfoundry.errors import CapabilityError, ConfigurationError, ValidationError
from openfoundry.executors.base import Executor
from openfoundry.executors.local import LocalExecutor

ENTRY_POINT_GROUP = "openfoundry.executors"
EXECUTOR_API_VERSION = "openfoundry.executor/v1"


@dataclass(frozen=True)
class ExecutorContext:
    project_root: Path
    state_root: Path
    actor: str
    config: Mapping[str, Any]
    declaration: Mapping[str, Any]


ExecutorFactory = Callable[[ExecutorContext], Executor]


@dataclass(frozen=True)
class ExecutorProvider:
    name: str
    api_version: str
    factory: ExecutorFactory = field(repr=False, compare=False)
    description: str = ""
    capabilities: frozenset[str] = frozenset()
    config_contract: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedExecutor:
    provider: ExecutorProvider
    executor: Executor
    source: str
    config: dict[str, Any]


class ExecutorRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, tuple[ExecutorProvider, str]] = {}

    def register(self, provider: ExecutorProvider, *, source: str = "runtime") -> None:
        name = provider.name.strip()
        if not name or name != provider.name:
            raise ConfigurationError("executor provider name must be non-empty and normalized")
        if provider.api_version != EXECUTOR_API_VERSION:
            raise ConfigurationError(
                f"executor provider {name!r} uses unsupported API version",
                details={
                    "providerApiVersion": provider.api_version,
                    "supportedApiVersions": [EXECUTOR_API_VERSION],
                },
            )
        if name in self._providers:
            raise ConfigurationError(
                f"duplicate executor provider: {name}",
                details={
                    "existingSource": self._providers[name][1],
                    "duplicateSource": source,
                },
            )
        try:
            Draft202012Validator.check_schema(dict(provider.config_contract))
        except SchemaError as exc:
            raise ConfigurationError(
                f"executor provider {name!r} has an invalid config contract"
            ) from exc
        self._providers[name] = (provider, source)

    def discover(self) -> None:
        selected = metadata.entry_points().select(group=ENTRY_POINT_GROUP)
        for entry_point in sorted(selected, key=lambda item: (item.name, item.value)):
            try:
                loaded = entry_point.load()
            except Exception as exc:
                raise ConfigurationError(
                    f"executor entry point {entry_point.name!r} could not be loaded"
                ) from exc
            if not isinstance(loaded, ExecutorProvider):
                raise ConfigurationError(
                    f"executor entry point {entry_point.name!r} did not export ExecutorProvider"
                )
            if loaded.name != entry_point.name:
                raise ConfigurationError(
                    f"executor entry point name {entry_point.name!r} does not match provider "
                    f"name {loaded.name!r}"
                )
            distribution = getattr(entry_point, "dist", None)
            distribution_name = getattr(distribution, "name", "unknown")
            self.register(
                loaded,
                source=f"entry-point:{distribution_name}:{entry_point.value}",
            )

    def catalog(self) -> dict[str, Any]:
        providers = []
        for name, (provider, source) in sorted(self._providers.items()):
            providers.append(
                {
                    "name": name,
                    "apiVersion": provider.api_version,
                    "source": source,
                    "description": provider.description,
                    "capabilities": sorted(provider.capabilities),
                    "configContract": dict(provider.config_contract),
                }
            )
        return {
            "apiVersion": EXECUTOR_API_VERSION,
            "entryPointGroup": ENTRY_POINT_GROUP,
            "providers": providers,
        }

    def resolve(
        self,
        name: str,
        *,
        project_root: Path,
        state_root: Path,
        actor: str,
        declaration: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
    ) -> ResolvedExecutor:
        try:
            provider, source = self._providers[name]
        except KeyError as exc:
            raise CapabilityError(
                f"unknown executor provider: {name}",
                details={"requested": name, "available": sorted(self._providers)},
                remediation=[
                    {
                        "action": "executor.list",
                        "command": "openfoundry executor list",
                        "description": "Choose an installed provider or install a trusted plugin.",
                    }
                ],
            ) from exc
        options = dict(config or {})
        reserved = {
            "argv",
            "run_dir",
            "cwd",
            "resources",
            "timeout",
            "deny_network",
            "requires_result",
            "environment",
        }
        conflicts = sorted(reserved & options.keys())
        if conflicts:
            raise ValidationError(
                "executor config contains controller-owned plan fields",
                details={"fields": conflicts},
            )
        errors = sorted(
            Draft202012Validator(dict(provider.config_contract)).iter_errors(options),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        if errors:
            raise ValidationError(
                f"executor config failed the {name!r} provider contract",
                details={
                    "errors": [
                        {
                            "path": "$"
                            + "".join(
                                f"[{item}]" if isinstance(item, int) else f".{item}"
                                for item in error.absolute_path
                            ),
                            "constraint": str(error.validator),
                            "message": "value does not satisfy the provider contract",
                        }
                        for error in errors
                    ]
                },
            )
        context = ExecutorContext(
            project_root=project_root,
            state_root=state_root,
            actor=actor,
            config=deepcopy(options),
            declaration=deepcopy(declaration),
        )
        executor = provider.factory(context)
        if not isinstance(executor, Executor):
            raise ConfigurationError(f"executor provider {name!r} returned an invalid adapter")
        return ResolvedExecutor(provider, executor, source, options)

    @staticmethod
    def preflight(
        resolved: ResolvedExecutor, *, required_capabilities: frozenset[str] = frozenset()
    ) -> dict[str, Any]:
        actual = resolved.executor.capabilities
        missing = sorted(required_capabilities - actual)
        issues = list(resolved.executor.preflight())
        return {
            "provider": resolved.provider.name,
            "source": resolved.source,
            "ready": not missing and not issues,
            "capabilities": sorted(actual),
            "requiredCapabilities": sorted(required_capabilities),
            "missingCapabilities": missing,
            "issues": issues,
        }


def _local_provider(context: ExecutorContext) -> Executor:
    spec = context.declaration.get("spec", {})
    if not isinstance(spec, dict):
        raise ValidationError("local binding spec must be an object")
    binding_spec = spec if context.declaration.get("kind") == "Binding" else {}
    resources = binding_spec.get("resources", {})
    if not isinstance(resources, dict):
        raise ValidationError("local binding resources must be an object")
    wheelhouse = context.config.get("dependencyWheelhouse")
    wheelhouse_path: Path | None = None
    if wheelhouse is not None:
        if not isinstance(wheelhouse, str) or not wheelhouse:
            raise ValidationError("local dependencyWheelhouse must be a non-empty string")
        wheelhouse_path = Path(wheelhouse)
        if not wheelhouse_path.is_absolute():
            wheelhouse_path = context.project_root / wheelhouse_path
    index = context.config.get("dependencyIndex", True)
    if not isinstance(index, bool):
        raise ValidationError("local dependencyIndex must be a boolean")
    return LocalExecutor(
        limits=resources,
        environment_root=context.state_root / "environments",
        dependency_wheelhouse=wheelhouse_path,
        dependency_index=index,
    )


def default_executor_registry(*, discover: bool = True) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "local",
            EXECUTOR_API_VERSION,
            _local_provider,
            "Run modules as supervised local POSIX process groups.",
            LocalExecutor().capabilities,
            {
                "type": "object",
                "properties": {
                    "dependencyWheelhouse": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Directory of wheels used with pip --find-links when a module "
                            "declares a non-empty dependency lock."
                        ),
                    },
                    "dependencyIndex": {
                        "type": "boolean",
                        "description": (
                            "Whether dependency realization may use pip's configured package "
                            "index. False installs only from the wheelhouse."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        source="builtin",
    )
    if discover:
        registry.discover()
    return registry
