# Executors

An executor provider is the bridge between a `Binding` and a process,
scheduler, or cloud runner. Moving a workload between providers must not
change the workload or the module protocol. Only `local` is built in; other
providers are installed plugins.

## Discover and preflight

```sh
openfoundry executor list
openfoundry executor preflight bindings/local.yaml --workload workloads/train.yaml
```

The catalog reports each provider's source, capabilities, and configuration
contract. Preflight instantiates the configured provider and reports actual
capabilities, missing workload requirements, and host issues without creating
a run, a resource, or an event. Provider selection is exact: an unknown or
unready provider is an error, never a fallback to local.

## Capabilities

A provider may advertise `openfoundry.module/v1` only when it carries the complete
protocol across its execution boundary:

| Capability | Required behavior |
| --- | --- |
| `protocol:openfoundry.module/v1` | Preserve the request and result exactly |
| `transport:module-source` | Make the exact admitted source package available to the worker |
| `transport:request-result` | Deliver `request.json`; retrieve `result.json` before success |
| `transport:artifacts` | Retrieve declared artifacts into the stage run directory |
| `isolation:network-deny` | Actually deny network egress; every module run requires it |
| `protocol:openfoundry.deployment/v1` | Run deployment commands and serving workers |

The local provider adds `environment:executable-drift-detection` (the worker
re-hashes the module's interpreter immediately before exec and records the
digest) and `environment:dependency-lock-realization` (hash-pinned locks are
installed into cached virtual environments under `.openfoundry/environments/`, keyed
by lock, interpreter, inherited environment, and options, with the interpreter's site directories
layered after the lock). Neither is a byte-sealed runtime closure.

## The local provider

Modules run as supervised POSIX process groups without a shell. The plan
carries the argument vector, the run directory, the captured source as the
working directory, network denial through an unprivileged user namespace, and
the binding's resource limits. A detached worker records completion durably so
status, logs, cancellation, and reattachment after a controller restart do not
depend on the launching process. Set `spec.config.dependencyWheelhouse` to
install lock contents from a local wheel directory and
`spec.config.dependencyIndex: false` to forbid index access.

## Writing a provider

`openfoundry.executor/v1` is the stable plugin boundary. A provider package exports
one `ExecutorProvider` through the `openfoundry.executors` entry-point group:

```python
from openfoundry.executors import (
    EXECUTOR_API_VERSION,
    MODULE_PROTOCOL_CAPABILITIES,
    ExecutorContext,
    ExecutorProvider,
)
from .executor import RemoteExecutor


def create(context: ExecutorContext) -> RemoteExecutor:
    return RemoteExecutor(project_root=context.project_root, image=str(context.config["image"]))


provider = ExecutorProvider(
    name="remote",
    api_version=EXECUTOR_API_VERSION,
    factory=create,
    description="Remote runner with object-store protocol transport.",
    capabilities=MODULE_PROTOCOL_CAPABILITIES | frozenset({"isolation:network-deny"}),
    config_contract={
        "type": "object",
        "required": ["image"],
        "properties": {"image": {"type": "string"}},
        "additionalProperties": False,
    },
)
```

```toml
[project.entry-points."openfoundry.executors"]
remote = "openfoundry_remote.provider:provider"
```

The registry validates the binding's `spec.config` against `config_contract`
before calling the factory, rejects controller-owned plan fields in provider
options, and requires the entry-point name to match the provider name. The
`Executor` methods are `capabilities`, `preflight`, `plan`, `submit`, `status`,
`cancel`, `logs`, `read_logs`, and optionally `attach`, `recover`, and
`prepare_environment`. Plans must be deterministic; `submit` returns a durable
id; `recover` returns `None` only when no allocation happened and raises when
the outcome is ambiguous; status and logs must survive a controller restart.
A remote provider must stage the exact working directory and request, set
`OPENFOUNDRY_REQUEST_FILE`, `OPENFOUNDRY_RESULT_FILE`, and `OPENFOUNDRY_RUN_ID` remotely, retrieve the
result, logs, and declared artifacts before reporting success, and report
failure rather than fabricate a result.

For embedding, construct a registry and inject it; injection replaces the
default registry so the trust boundary stays explicit:

```python
registry = ExecutorRegistry()
registry.register(provider, source="site")
factory = Factory(paths, executors=registry)
```

## Acceptance

Before supporting a binding for real work, test exact selection and
unknown-provider failure, preflight failure before allocation, source and
request staging with digest verification, result and multi-artifact
retrieval, nonzero exit, timeout, cancellation, lost worker, lost controller,
reattachment after restart, bounded logs without credential leakage, network
and resource enforcement, checkpoint recovery, and the same workload unchanged
on local and on the target binding. A provider name or an accepted job proves
none of these.
