from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from openfoundry.canonical import sha256_digest
from openfoundry.errors import NotFoundError


@dataclass(frozen=True)
class ActionDefinition:
    action: str
    description: str
    command: str
    method: str | None = "GET"
    path: str | None = None
    scope: Literal["read", "write", "admin"] = "read"
    mutates: bool = False

    @property
    def cli_path(self) -> tuple[str, ...]:
        parts = []
        for word in self.command.split()[1:]:
            if re.fullmatch(r"[a-z][a-z-]*", word) is None:
                break
            parts.append(word)
        return tuple(parts)

    def as_dict(self) -> dict[str, Any]:
        interfaces: dict[str, Any] = {"cli": self.command}
        if self.method is not None and self.path is not None:
            pointer = self.path.replace("~", "~0").replace("/", "~1")
            interfaces["http"] = {
                "method": self.method,
                "path": self.path,
                "schema": f"/openapi.json#/paths/{pointer}/{self.method.lower()}",
            }
        return {
            "action": self.action,
            "description": self.description,
            "interfaces": interfaces,
            "requiredScope": self.scope,
            "mutates": self.mutates,
        }


_ACTIONS = (
    ActionDefinition(
        "experiment.init",
        "Initialize an experiment around existing training and evaluation scripts.",
        (
            "openfoundry experiment init [<path>] --name <name> --objective <text> "
            "[--source <directory>]"
        ),
        method=None,
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "experiment.schema",
        "Read the schema for an experiment definition.",
        "openfoundry experiment schema",
        path="/v1/experiment-schema",
    ),
    ActionDefinition(
        "experiment.run",
        "Capture scripts and data, train a candidate, and evaluate its model.",
        "openfoundry experiment run <definition> --candidate <name> [--detach]",
        method="POST",
        path="/v1/experiment-runs",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "experiment.list",
        "List experiment candidates and their recorded results.",
        "openfoundry experiment list [--name <name>]",
        path="/v1/experiment-runs",
    ),
    ActionDefinition(
        "experiment.status",
        "Inspect candidate progress, results, and cancellation intent.",
        "openfoundry experiment status <run-id>",
        path="/v1/experiment-runs/{run_id}",
    ),
    ActionDefinition(
        "experiment.review",
        "Review quality, regressions, source changes, examples, and compute used.",
        "openfoundry experiment review <run-id> [--baseline <run-id>] [--html <path>] [--details]",
        path="/v1/experiment-runs/{run_id}/review",
    ),
    ActionDefinition(
        "experiment.reproduce",
        "Reproduce a candidate from its captured source and pinned data.",
        "openfoundry experiment reproduce <run-id> [--detach]",
        method="POST",
        path="/v1/experiment-runs/{run_id}/reproduce",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "experiment.export",
        "Export the trained model and the evidence needed to use and reproduce it.",
        "openfoundry experiment export <run-id> --to <directory>",
        method="POST",
        path="/v1/experiment-runs/{run_id}/export",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "experiment.track",
        "Log an experiment result and its review to a chosen MLflow store.",
        "openfoundry experiment track <run-id> --uri <tracking-uri>",
        method="POST",
        path="/v1/experiment-runs/{run_id}/tracking",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "operation.cancel",
        "Request durable cancellation and stop active or abandoned run execution.",
        "openfoundry operation cancel <operation-id> [--reason <text>]",
        method="POST",
        path="/v1/operations/{operation_id}/cancel",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "project.bootstrap",
        "Plan and initialize repository-scoped local factory state.",
        "openfoundry bootstrap [--plan]",
        method=None,
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "agent.context",
        "Read a bounded decision context and incremental event cursor.",
        "openfoundry agent context",
        path="/v1/agent/context",
    ),
    ActionDefinition(
        "agent.capabilities",
        "Discover factory commands, HTTP routes, and required scopes.",
        "openfoundry agent capabilities",
        path="/v1/agent/capabilities",
    ),
    ActionDefinition(
        "project.doctor",
        "Run non-mutating repository and factory readiness checks.",
        "openfoundry doctor",
        path="/v1/doctor",
    ),
    ActionDefinition(
        "executor.list",
        "Discover built-in and trusted plugin executor providers and configuration contracts.",
        "openfoundry executor list",
        path="/v1/executors",
    ),
    ActionDefinition(
        "executor.preflight",
        "Check a binding provider and workload transport contract without allocating a run.",
        "openfoundry executor preflight <binding> [--workload <workload>]",
        method="POST",
        path="/v1/executors/preflight",
    ),
    ActionDefinition(
        "resource.apply",
        "Validate and commit an immutable resource revision.",
        "openfoundry resource apply <manifest>",
        method="POST",
        path="/v1/resources",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "module.validate",
        "Package and validate an exact module source revision and contract.",
        "openfoundry module validate [manifest]",
        method="POST",
        path="/v1/modules/validate",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "module.test",
        "Execute module fixtures through the selected binding and its isolation limits.",
        "openfoundry module test [manifest]",
        method="POST",
        path="/v1/modules/test",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "data.add",
        "Create an immutable rights-declared dataset snapshot.",
        "openfoundry data add <source> --name <name> --rights <manifest>",
        method="POST",
        path="/v1/data",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "store.add",
        "Declare a user-selected artifact holding site by symbolic secret reference.",
        "openfoundry store add <name> --driver <driver> --endpoint <endpoint> [--plan]",
        method="POST",
        path="/v1/stores",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "sync.execute",
        "Plan or transfer only missing verified artifact chunks.",
        "openfoundry sync push <asset> --to <store> [--from <store>] [--plan]",
        method="POST",
        path="/v1/sync",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "workload.run",
        "Admit and execute a model-neutral workload through an explicit binding.",
        "openfoundry run <workload> --binding <binding>",
        method="POST",
        path="/v1/runs",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "run.status",
        "Read observed run state and exact admitted execution digests.",
        "openfoundry runs status <run-id>",
        path="/v1/runs/{run_id}",
    ),
    ActionDefinition(
        "evaluation.create",
        "Materialize immutable evaluation evidence for a completed run.",
        "openfoundry evaluate run/<run-id>",
        method="POST",
        path="/v1/evaluations",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "experiment.create",
        "Compare one numeric metric under identical immutable evaluation revisions.",
        "openfoundry experiment create <name> --baseline <ref> --candidate <ref> --metric <name>",
        method="POST",
        path="/v1/experiments",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "release.create",
        "Save a signed model version with its artifacts, data, recipe, and measured evidence.",
        "openfoundry release create <run-id> --name <name> --intended-use <use>",
        method="POST",
        path="/v1/releases",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "release.promote",
        "Move an alias to a saved release after checking current project requirements.",
        "openfoundry release promote <name> --alias <alias>",
        method="POST",
        path="/v1/releases/{name}/promote",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "deployment.apply",
        "Policy-check and apply an explicit deployment revision.",
        "openfoundry deploy <manifest>",
        method="POST",
        path="/v1/deployments",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "deployment.status",
        "Reconcile and read observed deployment state.",
        "openfoundry deployment status <name>",
        path="/v1/deployments/{name}",
        mutates=True,
    ),
    ActionDefinition(
        "deployment.rollback",
        "Guard and restore the previous immutable deployment revision.",
        "openfoundry deployment rollback <name> --expected-version <version>",
        method="POST",
        path="/v1/deployments/{name}/rollback",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "lineage.query",
        "Trace upstream derivation or downstream impact.",
        "openfoundry lineage show <subject> --direction <upstream|downstream>",
        path="/v1/lineage",
    ),
    ActionDefinition(
        "backup.create",
        "Archive and verify the complete durable local state.",
        "openfoundry admin backup <destination>",
        method="POST",
        path="/v1/backups",
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "schema.list",
        "List installed resource kinds and schema contracts.",
        "openfoundry schema list",
        path="/v1/schemas",
    ),
    ActionDefinition(
        "schema.show",
        "Read the exact JSON Schema for one resource kind.",
        "openfoundry schema show <kind>",
        path="/v1/schemas/{kind}",
    ),
    ActionDefinition(
        "schema.validate",
        "Validate a local desired-state document without committing it.",
        "openfoundry schema validate <manifest>",
        method=None,
    ),
    ActionDefinition(
        "resource.list",
        "Read immutable resource revisions with an optional kind filter.",
        "openfoundry resource list [--kind <kind>]",
        path="/v1/resources",
    ),
    ActionDefinition(
        "event.list",
        "Read signed event records, including governed event payloads.",
        "openfoundry event list [--run-id <run-id>]",
        path="/v1/events",
    ),
    ActionDefinition(
        "data.verify",
        "Verify a dataset snapshot and locally held content by digest.",
        "openfoundry data verify <name>",
        path="/v1/data/{name}/verify",
    ),
    ActionDefinition(
        "data.revoke",
        "Stop future and resumed training from a dataset without rewriting its history.",
        "openfoundry data revoke <name> --reason <reason>",
        method="POST",
        path="/v1/data/{name}/revoke",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "deployment.cancel",
        "Stop a deployment and record its terminal status.",
        "openfoundry deployment cancel <name>",
        method="POST",
        path="/v1/deployments/{name}/cancel",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "token.create",
        "Create an attributable scoped API credential returned exactly once.",
        "openfoundry admin token create --actor <actor> --scope <scope>",
        method="POST",
        path="/v1/tokens",
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "token.list",
        "List API credential metadata without plaintext token values.",
        "openfoundry admin token list",
        path="/v1/tokens",
        scope="admin",
    ),
    ActionDefinition(
        "token.revoke",
        "Irreversibly revoke one API credential by stable token ID.",
        "openfoundry admin token revoke <token-id>",
        method="DELETE",
        path="/v1/tokens/{token_id}",
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "operation.list",
        "Read operation lifecycle metadata with an optional state filter.",
        "openfoundry operation list [--state <state>]",
        path="/v1/operations",
    ),
    ActionDefinition(
        "operation.get",
        "Read one exact operation lifecycle record.",
        "openfoundry operation get <operation-id>",
        path="/v1/operations/{operation_id}",
    ),
    ActionDefinition(
        "operation.reconcile",
        "Execute a pending run or safely reconcile a stale running operation.",
        "openfoundry operation reconcile <operation-id>",
        method="POST",
        path="/v1/operations/{operation_id}/reconcile",
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "secret.set",
        "Encrypt and create or replace a purpose-bound local secret.",
        "openfoundry admin secret set <name> --purpose <purpose> [--expected-version <version>]",
        method=None,
        scope="admin",
        mutates=True,
    ),
    ActionDefinition(
        "secret.list",
        "List secret names, purposes, and versions without plaintext values.",
        "openfoundry admin secret list",
        method=None,
        scope="admin",
    ),
    ActionDefinition(
        "run.list", "List runs and their current status.", "openfoundry runs list", path="/v1/runs"
    ),
    ActionDefinition(
        "release.list",
        "List signed releases and current aliases.",
        "openfoundry release list",
        path="/v1/releases",
    ),
    ActionDefinition(
        "release.show",
        "Read one signed release by name.",
        "openfoundry release show <name>",
        path="/v1/releases/{name}",
    ),
    ActionDefinition(
        "release.evidence",
        "Inspect the evidence required to create a release.",
        "openfoundry release evidence <run-id>",
        path="/v1/runs/{run_id}/release-evidence",
    ),
    ActionDefinition(
        "deployment.list",
        "List deployments and recorded status.",
        "openfoundry deployment list",
        path="/v1/deployments",
    ),
    ActionDefinition(
        "store.list",
        "List artifact store declarations.",
        "openfoundry store list",
        path="/v1/stores",
    ),
    ActionDefinition(
        "data.list", "List dataset snapshots.", "openfoundry data list", path="/v1/data"
    ),
    ActionDefinition(
        "sync.pull",
        "Fetch missing verified chunks from a source store.",
        "openfoundry sync pull <asset> --from <store> [--to <store>] [--plan]",
        method=None,
        scope="write",
        mutates=True,
    ),
    ActionDefinition(
        "module.init",
        "Create a starter module in the project.",
        "openfoundry module init <directory> [--name <name>]",
        mutates=True,
        scope="write",
    ),
    ActionDefinition(
        "backup.restore",
        "Restore a verified backup into an uninitialized project.",
        "openfoundry admin restore <source> [--expected-key-id <id>]",
        mutates=True,
        scope="admin",
    ),
    ActionDefinition(
        "api.serve",
        "Serve the authenticated local HTTP API.",
        "openfoundry api serve [--host <host>] [--port <port>]",
        mutates=True,
        scope="admin",
    ),
)


ACTION_BY_NAME = {item.action: item for item in _ACTIONS}


def action_definition(action: str) -> ActionDefinition:
    try:
        return ACTION_BY_NAME[action]
    except KeyError as exc:
        raise NotFoundError("unknown action", details={"action": action}) from exc


def capability_catalog(action: str | None = None) -> dict[str, Any]:
    selected = (action_definition(action),) if action is not None else _ACTIONS
    body = {
        "apiVersion": "openfoundry.agent/v1alpha1",
        "catalogVersion": 2,
        "actions": [item.as_dict() for item in selected],
    }
    return {**body, "catalogDigest": sha256_digest(body)}
