from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openfoundry import __version__
from openfoundry.actions import action_definition
from openfoundry.candidate_review import review
from openfoundry.config import ProjectPaths
from openfoundry.errors import AuthorizationError, OpenFoundryError
from openfoundry.executors import ExecutorRegistry
from openfoundry.experiment_definition import ExperimentDefinition
from openfoundry.factory import Factory
from openfoundry.schema_registry import default_registry
from openfoundry.tracking import track


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptExperimentRequest(RequestModel):
    definition: str
    candidate: str
    detach: bool = True


class SearchExperimentRequest(RequestModel):
    definition: str
    detach: bool = True


class ReproduceRequest(RequestModel):
    detach: bool = True


class ExportRequest(RequestModel):
    destination: str


class TrackingRequest(RequestModel):
    uri: str


class CancellationRequest(RequestModel):
    reason: str = Field(default="Requested by the operator", min_length=1, max_length=1024)


class StoreRequest(RequestModel):
    name: str
    driver: str
    endpoint: str
    secret_ref: str | None = None
    plan: bool = False


class DataRequest(RequestModel):
    source: str
    name: str
    mode: str = "copy"
    rights: dict[str, Any] = Field(default_factory=dict)
    sample_schema: str = "application/octet-stream"
    cursor_policy: dict[str, Any] = Field(default_factory=dict)


class DataRevocationRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=1024)


class SyncRequest(RequestModel):
    asset: str
    source: str = "local"
    destination: str
    direction: str = "push"
    concurrency: int = Field(default=4, ge=1, le=256)
    plan: bool = False


class ModuleRequest(RequestModel):
    manifest: str
    binding: str | None = None


class RunRequest(RequestModel):
    workload: str
    binding: str = "bindings/local.yaml"
    detach: bool = False


class ExecutorPreflightRequest(RequestModel):
    binding: str
    workload: str | None = None


class BackupRequest(RequestModel):
    destination: str


class EvaluationRequest(RequestModel):
    subject: str


class ReleaseRequest(RequestModel):
    run_id: str
    name: str
    intended_use: str
    limitations: list[str] = Field(default_factory=list)
    promote: bool = False
    alias: str = "candidate"
    vulnerability_report: str | None = None
    evaluation_ref: str | None = None


class ReleasePromotionRequest(RequestModel):
    alias: str = "candidate"
    expected_version: int | None = Field(default=None, ge=1)


class ExperimentRequest(RequestModel):
    name: str
    baseline_ref: str
    candidate_ref: str
    metric: str
    direction: str = "maximize"


class DeploymentRequest(RequestModel):
    manifest: str


class DeploymentRollbackRequest(RequestModel):
    expected_version: int = Field(ge=1)


class ApiTokenRequest(RequestModel):
    actor: str = Field(min_length=1)
    scopes: set[str]
    expires_at: str | None = None


Authorized = Callable[..., Iterator[Factory]]


def _error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OpenFoundryError)
    async def openfoundry_error(_request: Request, exc: OpenFoundryError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "path": "$"
                + "".join(
                    f"[{item}]" if isinstance(item, int) else f".{item}" for item in error["loc"]
                ),
                "message": str(error["msg"]),
                "type": str(error["type"]),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": f"request failed validation ({len(errors)} error(s))",
                    "retryable": False,
                    "remediation": [
                        {
                            "action": "agent.capabilities",
                            "command": "openfoundry agent capabilities",
                            "description": "Inspect the action contract and endpoint schema.",
                        }
                    ],
                    "details": {"errors": errors},
                }
            },
        )


def _action_route(app: FastAPI, action: str) -> Callable[[Callable[..., Any]], Any]:
    definition = action_definition(action)
    if definition.path is None or definition.method is None:
        raise ValueError(f"action has no HTTP interface: {action}")
    return app.api_route(
        definition.path,
        methods=[definition.method],
        operation_id=action,
        summary=definition.description,
        openapi_extra={"x-openfoundry-action": definition.as_dict()},
    )


def _authorizer(paths: ProjectPaths, factory: Factory) -> Authorized:
    def authorized(request: Request, authorization: str = Header(default="")) -> Iterator[Factory]:
        scheme, _, token = authorization.partition(" ")
        principal = factory.authenticate_principal(token) if scheme.lower() == "bearer" else None
        if principal is None:
            raise AuthorizationError("valid Bearer authentication is required")
        required_scope = action_definition(request.scope["route"].operation_id).scope
        if not principal.allows(required_scope):
            raise AuthorizationError(f"API token lacks {required_scope} scope")
        service = Factory(paths, actor=principal.actor, executors=factory.executors)
        try:
            yield service
        finally:
            service.close()

    return authorized


def _core_routes(
    app: FastAPI, factory: Factory, paths: ProjectPaths, authorized: Authorized
) -> None:
    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"status": "ok", "project": factory.project["metadata"]["name"]}

    @_action_route(app, "project.doctor")
    def doctor(service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.doctor()

    @_action_route(app, "executor.list")
    def executors_catalog(service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.executor_catalog()

    @_action_route(app, "executor.preflight")
    def executor_preflight(
        request: ExecutorPreflightRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.executor_preflight(
            paths.root / request.binding,
            workload_path=paths.root / request.workload if request.workload else None,
        )

    @_action_route(app, "schema.list")
    def schemas(_service: Factory = Depends(authorized)) -> dict[str, Any]:
        return {"apiVersion": "openfoundry.dev/v1alpha1", "kinds": default_registry.kinds}

    @_action_route(app, "schema.show")
    def schema(kind: str, _service: Factory = Depends(authorized)) -> dict[str, Any]:
        return default_registry.schema_for(kind)


def _cached(response: Response, value: dict[str, Any], key: str, if_none_match: str | None) -> Any:
    etag = f'"{value[key]}"'
    if if_none_match in {etag, value[key]}:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return value


def _agent_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "agent.capabilities")
    def agent_capabilities(
        response: Response,
        action: str | None = None,
        if_none_match: str | None = Header(default=None),
        service: Factory = Depends(authorized),
    ) -> Any:
        return _cached(response, service.agent.capabilities(action), "catalogDigest", if_none_match)

    @_action_route(app, "agent.context")
    def agent_context(
        response: Response,
        focus: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        since: str | None = None,
        max_bytes: int = Query(default=65_536, ge=16_384, le=1_048_576),
        if_none_match: str | None = Header(default=None),
        service: Factory = Depends(authorized),
    ) -> Any:
        value = service.agent.context(focus=focus, limit=limit, since=since, max_bytes=max_bytes)
        return _cached(response, value, "viewDigest", if_none_match)


def _data_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "resource.list")
    def resources(
        kind: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        service: Factory = Depends(authorized),
    ) -> list[dict[str, Any]]:
        return service.list_resources(kind=kind)[offset : offset + limit]

    @_action_route(app, "resource.apply")
    def apply_resource(
        resource: dict[str, Any], service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.apply_resource(resource)

    @_action_route(app, "event.list")
    def events(
        run_id: str | None = None,
        resource_uid: str | None = None,
        event_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        service: Factory = Depends(authorized),
    ) -> list[dict[str, Any]]:
        values = [
            event.as_dict()
            for event in service.events.query(
                run_id=run_id, resource_uid=resource_uid, type=event_type
            )
        ]
        return values[offset : offset + limit]

    @_action_route(app, "store.add")
    def add_store(request: StoreRequest, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.add_store(**request.model_dump())

    @_action_route(app, "data.add")
    def add_data(request: DataRequest, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.add_data(**request.model_dump())

    @_action_route(app, "data.verify")
    def verify_data(name: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return {"name": name, "valid": service.verify_data(name)}

    @_action_route(app, "data.revoke")
    def revoke_data(
        name: str,
        request: DataRevocationRequest,
        service: Factory = Depends(authorized),
    ) -> dict[str, Any]:
        return service.revoke_data(name, reason=request.reason)

    @_action_route(app, "sync.execute")
    def sync(request: SyncRequest, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.sync(**request.model_dump())


def _run_routes(
    app: FastAPI, factory: Factory, paths: ProjectPaths, authorized: Authorized
) -> None:
    @_action_route(app, "module.validate")
    def validate_module(
        request: ModuleRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.validate_module(paths.root / request.manifest)

    @_action_route(app, "module.test")
    def test_module(
        request: ModuleRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.test_module(
            paths.root / request.manifest,
            binding_path=paths.root / request.binding if request.binding else None,
        )

    def execute_run_operation(operation_id: str) -> None:
        with Factory(paths, executors=factory.executors) as reader:
            actor = str(reader.operations.get(operation_id)["request"]["actor"])
        with Factory(paths, actor=actor, executors=factory.executors) as service:
            service.execute_run_operation(operation_id)

    @_action_route(app, "workload.run")
    def run(
        request: RunRequest,
        background: BackgroundTasks,
        service: Factory = Depends(authorized),
    ) -> dict[str, Any]:
        if request.detach:
            operation = service.create_run_operation(
                paths.root / request.workload, paths.root / request.binding
            )
            background.add_task(execute_run_operation, operation["id"])
            return operation
        return service.run(paths.root / request.workload, paths.root / request.binding)

    @_action_route(app, "run.status")
    def run_status(run_id: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.run_status(run_id)

    @_action_route(app, "evaluation.create")
    def evaluate(
        request: EvaluationRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.evaluate(request.subject)


def _release_routes(app: FastAPI, paths: ProjectPaths, authorized: Authorized) -> None:
    @_action_route(app, "release.create")
    def release(request: ReleaseRequest, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.create_release(
            request.run_id,
            name=request.name,
            intended_use=request.intended_use,
            limitations=request.limitations,
            promote=request.promote,
            alias=request.alias,
            vulnerability_report=(
                paths.root / request.vulnerability_report if request.vulnerability_report else None
            ),
            evaluation_ref=request.evaluation_ref,
        )

    @_action_route(app, "release.promote")
    def promote_release(
        name: str, request: ReleasePromotionRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.promote_release(name, **request.model_dump())

    @_action_route(app, "experiment.create")
    def experiment(
        request: ExperimentRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.create_experiment(**request.model_dump())

    @_action_route(app, "deployment.apply")
    def deploy(
        request: DeploymentRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.deploy(paths.root / request.manifest)

    @_action_route(app, "deployment.status")
    def deployment_status(name: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.deployment_status(name)

    @_action_route(app, "deployment.cancel")
    def deployment_cancel(name: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.cancel_deployment(name)

    @_action_route(app, "deployment.rollback")
    def deployment_rollback(
        name: str,
        request: DeploymentRollbackRequest,
        service: Factory = Depends(authorized),
    ) -> dict[str, Any]:
        return service.rollback_deployment(name, expected_version=request.expected_version)

    @_action_route(app, "lineage.query")
    def lineage(
        subject: str,
        direction: str = "upstream",
        max_depth: int = Query(default=100, ge=1, le=1000),
        service: Factory = Depends(authorized),
    ) -> list[dict[str, Any]]:
        return service.lineage_query(subject, direction=direction, max_depth=max_depth)


def _read_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "run.list")
    def runs(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.list_runs()

    @_action_route(app, "release.list")
    def releases(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.list_releases()

    @_action_route(app, "release.show")
    def release(name: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.show_release(name)

    @_action_route(app, "release.evidence")
    def release_evidence(run_id: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.release_evidence(run_id)

    @_action_route(app, "deployment.list")
    def deployments(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.list_deployments()

    @_action_route(app, "store.list")
    def stores(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.list_resources(kind="ArtifactStore")

    @_action_route(app, "data.list")
    def datasets(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.list_resources(kind="DatasetSnapshot")


def _admin_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "backup.create")
    def backup(request: BackupRequest, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.backup(request.destination)

    @_action_route(app, "token.create")
    def token_create(
        request: ApiTokenRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.create_api_token(**request.model_dump())

    @_action_route(app, "token.list")
    def token_list(service: Factory = Depends(authorized)) -> list[dict[str, Any]]:
        return service.api_tokens.list()

    @_action_route(app, "token.revoke")
    def token_revoke(token_id: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.revoke_api_token(token_id)

    @_action_route(app, "operation.list")
    def operations(
        state: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        service: Factory = Depends(authorized),
    ) -> list[dict[str, Any]]:
        return service.operations.list(state=state)[offset : offset + limit]

    @_action_route(app, "operation.get")
    def operation(operation_id: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.operations.get(operation_id)

    @_action_route(app, "operation.reconcile")
    def operation_reconcile(
        operation_id: str, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.execute_run_operation(operation_id)


def _experiment_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "experiment.schema")
    def experiment_schema(_service: Factory = Depends(authorized)) -> dict[str, Any]:
        return ExperimentDefinition.model_json_schema()

    @_action_route(app, "experiment.run")
    def experiment_run(
        request: ScriptExperimentRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.experiments.run(request.definition, request.candidate, detach=request.detach)

    @_action_route(app, "experiment.list")
    def experiments(
        name: str | None = None, service: Factory = Depends(authorized)
    ) -> list[dict[str, Any]]:
        return service.experiments.list(name)

    @_action_route(app, "experiment.status")
    def status(run_id: str, service: Factory = Depends(authorized)) -> dict[str, Any]:
        return service.experiments.status(run_id)

    @_action_route(app, "experiment.review")
    def experiment_review(
        run_id: str,
        baseline: str | None = None,
        details: bool = False,
        service: Factory = Depends(authorized),
    ) -> dict[str, Any]:
        return review(service.experiments, run_id, baseline, details=details)

    @_action_route(app, "experiment.reproduce")
    def reproduce(
        run_id: str, request: ReproduceRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.experiments.reproduce(run_id, detach=request.detach)

    @_action_route(app, "experiment.export")
    def export(
        run_id: str, request: ExportRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.experiments.export(run_id, Path(request.destination))

    @_action_route(app, "experiment.track")
    def tracking(
        run_id: str, request: TrackingRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return track(service.experiments, run_id, request.uri)

    @_action_route(app, "operation.cancel")
    def cancel(
        operation_id: str, request: CancellationRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.run_control.request(operation_id, request.reason)


def _search_routes(app: FastAPI, authorized: Authorized) -> None:
    @_action_route(app, "experiment.search")
    def experiment_search(
        request: SearchExperimentRequest, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.experiments.run_search(request.definition, detach=request.detach)

    @_action_route(app, "experiment.leaderboard")
    def experiment_leaderboard(
        definition: str, service: Factory = Depends(authorized)
    ) -> dict[str, Any]:
        return service.experiments.leaderboard(definition)


def create_app(paths: ProjectPaths, *, executors: ExecutorRegistry | None = None) -> FastAPI:
    factory = Factory(paths, executors=executors)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            factory.close()

    app = FastAPI(
        title="OpenFoundry API",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.factory = factory
    _error_handlers(app)
    authorized = _authorizer(paths, factory)
    _core_routes(app, factory, paths, authorized)
    _agent_routes(app, authorized)
    _data_routes(app, authorized)
    _run_routes(app, factory, paths, authorized)
    _read_routes(app, authorized)
    _release_routes(app, paths, authorized)
    _admin_routes(app, authorized)
    _experiment_routes(app, authorized)
    _search_routes(app, authorized)
    return app


def app_from_directory(project: str | Path) -> FastAPI:
    return create_app(ProjectPaths(Path(project).resolve()))
