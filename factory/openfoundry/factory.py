from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openfoundry.agent import FactoryView
from openfoundry.artifacts import ArtifactBuilder, ArtifactManifest, AtomicCheckpointPublisher
from openfoundry.backups import create_backup
from openfoundry.canonical import (
    canonical_json,
    load_document,
    portable_relative_path,
    sha256_digest,
)
from openfoundry.config import ProjectPaths, load_project, local_actor
from openfoundry.data import DataService, DatasetSnapshot
from openfoundry.database import Database, ResourceRepository
from openfoundry.deployments import DeploymentService
from openfoundry.errors import (
    AuthorizationError,
    CapabilityError,
    ConfigurationError,
    ConflictError,
    IntegrityError,
    NotFoundError,
    OpenFoundryError,
    OperationCanceled,
    ValidationError,
)
from openfoundry.evaluation import EvaluationService
from openfoundry.events import EventStore
from openfoundry.executors import (
    MODULE_EXECUTION_CAPABILITIES,
    MODULE_PROTOCOL_CAPABILITIES,
    Executor,
    ExecutorRegistry,
    ResolvedExecutor,
    default_executor_registry,
)
from openfoundry.experiments import ExperimentService
from openfoundry.ids import uuid7
from openfoundry.lineage import LineageEdge, LineageStore
from openfoundry.modules import (
    ModuleManifest,
    dependency_lock,
    extract_module_package,
    load_manifest,
    package_module,
    validate_contract,
    worktree_state,
)
from openfoundry.operations import OperationStore
from openfoundry.policy import PolicyDecision, ProjectPolicy
from openfoundry.publishing import PublishingService
from openfoundry.run_control import RunControl
from openfoundry.run_support import (
    _CapturedSource,
    _check_metric_names,
    _ensure_execution,
    _execution_plan_digest,
    _operation_lease,
    _pin_checkpoint,
    _RunContext,
    _training_stage,
    _utc_now,
    _validate_package_signatures,
    _write_module_request,
)
from openfoundry.schema_registry import default_registry
from openfoundry.sdk import ProtocolRequest, ProtocolResult
from openfoundry.security import ApiPrincipal, ApiTokenStore, SecretStore, SigningIdentity
from openfoundry.stores.base import ArtifactStore
from openfoundry.stores.filesystem import FilesystemStore
from openfoundry.stores.s3 import S3Store
from openfoundry.sync import SyncEngine
from openfoundry.workloads import (
    AdmittedWorkload,
    DataUse,
    RunState,
    Stage,
    StateStore,
    WorkloadRunner,
    dataset_uses,
    project_workload,
)


class Factory:
    def __init__(
        self,
        paths: ProjectPaths,
        *,
        actor: str | None = None,
        executors: ExecutorRegistry | None = None,
    ) -> None:
        if not paths.database.exists():
            raise ConfigurationError(
                "factory is not bootstrapped; run `openfoundry bootstrap` first",
                remediation=[
                    {
                        "action": "project.bootstrap",
                        "command": "openfoundry bootstrap --plan && openfoundry bootstrap",
                        "description": "Inspect and then apply repository-scoped initialization.",
                    }
                ],
            )
        self.paths = paths
        self.project = load_project(paths)
        self.actor = local_actor(self.project) if actor is None else actor
        self.namespace = str(self.project["metadata"]["namespace"])
        self.db = Database(paths.database)
        self.identity = SigningIdentity(paths.signing_key)
        self.secrets = SecretStore(self.db, paths.secret_key)
        self.api_tokens = ApiTokenStore(self.db)
        local_token = self.secrets.get("local-api-token", "api-authentication").decode()
        local_principal = self.api_tokens.register(
            local_token,
            actor=local_actor(self.project),
            scopes={"*"},
        )
        self.local_token_id = local_principal.token_id
        self.events = EventStore(self.db, self.identity)
        self.resources = ResourceRepository(self.db)
        self.lineage = LineageStore(self.db)
        self.operations = OperationStore(self.db)
        self.local_store = FilesystemStore(paths.store)
        self.executors = executors or default_executor_registry()
        self._policy_cache: tuple[tuple[tuple[str, int, int], ...], ProjectPolicy] | None = None
        self.agent = FactoryView(self)
        self.evaluation = EvaluationService(self)
        self.publishing = PublishingService(self)
        self.deployments = DeploymentService(self)
        self.experiments = ExperimentService(self)
        self.run_control = RunControl(self)

    def close(self) -> None:
        self.db.close()

    @property
    def policy(self) -> ProjectPolicy:
        extensions = self.project["spec"].get("extensions", {})
        directory = self.paths.root / str(extensions.get("policyDirectory", "policies"))
        signature: tuple[tuple[str, int, int], ...] = ()
        if directory.is_dir():
            signature = tuple(
                (item.name, item.stat().st_mtime_ns, item.stat().st_size)
                for item in sorted(directory.iterdir())
                if item.is_file()
            )
        cached = self._policy_cache
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            policy = ProjectPolicy.load(self.paths.root, self.project)
        except OpenFoundryError as exc:
            raise ConfigurationError(
                f"project policy is invalid: {exc.message}",
                details=exc.details,
                remediation=[
                    {
                        "action": "project.doctor",
                        "command": "openfoundry doctor",
                        "description": "Fix the policy documents in the project policy directory.",
                    }
                ],
            ) from exc
        self._policy_cache = (signature, policy)
        return policy

    def _authorize(
        self, action: str, *, purpose: str | None = None, resource: str | None = None
    ) -> PolicyDecision:
        policy = self.policy
        context: dict[str, Any] = {
            "actor": self.actor,
            "action": action,
            "resource": resource or self.namespace,
        }
        if purpose is not None:
            context["purpose"] = purpose
        decision = policy.authorize(context)
        if decision.outcome != "deny":
            return decision
        rules = [item["rule"] for item in decision.explanations]
        self.events.append(
            type="PolicyDecisionRecorded",
            source=f"openfoundry://{self.namespace}",
            subject=action,
            resource_uid=str(uuid7()),
            revision=policy.digest,
            actor=self.actor,
            data={"outcome": "deny", "action": action, "rules": rules},
            dataschema="https://schemas.openfoundry.dev/events/policy-decision/v1",
            policy_digest=policy.digest,
        )
        raise AuthorizationError(
            f"policy denies actor {self.actor!r} action {action!r} on {context['resource']}",
            details={"policyDigest": policy.digest, "rules": rules},
            remediation=[
                {
                    "action": "project.doctor",
                    "command": "openfoundry doctor",
                    "description": (
                        "Ask the project owner to review the denied action and policy decision. "
                        "Continue work that is already authorized."
                    ),
                }
            ],
        )

    def _admission_worktree(self) -> dict[str, Any]:
        state = worktree_state(self.paths.root)
        mode = self.policy.dirty_worktree
        record: dict[str, Any] = {
            "commit": state["commit"],
            "dirty": state["dirty"],
            "patchDigest": state["patchDigest"],
            "untracked": state["untracked"][:64],
            "untrackedCount": len(state["untracked"]),
            "policy": mode,
        }
        if state["dirty"] and mode == "deny":
            raise ValidationError(
                "policy denies workload admission from a dirty worktree; commit the project "
                "configuration and code first",
                details={
                    "commit": state["commit"],
                    "untrackedCount": len(state["untracked"]),
                    "patchDigest": state["patchDigest"],
                },
            )
        if state["dirty"] and mode == "archive" and state["patch"]:
            with tempfile.NamedTemporaryFile(
                dir=self.paths.packages, suffix=".patch", delete=False
            ) as temporary:
                temporary.write(state["patch"])
                patch_path = Path(temporary.name)
            try:
                artifact = ArtifactBuilder(self.local_store).import_path(
                    patch_path,
                    logical_kind="worktree-patch",
                    provenance={"commit": state["commit"]},
                )
            finally:
                patch_path.unlink(missing_ok=True)
            record["patchArtifact"] = artifact.manifest_digest
        return record

    @contextlib.contextmanager
    def _dataset_rights_locks(self, datasets: list[dict[str, Any]]) -> Iterator[None]:
        lock_root = self.paths.state / "operations" / "data-rights"
        lock_root.mkdir(parents=True, exist_ok=True)
        handles: list[Any] = []
        try:
            for uid in sorted({str(item["metadata"]["uid"]) for item in datasets}):
                name = hashlib.sha256(uid.encode()).hexdigest() + ".lock"
                handle = (lock_root / name).open("a+")
                fcntl.flock(handle, fcntl.LOCK_EX)
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def __enter__(self) -> Factory:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def authenticate(self, token: str) -> bool:
        return self.authenticate_principal(token) is not None

    def authenticate_principal(self, token: str) -> ApiPrincipal | None:
        return self.api_tokens.authenticate(token)

    def create_api_token(
        self, *, actor: str, scopes: set[str], expires_at: str | None
    ) -> dict[str, Any]:
        token, principal = self.api_tokens.create(actor=actor, scopes=scopes, expires_at=expires_at)
        return {
            "token": token,
            "tokenId": principal.token_id,
            "actor": principal.actor,
            "scopes": sorted(principal.scopes),
            "expiresAt": principal.expires_at,
        }

    def revoke_api_token(self, token_id: str) -> dict[str, Any]:
        if token_id == self.local_token_id:
            raise ValidationError("the bootstrap operator token cannot be revoked")
        self.api_tokens.revoke(token_id)
        return {"tokenId": token_id, "revoked": True}

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, function: Any, remediation: str) -> None:
            try:
                detail = function()
                checks.append({"name": name, "status": "pass", "detail": detail})
            except Exception as exc:
                checks.append(
                    {
                        "name": name,
                        "status": "fail",
                        "detail": str(exc),
                        "remediation": remediation,
                    }
                )

        check(
            "project-schema",
            lambda: default_registry.validate(self.project)["metadata"]["name"],
            "fix openfoundry.yaml according to `openfoundry schema show Project`",
        )
        check(
            "database-integrity",
            lambda: (
                "ok"
                if self.db.integrity_check()
                else (_ for _ in ()).throw(IntegrityError("database integrity check failed"))
            ),
            "restore .openfoundry/metadata.db from a verified backup",
        )
        check(
            "signing-identity",
            lambda: self._check_identity(),
            "restore the signing identity or bootstrap a new project trust domain",
        )
        check(
            "secret-service",
            lambda: f"token:{len(self.secrets.get('local-api-token', 'api-authentication'))} bytes",
            "verify .openfoundry/identity ownership and permissions",
        )
        check(
            "artifact-store",
            lambda: f"{shutil.disk_usage(self.paths.store).free} bytes free",
            "make .openfoundry/store writable or configure another store",
        )
        check(
            "git",
            lambda: subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.paths.root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "initialize a Git repository and commit desired state",
        )
        check(
            "clock",
            lambda: _utc_now(),
            "configure a trusted UTC time source",
        )
        check(
            "policy",
            lambda: (
                f"{len(self.policy.documents)} document(s) in {self.policy.directory!r} "
                f"{self.policy.digest}"
            ),
            "fix the policy documents in the project policy directory",
        )
        failures = sum(item["status"] == "fail" for item in checks)
        return {
            "ready": failures == 0,
            "project": self.project["metadata"]["name"],
            "profile": "local",
            "checks": checks,
            "failures": failures,
        }

    def _check_identity(self) -> str:
        value = {"probe": str(uuid7())}
        signature = self.identity.sign(value)
        self.identity.verify(value, signature)
        mode = self.paths.signing_key.stat().st_mode & 0o777
        if mode & 0o077:
            raise IntegrityError("signing key permissions are broader than 0600")
        return self.identity.key_id

    def _validate_namespace(self, value: dict[str, Any]) -> None:
        metadata = value.get("metadata", {})
        namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
        if namespace != self.namespace:
            raise ValidationError(
                f"resource namespace {namespace!r} does not match project namespace "
                f"{self.namespace!r}"
            )

    def _stamp_namespace(self, value: dict[str, Any]) -> dict[str, Any]:
        metadata = value.get("metadata")
        if isinstance(metadata, dict):
            metadata.setdefault("namespace", self.namespace)
        return value

    def _load_resource(self, path: str | Path, *, kind: str | None = None) -> dict[str, Any]:
        value = load_document(Path(path).read_bytes())
        if not isinstance(value, dict):
            raise ValidationError(f"{Path(path).name} must contain one resource object")
        self._stamp_namespace(value)
        if kind is not None:
            default_registry.validate_as(value, kind)
        return value

    def apply_resource(self, value: dict[str, Any], *, _system: bool = False) -> dict[str, Any]:
        if not _system:
            self._authorize("resource.apply")
        value = self._stamp_namespace(deepcopy(value))
        metadata = value.get("metadata", {})
        existing = [
            resource
            for resource in self.resources.latest(kind="DatasetSnapshot")
            if value.get("kind") == "DatasetSnapshot"
            and isinstance(metadata, dict)
            and resource["metadata"]["name"] == metadata.get("name")
            and resource["metadata"]["namespace"] == metadata.get("namespace")
        ]
        if len(existing) > 1:
            raise IntegrityError("dataset name is bound to multiple identities")
        if existing:
            with self._dataset_rights_locks(existing):
                return self._apply_resource_unlocked(value, _system=_system)
        return self._apply_resource_unlocked(value, _system=_system)

    def _apply_resource_unlocked(
        self, value: dict[str, Any], *, _system: bool = False
    ) -> dict[str, Any]:
        generated_kinds = {
            "Checkpoint",
            "EvaluationResult",
            "Experiment",
            "Run",
            "RunResult",
            "SamplerState",
        }
        if value.get("kind") in generated_kinds and not _system:
            raise ValidationError(
                f"{value.get('kind')} resources are created only by the factory coordinator"
            )
        self._validate_namespace(value)
        candidate = deepcopy(value)
        metadata_value = candidate.get("metadata", {})
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        existing = [
            resource
            for resource in self.resources.latest(kind=str(candidate.get("kind", "")))
            if resource["metadata"]["name"] == metadata.get("name")
            and resource["metadata"]["namespace"] == metadata.get("namespace")
        ]
        if len(existing) > 1:
            raise IntegrityError("resource name is bound to multiple identities")
        latest = existing[0] if existing else None
        if latest is not None:
            supplied_uid = metadata.get("uid")
            if supplied_uid is not None and supplied_uid != latest["metadata"]["uid"]:
                raise ConflictError("resource name is already bound to a different uid")
            metadata["uid"] = latest["metadata"]["uid"]
        normalized = default_registry.normalize(candidate, actor=self.actor)
        metadata = normalized["metadata"]
        if latest is not None and metadata["revision"] == latest["metadata"]["revision"]:
            self._record_spec_validated(latest)
            return latest
        stored = self.resources.put(
            metadata["uid"],
            metadata["revision"],
            normalized["kind"],
            normalized,
            created_at=metadata["createdAt"],
        )
        self._record_spec_validated(stored)
        return stored

    def _record_spec_validated(self, resource: dict[str, Any]) -> None:
        self.events.append(
            type="SpecValidated",
            source=f"openfoundry://{self.namespace}",
            subject=f"{resource['kind']}/{resource['metadata']['name']}",
            resource_uid=resource["metadata"]["uid"],
            revision=resource["metadata"]["revision"],
            actor=self.actor,
            data={"specDigest": resource["specDigest"], "kind": resource["kind"]},
            dataschema="https://schemas.openfoundry.dev/events/spec-validated/v1",
            dedupe_revision=True,
        )

    def apply_resource_file(self, path: str | Path) -> dict[str, Any]:
        return self.apply_resource(self._load_resource(path))

    def list_resources(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.resources.list(kind=kind)

    def find_resource(self, kind: str, name: str) -> dict[str, Any]:
        matches = [
            resource
            for resource in self.resources.latest(kind=kind)
            if resource["metadata"]["name"] == name
        ]
        if not matches:
            raise NotFoundError(f"{kind} resource not found: {name}")
        if len(matches) > 1:
            raise IntegrityError("resource name is bound to multiple identities")
        return matches[0]

    def add_store(
        self,
        name: str,
        *,
        driver: str,
        endpoint: str,
        secret_ref: str | None = None,
        plan: bool = False,
    ) -> dict[str, Any]:
        resource = {
            "apiVersion": "openfoundry.dev/v1alpha1",
            "kind": "ArtifactStore",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "storeType": driver,
                "location": endpoint,
                "capabilities": {},
                "config": {"secretRef": secret_ref} if secret_ref else {},
            },
        }
        default_registry.validate(resource)
        if plan:
            return {"plan": "add-store", "resource": resource, "mutates": False}
        return self.apply_resource(resource)

    def get_store(self, name: str) -> ArtifactStore:
        if name == "local":
            return self.local_store
        resource = self.find_resource("ArtifactStore", name)
        spec = resource["spec"]
        driver = spec["storeType"]
        location = str(spec["location"])
        if driver == "filesystem":
            return FilesystemStore(self.paths.root / location)
        if driver == "s3":
            config = spec.get("config", {})
            endpoint = config.get("endpointUrl")
            bucket, _, prefix = location.removeprefix("s3://").partition("/")
            credentials: dict[str, Any] = {}
            secret_ref = config.get("secretRef")
            if secret_ref:
                try:
                    value = json.loads(
                        self.secrets.get(str(secret_ref), "artifact-store-credentials").decode()
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ConfigurationError(
                        f"artifact store credential {secret_ref!r} must be a JSON object"
                    ) from exc
                if not isinstance(value, dict):
                    raise ConfigurationError(
                        f"artifact store credential {secret_ref!r} must be a JSON object"
                    )
                allowed = {
                    "aws_access_key_id",
                    "aws_secret_access_key",
                    "aws_session_token",
                    "region_name",
                    "use_ssl",
                    "verify",
                }
                unexpected = set(value) - allowed
                if unexpected:
                    raise ConfigurationError(
                        f"unsupported S3 credential options: {sorted(unexpected)}"
                    )
                credentials = value
            return S3Store(
                bucket,
                prefix,
                endpoint_url=endpoint,
                credential_reference=secret_ref,
                **credentials,
            )
        raise CapabilityError(f"unsupported artifact store driver: {driver}")

    def add_data(
        self,
        source: str | Path,
        *,
        name: str,
        mode: str,
        rights: dict[str, Any] | None = None,
        sample_schema: str = "application/octet-stream",
        cursor_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = DataService(self.local_store).add(
            name,
            source,
            mode,
            rights=rights,
            sample_schema=sample_schema,
            cursor_policy=cursor_policy,
        )
        extension: dict[str, Any] = {
            "mode": snapshot.mode,
            "source": (
                snapshot.artifact.manifest_digest
                if snapshot.artifact is not None
                else snapshot.source
            ),
            "cursorPolicy": snapshot.cursor_policy,
        }
        if snapshot.artifact is not None:
            extension["artifact"] = snapshot.artifact.to_dict()
            extension["manifestDigest"] = snapshot.artifact.manifest_digest
        resource = {
            "apiVersion": "openfoundry.dev/v1alpha1",
            "kind": "DatasetSnapshot",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "sampleSchema": snapshot.sample_schema,
                "partitions": list(snapshot.partitions),
                "rights": snapshot.rights,
                "statistics": snapshot.statistics,
                "extensions": extension,
            },
        }
        stored = self.apply_resource(resource)
        metadata = stored["metadata"]
        self.events.append(
            type="ArtifactCommitted",
            source=f"openfoundry://{self.namespace}",
            subject=f"DatasetSnapshot/{name}",
            resource_uid=metadata["uid"],
            revision=metadata["revision"],
            actor=self.actor,
            data={
                "mode": mode,
                "manifestDigest": extension.get("manifestDigest"),
                "partitions": len(snapshot.partitions),
            },
            dataschema="https://schemas.openfoundry.dev/events/artifact-committed/v1",
        )
        self.lineage.add(
            LineageEdge(
                f"source:{sha256_digest(snapshot.source)}",
                self._resource_uri(stored),
                "wasDerivedFrom",
                "entity",
                "entity",
                attributes={"mode": mode},
            )
        )
        return stored

    def _snapshot_from_resource(self, resource: dict[str, Any]) -> DatasetSnapshot:
        spec = resource["spec"]
        extension = spec.get("extensions", {})
        artifact = extension.get("artifact")
        return DatasetSnapshot(
            resource["metadata"]["name"],
            extension["mode"],
            extension["source"],
            tuple(spec["partitions"]),
            spec["sampleSchema"],
            spec.get("rights", {}),
            spec.get("statistics", {}),
            extension.get("cursorPolicy", {}),
            ArtifactManifest.from_dict(artifact) if artifact else None,
        )

    def verify_data(self, name: str) -> bool:
        return DataService(self.local_store).verify(
            self._snapshot_from_resource(self.find_resource("DatasetSnapshot", name))
        )

    def revoke_data(self, name: str, *, reason: str) -> dict[str, Any]:
        self._authorize("data.revoke")
        if not reason.strip():
            raise ValidationError("dataset revocation requires a reason")
        current = self.find_resource("DatasetSnapshot", name)
        with self._dataset_rights_locks([current]):
            current = self.find_resource("DatasetSnapshot", name)
            replacement = {
                "apiVersion": current["apiVersion"],
                "kind": current["kind"],
                "metadata": {
                    "name": current["metadata"]["name"],
                    "namespace": current["metadata"]["namespace"],
                    "uid": current["metadata"]["uid"],
                },
                "spec": deepcopy(current["spec"]),
            }
            replacement["spec"].setdefault("rights", {}).update(
                {"trainingAllowed": False, "revoked": True, "revocationReason": reason.strip()}
            )
            revoked = self._apply_resource_unlocked(replacement)
            metadata = revoked["metadata"]
            self.events.append(
                type="DataRightsRevoked",
                source=f"openfoundry://{self.namespace}",
                subject=f"DatasetSnapshot/{name}",
                resource_uid=metadata["uid"],
                revision=metadata["revision"],
                actor=self.actor,
                data={"reason": reason.strip()},
                dataschema="https://schemas.openfoundry.dev/events/data-rights-revoked/v1",
                dedupe_revision=True,
            )
        return revoked

    def sync(
        self,
        asset: str,
        *,
        source: str = "local",
        destination: str,
        direction: str = "push",
        concurrency: int = 4,
        plan: bool = False,
    ) -> dict[str, Any]:
        manifest_digest = asset
        if not asset.startswith("sha256:"):
            dataset_name = asset.removeprefix("dataset/")
            dataset = self.find_resource("DatasetSnapshot", dataset_name)
            try:
                manifest_digest = dataset["spec"]["extensions"]["manifestDigest"]
            except KeyError as exc:
                raise ValidationError(
                    "registered/mounted/stream data has no copyable manifest"
                ) from exc
        source_store, destination_store = self.get_store(source), self.get_store(destination)
        engine = SyncEngine()
        sync_plan = engine.plan(
            source_store,
            destination_store,
            manifest_digest,
            direction=direction,
            concurrency=concurrency,
        )
        result = {
            "manifestDigest": manifest_digest,
            "direction": direction,
            "source": source,
            "destination": destination,
            "missingChunks": [asdict(chunk) for chunk in sync_plan.missing_chunks],
            "bytes": sum(chunk.size for chunk in sync_plan.missing_chunks),
        }
        if plan:
            return {"plan": result, "mutates": False}
        self._authorize("sync.execute", purpose=direction)
        manifest = engine.execute(sync_plan, source_store, destination_store)
        self.events.append(
            type="ArtifactCommitted",
            source=f"openfoundry://{self.namespace}",
            subject=manifest_digest,
            resource_uid=str(uuid7()),
            revision=manifest.manifest_digest,
            actor=self.actor,
            data=result,
            dataschema="https://schemas.openfoundry.dev/events/replica-committed/v1",
        )
        return {**result, "committed": True}

    def validate_module(self, manifest_path: str | Path) -> dict[str, Any]:
        manifest, code_root, package_digest, artifact_digest = self._capture_module_source(
            manifest_path
        )
        return {
            "valid": True,
            "codeRoot": str(code_root.relative_to(self.paths.root)),
            "packageDigest": package_digest,
            "artifactManifest": artifact_digest,
            "dependencyLock": {
                "path": manifest.dependency_lock,
                "digest": manifest.dependency_digest,
                "size": len(manifest.dependency_contents),
            },
            "fixtures": len(manifest.fixtures),
        }

    def _capture_module_source(
        self, manifest_path: str | Path, *, extract_to: Path | None = None
    ) -> tuple[ModuleManifest, Path, str, str]:
        manifest_path = Path(manifest_path).resolve()
        manifest, code_root = load_manifest(manifest_path, self.paths.root)
        with tempfile.NamedTemporaryFile(
            dir=self.paths.packages, suffix=".tar", delete=False
        ) as temporary:
            package_path = Path(temporary.name)
        try:
            package_digest = package_module(manifest_path.parent, package_path)
            manifest_resource_path = manifest_path
            if extract_to is not None:
                bundle_root = extract_module_package(package_path, extract_to)
                manifest_resource_path = bundle_root / manifest_path.name
                manifest, code_root = load_manifest(manifest_resource_path, bundle_root)
            module_resource = self._load_resource(manifest_resource_path, kind="Module")
            module_digest = sha256_digest(
                {
                    "apiVersion": module_resource["apiVersion"],
                    "kind": module_resource["kind"],
                    "metadata": {
                        "name": module_resource["metadata"]["name"],
                        "namespace": module_resource["metadata"]["namespace"],
                    },
                    "spec": module_resource["spec"],
                }
            )
            artifact = ArtifactBuilder(self.local_store).import_path(
                package_path,
                logical_kind="module-source",
                provenance={
                    "manifest": manifest_path.relative_to(self.paths.root.resolve()).as_posix(),
                    "moduleDigest": module_digest,
                    "packageDigest": package_digest,
                },
            )
        finally:
            package_path.unlink(missing_ok=True)
        return manifest, code_root, package_digest, artifact.manifest_digest

    def test_module(
        self, manifest_path: str | Path, *, binding_path: str | Path | None = None
    ) -> dict[str, Any]:
        manifest, code_root = load_manifest(manifest_path, self.paths.root)
        if binding_path is None:
            resolved = self._resolve_executor(
                "local",
                {"kind": "ModuleTest", "manifest": str(Path(manifest_path).resolve())},
                {},
            )
        else:
            binding = self._load_resource(
                self._project_file(binding_path, kind="binding"), kind="Binding"
            )
            self._validate_namespace(binding)
            resolved = self._resolve_executor(
                str(binding["spec"]["executor"]), binding, self._executor_config(binding)
            )
        self._require_executor(resolved, MODULE_EXECUTION_CAPABILITIES)
        environment = self._prepare_module_environment(resolved.executor, manifest, code_root)
        fixtures = manifest.fixtures or [
            {"request": {"operation": "validate"}, "result": {"status": "ok"}}
        ]
        results = []
        for index, fixture in enumerate(fixtures):
            request = dict(fixture["request"])
            request.setdefault("operation", "validate")
            protocol = ProtocolRequest.model_validate(request)
            result = self._execute_module(
                manifest,
                code_root,
                protocol,
                self.paths.runs / "module-tests" / f"{Path(manifest_path).stem}-{index}",
                executor=resolved.executor,
                executor_config=resolved.config,
                environment=environment,
            )
            expected = fixture.get("result", {})
            actual = result.model_dump(mode="json")
            for key, value in expected.items():
                if actual.get(key) != value:
                    raise IntegrityError(f"module fixture {index} mismatch at {key}")
            results.append({"fixture": index, "status": result.status})
        return {"passed": len(results), "results": results}

    def _execute_module(
        self,
        manifest: ModuleManifest,
        code_root: Path,
        request: ProtocolRequest,
        run_dir: Path,
        *,
        executor: Executor,
        executor_config: dict[str, Any],
        environment: dict[str, Any],
        recovering: bool = False,
        datasets: list[dict[str, Any]] | None = None,
        data_use: DataUse = "training",
    ) -> ProtocolResult:
        validate_contract(manifest.schemas["input"], request.inputs, "input")
        validate_contract(manifest.schemas["config"], request.config, "config")
        validate_contract(manifest.schemas["state"], request.state, "state input")
        run_id = request.context.get("runId")
        if run_id and request.context.get("stage"):
            self.run_control.check(str(run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_module_request(run_dir, request, recovering)
        plan = executor.plan(
            argv=[str(item) for item in environment["command"]],
            run_dir=run_dir,
            cwd=code_root,
            deny_network=True,
            requires_result=True,
            environment=environment,
            **executor_config,
        )
        plan_digest = _execution_plan_digest(
            plan,
            request_digest=sha256_digest(request.model_dump(mode="json")),
            environment_digest=environment["digest"],
        )
        with self._dataset_rights_locks(datasets or []):
            for dataset in datasets or []:
                self._require_data_rights(dataset, data_use)
            execution_id = _ensure_execution(executor, run_dir, plan, plan_digest, recovering)
        status = executor.status(execution_id)
        while status.state in {"pending", "running"}:
            if run_id and request.context.get("stage"):
                self.run_control.check(str(run_id))
            time.sleep(0.05)
            status = executor.status(execution_id)
        result_path = run_dir / "result.json"
        if status.state != "succeeded" or not result_path.exists():
            stdout, stderr = executor.read_logs(execution_id)
            raise OpenFoundryError(
                f"module execution {status.state}: {status.reason or 'no result'}",
                details={"stdout": stdout, "stderr": stderr},
            )
        result = ProtocolResult.model_validate_json(result_path.read_bytes())
        if result.status != "ok":
            raise OpenFoundryError(
                result.error.message if result.error else "module returned an error",
                details=result.error.details if result.error else {},
            )
        validate_contract(manifest.schemas["output"], result.outputs, "output")
        validate_contract(manifest.schemas["state"], result.state, "state output")
        return result

    @staticmethod
    def _executor_config(declaration: dict[str, Any]) -> dict[str, Any]:
        spec = declaration.get("spec", {})
        config = spec.get("config", {}) if isinstance(spec, dict) else {}
        if not isinstance(config, dict):
            raise ValidationError("declaration spec.config must be an object")
        return dict(config)

    @staticmethod
    def _prepare_module_environment(
        executor: Executor, manifest: ModuleManifest, code_root: Path
    ) -> dict[str, Any]:
        environment = executor.prepare_environment(
            argv=manifest.argv,
            cwd=code_root,
            dependency=dependency_lock(manifest),
            deny_network=True,
        )
        if not isinstance(environment, dict):
            raise IntegrityError("executor environment descriptor must be an object")
        command = environment.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise IntegrityError("executor environment descriptor requires command argv")
        if environment.get("dependencyDigest") != manifest.dependency_digest:
            raise IntegrityError("executor environment descriptor changed the dependency lock")
        digest = environment.get("digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise IntegrityError("executor environment descriptor requires a canonical digest")
        try:
            canonical_json(environment)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityError("executor environment descriptor must be canonical JSON") from exc
        return environment

    def _resolve_executor(
        self, name: str, declaration: dict[str, Any], config: dict[str, Any]
    ) -> ResolvedExecutor:
        return self.executors.resolve(
            name,
            project_root=self.paths.root,
            state_root=self.paths.state,
            actor=self.actor,
            declaration=declaration,
            config=config,
        )

    def _require_executor(
        self, resolved: ResolvedExecutor, required: frozenset[str]
    ) -> dict[str, Any]:
        report = self.executors.preflight(resolved, required_capabilities=required)
        if report["ready"]:
            return report
        raise CapabilityError(
            f"executor provider {resolved.provider.name!r} is not ready",
            details=report,
            remediation=[
                {
                    "action": "executor.preflight",
                    "command": "openfoundry executor preflight <binding> --workload <workload>",
                    "description": "Inspect missing transport capabilities and host prerequisites.",
                }
            ],
        )

    def _admit_module_environments(
        self, stages: list[Stage], resolved: ResolvedExecutor
    ) -> dict[str, tuple[ModuleManifest, Path, dict[str, Any]]]:
        admitted: dict[str, tuple[ModuleManifest, Path, dict[str, Any]]] = {}
        for stage in stages:
            manifest, code_root = load_manifest(self.paths.root / stage.module, self.paths.root)
            environment = self._prepare_module_environment(resolved.executor, manifest, code_root)
            admitted[stage.name] = (manifest, code_root, environment)
        return admitted

    def executor_catalog(self) -> dict[str, Any]:
        return self.executors.catalog()

    def _project_file(self, value: str | Path, *, kind: str) -> Path:
        resolved = (self.paths.root / value).resolve()
        try:
            resolved.relative_to(self.paths.root.resolve())
        except ValueError as exc:
            raise ValidationError(f"{kind} must be inside the project repository") from exc
        if not resolved.is_file():
            raise ValidationError(f"{kind} file does not exist")
        return resolved

    def executor_preflight(
        self,
        binding_path: str | Path,
        *,
        workload_path: str | Path | None = None,
    ) -> dict[str, Any]:
        binding = self._load_resource(
            self._project_file(binding_path, kind="binding"), kind="Binding"
        )
        self._validate_namespace(binding)
        required = MODULE_PROTOCOL_CAPABILITIES
        if workload_path is not None:
            workload = self._load_resource(self._project_file(workload_path, kind="workload"))
            admitted = project_workload(workload)
            self._validate_namespace(workload)
            model_package = self._pin_model_package(admitted.model_package_ref, admitted.stages)
            execution_stages = [*admitted.stages]
            if model_package is not None:
                execution_stages.append(self._inference_adapter_stage(model_package))
            required = MODULE_EXECUTION_CAPABILITIES
        name = str(binding["spec"]["executor"])
        resolved = self._resolve_executor(name, binding, self._executor_config(binding))
        report = self.executors.preflight(resolved, required_capabilities=required)
        if workload_path is not None and report["ready"]:
            try:
                self._admit_module_environments(execution_stages, resolved)
            except Exception as exc:
                report["ready"] = False
                report["issues"].append(str(exc))
        return report

    def create_run_operation(
        self, workload_path: str | Path, binding_path: str | Path
    ) -> dict[str, Any]:
        self._authorize("workload.run")
        workload = self._project_file(workload_path, kind="workload")
        binding = self._project_file(binding_path, kind="binding")
        workload_raw = self._load_resource(workload)
        binding_raw = self._load_resource(binding, kind="Binding")
        admitted = project_workload(workload_raw)
        stages = admitted.stages
        self._validate_namespace(workload_raw)
        self._validate_namespace(binding_raw)
        model_package = self._pin_model_package(admitted.model_package_ref, stages)
        execution_stages = [*stages]
        if model_package is not None:
            execution_stages.append(self._inference_adapter_stage(model_package))
        resolved = self._resolve_executor(
            str(binding_raw["spec"]["executor"]),
            binding_raw,
            self._executor_config(binding_raw),
        )
        self._require_executor(resolved, MODULE_EXECUTION_CAPABILITIES)
        self._admit_module_environments(execution_stages, resolved)
        pinned_inputs = self._pin_stage_inputs(stages)
        pinned_references = self._pin_reference_inputs(stages)
        evaluation_specs = self._pin_named_resources(
            admitted.evaluation_refs, "evaluationspec/", "EvaluationSpec"
        )
        module_packages: dict[str, str] = {}
        for stage in stages:
            module_path = self._project_file(stage.module, kind="module")
            load_manifest(module_path, self.paths.root)
            with tempfile.NamedTemporaryFile(dir=self.paths.packages, suffix=".tar") as package:
                module_packages[stage.name] = package_module(module_path.parent, package.name)
        adapter_packages: dict[str, str] = {}
        if model_package is not None:
            adapter = self._inference_adapter_stage(model_package)
            module_path = self._project_file(adapter.module, kind="inference adapter")
            with tempfile.NamedTemporaryFile(dir=self.paths.packages, suffix=".tar") as package:
                adapter_packages["inference"] = package_module(module_path.parent, package.name)
        worktree = self._admission_worktree()
        operation_id = str(uuid7())
        return self.operations.create(
            "run",
            {
                "workload": workload.relative_to(self.paths.root).as_posix(),
                "binding": binding.relative_to(self.paths.root).as_posix(),
                "actor": self.actor,
                "policyDigest": self.policy.digest,
                "worktree": worktree,
                "workloadDigest": sha256_digest(workload_raw),
                "bindingDigest": sha256_digest(binding_raw),
                "modulePackages": module_packages,
                "adapterPackages": adapter_packages,
                "experiment": workload_raw["spec"].get("extensions", {}).get("experiment"),
                "resources": {
                    "datasets": {
                        reference: self._resource_uri(resource)
                        for reference, resource in pinned_inputs.items()
                    },
                    "references": pinned_references,
                    "modelPackage": (
                        self._resource_uri(model_package) if model_package is not None else None
                    ),
                    "evaluationSpecs": [
                        self._resource_uri(resource) for resource in evaluation_specs
                    ],
                },
            },
            operation_id=operation_id,
        )

    def _verify_run_request(self, request: dict[str, Any]) -> None:
        workload_path = self.paths.root / request["workload"]
        binding_path = self.paths.root / request["binding"]
        workload_raw = self._load_resource(workload_path)
        binding_raw = self._load_resource(binding_path)
        if (
            sha256_digest(workload_raw) != request["workloadDigest"]
            or sha256_digest(binding_raw) != request["bindingDigest"]
        ):
            raise IntegrityError("queued run desired state changed before admission")
        for stage in project_workload(workload_raw).stages:
            module_path = self._project_file(stage.module, kind="module")
            load_manifest(module_path, self.paths.root)
            with tempfile.NamedTemporaryFile(dir=self.paths.packages, suffix=".tar") as package:
                digest = package_module(module_path.parent, package.name)
            if digest != request["modulePackages"].get(stage.name):
                raise IntegrityError("queued module source changed before admission")
        model_package_ref = request["resources"]["modelPackage"]
        if model_package_ref is not None:
            model_package = self._resource_by_uri("ModelPackage", model_package_ref)
            adapter = self._inference_adapter_stage(model_package)
            module_path = self._project_file(adapter.module, kind="inference adapter")
            with tempfile.NamedTemporaryFile(dir=self.paths.packages, suffix=".tar") as package:
                digest = package_module(module_path.parent, package.name)
            if digest != request["adapterPackages"].get("inference"):
                raise IntegrityError("queued inference adapter source changed before admission")

    def execute_run_operation(self, operation_id: str) -> dict[str, Any]:
        lease = self.paths.state / "operations" / f"{operation_id}.lock"
        with _operation_lease(lease):
            operation = self.operations.get(operation_id)
            if operation["kind"] == "run" and operation["request"]["actor"] != self.actor:
                raise ValidationError("run operation actor does not match the executing controller")
            if (
                operation["kind"] == "run"
                and operation["state"] == "succeeded"
                and self.experiments.metadata(operation_id)
            ):
                self.experiments.complete(operation)
                return operation
            if operation["kind"] != "run" or operation["state"] not in {
                "pending",
                "running",
                "recovering",
                "finalizing",
            }:
                raise ValidationError("operation is not an executable pending run")
            try:
                self.run_control.check(operation_id)
                if operation["state"] != "pending":
                    result = self._resume_run_operation(operation)
                else:
                    self._admit_run_operation(operation)
                    self.run_control.check(operation_id)
                    result = self._continue_run_operation(operation, recovering=False)
            except OperationCanceled:
                return self.run_control.stop(operation_id)
            self.experiments.complete(result)
            return self.operations.advance(operation_id, state="succeeded", result=result["result"])

    def _resume_run_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(operation["id"])
        reconciled = self._reconcile_completed_run(operation_id)
        if reconciled is not None:
            return self.operations.advance(
                operation_id,
                state="finalizing",
                result=reconciled,
            )
        try:
            run_resource: dict[str, Any] | None = self._run_resource(operation_id)
        except IntegrityError:
            run_resource = None
        if run_resource is not None and (self.paths.runs / operation_id / "state.json").is_file():
            return self._continue_run_operation(operation, recovering=True)
        checkpoints = [
            item["metadata"]["name"]
            for item in self.resources.latest(kind="Checkpoint")
            if isinstance(item.get("spec"), dict)
            and str(item["spec"].get("runRef", "")).endswith(operation_id)
        ]
        message = "run outcome is indeterminate; automatic replay is disabled"
        if checkpoints:
            message += (
                f"; {len(checkpoints)} checkpoint(s) survived "
                "(branch a new candidate with from: checkpoint/<name>)"
            )
        if run_resource is not None:
            self.resources.set_status(
                operation_id,
                {"state": "Failed", "reason": message, "outputs": {}},
                expected_version=None,
            )
        self._fail_operation(operation, "indeterminate_execution", message)
        raise IntegrityError(message)

    def _admit_run_operation(self, operation: dict[str, Any]) -> None:
        try:
            self._verify_run_request(operation["request"])
        except OpenFoundryError as exc:
            error = exc.as_dict()["error"]
            self._fail_operation(operation, error["code"], error["message"], error["retryable"])
            raise
        except Exception:
            self._fail_operation(operation, "run_admission_error", "run admission failed")
            raise

    def _fail_operation(
        self, operation: dict[str, Any], code: str, message: str, retryable: bool = False
    ) -> None:
        self.operations.advance(
            str(operation["id"]),
            state="failed",
            error={"code": code, "message": message, "retryable": retryable},
        )

    def _continue_run_operation(
        self, operation: dict[str, Any], *, recovering: bool
    ) -> dict[str, Any]:
        operation_id = str(operation["id"])
        request = operation["request"]
        active = self.operations.advance(
            operation_id,
            state="finalizing"
            if operation["state"] == "finalizing"
            else "recovering"
            if recovering
            else "running",
            result={
                "phase": "recovery" if recovering else "admission",
                "runId": operation_id,
            },
        )
        try:
            result = self._run_impl(
                self.paths.root / request["workload"],
                self.paths.root / request["binding"],
                operation_id=operation_id,
                expected_workload_digest=request["workloadDigest"],
                expected_binding_digest=request["bindingDigest"],
                expected_module_packages=request["modulePackages"],
                expected_adapter_packages=request.get("adapterPackages", {}),
                expected_resources=request["resources"],
                recovering=recovering,
            )
        except OperationCanceled:
            raise
        except OpenFoundryError as exc:
            error = exc.as_dict()["error"]
            if recovering:
                self._fail_recovered_run(operation_id, error["message"])
            self._fail_operation(active, error["code"], error["message"], error["retryable"])
            raise
        except Exception:
            if recovering:
                self._fail_recovered_run(operation_id, "run worker failed during recovery")
            self._fail_operation(active, "run_worker_error", "run worker failed")
            raise
        return self.operations.advance(
            operation_id,
            state="finalizing",
            result=result,
        )

    def _fail_recovered_run(self, run_id: str, reason: str) -> None:
        run_resource = self._run_resource(run_id)
        state_store = StateStore(self.paths.runs / run_id / "state.json")
        state = state_store.read()["state"]
        if state in {RunState.RUNNING.value, RunState.RECOVERING.value}:
            state_store.transition(RunState(state), RunState.FAILED, reason)
        desired = {"state": "Failed", "reason": reason, "outputs": {}}
        if self._status_state(run_id) == "Failed":
            desired = self.resources.get_status(run_id)[0]
            reason = str(desired.get("reason", reason))
        self._settle_run(run_resource, run_id, desired, reason)

    def _settle_run(
        self, run_resource: dict[str, Any], run_id: str, desired: dict[str, Any], reason: str
    ) -> None:
        try:
            status, version = self.resources.get_status(run_id)
        except NotFoundError:
            status, version = {}, None
        if status != desired:
            self.resources.set_status(run_id, desired, expected_version=version)
        events = self.events.query(run_id=run_id, resource_uid=run_id, type="RunStateChanged")
        if not any(event.data.get("state") == desired["state"] for event in events):
            self._run_state_event(run_resource, run_id, desired["state"], reason)

    def _reconcile_completed_run(self, operation_id: str) -> dict[str, Any] | None:
        runs = [
            resource
            for resource in self.resources.list(kind="Run")
            if resource["metadata"]["uid"] == operation_id
            and resource["spec"]["operationId"] == operation_id
        ]
        if not runs:
            return None
        if len(runs) != 1:
            raise IntegrityError("run resource identity is ambiguous")
        run_resource = runs[0]
        run_ref = self._resource_uri(run_resource)
        results = [
            resource
            for resource in self.resources.list(kind="RunResult")
            if resource["spec"]["runRef"] == run_ref
        ]
        if not results:
            return None
        if len(results) != 1:
            raise IntegrityError("run result identity is ambiguous")
        run_result = results[0]
        admission = run_result["spec"]["admission"]
        extensions = run_resource["spec"]["extensions"]
        result = {
            "runId": operation_id,
            "state": "Succeeded",
            "outputs": run_result["spec"]["outputs"],
            "stages": run_result["spec"]["stages"],
            "workloadDigest": admission["workloadDigest"],
            "bindingDigest": extensions["bindingDigest"],
            "resultRef": self._resource_uri(run_result),
        }
        self.lineage.add(
            LineageEdge(
                f"run:{operation_id}",
                result["resultRef"],
                "generated",
                "activity",
                "entity",
                run_id=operation_id,
            )
        )
        self._settle_run(
            run_resource,
            operation_id,
            {
                "state": "Succeeded",
                "reason": "completed",
                "outputs": result["outputs"],
                "resultRef": result["resultRef"],
            },
            "completed",
        )
        return result

    def run(self, workload_path: str | Path, binding_path: str | Path) -> dict[str, Any]:
        operation = self.create_run_operation(workload_path, binding_path)
        completed = self.execute_run_operation(operation["id"])
        return {**completed["result"], "operationId": operation["id"]}

    def _run_impl(
        self,
        workload_path: str | Path,
        binding_path: str | Path,
        *,
        operation_id: str,
        expected_workload_digest: str,
        expected_binding_digest: str,
        expected_module_packages: dict[str, str],
        expected_adapter_packages: dict[str, str],
        expected_resources: dict[str, Any],
        recovering: bool = False,
    ) -> dict[str, Any]:
        run_id = operation_id
        run_dir = self.paths.runs / run_id
        run_resource, workload_raw, binding_raw = self._run_desired_state(
            run_id,
            workload_path,
            binding_path,
            expected_workload_digest,
            expected_binding_digest,
            recovering=recovering,
        )
        admitted = project_workload(workload_raw)
        default_registry.validate_as(binding_raw, "Binding")
        self._validate_namespace(workload_raw)
        self._validate_namespace(binding_raw)
        stages = admitted.stages
        pinned_inputs = self._pin_stage_inputs(stages, expected_resources["datasets"])
        pinned_references = self._pin_reference_inputs(
            stages, expected_resources.get("references", {})
        )
        if run_resource is not None:
            self._readmit_run(run_resource, stages, pinned_inputs)
        model_package = self._pin_model_package(
            admitted.model_package_ref,
            stages,
            expected_resources["modelPackage"],
            validate_source=not recovering,
        )
        inference_stage = (
            self._inference_adapter_stage(model_package) if model_package is not None else None
        )
        if set(expected_adapter_packages) != ({"inference"} if inference_stage else set()):
            raise IntegrityError("queued model adapter sources do not match the workload")
        execution_stages = [*stages, *([inference_stage] if inference_stage is not None else [])]
        resolved_executor = self._resolve_executor(
            str(binding_raw["spec"]["executor"]), binding_raw, self._executor_config(binding_raw)
        )
        initial_admission = None
        if not recovering:
            self._require_executor(resolved_executor, MODULE_EXECUTION_CAPABILITIES)
            initial_admission = self._admit_module_environments(execution_stages, resolved_executor)
        evaluation_specs = self._pin_named_resources(
            admitted.evaluation_refs,
            "evaluationspec/",
            "EvaluationSpec",
            expected_resources["evaluationSpecs"],
        )
        _check_metric_names(evaluation_specs)
        workload_resource = workload_raw if recovering else self.apply_resource(workload_raw)
        binding_resource = binding_raw if recovering else self.apply_resource(binding_raw)
        captured = self._capture_run_sources(
            execution_stages,
            inference_stage,
            run_dir,
            run_resource,
            expected_module_packages,
            expected_adapter_packages,
        )
        if recovering:
            self._require_executor(resolved_executor, MODULE_EXECUTION_CAPABILITIES)
        admitted_modules, module_digests, environments, inference_evidence = (
            self._admit_run_sources(captured, resolved_executor, initial_admission)
        )
        spec = admitted.model_copy(
            update={
                "source_digest": workload_resource["metadata"]["revision"],
                "binding_digest": binding_resource["metadata"]["revision"],
                "module_digests": module_digests,
                "environments": environments,
                "input_revisions": {
                    reference: self._resource_uri(resource)
                    for reference, resource in pinned_inputs.items()
                },
                "reference_revisions": {
                    reference: str(pinned["uri"]) for reference, pinned in pinned_references.items()
                },
                "model_package_ref": (
                    self._resource_uri(model_package) if model_package is not None else None
                ),
                "evaluation_refs": [self._resource_uri(item) for item in evaluation_specs],
            },
        )
        state_store = StateStore(run_dir / "state.json")
        already_succeeded = False
        if run_resource is not None:
            already_succeeded = self._recover_run_state(state_store, spec, run_resource)
        else:
            run_resource = self._admit_run(
                state_store,
                spec,
                operation_id,
                workload_resource,
                binding_resource,
                pinned_inputs,
                inference_evidence,
            )
        if model_package is not None:
            self.lineage.add(
                LineageEdge(
                    self._resource_uri(model_package),
                    f"run:{run_id}",
                    "used",
                    "entity",
                    "activity",
                    run_id=run_id,
                )
            )
        context = _RunContext(
            run_id=run_id,
            run_dir=run_dir,
            recovering=recovering,
            spec=spec,
            stages=stages,
            run_resource=run_resource,
            executor=resolved_executor,
            admitted_modules=admitted_modules,
            pinned_inputs=pinned_inputs,
            pinned_references=pinned_references,
            outputs={
                f"{stage_name}.{name}": value
                for stage_name, stage_state in state_store.read()["stages"].items()
                if stage_state.get("status") == "succeeded"
                for name, value in stage_state.get("outputs", {}).items()
            },
        )
        return self._finish_run(context, state_store, already_succeeded, binding_resource)

    def _run_desired_state(
        self,
        run_id: str,
        workload_path: str | Path,
        binding_path: str | Path,
        expected_workload_digest: str,
        expected_binding_digest: str,
        *,
        recovering: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        if recovering:
            run_resource = self._run_resource(run_id)
            self._record_spec_validated(run_resource)
            workload = self._resource_by_uri("WorkloadSpec", run_resource["spec"]["workloadRef"])
            binding = self._resource_by_uri("Binding", run_resource["spec"]["bindingRef"])
            if (
                workload["metadata"]["revision"] != expected_workload_digest
                or binding["metadata"]["revision"] != expected_binding_digest
            ):
                raise IntegrityError("recovered run does not match its admitted request")
            return run_resource, workload, binding
        workload = self._load_resource(self._project_file(workload_path, kind="workload"))
        binding = self._load_resource(self._project_file(binding_path, kind="binding"))
        if (
            sha256_digest(workload) != expected_workload_digest
            or sha256_digest(binding) != expected_binding_digest
        ):
            raise IntegrityError("run desired state changed during admission")
        return None, workload, binding

    def _readmit_run(
        self, run_resource: dict[str, Any], stages: list[Stage], inputs: dict[str, dict[str, Any]]
    ) -> None:
        with self._dataset_rights_locks(list(inputs.values())):
            self._require_input_rights(stages, inputs)
            self._record_run_admitted(run_resource)

    def _capture_run_sources(
        self,
        execution_stages: list[Stage],
        inference_stage: Stage | None,
        run_dir: Path,
        run_resource: dict[str, Any] | None,
        expected_module_packages: dict[str, str],
        expected_adapter_packages: dict[str, str],
    ) -> list[_CapturedSource]:
        captured = []
        for stage in execution_stages:
            is_inference = stage is inference_stage
            if run_resource is not None:
                source = self._recovered_source(run_dir, stage, is_inference, run_resource)
            else:
                manifest, code_root, package_digest, artifact_digest = self._capture_module_source(
                    self.paths.root / stage.module, extract_to=run_dir / "sources" / stage.name
                )
                source = _CapturedSource(
                    stage, is_inference, manifest, code_root, package_digest, artifact_digest, None
                )
            expected_package = (
                expected_adapter_packages.get("inference")
                if is_inference
                else expected_module_packages.get(stage.name)
            )
            if source.package_digest != expected_package:
                raise IntegrityError("module source changed during admission")
            captured.append(source)
        return captured

    def _recovered_source(
        self, run_dir: Path, stage: Stage, is_inference: bool, run_resource: dict[str, Any]
    ) -> _CapturedSource:
        source_root = run_dir / "sources" / stage.name
        manifest, code_root = load_manifest(source_root / Path(stage.module).name, source_root)
        with tempfile.NamedTemporaryFile(dir=self.paths.packages, suffix=".tar") as package:
            package_digest = package_module(source_root, package.name)
        extensions = run_resource["spec"]["extensions"]
        if is_inference:
            inference_admission = extensions.get("inferenceAdapter")
            if not isinstance(inference_admission, dict):
                raise IntegrityError("recovered run has no admitted inference adapter")
            artifact_digest = inference_admission.get("sourceDigest")
            expected_environment = inference_admission.get("environment")
        else:
            artifact_digest = extensions["moduleDigests"].get(stage.name)
            expected_environment = extensions["environments"].get(stage.name)
        if not isinstance(artifact_digest, str):
            raise IntegrityError("recovered run has no admitted module source")
        if not isinstance(expected_environment, dict):
            raise IntegrityError("recovered run has no admitted module environment")
        source_manifest = self.local_store.read_manifest(artifact_digest)
        if not ArtifactBuilder(self.local_store).verify(source_manifest):
            raise IntegrityError("recovered module source failed integrity verification")
        if source_manifest.digest != package_digest:
            raise IntegrityError("recovered module source differs from admitted artifact")
        return _CapturedSource(
            stage,
            is_inference,
            manifest,
            code_root,
            package_digest,
            artifact_digest,
            expected_environment,
        )

    def _admit_run_sources(
        self,
        captured: list[_CapturedSource],
        resolved_executor: ResolvedExecutor,
        initial_admission: dict[str, tuple[ModuleManifest, Path, dict[str, Any]]] | None,
    ) -> tuple[
        dict[str, tuple[ModuleManifest, Path, str]],
        dict[str, str],
        dict[str, dict[str, Any]],
        dict[str, Any] | None,
    ]:
        admitted_modules: dict[str, tuple[ModuleManifest, Path, str]] = {}
        module_digests: dict[str, str] = {}
        environments: dict[str, dict[str, Any]] = {}
        inference_evidence: dict[str, Any] | None = None
        for source in captured:
            environment = self._prepare_module_environment(
                resolved_executor.executor, source.manifest, source.code_root
            )
            expected_environment = (
                source.expected_environment
                if initial_admission is None
                else initial_admission[source.stage.name][2]
            )
            assert expected_environment is not None
            if environment["digest"] != expected_environment["digest"]:
                raise IntegrityError("module environment changed after admission")
            if source.is_inference:
                inference_evidence = {
                    "sourceDigest": source.artifact_digest,
                    "packageDigest": source.package_digest,
                    "environment": environment,
                }
            else:
                name = source.stage.name
                admitted_modules[name] = (source.manifest, source.code_root, source.artifact_digest)
                module_digests[name] = source.artifact_digest
                environments[name] = environment
        return admitted_modules, module_digests, environments, inference_evidence

    @staticmethod
    def _recover_run_state(
        state_store: StateStore, spec: AdmittedWorkload, run_resource: dict[str, Any]
    ) -> bool:
        admission = run_resource["spec"]["extensions"]
        if (
            spec.digest != admission["workloadDigest"]
            or spec.binding_digest != admission["bindingDigest"]
        ):
            raise IntegrityError("recovered run admission digest differs from durable state")
        state = state_store.verify(spec)["state"]
        if state == RunState.RUNNING.value:
            state_store.transition(RunState.RUNNING, RunState.RECOVERING)
            state = RunState.RECOVERING.value
        if state == RunState.RECOVERING.value:
            state_store.transition(RunState.RECOVERING, RunState.RUNNING)
            return False
        if state == RunState.SUCCEEDED.value:
            return True
        raise IntegrityError(f"run state {state!r} cannot be recovered")

    def _admit_run(
        self,
        state_store: StateStore,
        spec: AdmittedWorkload,
        operation_id: str,
        workload_resource: dict[str, Any],
        binding_resource: dict[str, Any],
        pinned_inputs: dict[str, dict[str, Any]],
        inference_evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        worktree = self._admission_worktree()
        state_store.initialize(spec)
        state_store.transition(RunState.DRAFT, RunState.VALIDATED)
        datasets = list(pinned_inputs.values())
        with self._dataset_rights_locks(datasets):
            self._require_input_rights(spec.stages, pinned_inputs)
            state_store.transition(RunState.VALIDATED, RunState.ADMITTED)
            state_store.transition(RunState.ADMITTED, RunState.RUNNING)
            run_resource = self.apply_resource(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "Run",
                    "metadata": {
                        "name": f"run-{operation_id}",
                        "namespace": self.namespace,
                        "uid": operation_id,
                    },
                    "spec": {
                        "runId": operation_id,
                        "operationId": operation_id,
                        "workloadRef": self._resource_uri(workload_resource),
                        "bindingRef": self._resource_uri(binding_resource),
                        "extensions": {
                            "workloadDigest": spec.digest,
                            "operationId": operation_id,
                            "bindingDigest": spec.binding_digest,
                            "admittedInputs": spec.input_revisions,
                            "admittedReferences": spec.reference_revisions,
                            "policyDigest": self.policy.digest,
                            "worktree": worktree,
                            "modelPackageRef": spec.model_package_ref,
                            "evaluationRefs": spec.evaluation_refs,
                            "moduleDigests": spec.module_digests,
                            "environments": spec.environments,
                            "inferenceAdapter": inference_evidence,
                        },
                    },
                },
                _system=True,
            )
            self._record_run_admitted(run_resource)
        return run_resource

    def _execute_stage(self, context: _RunContext, stage: Stage) -> dict[str, Any]:
        run_id = context.run_id
        manifest, code_root, module_digest = context.admitted_modules[stage.name]
        environment = context.spec.environments[stage.name]
        self.lineage.add(
            LineageEdge(
                f"artifact:{module_digest}",
                f"run:{run_id}/stage:{stage.name}",
                "used",
                "entity",
                "activity",
                run_id=run_id,
            )
        )
        stage_dir = context.run_dir / "stages" / stage.name
        for reference in stage.inputs.values():
            if reference.partition(".")[0] in stage.needs:
                value = self._resolve_output_reference(reference, context.outputs, context.stages)
                if isinstance(value, str) and self._is_reference_input(value):
                    context.pinned_references[value] = self._pin_reference(value, None)
        stage_inputs = {
            name: self._resolve_stage_input(
                self._resolve_output_reference(reference, context.outputs, context.stages),
                stage_dir / "inputs" / name,
                context.pinned_inputs,
                run_id=run_id,
                stage_name=stage.name,
                pinned_references=context.pinned_references,
                allow_existing=context.recovering,
            )
            for name, reference in stage.inputs.items()
        }
        request = ProtocolRequest(
            operation=stage.operation,  # type: ignore[arg-type]
            inputs=stage_inputs,
            config=stage.config,
            context={
                "runId": run_id,
                "stage": stage.name,
                "runDirectory": str(context.run_dir / stage.name),
            },
        )
        result = self._execute_module(
            manifest,
            code_root,
            request,
            stage_dir,
            executor=context.executor.executor,
            executor_config=context.executor.config,
            environment=environment,
            recovering=context.recovering,
            data_use=stage.data_use,
            datasets=[
                context.pinned_inputs[reference]
                for reference in stage.inputs.values()
                if reference in context.pinned_inputs
            ],
        )
        stage_outputs = dict(result.outputs)
        if sum(str(item.get("kind")) == "checkpoint" for item in result.artifacts) > 1:
            raise ValidationError("one stage result may emit only one aggregate checkpoint")
        for index, value in enumerate(result.artifacts):
            name = str(value.get("name", f"artifact-{index}"))
            if name in stage_outputs:
                raise IntegrityError(f"stage artifact collides with output: {name}")
            stage_outputs[name] = self._import_stage_artifact(
                context, stage, value, result, manifest, module_digest, environment
            )
            self.lineage.add(
                LineageEdge(
                    f"run:{run_id}/stage:{stage.name}",
                    f"artifact:{stage_outputs[name]}",
                    "generated",
                    "activity",
                    "entity",
                    run_id=run_id,
                )
            )
        missing_outputs = sorted(set(stage.outputs) - stage_outputs.keys())
        if missing_outputs:
            raise IntegrityError(
                f"stage {stage.name!r} did not produce declared outputs",
                details={"outputs": missing_outputs},
            )
        for name, value in stage_outputs.items():
            context.outputs[f"{stage.name}.{name}"] = value
        return stage_outputs

    def _import_stage_artifact(
        self,
        context: _RunContext,
        stage: Stage,
        value: dict[str, Any],
        result: ProtocolResult,
        manifest: ModuleManifest,
        module_digest: str,
        environment: dict[str, Any],
    ) -> str:
        stage_dir = context.run_dir / "stages" / stage.name
        artifact_path = (stage_dir / str(value["path"])).resolve()
        allowed_root = stage_dir.resolve()
        if artifact_path != allowed_root and allowed_root not in artifact_path.parents:
            raise ValidationError("module artifact path escapes the stage run directory")
        kind = str(value.get("kind", "stage-output"))
        artifact = ArtifactBuilder(self.local_store).import_path(
            artifact_path,
            logical_kind="checkpoint-shard" if kind == "checkpoint" else kind,
            provenance={"runId": context.run_id, "stage": stage.name},
        )
        if kind != "checkpoint":
            return artifact.manifest_digest
        return self._publish_checkpoint(
            context, stage, artifact, result, manifest, module_digest, environment
        )

    def _publish_checkpoint(
        self,
        context: _RunContext,
        stage: Stage,
        artifact: ArtifactManifest,
        result: ProtocolResult,
        manifest: ModuleManifest,
        module_digest: str,
        environment: dict[str, Any],
    ) -> str:
        if not manifest.checkpoint:
            raise ValidationError("module emitted a checkpoint without declaring support")
        if not result.state:
            raise ValidationError("checkpoint publication requires protocol state")
        run_id = context.run_id
        with tempfile.NamedTemporaryFile(
            dir=context.run_dir / "stages" / stage.name,
            prefix=".openfoundry-checkpoint-state-",
            delete=False,
        ) as state_file:
            state_file.write(canonical_json(result.state))
            state_path = Path(state_file.name)
        try:
            state_artifact = ArtifactBuilder(self.local_store).import_path(
                state_path,
                logical_kind="checkpoint-state",
                provenance={"runId": run_id, "stage": stage.name},
            )
        finally:
            state_path.unlink(missing_ok=True)
        components = {"module-state": artifact, "protocol-state": state_artifact}
        checkpoint_manifest = AtomicCheckpointPublisher(self.local_store).publish(
            components,
            {
                "workload": context.spec.source_digest,
                "binding": str(context.spec.binding_digest),
                "module": module_digest,
                "environment": environment["digest"],
            },
        )
        artifact_digest = checkpoint_manifest.manifest_digest
        checkpoint = self.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Checkpoint",
                "metadata": {
                    "name": f"checkpoint-{run_id[:8]}-{stage.name}",
                    "namespace": self.namespace,
                },
                "spec": {
                    "runRef": self._resource_uri(context.run_resource),
                    "artifactRef": artifact_digest,
                    "components": {
                        role: component.manifest_digest for role, component in components.items()
                    },
                    "extensions": {"stage": stage.name},
                },
            },
            _system=True,
        )
        self.events.append(
            type="CheckpointCommitted",
            source=f"openfoundry://{self.namespace}",
            subject=self._resource_uri(checkpoint),
            resource_uid=checkpoint["metadata"]["uid"],
            revision=checkpoint["metadata"]["revision"],
            actor=self.actor,
            run_id=run_id,
            data={"artifactRef": artifact_digest, "stage": stage.name},
            dataschema="https://schemas.openfoundry.dev/events/checkpoint-committed/v1",
            dedupe_revision=True,
        )
        return artifact_digest

    def _finish_run(
        self,
        context: _RunContext,
        state_store: StateStore,
        already_succeeded: bool,
        binding_resource: dict[str, Any],
    ) -> dict[str, Any]:
        run_id, spec, outputs = context.run_id, context.spec, context.outputs
        if already_succeeded:
            result_state = state_store.read()
            if any(
                stage.get("status") == "succeeded"
                and not self._verify_stage_outputs(stage.get("outputs", {}))
                for stage in result_state["stages"].values()
            ):
                raise IntegrityError("succeeded run output evidence failed verification")
        else:
            try:
                result_state = WorkloadRunner(spec, state_store).run(
                    lambda stage: self._execute_stage(context, stage),
                    verify=self._verify_stage_outputs,
                )
                self.operations.advance(run_id, state="finalizing", result={"runId": run_id})
                state_store.transition(RunState.RUNNING, RunState.SUCCEEDED)
            except OperationCanceled:
                raise
            except Exception as exc:
                state_store.transition(RunState.RUNNING, RunState.FAILED, str(exc))
                self.resources.set_status(
                    run_id,
                    {"state": "Failed", "reason": str(exc), "outputs": outputs},
                    expected_version=None,
                )
                self._run_state_event(context.run_resource, run_id, "Failed", str(exc))
                raise
        run_result = self.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "RunResult",
                "metadata": {"name": f"result-{run_id}", "namespace": self.namespace},
                "spec": {
                    "runRef": self._resource_uri(context.run_resource),
                    "outputs": outputs,
                    "stages": result_state["stages"],
                    "admission": {
                        "workloadDigest": spec.digest,
                        "bindingRef": self._resource_uri(binding_resource),
                        "modelPackageRef": spec.model_package_ref,
                        "moduleDigests": spec.module_digests,
                        "environments": spec.environments,
                    },
                    "extensions": {"runId": run_id},
                },
            },
            _system=True,
        )
        self.lineage.add(
            LineageEdge(
                f"run:{run_id}",
                self._resource_uri(run_result),
                "generated",
                "activity",
                "entity",
                run_id=run_id,
            )
        )
        self.resources.set_status(
            run_id,
            {
                "state": "Succeeded",
                "reason": "completed",
                "outputs": outputs,
                "resultRef": self._resource_uri(run_result),
            },
            expected_version=None,
        )
        self._run_state_event(context.run_resource, run_id, "Succeeded", "completed")
        return {
            "runId": run_id,
            "state": "Succeeded",
            "outputs": outputs,
            "stages": result_state["stages"],
            "workloadDigest": spec.digest,
            "bindingDigest": spec.binding_digest,
            "resultRef": self._resource_uri(run_result),
        }

    def _pin_stage_inputs(
        self,
        stages: list[Stage],
        expected_revisions: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        pinned: dict[str, dict[str, Any]] = {}
        for reference, uses in dataset_uses(stages).items():
            if expected_revisions is not None and reference not in expected_revisions:
                raise IntegrityError("queued dataset reference was not pinned")
            resource = (
                self._resource_by_uri("DatasetSnapshot", expected_revisions[reference])
                if expected_revisions is not None
                else self.find_resource("DatasetSnapshot", reference.removeprefix("dataset/"))
            )
            for use in uses:
                self._require_data_rights(resource, use)
            snapshot = self._snapshot_from_resource(resource)
            if snapshot.mode != "copy":
                raise CapabilityError("only copied dataset snapshots can be executed reproducibly")
            if snapshot.artifact is None or not ArtifactBuilder(self.local_store).verify(
                snapshot.artifact
            ):
                raise IntegrityError("admitted dataset artifact failed integrity verification")
            pinned[reference] = resource
        return pinned

    @staticmethod
    def _data_rights_valid(resource: dict[str, Any], use: DataUse = "training") -> bool:
        rights = resource["spec"].get("rights")
        return bool(
            isinstance(rights, dict)
            and rights.get(f"{use}Allowed", use == "evaluation") is True
            and rights.get("revoked", False) is False
        )

    def _current_data_rights_valid(
        self, resource: dict[str, Any], use: DataUse = "training"
    ) -> bool:
        return any(
            item["metadata"]["uid"] == resource["metadata"]["uid"]
            and self._data_rights_valid(item, use)
            for item in self.resources.latest(kind="DatasetSnapshot")
        )

    def _require_data_rights(self, resource: dict[str, Any], use: DataUse = "training") -> None:
        name = str(resource["metadata"]["name"])
        if not self._data_rights_valid(resource, use):
            raise ValidationError(f"dataset {name!r} pinned rights do not allow {use}")
        if not self._current_data_rights_valid(resource, use):
            raise ValidationError(f"dataset {name!r} current rights do not allow {use}")

    def _require_input_rights(self, stages: list[Stage], inputs: dict[str, dict[str, Any]]) -> None:
        for reference, uses in dataset_uses(stages).items():
            for use in uses:
                self._require_data_rights(inputs[reference], use)

    def _input_rights_valid(
        self, inputs: dict[str, dict[str, Any]], uses: dict[str, list[DataUse]]
    ) -> bool:
        return all(
            self._data_rights_valid(item, use) and self._current_data_rights_valid(item, use)
            for reference, item in inputs.items()
            for use in uses.get(reference, ["training"])
        )

    def _pin_model_package(
        self,
        reference: str | None,
        stages: list[Stage],
        expected_revision: str | None = None,
        *,
        validate_source: bool = True,
    ) -> dict[str, Any] | None:
        if reference is None:
            if expected_revision is not None:
                raise IntegrityError("queued model package does not match the workload")
            return None
        prefix = "modelpackage/"
        if not reference.startswith(prefix) or "@" in reference:
            raise ValidationError("modelPackageRef must use modelpackage/<name>")
        resource = (
            self._resource_by_uri("ModelPackage", expected_revision)
            if expected_revision is not None
            else self.find_resource("ModelPackage", reference.removeprefix(prefix))
        )
        package_spec = resource["spec"]
        _validate_package_signatures(package_spec)
        training_stage = _training_stage(package_spec["adapters"]["trainingReference"], stages)
        inference = package_spec["adapters"]["inferenceReference"]
        inference_module = portable_relative_path(inference["module"], "inference adapter")
        if inference_module == portable_relative_path(training_stage.module, "training adapter"):
            raise ValidationError("ModelPackage inferenceReference must use an independent module")
        if validate_source:
            inference_path = self._project_file(str(inference_module), kind="inference adapter")
            load_manifest(inference_path, self.paths.root)
        stage_outputs = {stage.name: set(stage.outputs) for stage in stages}
        stage_name, _, output_name = inference["stateOutput"].partition(".")
        if stage_name not in stage_outputs or output_name not in stage_outputs[stage_name]:
            raise ValidationError("ModelPackage stateOutput is not declared by the workload")
        return resource

    @staticmethod
    def _inference_adapter_stage(model_package: dict[str, Any]) -> Stage:
        adapter = model_package["spec"]["adapters"]["inferenceReference"]
        return Stage(
            name="_inference",
            module=adapter["module"],
            operation=adapter["operation"],
            config=adapter["config"],
        )

    def _pin_named_resources(
        self,
        references: list[str],
        prefix: str,
        kind: str,
        expected_revisions: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if expected_revisions is not None and len(expected_revisions) != len(references):
            raise IntegrityError(f"queued {kind} references do not match the workload")
        resources = []
        for index, reference in enumerate(references):
            if not reference.startswith(prefix) or "@" in reference:
                raise ValidationError(f"{kind} reference must use {prefix}<name>")
            resources.append(
                self._resource_by_uri(kind, expected_revisions[index])
                if expected_revisions is not None
                else self.find_resource(kind, reference.removeprefix(prefix))
            )
        return resources

    @staticmethod
    def _resolve_output_reference(
        reference: str, outputs: dict[str, Any], stages: list[Stage]
    ) -> Any:
        producer = reference.partition(".")[0]
        if producer not in {stage.name for stage in stages}:
            return reference
        try:
            return outputs[reference]
        except KeyError as exc:
            raise IntegrityError(f"stage output reference is unavailable: {reference}") from exc

    @staticmethod
    def _is_reference_input(reference: str) -> bool:
        return (
            reference.startswith(("release/", "alias/", "checkpoint/", "artifact:sha256:"))
            or re.fullmatch(r"sha256:[0-9a-f]{64}", reference) is not None
        )

    def _pin_reference_inputs(
        self,
        stages: list[Stage],
        expected: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        pinned: dict[str, dict[str, Any]] = {}
        for stage in stages:
            for reference in stage.inputs.values():
                if reference in pinned or not self._is_reference_input(reference):
                    continue
                if expected is not None and reference not in expected:
                    raise IntegrityError("queued reference input was not pinned")
                pinned[reference] = self._pin_reference(
                    reference, None if expected is None else expected[reference]
                )
        return pinned

    def _pin_reference(self, reference: str, expected: dict[str, Any] | None) -> dict[str, Any]:
        if reference.startswith(("release/", "alias/", "checkpoint/")):
            kind = "Checkpoint" if reference.startswith("checkpoint/") else "Release"
            name = reference.split("/", 1)[1]
            if not name or "@" in name:
                raise ValidationError(f"reference input must use {kind.lower()}/<name>")
            resource = (
                self._resource_by_uri(kind, str(expected["uri"]))
                if expected is not None
                else (
                    self.publishing.resolve_release(reference)
                    if kind == "Release"
                    else self.find_resource(kind, name)
                )
            )
            pinned = (
                self._pin_release(resource, name)
                if kind == "Release"
                else _pin_checkpoint(resource, name, self._resource_uri(resource))
            )
        else:
            digest = reference.removeprefix("artifact:")
            pinned = {
                "kind": "artifact",
                "uri": f"artifact:{digest}",
                "artifacts": {"payload": digest},
            }
        self._verify_reference_artifacts(reference, pinned["artifacts"].values())
        if expected is not None and expected != pinned:
            raise IntegrityError("queued reference input changed before admission")
        return pinned

    def _pin_release(self, resource: dict[str, Any], name: str) -> dict[str, Any]:
        manifest = resource["spec"].get("extensions", {}).get("manifest", {})
        model_digest = manifest.get("model", {}).get("digest")
        state_digest = manifest.get("state", {}).get("digest")
        if not isinstance(model_digest, str) or not isinstance(state_digest, str):
            raise IntegrityError(f"release has no model artifact to consume: {name}")
        artifacts = {"model": model_digest, "state": state_digest}
        try:
            state_manifest = self.local_store.read_manifest(state_digest)
        except OpenFoundryError as exc:
            raise IntegrityError(f"release state artifact is unavailable: {name}") from exc
        if state_manifest.logical_kind == "checkpoint":
            components = state_manifest.provenance.get("components", {})
            for role in ("module-state", "protocol-state"):
                if isinstance(components.get(role), str):
                    artifacts[role] = str(components[role])
        return {
            "kind": "release",
            "uri": self._resource_uri(resource),
            "artifacts": artifacts,
            "modelPackageRef": manifest.get("modelPackage", {}).get("ref"),
        }

    def _verify_reference_artifacts(self, reference: str, digests: Iterable[Any]) -> None:
        builder = ArtifactBuilder(self.local_store)
        for digest in digests:
            try:
                manifest = self.local_store.read_manifest(str(digest))
            except OpenFoundryError as exc:
                raise IntegrityError(
                    f"reference input artifact is unavailable: {reference}"
                ) from exc
            if not builder.verify(manifest):
                raise IntegrityError(f"reference input artifact failed verification: {reference}")

    def _resolve_model_state(self, state: Any, target: Path) -> dict[str, Any]:
        if isinstance(state, str) and state.startswith(("sha256:", "artifact:sha256:")):
            state = self._materialize_reference(
                self._pin_reference(state, None), target, allow_existing=False
            )
        if not isinstance(state, dict):
            raise ValidationError(
                "inference stateOutput must contain an object or artifact reference"
            )
        return state

    def _materialize_reference(
        self,
        pinned: dict[str, Any],
        target_root: Path,
        *,
        allow_existing: bool,
    ) -> dict[str, Any]:
        builder = ArtifactBuilder(self.local_store)
        paths: dict[str, str] = {}
        for role, digest in sorted(pinned["artifacts"].items()):
            manifest = self.local_store.read_manifest(str(digest))
            target = target_root / role
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                if not allow_existing:
                    raise IntegrityError("reference materialization target already exists")
                if not ArtifactBuilder.verify_restored(manifest, target):
                    raise IntegrityError("materialized reference differs from admitted artifact")
            else:
                builder.restore(manifest, target)
            paths[role] = str(target if manifest.is_directory else target / "payload")
        primary = {"release": "model", "checkpoint": "module-state", "artifact": "payload"}
        value: dict[str, Any] = {
            "resource": pinned["uri"],
            "kind": pinned["kind"],
            "artifacts": dict(pinned["artifacts"]),
            "paths": paths,
            "path": paths[primary[str(pinned["kind"])]],
        }
        if "protocol-state" in paths:
            value["state"] = json.loads(Path(paths["protocol-state"]).read_bytes())
        for key in ("modelPackageRef", "runRef"):
            if key in pinned:
                value[key] = pinned[key]
        return value

    def _resolve_stage_input(
        self,
        value: Any,
        target_root: Path,
        pinned_inputs: dict[str, dict[str, Any]],
        *,
        run_id: str,
        stage_name: str,
        pinned_references: dict[str, dict[str, Any]] | None = None,
        allow_existing: bool = False,
    ) -> Any:
        if not isinstance(value, str):
            return value
        stage_activity = f"run:{run_id}/stage:{stage_name}"
        if self._is_reference_input(value):
            pinned = (pinned_references or {}).get(value)
            if pinned is None:
                raise IntegrityError("reference input was not pinned at admission")
            self.lineage.add(
                LineageEdge(
                    str(pinned["uri"]),
                    stage_activity,
                    "used",
                    "entity",
                    "activity",
                    run_id=run_id,
                )
            )
            return self._materialize_reference(pinned, target_root, allow_existing=allow_existing)
        if not value.startswith("dataset/"):
            return value
        try:
            dataset = pinned_inputs[value]
        except KeyError as exc:
            raise IntegrityError("dataset input was not pinned at admission") from exc
        snapshot = self._snapshot_from_resource(dataset)
        self.lineage.add(
            LineageEdge(
                self._resource_uri(dataset),
                stage_activity,
                "used",
                "entity",
                "activity",
                run_id=run_id,
            )
        )
        if snapshot.mode == "copy" and snapshot.artifact is not None:
            target = target_root / dataset["metadata"]["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                if not allow_existing:
                    raise IntegrityError("dataset materialization target already exists")
                if not ArtifactBuilder.verify_restored(snapshot.artifact, target):
                    raise IntegrityError("materialized dataset differs from admitted artifact")
            else:
                ArtifactBuilder(self.local_store).restore(snapshot.artifact, target)
            return {
                "resource": self._resource_uri(dataset),
                "mode": snapshot.mode,
                "path": str(target if snapshot.artifact.is_directory else target / "payload"),
                "manifestDigest": snapshot.artifact.manifest_digest,
            }
        return {
            "resource": self._resource_uri(dataset),
            "mode": snapshot.mode,
            "path": snapshot.source,
        }

    def _verify_stage_outputs(self, outputs: dict[str, Any]) -> bool:
        verifier = ArtifactBuilder(self.local_store)
        for value in outputs.values():
            if isinstance(value, str) and value.startswith("sha256:"):
                try:
                    manifest = self.local_store.read_manifest(value)
                    if not verifier.verify_graph(manifest):
                        return False
                except OpenFoundryError:
                    return False
        return True

    def _record_run_admitted(self, resource: dict[str, Any]) -> None:
        admission = resource["spec"]["extensions"]
        self.events.append(
            type="RunAdmitted",
            source=f"openfoundry://{self.namespace}",
            subject=f"run/{resource['spec']['runId']}",
            resource_uid=resource["metadata"]["uid"],
            revision=resource["metadata"]["revision"],
            actor=self.actor,
            run_id=resource["spec"]["runId"],
            data={
                "workloadDigest": admission["workloadDigest"],
                "bindingDigest": admission["bindingDigest"],
            },
            dataschema="https://schemas.openfoundry.dev/events/run-admitted/v1",
            workload_digest=admission["workloadDigest"],
            binding_digest=admission["bindingDigest"],
            policy_digest=admission.get("policyDigest"),
            dedupe_revision=True,
        )

    def _run_state_event(
        self, resource: dict[str, Any], run_id: str, state: str, reason: str
    ) -> None:
        self.events.append(
            type="RunStateChanged",
            source=f"openfoundry://{self.namespace}",
            subject=f"run/{run_id}",
            resource_uid=resource["metadata"]["uid"],
            revision=resource["metadata"]["revision"],
            actor=self.actor,
            run_id=run_id,
            data={"state": state, "reason": reason},
            dataschema="https://schemas.openfoundry.dev/events/run-state/v1",
            dedupe_revision=True,
        )

    def _status_state(self, uid: str) -> str | None:
        try:
            return str(self.resources.get_status(uid)[0].get("state"))
        except NotFoundError:
            return None

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            {
                "runId": resource["spec"]["runId"],
                "state": self._status_state(resource["metadata"]["uid"]),
                "workload": resource["spec"]["workloadRef"],
                "createdAt": resource["metadata"]["createdAt"],
            }
            for resource in self.resources.latest(kind="Run")
        ]

    def list_releases(self) -> list[dict[str, Any]]:
        return self.publishing.list_releases()

    def show_release(self, name: str) -> dict[str, Any]:
        return self.publishing.show_release(name)

    def list_deployments(self) -> list[dict[str, Any]]:
        return self.deployments.list_deployments()

    def run_status(self, run_id: str) -> dict[str, Any]:
        status, version = self.resources.get_status(run_id)
        state_path = self.paths.runs / run_id / "state.json"
        return {
            "runId": run_id,
            "status": status,
            "statusVersion": version,
            "execution": json.loads(state_path.read_text()) if state_path.exists() else None,
        }

    def _resource_by_uri(self, kind: str, uri: str) -> dict[str, Any]:
        for resource in self.resources.list(kind=kind):
            if self._resource_uri(resource) == uri:
                return resource
        raise NotFoundError(f"pinned {kind} resource not found")

    def _run_resource(self, run_id: str) -> dict[str, Any]:
        matches = [
            resource
            for resource in self.resources.list(kind="Run")
            if resource["metadata"]["uid"] == run_id
        ]
        if len(matches) != 1:
            raise IntegrityError("run resource identity is ambiguous")
        return matches[0]

    def _run_result(self, run_id: str, status: dict[str, Any]) -> dict[str, Any]:
        reference = status.get("resultRef")
        if not isinstance(reference, str):
            raise IntegrityError("succeeded run has no immutable result")
        result = self._resource_by_uri("RunResult", reference)
        if result["spec"]["runRef"] != self._resource_uri(self._run_resource(run_id)):
            raise IntegrityError("run result does not identify the admitted run")
        return result

    def evaluate(self, subject: str) -> dict[str, Any]:
        return self.evaluation.evaluate(subject)

    def create_release(
        self,
        run_id: str,
        *,
        name: str,
        intended_use: str,
        limitations: list[str] | None = None,
        promote: bool = False,
        alias: str = "candidate",
        vulnerability_report: str | Path | None = None,
        evaluation_ref: str | None = None,
    ) -> dict[str, Any]:
        return self.publishing.create_release(
            run_id,
            name=name,
            intended_use=intended_use,
            limitations=limitations,
            promote=promote,
            alias=alias,
            vulnerability_report=vulnerability_report,
            evaluation_ref=evaluation_ref,
        )

    def promote_release(
        self, name: str, *, alias: str = "candidate", expected_version: int | None = None
    ) -> dict[str, Any]:
        return self.publishing.promote_release(name, alias=alias, expected_version=expected_version)

    def create_experiment(
        self,
        *,
        name: str,
        baseline_ref: str,
        candidate_ref: str,
        metric: str,
        direction: str,
    ) -> dict[str, Any]:
        return self.evaluation.create_experiment(
            name=name,
            baseline_ref=baseline_ref,
            candidate_ref=candidate_ref,
            metric=metric,
            direction=direction,
        )

    def release_evidence(self, run_id: str) -> dict[str, Any]:
        return self.publishing.release_evidence(run_id)

    def _evaluation_result(self, reference: str) -> dict[str, Any]:
        if reference.startswith("openfoundry://"):
            return self._resource_by_uri("EvaluationResult", reference)
        if reference.startswith("run/"):
            return self.find_resource("EvaluationResult", f"evaluation-{reference[4:]}")
        return self.find_resource("EvaluationResult", reference)

    def deploy(self, deployment_path: str | Path) -> dict[str, Any]:
        return self.deployments.deploy(deployment_path)

    def deployment_status(self, name: str) -> dict[str, Any]:
        return self.deployments.deployment_status(name)

    def cancel_deployment(self, name: str) -> dict[str, Any]:
        return self.deployments.cancel_deployment(name)

    def rollback_deployment(self, name: str, *, expected_version: int) -> dict[str, Any]:
        return self.deployments.rollback_deployment(name, expected_version=expected_version)

    def lineage_query(
        self, subject: str, *, direction: str = "upstream", max_depth: int = 100
    ) -> list[dict[str, Any]]:
        edges = (
            self.lineage.upstream(subject, max_depth=max_depth)
            if direction == "upstream"
            else self.lineage.downstream(subject, max_depth=max_depth)
        )
        return [asdict(edge) for edge in edges]

    def backup(self, destination: str | Path) -> dict[str, Any]:
        return create_backup(self.paths, self.db, self.identity, destination)

    @staticmethod
    def _resource_uri(resource: dict[str, Any]) -> str:
        metadata = resource["metadata"]
        return (
            f"openfoundry://{metadata['namespace']}/{resource['kind'].lower()}/"
            f"{metadata['name']}@{metadata['revision']}"
        )
