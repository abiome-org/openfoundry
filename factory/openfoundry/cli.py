from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import typer
import uvicorn
import yaml

from openfoundry import __version__
from openfoundry.actions import action_definition, capability_catalog
from openfoundry.agent import initial_context
from openfoundry.api import create_app
from openfoundry.backups import restore_backup
from openfoundry.candidate_review import review, summarize_review, write_review
from openfoundry.canonical import load_document
from openfoundry.config import ProjectPaths, discover_project
from openfoundry.config import bootstrap as bootstrap_project
from openfoundry.errors import OpenFoundryError, ValidationError
from openfoundry.experiment_definition import ExperimentDefinition, initialize
from openfoundry.experiments import launch_worker
from openfoundry.factory import Factory
from openfoundry.modules import scaffold_module
from openfoundry.schema_registry import default_registry
from openfoundry.tracking import track

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
module_app = typer.Typer(no_args_is_help=True)
data_app = typer.Typer(no_args_is_help=True)
store_app = typer.Typer(no_args_is_help=True)
sync_app = typer.Typer(no_args_is_help=True)
resource_app = typer.Typer(no_args_is_help=True)
schema_app = typer.Typer(no_args_is_help=True)
runs_app = typer.Typer(no_args_is_help=True)
lineage_app = typer.Typer(no_args_is_help=True)
secret_app = typer.Typer(no_args_is_help=True)
api_app = typer.Typer(no_args_is_help=True)
release_app = typer.Typer(no_args_is_help=True)
experiment_app = typer.Typer(no_args_is_help=True)
deployment_app = typer.Typer(no_args_is_help=True)
operation_app = typer.Typer(no_args_is_help=True)
token_app = typer.Typer(no_args_is_help=True)
admin_app = typer.Typer(no_args_is_help=True)
agent_app = typer.Typer(no_args_is_help=True)
executor_app = typer.Typer(no_args_is_help=True)
event_app = typer.Typer(no_args_is_help=True)

app.add_typer(module_app, name="module")
app.add_typer(data_app, name="data")
app.add_typer(store_app, name="store")
app.add_typer(sync_app, name="sync")
app.add_typer(resource_app, name="resource")
app.add_typer(schema_app, name="schema")
app.add_typer(runs_app, name="runs")
app.add_typer(lineage_app, name="lineage")
app.add_typer(api_app, name="api")
app.add_typer(release_app, name="release")
app.add_typer(experiment_app, name="experiment")
app.add_typer(deployment_app, name="deployment")
app.add_typer(operation_app, name="operation")
app.add_typer(admin_app, name="admin")
admin_app.add_typer(token_app, name="token")
admin_app.add_typer(secret_app, name="secret")
app.add_typer(agent_app, name="agent")
app.add_typer(executor_app, name="executor")
app.add_typer(event_app, name="event")


def _action_command(group: typer.Typer, action: str) -> Callable[[Callable[..., Any]], Any]:
    definition = action_definition(action)
    return group.command(definition.cli_path[-1], help=definition.description)


class State:
    project: Path | None = None
    output: str = "table"
    actor: str | None = None


_invocation_state: ContextVar[State] = ContextVar("openfoundry_cli_state")


def _state() -> State:
    return _invocation_state.get()


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    project: Path | None = typer.Option(
        None, "--project", "-p", help="OpenFoundry project directory"
    ),
    output: str = typer.Option("table", "--output", "-o", help="table, json, or yaml"),
    actor: str | None = typer.Option(
        None, "--actor", help="Configured policy identity; defaults to the local project owner"
    ),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    del version
    if output not in {"table", "json", "yaml"}:
        raise typer.BadParameter("output must be table, json, or yaml")
    state = State()
    state.project, state.output, state.actor = project, output, actor
    token = _invocation_state.set(state)
    ctx.call_on_close(lambda: _invocation_state.reset(token))


def _paths() -> ProjectPaths:
    return discover_project(_state().project)


@contextmanager
def _factory() -> Iterator[Factory]:
    factory = Factory(_paths(), actor=_state().actor)
    try:
        yield factory
    finally:
        factory.close()


def _run(function: Callable[[Factory], Any]) -> None:
    def call() -> Any:
        with _factory() as factory:
            return function(factory)

    _handle(call)


def _row(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    if isinstance(metadata, dict) and "kind" in item:
        return {
            "kind": item["kind"],
            "name": metadata.get("name"),
            "revision": str(metadata.get("revision", ""))[:19],
            "createdAt": metadata.get("createdAt"),
        }
    return {key: value for key, value in item.items() if not isinstance(value, dict)}


def _table(rows: list[dict[str, Any]]) -> str:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    cells = [[str(row.get(column, "")) for column in columns] for row in rows]
    widths = [
        max(len(column), *(len(line[index]) for line in cells))
        for index, column in enumerate(columns)
    ]
    lines = [columns, *cells]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in lines
    )


def _emit(value: Any) -> None:
    tabular = isinstance(value, list) and value and all(isinstance(item, dict) for item in value)
    if _state().output == "json":
        typer.echo(json.dumps(value, sort_keys=True, indent=2, default=str))
    elif tabular and _state().output == "table":
        typer.echo(_table([_row(item) for item in value]))
    elif isinstance(value, dict | list):
        typer.echo(yaml.safe_dump(value, sort_keys=True), nl=False)
    else:
        typer.echo(value)


def _load_value(path: Path) -> Any:
    return load_document(path.read_bytes())


def _handle(function: Any) -> None:
    try:
        _emit(function())
    except OpenFoundryError as exc:
        if _state().output in {"json", "yaml"}:
            _emit(exc.as_dict())
        else:
            typer.secho(f"{exc.code}: {exc.message}", fg="red", err=True)
            if exc.details:
                typer.echo(yaml.safe_dump(exc.details, sort_keys=True), nl=False, err=True)
            for item in exc.remediation:
                typer.echo(f"next: {item.get('command', item['description'])}", err=True)
        raise typer.Exit(code=1) from exc


@_action_command(app, "project.bootstrap")
def bootstrap(
    profile: str = typer.Option("local", help="Bootstrap profile"),
    plan: bool = typer.Option(False, "--plan", "--dry-run", help="Show changes only"),
) -> None:
    _handle(lambda: bootstrap_project(_paths(), profile=profile, plan=plan))


@_action_command(app, "project.doctor")
def doctor() -> None:
    _run(lambda factory: factory.doctor())


@_action_command(executor_app, "executor.list")
def executor_list() -> None:
    _run(lambda factory: factory.executor_catalog())


@_action_command(executor_app, "executor.preflight")
def executor_preflight(
    binding: Path,
    workload: Path | None = typer.Option(None, "--workload"),
) -> None:
    _run(lambda factory: factory.executor_preflight(binding, workload_path=workload))


@_action_command(agent_app, "agent.capabilities")
def agent_capabilities(action: str | None = typer.Argument(None)) -> None:
    _handle(lambda: capability_catalog(action))


@_action_command(agent_app, "agent.context")
def agent_context(
    focus: str | None = typer.Option(None, "--focus", help="Filter bounded detail by term"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    since: str | None = typer.Option(None, "--since", help="Increment from an event cursor"),
    max_bytes: int = typer.Option(65_536, "--max-bytes", min=16_384, max=1_048_576),
) -> None:
    def run() -> dict[str, Any]:
        paths = _paths()
        if not paths.database.exists():
            return initial_context(
                paths, focus=focus, limit=limit, since=since, max_bytes=max_bytes
            )
        with Factory(paths, actor=_state().actor) as factory:
            return factory.agent.context(focus=focus, limit=limit, since=since, max_bytes=max_bytes)

    _handle(run)


@_action_command(schema_app, "schema.list")
def schema_list() -> None:
    _emit({"apiVersion": "openfoundry.dev/v1alpha1", "kinds": default_registry.kinds})


@_action_command(schema_app, "schema.show")
def schema_show(kind: str) -> None:
    _handle(lambda: default_registry.schema_for(kind))


@_action_command(schema_app, "schema.validate")
def schema_validate(path: Path) -> None:
    _handle(lambda: default_registry.load(path))


@_action_command(resource_app, "resource.apply")
def resource_apply(path: Path) -> None:
    _run(lambda factory: factory.apply_resource_file(path))


@_action_command(resource_app, "resource.list")
def resource_list(kind: str | None = typer.Option(None)) -> None:
    _run(lambda factory: factory.list_resources(kind=kind))


def _module_paths(path: Path | None) -> list[Path]:
    if path is not None:
        return [path]
    paths = sorted(_paths().root.glob("modules/**/module.yaml"))
    if not paths:
        raise typer.BadParameter("no modules/**/module.yaml manifests found")
    return paths


@_action_command(module_app, "module.init")
def module_init(directory: Path, name: str | None = typer.Option(None, "--name")) -> None:
    _run(lambda factory: factory.validate_module(scaffold_module(directory, name)))


@_action_command(module_app, "module.validate")
def module_validate(path: Path | None = typer.Argument(None)) -> None:
    _run(lambda factory: [factory.validate_module(item) for item in _module_paths(path)])


@_action_command(module_app, "module.test")
def module_test(
    path: Path | None = typer.Argument(None),
    binding: Path | None = typer.Option(
        None,
        "--binding",
        help="Binding whose executor and dependency options run the fixtures (default: local).",
    ),
) -> None:
    _run(
        lambda factory: [
            factory.test_module(item, binding_path=binding) for item in _module_paths(path)
        ]
    )


@_action_command(data_app, "data.add")
def data_add(
    source: str,
    name: str = typer.Option(..., "--name"),
    mode: str = typer.Option("copy", "--mode"),
    sample_schema: str = typer.Option("application/octet-stream", "--sample-schema"),
    cursor: str | None = typer.Option(None, "--cursor"),
    rights: Path | None = typer.Option(None, "--rights", help="YAML/JSON rights declaration"),
) -> None:
    def run() -> dict[str, Any]:
        rights_value: dict[str, Any] | None = None
        if rights is not None:
            loaded = _load_value(rights)
            if not isinstance(loaded, dict):
                raise typer.BadParameter("--rights must contain a YAML/JSON object")
            rights_value = loaded
        with _factory() as factory:
            return factory.add_data(
                source,
                name=name,
                mode=mode,
                rights=rights_value,
                sample_schema=sample_schema,
                cursor_policy={"cursor": cursor} if cursor else None,
            )

    _handle(run)


@_action_command(data_app, "data.list")
def data_list() -> None:
    _run(lambda factory: factory.list_resources(kind="DatasetSnapshot"))


@_action_command(data_app, "data.verify")
def data_verify(name: str) -> None:
    _run(lambda factory: {"name": name, "valid": factory.verify_data(name)})


@_action_command(data_app, "data.revoke")
def data_revoke(
    name: str,
    reason: str = typer.Option(..., "--reason", help="Non-sensitive revocation reason"),
) -> None:
    _run(lambda factory: factory.revoke_data(name, reason=reason))


@_action_command(store_app, "store.add")
def store_add(
    name: str,
    driver: str = typer.Option(..., "--driver"),
    endpoint: str = typer.Option(..., "--endpoint"),
    secret_ref: str | None = typer.Option(None, "--secret-ref"),
    plan: bool = typer.Option(False, "--plan", "--dry-run"),
) -> None:
    _run(
        lambda factory: factory.add_store(
            name,
            driver=driver,
            endpoint=endpoint,
            secret_ref=secret_ref,
            plan=plan,
        )
    )


@_action_command(store_app, "store.list")
def store_list() -> None:
    _run(lambda factory: factory.list_resources(kind="ArtifactStore"))


def _sync(
    asset: str,
    source: str,
    destination: str,
    direction: str,
    concurrency: int,
    plan: bool,
) -> dict[str, Any]:
    with _factory() as factory:
        return factory.sync(
            asset,
            source=source,
            destination=destination,
            direction=direction,
            concurrency=concurrency,
            plan=plan,
        )


@_action_command(sync_app, "sync.execute")
def sync_push(
    asset: str,
    destination: str = typer.Option(..., "--to"),
    source: str = typer.Option("local", "--from"),
    concurrency: int = typer.Option(4, min=1, max=256),
    plan: bool = typer.Option(False, "--plan", "--dry-run"),
) -> None:
    _handle(lambda: _sync(asset, source, destination, "push", concurrency, plan))


@_action_command(sync_app, "sync.pull")
def sync_pull(
    asset: str,
    source: str = typer.Option(..., "--from"),
    destination: str = typer.Option("local", "--to"),
    concurrency: int = typer.Option(4, min=1, max=256),
    plan: bool = typer.Option(False, "--plan", "--dry-run"),
) -> None:
    _handle(lambda: _sync(asset, destination, source, "pull", concurrency, plan))


@_action_command(app, "workload.run")
def run_workload(
    workload: Path,
    binding: Path = typer.Option(Path("bindings/local.yaml"), "--binding"),
    detach: bool = typer.Option(False, "--detach"),
) -> None:
    def run(factory: Factory) -> dict[str, Any]:
        if not detach:
            return factory.run(workload, binding)
        operation = factory.create_run_operation(workload, binding)
        launch_worker(factory, operation["id"])
        return operation

    _run(run)


@_action_command(runs_app, "run.list")
def runs_list() -> None:
    _run(lambda factory: factory.list_runs())


@_action_command(runs_app, "run.status")
def runs_status(run_id: str) -> None:
    _run(lambda factory: factory.run_status(run_id.removeprefix("run/")))


@_action_command(app, "evaluation.create")
def evaluate(subject: str) -> None:
    _run(lambda factory: factory.evaluate(subject))


@_action_command(release_app, "release.create")
def release_create(
    run_id: str,
    name: str = typer.Option(..., "--name"),
    intended_use: str = typer.Option(..., "--intended-use"),
    limitation: list[str] | None = typer.Option(None, "--limitation"),
    promote: bool = typer.Option(False, "--promote"),
    alias: str = typer.Option("candidate", "--alias"),
    vulnerability_report: Path | None = typer.Option(None, "--vulnerability-report"),
    evaluation: str | None = typer.Option(None, "--evaluation"),
) -> None:
    _run(
        lambda factory: factory.create_release(
            run_id,
            name=name,
            intended_use=intended_use,
            limitations=limitation,
            promote=promote,
            alias=alias,
            vulnerability_report=vulnerability_report,
            evaluation_ref=evaluation,
        )
    )


@_action_command(release_app, "release.promote")
def release_promote(
    name: str,
    alias: str = typer.Option("candidate", "--alias"),
    expected_version: int | None = typer.Option(None, "--expected-version", min=1),
) -> None:
    _run(
        lambda factory: factory.promote_release(
            name, alias=alias, expected_version=expected_version
        )
    )


@_action_command(release_app, "release.evidence")
def release_evidence(run_id: str) -> None:
    _run(lambda factory: factory.release_evidence(run_id))


@_action_command(release_app, "release.list")
def release_list() -> None:
    _run(lambda factory: factory.list_releases())


@_action_command(release_app, "release.show")
def release_show(name: str) -> None:
    _run(lambda factory: factory.show_release(name.removeprefix("release/")))


@_action_command(experiment_app, "experiment.create")
def experiment_create(
    name: str,
    baseline: str = typer.Option(..., "--baseline"),
    candidate: str = typer.Option(..., "--candidate"),
    metric: str = typer.Option(..., "--metric"),
    direction: str = typer.Option("maximize", "--direction"),
) -> None:
    _run(
        lambda factory: factory.create_experiment(
            name=name,
            baseline_ref=baseline,
            candidate_ref=candidate,
            metric=metric,
            direction=direction,
        )
    )


@_action_command(experiment_app, "experiment.init")
def experiment_init(
    path: Path = typer.Argument(Path("experiment.yaml")),
    name: str = typer.Option(...),
    objective: str = typer.Option(...),
    source: str = typer.Option("src"),
) -> None:
    _handle(
        lambda: initialize(
            (_state().project or Path.cwd()) / path,
            name=name,
            objective=objective,
            source=source,
            actor=_state().actor,
        )
    )


@_action_command(experiment_app, "experiment.schema")
def experiment_schema() -> None:
    _handle(ExperimentDefinition.model_json_schema)


@_action_command(experiment_app, "experiment.run")
def experiment_run(
    definition: Path,
    candidate: str = typer.Option(...),
    detach: bool = typer.Option(False),
) -> None:
    _run(lambda factory: factory.experiments.run(definition, candidate, detach=detach))


@_action_command(experiment_app, "experiment.list")
def experiment_list(name: str | None = typer.Option(None)) -> None:
    _run(lambda factory: factory.experiments.list(name))


@_action_command(experiment_app, "experiment.status")
def experiment_status(run_id: str) -> None:
    _run(lambda factory: factory.experiments.status(run_id))


@_action_command(experiment_app, "experiment.review")
def experiment_review(
    run_id: str,
    baseline: str | None = typer.Option(None),
    html: Path | None = typer.Option(None),
    details: bool = typer.Option(
        False, help="Include source diffs, examples, and runtime evidence"
    ),
) -> None:
    def render(factory: Factory) -> dict[str, Any]:
        result = review(factory.experiments, run_id, baseline, details=True)
        if html is not None:
            write_review(result, html)
        return result if details else summarize_review(result)

    _run(render)


@_action_command(experiment_app, "experiment.reproduce")
def experiment_reproduce(run_id: str, detach: bool = typer.Option(False)) -> None:
    _run(lambda factory: factory.experiments.reproduce(run_id, detach=detach))


@_action_command(experiment_app, "experiment.export")
def experiment_export(run_id: str, destination: Path = typer.Option(..., "--to")) -> None:
    _run(lambda factory: factory.experiments.export(run_id, destination))


@_action_command(experiment_app, "experiment.track")
def experiment_track(run_id: str, uri: str = typer.Option(...)) -> None:
    _run(lambda factory: track(factory.experiments, run_id, uri))


@_action_command(operation_app, "operation.cancel")
def operation_cancel(
    operation_id: str, reason: str = typer.Option("Requested by the operator")
) -> None:
    _run(lambda factory: factory.run_control.request(operation_id, reason))


@_action_command(app, "deployment.apply")
def deploy(path: Path) -> None:
    _run(lambda factory: factory.deploy(path))


@_action_command(deployment_app, "deployment.list")
def deployment_list() -> None:
    _run(lambda factory: factory.list_deployments())


@_action_command(deployment_app, "deployment.status")
def deployment_status(name: str) -> None:
    _run(lambda factory: factory.deployment_status(name))


@_action_command(deployment_app, "deployment.cancel")
def deployment_cancel(name: str) -> None:
    _run(lambda factory: factory.cancel_deployment(name))


@_action_command(deployment_app, "deployment.rollback")
def deployment_rollback(
    name: str, expected_version: int = typer.Option(..., "--expected-version", min=1)
) -> None:
    _run(lambda factory: factory.rollback_deployment(name, expected_version=expected_version))


@_action_command(operation_app, "operation.list")
def operation_list(state_filter: str | None = typer.Option(None, "--state")) -> None:
    _run(lambda factory: factory.operations.list(state=state_filter))


@_action_command(operation_app, "operation.get")
def operation_get(operation_id: str) -> None:
    _run(lambda factory: factory.operations.get(operation_id))


@_action_command(operation_app, "operation.reconcile")
def operation_reconcile(operation_id: str) -> None:
    _run(lambda factory: factory.execute_run_operation(operation_id))


@_action_command(token_app, "token.create")
def token_create(
    actor: str = typer.Option(..., "--actor"),
    scope: list[str] | None = typer.Option(None, "--scope"),
    expires_at: str | None = typer.Option(None, "--expires-at"),
) -> None:
    _run(
        lambda factory: factory.create_api_token(
            actor=actor, scopes=set(scope or ["read"]), expires_at=expires_at
        )
    )


@_action_command(token_app, "token.list")
def token_list() -> None:
    _run(lambda factory: factory.api_tokens.list())


@_action_command(token_app, "token.revoke")
def token_revoke(token_id: str) -> None:
    _run(lambda factory: factory.revoke_api_token(token_id))


@_action_command(lineage_app, "lineage.query")
def lineage_show(
    subject: str,
    direction: str = typer.Option("upstream"),
    max_depth: int = typer.Option(100, min=1, max=1000),
) -> None:
    _run(lambda factory: factory.lineage_query(subject, direction=direction, max_depth=max_depth))


@_action_command(event_app, "event.list")
def event_list(
    run_id: str | None = typer.Option(None, "--run-id"),
    resource_uid: str | None = typer.Option(None, "--resource-uid"),
    event_type: str | None = typer.Option(None, "--event-type"),
    limit: int = typer.Option(100, min=1, max=1000),
    offset: int = typer.Option(0, min=0),
) -> None:
    _run(
        lambda factory: [
            event.as_dict()
            for event in factory.events.query(
                run_id=run_id, resource_uid=resource_uid, type=event_type
            )
        ][offset : offset + limit]
    )


@_action_command(secret_app, "secret.set")
def secret_set(
    name: str,
    purpose: str = typer.Option(...),
    value: str | None = typer.Option(None, help="Prefer the hidden prompt or --value-stdin."),
    value_stdin: bool = typer.Option(False, "--value-stdin", help="Read the secret from stdin."),
    expected_version: int | None = typer.Option(None, "--expected-version", min=1),
) -> None:
    def run(factory: Factory) -> dict[str, Any]:
        if value is not None and value_stdin:
            raise ValidationError("use either --value or --value-stdin")
        secret = sys.stdin.read().removesuffix("\n") if value_stdin else value
        if secret is None:
            secret = typer.prompt("Secret value", hide_input=True)
        version = factory.secrets.put(name, secret, purpose, expected_version=expected_version)
        return {"name": name, "purpose": purpose, "version": version}

    _run(run)


@_action_command(secret_app, "secret.list")
def secret_list() -> None:
    _run(lambda factory: factory.secrets.list())


@_action_command(admin_app, "backup.create")
def backup(destination: Path) -> None:
    _run(lambda factory: factory.backup(destination))


@_action_command(admin_app, "backup.restore")
def restore(
    source: Path,
    expected_key_id: str | None = typer.Option(None, "--expected-key-id"),
) -> None:
    _handle(lambda: restore_backup(_paths(), source, expected_key_id=expected_key_id))


@_action_command(api_app, "api.serve")
def api_serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8080, min=1, max=65535),
) -> None:
    uvicorn.run(create_app(_paths()), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
