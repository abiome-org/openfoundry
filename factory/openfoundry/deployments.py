from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.canonical import canonical_json
from openfoundry.errors import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    ValidationError,
)
from openfoundry.executors import (
    DEPLOYMENT_PROTOCOL_CAPABILITIES,
    MODULE_EXECUTION_CAPABILITIES,
    Executor,
    ResolvedExecutor,
)
from openfoundry.ids import uuid7
from openfoundry.lineage import LineageEdge
from openfoundry.modules import (
    extract_module_package,
    load_manifest,
    validate_contract,
)
from openfoundry.schema_registry import default_registry

if TYPE_CHECKING:
    from openfoundry.factory import Factory


class DeploymentService:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    def list_deployments(self) -> list[dict[str, Any]]:
        return [
            {
                "name": resource["metadata"]["name"],
                "release": resource["spec"]["releaseRef"],
                "state": self.factory._status_state(resource["metadata"]["uid"]),
                "revision": resource["metadata"]["revision"],
            }
            for resource in self.factory.resources.latest(kind="DeploymentSpec")
        ]

    def deploy(self, deployment_path: str | Path) -> dict[str, Any]:
        self.factory._authorize("deployment.apply")
        raw = self.factory._load_resource(deployment_path, kind="DeploymentSpec")
        release = self.factory.publishing.resolve_release(str(raw["spec"]["releaseRef"]))
        raw["spec"]["releaseRef"] = self.factory._resource_uri(release)
        decision = self.factory.publishing.promotion_decision(release)
        if decision.outcome != "allow":
            raise IntegrityError("deployment release does not meet the current promotion policy")
        extension = raw["spec"].get("extensions", {})
        form = extension.get("form", "service")
        command = extension.get("command")
        if form not in {"edge", "service"} and not command:
            raise ValidationError(f"{form} deployments require extensions.command argv")
        if command:
            resolved = self._deployment_executor(raw)
            required = DEPLOYMENT_PROTOCOL_CAPABILITIES
            if bool(extension.get("denyNetwork", False)):
                required |= frozenset({"isolation:network-deny"})
            self.factory._require_executor(resolved, required)
        name = str(raw["metadata"]["name"])
        desired_revision = default_registry.normalize(raw, actor=self.factory.actor)["metadata"][
            "revision"
        ]
        expected_version: int | None = None
        previous_status: dict[str, Any] = {}
        try:
            current = self.deployment_status(name)
            previous_status = current["status"]
            expected_version = int(current["statusVersion"])
            if previous_status.get(
                "deploymentRevision"
            ) == desired_revision and previous_status.get("state") in {"running", "packaged"}:
                return {
                    "deployment": current["deployment"],
                    "state": current["status"]["state"],
                    "executionId": current["status"].get("executionId"),
                }
            if previous_status.get("state") == "running":
                canceled = self.cancel_deployment(name)
                previous_status = canceled["status"]
                expected_version = int(canceled["statusVersion"])
        except NotFoundError:
            pass
        previous_revision = previous_status.get("deploymentRevision")
        if previous_revision == desired_revision:
            previous_revision = previous_status.get("previousDeploymentRevision")
        resource = self.factory.apply_resource(raw)
        state, execution_id, run_dir, executor_name, endpoint = self._launch_deployment(resource)
        self.factory.resources.set_status(
            resource["metadata"]["uid"],
            {
                "state": state,
                "releaseRevision": release["metadata"]["revision"],
                "deploymentRevision": resource["metadata"]["revision"],
                "previousDeploymentRevision": previous_revision,
                "executionId": execution_id,
                "executor": executor_name,
                "runDirectory": str(run_dir) if run_dir else None,
                "endpoint": endpoint,
            },
            expected_version=expected_version,
        )
        self.factory.lineage.add(
            LineageEdge(
                self.factory._resource_uri(release),
                self.factory._resource_uri(resource),
                "wasDerivedFrom",
                "entity",
                "entity",
            )
        )
        self.factory.events.append(
            type="DeploymentChanged",
            source=f"openfoundry://{self.factory.namespace}",
            subject=f"Deployment/{resource['metadata']['name']}",
            resource_uid=resource["metadata"]["uid"],
            revision=resource["metadata"]["revision"],
            actor=self.factory.actor,
            data={"state": state, "release": release["metadata"]["revision"]},
            dataschema="https://schemas.openfoundry.dev/events/deployment-changed/v1",
        )
        return {"deployment": resource, "state": state, "executionId": execution_id}

    def _attach_deployment(
        self, resource: dict[str, Any], status: dict[str, Any], execution_id: str
    ) -> Executor:
        resolved = self._deployment_executor(resource)
        if str(status.get("executor", resolved.provider.name)) != resolved.provider.name:
            raise IntegrityError("deployment executor does not match its immutable revision")
        default_dir = self.factory.paths.runs / "deployments" / resource["metadata"]["uid"]
        resolved.executor.attach(execution_id, Path(str(status.get("runDirectory") or default_dir)))
        return resolved.executor

    def _deployment_executor(self, resource: dict[str, Any]) -> ResolvedExecutor:
        extension = resource["spec"].get("extensions", {})
        name = str(extension.get("executor", "local"))
        config = extension.get("executorConfig", {})
        if not isinstance(config, dict):
            raise ValidationError("deployment extensions.executorConfig must be an object")
        return self.factory._resolve_executor(name, resource, config)

    def _prepare_serving(self, resource: dict[str, Any], run_dir: Path) -> tuple[list[str], str]:
        extension = resource["spec"].get("extensions", {})
        release = self.factory._resource_by_uri("Release", resource["spec"]["releaseRef"])
        manifest = release["spec"].get("extensions", {}).get("manifest", {})
        package_ref = manifest.get("modelPackage", {}).get("ref")
        adapter_digest = manifest.get("runtime", {}).get("sources", {}).get("inference")
        if not isinstance(package_ref, str) or not isinstance(adapter_digest, str):
            raise ValidationError(
                "a service deployment without a command requires a release built from a model "
                "package with an admitted inference adapter"
            )
        model_package = self.factory._resource_by_uri("ModelPackage", package_ref)
        adapter = model_package["spec"]["adapters"]["inferenceReference"]
        signatures = model_package["spec"]["signatures"]
        run_resource = self.factory._resource_by_uri("Run", manifest["provenance"]["runRef"])
        run_result = self.factory._resource_by_uri("RunResult", manifest["provenance"]["resultRef"])
        try:
            state = run_result["spec"]["outputs"][adapter["stateOutput"]]
            admission = run_resource["spec"]["extensions"]["inferenceAdapter"]
            admitted_environment = str(admission["environment"]["digest"])
        except (KeyError, TypeError) as exc:
            raise IntegrityError("release run has no admitted inference adapter state") from exc
        state = self.factory._resolve_model_state(state, run_dir / "state")
        validate_contract(signatures["state"], state, "model package state")
        source_manifest = self.factory.local_store.read_manifest(adapter_digest)
        builder = ArtifactBuilder(self.factory.local_store)
        if not builder.verify(source_manifest):
            raise IntegrityError("admitted serving adapter source failed verification")
        run_dir.mkdir(parents=True, exist_ok=True)
        archive = run_dir / "adapter-archive"
        builder.restore(source_manifest, archive)
        code_root = extract_module_package(archive / "payload", run_dir / "adapter")
        adapter_manifest, code_root = load_manifest(
            code_root / Path(adapter["module"]).name, code_root
        )
        resolved = self._deployment_executor(resource)
        self.factory._require_executor(resolved, MODULE_EXECUTION_CAPABILITIES)
        environment = self.factory._prepare_module_environment(
            resolved.executor, adapter_manifest, code_root
        )
        if environment["digest"] != admitted_environment:
            raise IntegrityError("serving adapter environment differs from run admission")
        host = str(extension.get("host", "127.0.0.1"))
        port = extension.get("port", 8090)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValidationError("deployment extensions.port must be an integer port number")
        timeout = extension.get("requestTimeoutSeconds", 60)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValidationError("deployment extensions.requestTimeoutSeconds must be positive")
        serving = {
            "deployment": resource["metadata"]["name"],
            "release": release["metadata"]["revision"],
            "modelPackageRef": package_ref,
            "operation": adapter["operation"],
            "method": "predict",
            "config": adapter.get("config", {}),
            "state": state,
            "signatures": {"input": signatures["input"], "output": signatures["output"]},
            "command": [str(item) for item in environment["command"]],
            "wrapper": [str(item) for item in environment.get("wrapper", [])],
            "cwd": str(code_root),
            "host": host,
            "port": port,
            "timeoutSeconds": float(timeout),
        }
        config_path = run_dir / "serving.json"
        config_path.write_bytes(canonical_json(serving))
        return (
            [sys.executable, "-m", "openfoundry.serve_worker", "--config", str(config_path)],
            f"http://{host}:{port}",
        )

    def _launch_deployment(
        self, resource: dict[str, Any], *, instance: str | None = None
    ) -> tuple[str, str | None, Path | None, str | None, str | None]:
        release = self.factory._resource_by_uri("Release", resource["spec"]["releaseRef"])
        with self.factory.publishing.selection(release):
            return self._execute_deployment(resource, instance=instance)

    def _execute_deployment(
        self, resource: dict[str, Any], *, instance: str | None = None
    ) -> tuple[str, str | None, Path | None, str | None, str | None]:
        extension = resource["spec"].get("extensions", {})
        command = extension.get("command")
        run_dir = (
            self.factory.paths.runs
            / "deployments"
            / resource["metadata"]["uid"]
            / resource["metadata"]["revision"]
        )
        run_dir /= instance or str(uuid7())
        endpoint: str | None = None
        if not command and extension.get("form", "service") == "service":
            command, endpoint = self._prepare_serving(resource, run_dir)
        if not command:
            return "packaged", None, None, None, None
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValidationError("deployment command must be an argv string array")
        resolved = self._deployment_executor(resource)
        required = DEPLOYMENT_PROTOCOL_CAPABILITIES
        if bool(extension.get("denyNetwork", False)):
            required |= frozenset({"isolation:network-deny"})
        self.factory._require_executor(resolved, required)
        plan = resolved.executor.plan(
            argv=command,
            run_dir=run_dir,
            cwd=self.factory.paths.root,
            resources=extension.get("resources", {}),
            timeout=float(extension.get("timeoutSeconds", 0)) or None,
            deny_network=bool(extension.get("denyNetwork", False)),
            requires_result=False,
            **resolved.config,
        )
        return "running", resolved.executor.submit(plan), run_dir, resolved.provider.name, endpoint

    def deployment_status(self, name: str) -> dict[str, Any]:
        resource = self.factory.find_resource("DeploymentSpec", name)
        uid = resource["metadata"]["uid"]
        status, version = self.factory.resources.get_status(uid)
        desired_revision = status.get("deploymentRevision")
        if desired_revision and desired_revision != resource["metadata"]["revision"]:
            resource = self.factory.resources.get(uid, str(desired_revision))
        execution_id = status.get("executionId")
        if status.get("state") == "running" and execution_id:
            executor = self._attach_deployment(resource, status, str(execution_id))
            observed = executor.status(str(execution_id))
            if observed.state != "running":
                updated = {
                    **status,
                    "state": observed.state,
                    "reason": observed.reason,
                    "exitCode": observed.exit_code,
                }
                try:
                    version = self.factory.resources.set_status(
                        uid, updated, expected_version=version
                    )
                    status = updated
                    self._deployment_event(resource, status)
                except ConflictError:
                    status, version = self.factory.resources.get_status(uid)
        return {"deployment": resource, "status": status, "statusVersion": version}

    def cancel_deployment(self, name: str) -> dict[str, Any]:
        self.factory._authorize("deployment.cancel")
        current = self.deployment_status(name)
        resource = current["deployment"]
        status = current["status"]
        version = int(current["statusVersion"])
        if status.get("state") != "running":
            return current
        execution_id = status.get("executionId")
        if not execution_id:
            raise IntegrityError("running deployment has no execution identity")
        executor = self._attach_deployment(resource, status, str(execution_id))
        executor.cancel(str(execution_id))
        observed = executor.status(str(execution_id))
        updated = {
            **status,
            "state": observed.state,
            "reason": observed.reason,
            "exitCode": observed.exit_code,
        }
        new_version = self.factory.resources.set_status(
            resource["metadata"]["uid"], updated, expected_version=version
        )
        self._deployment_event(resource, updated)
        return {"deployment": resource, "status": updated, "statusVersion": new_version}

    def rollback_deployment(self, name: str, *, expected_version: int) -> dict[str, Any]:
        self.factory._authorize("deployment.rollback")
        current = self.deployment_status(name)
        if int(current["statusVersion"]) != expected_version:
            raise ConflictError("deployment status version mismatch")
        resource = current["deployment"]
        status = current["status"]
        previous = status.get("previousDeploymentRevision")
        if not previous:
            raise ConflictError("deployment has no previous revision to roll back")
        target = self.factory.resources.get(resource["metadata"]["uid"], str(previous))
        release = self.factory._resource_by_uri("Release", target["spec"]["releaseRef"])
        decision = self.factory.publishing.promotion_decision(release)
        if decision.outcome != "allow":
            raise IntegrityError("rollback release does not meet the current promotion policy")
        if current["status"].get("state") == "running":
            current = self.cancel_deployment(name)
            expected_version = int(current["statusVersion"])
        state, execution_id, run_dir, executor_name, endpoint = self._launch_deployment(
            target, instance=f"rollback-{expected_version + 1}"
        )
        updated = {
            "state": state,
            "releaseRevision": release["metadata"]["revision"],
            "deploymentRevision": target["metadata"]["revision"],
            "previousDeploymentRevision": status["deploymentRevision"],
            "executionId": execution_id,
            "executor": executor_name,
            "runDirectory": str(run_dir) if run_dir else None,
            "endpoint": endpoint,
            "reason": "rollback",
        }
        new_version = self.factory.resources.set_status(
            target["metadata"]["uid"], updated, expected_version=expected_version
        )
        self._deployment_event(target, updated)
        return {"deployment": target, "status": updated, "statusVersion": new_version}

    def _deployment_event(self, resource: dict[str, Any], status: dict[str, Any]) -> None:
        self.factory.events.append(
            type="DeploymentChanged",
            source=f"openfoundry://{self.factory.namespace}",
            subject=f"Deployment/{resource['metadata']['name']}",
            resource_uid=resource["metadata"]["uid"],
            revision=resource["metadata"]["revision"],
            actor=self.factory.actor,
            data={"state": status["state"], "release": status["releaseRevision"]},
            dataschema="https://schemas.openfoundry.dev/events/deployment-changed/v1",
        )
