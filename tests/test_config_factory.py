import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import venv
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from openfoundry.artifacts import ArtifactBuilder
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.database import AliasRepository
from openfoundry.errors import (
    AuthorizationError,
    CapabilityError,
    ConfigurationError,
    ConflictError,
    IntegrityError,
    NotFoundError,
    OpenFoundryError,
    ValidationError,
)
from openfoundry.evaluation import EvaluationService
from openfoundry.executors import (
    EXECUTOR_API_VERSION,
    MODULE_PROTOCOL_CAPABILITIES,
    ExecutionPlan,
    ExecutorContext,
    ExecutorProvider,
    ExecutorRegistry,
    LocalExecutor,
)
from openfoundry.factory import Factory, _execution_plan_digest
from openfoundry.modules import load_manifest
from openfoundry.policy import PolicyDecision
from openfoundry.releases import promote_alias
from openfoundry.sdk import ProtocolRequest
from openfoundry.workloads import project_workload


def _project(tmp_path: Path) -> ProjectPaths:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "openfoundry.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Project",
                "metadata": {"name": "test-project", "namespace": "local/test-project"},
                "spec": {"owners": ["local-user"], "extensions": {}},
            }
        )
    )
    (root / "bindings").mkdir()
    shutil.copy(Path("bindings/local.yaml"), root / "bindings/local.yaml")
    shutil.copytree(Path("modules"), root / "modules")
    shutil.copytree(Path("workloads"), root / "workloads")
    (root / "data").mkdir()
    shutil.copy(Path("data/fixtures/numbers.jsonl"), root / "data/numbers.jsonl")
    return ProjectPaths(root)


@pytest.fixture
def paths(tmp_path: Path) -> ProjectPaths:
    project = _project(tmp_path)
    bootstrap(project)
    return project


_RIGHTS = {"license": "CC0-1.0", "trainingAllowed": True}


def _add_numbers(factory: Factory, paths: ProjectPaths, name: str = "example-numbers") -> dict:
    return factory.add_data(
        paths.root / "data/numbers.jsonl", name=name, mode="copy", rights=dict(_RIGHTS)
    )


def _add_affine(factory: Factory) -> dict:
    return factory.add_data(
        Path("data/fixtures/affine.jsonl").resolve(),
        name="example-affine",
        mode="copy",
        rights=dict(_RIGHTS),
    )


def _statistical(paths: ProjectPaths) -> tuple[Path, Path]:
    return paths.root / "workloads/example-statistical.yaml", paths.root / "bindings/local.yaml"


def _apply_affine_resources(factory: Factory) -> None:
    for source in ("model-packages/example-affine.yaml", "evaluations/example-affine.yaml"):
        factory.apply_resource_file(source)


def test_clean_clone_to_signed_release_and_edge_deployment(tmp_path):
    paths = _project(tmp_path)
    assert bootstrap(paths, plan=True)["actions"]
    assert bootstrap(paths)["ready"]
    assert bootstrap(paths)["actions"] == []
    with Factory(paths) as factory:
        assert factory.doctor()["ready"]
        _add_numbers(factory, paths)
        factory.add_store("secondary", driver="filesystem", endpoint=".openfoundry/secondary-store")
        planned = factory.sync("dataset/example-numbers", destination="secondary", plan=True)
        assert planned["plan"]["bytes"] > 0
        assert factory.sync("dataset/example-numbers", destination="secondary")["committed"]
        assert (
            factory.sync("dataset/example-numbers", destination="secondary", plan=True)["plan"][
                "missingChunks"
            ]
            == []
        )
        assert factory.validate_module(paths.root / "modules/examples/statistical/module.yaml")[
            "valid"
        ]
        assert (
            factory.test_module(paths.root / "modules/examples/statistical/module.yaml")["passed"]
            == 1
        )
        run = factory.run(*_statistical(paths))
        assert run["state"] == "Succeeded"
        assert factory.operations.get(run["operationId"])["state"] == "succeeded"
        assert factory.operations.get(run["operationId"])["result"]["runId"] == run["runId"]
        assert run["outputs"]["train.mean"] == 3.0
        evaluation = factory.evaluate(f"run/{run['runId']}")
        assert evaluation["spec"]["scores"]["passed"]
        run_status = factory.run_status(run["runId"])
        scan_path = paths.root / "vulnerability-report.yaml"
        scan_path.write_text(
            yaml.safe_dump(
                {
                    "scanner": {"name": "test-scanner", "version": "1"},
                    "databaseRevision": "test-db-1",
                    "generatedAt": "2026-09-01T00:00:00Z",
                    "subjects": [
                        run["outputs"]["train.model"],
                        *run_status["execution"]["digests"]["modules"].values(),
                    ],
                    "findings": [],
                    "waivers": [],
                }
            )
        )
        unscanned = factory.create_release(
            run["runId"],
            name="unscanned",
            intended_use="test",
            promote=True,
        )
        assert (
            unscanned["spec"]["extensions"]["manifest"]["vulnerabilities"]["status"]
            == "not-scanned"
        )
        conflicting_evaluation = deepcopy(evaluation)
        conflicting_evaluation["metadata"] = {
            "name": "conflicting-evaluation",
            "namespace": "local/test-project",
        }
        conflicting_evaluation["spec"]["scores"]["passed"] = False
        conflicting_evaluation["spec"]["extensions"]["passed"] = False
        with pytest.raises(ValidationError, match="factory coordinator"):
            factory.apply_resource(conflicting_evaluation)
        release = factory.create_release(
            run["runId"],
            name="release-one",
            intended_use="test",
            promote=True,
            vulnerability_report=scan_path,
            evaluation_ref=evaluation["metadata"]["revision"],
        )
        assert factory.publishing.promotion_decision(release).outcome == "allow"
        assert release["spec"]["extensions"]["manifest"]["vulnerabilities"]["status"] == "passed"
        assert release["spec"]["extensions"]["manifest"]["provenance"][
            "runRef"
        ] == factory._resource_uri(factory._run_resource(run["runId"]))
        assert release["spec"]["extensions"]["manifest"]["compatibility"]["passed"] is True
        assert (
            release["spec"]["extensions"]["manifest"]["compatibility"]["evaluationRevision"]
            == evaluation["metadata"]["revision"]
        )
        assert AliasRepository(factory.db).get("candidate")[1] == release["metadata"]["revision"]
        release_uri = factory._resource_uri(release)
        release_lineage = factory.lineage_query(release_uri)
        assert any(edge["source"] == f"run:{run['runId']}" for edge in release_lineage)
        assert any(edge["source"].startswith("artifact:sha256:") for edge in release_lineage)
        deployment = {
            "apiVersion": "openfoundry.dev/v1alpha1",
            "kind": "DeploymentSpec",
            "metadata": {"name": "edge-demo", "namespace": "local/test-project"},
            "spec": {
                "releaseRef": "release/release-one",
                "extensions": {"form": "edge"},
            },
        }
        deployment_path = paths.root / "deployment.yaml"
        deployment_path.write_text(yaml.safe_dump(deployment))
        edge_deployment = factory.deploy(deployment_path)
        assert edge_deployment["state"] == "packaged"
        assert any(
            edge["source"] == release_uri
            for edge in factory.lineage_query(factory._resource_uri(edge_deployment["deployment"]))
        )

        service = {**deployment, "metadata": {**deployment["metadata"], "name": "service-demo"}}
        service["spec"] = {
            **deployment["spec"],
            "extensions": {
                "form": "service",
                "command": ["python3", "-c", "pass"],
            },
        }
        service_path = paths.root / "service-deployment.yaml"
        service_path.write_text(yaml.safe_dump(service))
        assert factory.deploy(service_path)["state"] == "running"
        for _ in range(100):
            service_status = factory.deployment_status("service-demo")
            if service_status["status"]["state"] != "running":
                break
            time.sleep(0.02)
        assert service_status["status"]["state"] == "succeeded"
        assert service_status["status"]["executor"] == "local"
        first_deployment_revision = service_status["status"]["deploymentRevision"]

        service["spec"]["extensions"]["command"] = ["python3", "-c", "print('revision-two')"]
        service_path.write_text(yaml.safe_dump(service))
        factory.deploy(service_path)
        for _ in range(100):
            service_status = factory.deployment_status("service-demo")
            if service_status["status"]["state"] != "running":
                break
            time.sleep(0.02)
        second_deployment_revision = service_status["status"]["deploymentRevision"]
        assert second_deployment_revision != first_deployment_revision
        rolled_back = factory.rollback_deployment(
            "service-demo", expected_version=service_status["statusVersion"]
        )
        assert rolled_back["status"]["deploymentRevision"] == first_deployment_revision
        for _ in range(100):
            rolled_back = factory.deployment_status("service-demo")
            if rolled_back["status"]["state"] != "running":
                break
            time.sleep(0.02)
        assert rolled_back["status"]["state"] == "succeeded"

        rolled_forward = factory.deploy(service_path)
        assert rolled_forward["deployment"]["metadata"]["revision"] == second_deployment_revision
        for _ in range(100):
            rolled_forward = factory.deployment_status("service-demo")
            if rolled_forward["status"]["state"] != "running":
                break
            time.sleep(0.02)
        assert rolled_forward["status"]["state"] == "succeeded"

        service["metadata"]["name"] = "cancel-demo"
        service["spec"]["extensions"]["command"] = [
            "python3",
            "-c",
            "import time; time.sleep(30)",
        ]
        service_path.write_text(yaml.safe_dump(service))
        factory.deploy(service_path)
        assert factory.cancel_deployment("cancel-demo")["status"]["state"] == "canceled"

    with Factory(paths) as restarted:
        assert restarted.deployment_status("service-demo")["status"]["state"] == "succeeded"


def test_non_local_binding_is_not_silently_executed_locally(paths):
    binding = yaml.safe_load((paths.root / "bindings/local.yaml").read_text())
    binding["metadata"]["name"] = "cluster"
    binding["spec"]["executor"] = "cluster-provider"
    binding_path = paths.root / "bindings/cluster.yaml"
    binding_path.write_text(yaml.safe_dump(binding))

    with Factory(paths) as factory:
        with pytest.raises(CapabilityError, match="unknown executor provider") as failure:
            factory.run(paths.root / "workloads/example-statistical.yaml", binding_path)
        assert failure.value.details["available"] == ["local"]
        assert factory.list_resources(kind="Run") == []
        assert factory.operations.list() == []
        assert list(paths.runs.iterdir()) == []


def test_injected_executor_runs_unchanged_workload(paths):
    binding = yaml.safe_load((paths.root / "bindings/local.yaml").read_text())
    binding["metadata"]["name"] = "remote"
    binding["spec"]["executor"] = "test-remote"
    binding_path = paths.root / "bindings/remote.yaml"
    binding_path.write_text(yaml.safe_dump(binding))

    class RecordingExecutor(LocalExecutor):
        def plan(self, **kwargs):
            self.planned = True
            return super().plan(**kwargs)

    created: list[RecordingExecutor] = []

    def create(_context: ExecutorContext) -> LocalExecutor:
        executor = RecordingExecutor()
        executor.planned = False
        created.append(executor)
        return executor

    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "test-remote",
            EXECUTOR_API_VERSION,
            create,
            capabilities=MODULE_PROTOCOL_CAPABILITIES | frozenset({"isolation:network-deny"}),
        )
    )
    with Factory(paths, executors=registry) as factory:
        _add_numbers(factory, paths)
        result = factory.run(paths.root / "workloads/example-statistical.yaml", binding_path)
    assert result["state"] == "Succeeded"
    assert len(created) == 2
    assert not created[0].planned
    assert created[1].planned


def test_stable_executor_plugin_acceptance(tmp_path, monkeypatch):
    site = tmp_path / "site"
    info = site / "openfoundry_stable_executor_test_plugin-1.0.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: openfoundry-stable-executor-test-plugin\nVersion: 1.0.0\n"
    )
    (info / "entry_points.txt").write_text(
        "[openfoundry.executors]\nstable-test = openfoundry_stable_executor:provider\n"
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(Path("tests/fixtures/executor_plugin/src").resolve()))
    registry = ExecutorRegistry()
    registry.discover()
    assert registry.catalog() == {
        "apiVersion": EXECUTOR_API_VERSION,
        "entryPointGroup": "openfoundry.executors",
        "providers": [
            {
                "name": "stable-test",
                "apiVersion": EXECUTOR_API_VERSION,
                "source": (
                    "entry-point:openfoundry-stable-executor-test-plugin:openfoundry_stable_executor:provider"
                ),
                "description": "Independent acceptance-test executor.",
                "capabilities": sorted(
                    MODULE_PROTOCOL_CAPABILITIES
                    | frozenset({"isolation:network-deny", "recovery:attach"})
                ),
                "configContract": {
                    "type": "object",
                    "properties": {
                        "interruptStatusOnce": {"type": "boolean"},
                        "interruptSubmitOnce": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            }
        ],
    }
    with pytest.raises(CapabilityError, match="unknown executor provider") as missing:
        registry.resolve(
            "local",
            project_root=tmp_path,
            state_root=tmp_path / ".openfoundry",
            actor="tester",
            declaration={},
        )
    assert missing.value.details["available"] == ["stable-test"]

    paths = _project(tmp_path)
    stable_binding = yaml.safe_load((paths.root / "bindings/local.yaml").read_text())
    stable_binding["metadata"]["name"] = "stable-test"
    stable_binding["spec"]["executor"] = "stable-test"
    stable_binding["spec"]["config"] = {"interruptStatusOnce": True}
    stable_binding_path = paths.root / "bindings/stable-test.yaml"
    stable_binding_path.write_text(yaml.safe_dump(stable_binding))
    bootstrap(paths)

    with Factory(paths) as local:
        _add_numbers(local, paths)
        local_run = local.run(*_statistical(paths))
    with Factory(paths, executors=registry) as external:
        assert external.executor_preflight(
            stable_binding_path,
            workload_path=paths.root / "workloads/example-statistical.yaml",
        )["ready"]
        operation = external.create_run_operation(
            paths.root / "workloads/example-statistical.yaml", stable_binding_path
        )
        with pytest.raises(KeyboardInterrupt, match="after durable submit"):
            external.execute_run_operation(operation["id"])
    interrupted_record = next(paths.runs.rglob("stable-execution.json"))
    interrupted_id = str(json.loads(interrupted_record.read_text())["id"])
    assert (
        interrupted_record.parent / "stable-submit-attempts.jsonl"
    ).read_text().splitlines() == [interrupted_id]

    restarted_registry = ExecutorRegistry()
    restarted_registry.discover()
    with Factory(paths, executors=restarted_registry) as restarted:
        completed = restarted.execute_run_operation(operation["id"])
    plugin_run = completed["result"]
    assert json.loads(interrupted_record.read_text())["id"] == interrupted_id
    assert (
        interrupted_record.parent / "stable-submit-attempts.jsonl"
    ).read_text().splitlines() == [interrupted_id]

    assert plugin_run["state"] == "Succeeded"
    assert plugin_run["outputs"]["train.mean"] == local_run["outputs"]["train.mean"] == 3.0
    assert plugin_run["outputs"]["evaluate.passed"]
    assert plugin_run["outputs"]["train.model"].startswith("sha256:")
    execution_records = sorted(paths.runs.rglob("stable-execution.json"))
    assert len(execution_records) == 2
    for record_path in execution_records:
        run_dir = record_path.parent
        plan = json.loads((run_dir / "stable-plan.json").read_text())
        assert plan["environmentDigest"].startswith("sha256:")
        assert Path(plan["cwd"]).is_relative_to(paths.runs)
        assert (run_dir / "request.json").is_file()
        assert (run_dir / "result.json").is_file()
        assert (run_dir / "stable-submit-attempts.jsonl").read_text().splitlines() == [
            str(json.loads(record_path.read_text())["id"])
        ]

    recover_binding = deepcopy(stable_binding)
    recover_binding["metadata"]["name"] = "stable-test-recover"
    recover_binding["spec"]["config"] = {"interruptSubmitOnce": True}
    recover_binding_path = paths.root / "bindings/stable-test-recover.yaml"
    recover_binding_path.write_text(yaml.safe_dump(recover_binding))
    with Factory(paths, executors=restarted_registry) as external:
        recover_operation = external.create_run_operation(
            paths.root / "workloads/example-statistical.yaml", recover_binding_path
        )
        with pytest.raises(KeyboardInterrupt, match="during provider submit"):
            external.execute_run_operation(recover_operation["id"])
    recover_records = set(paths.runs.rglob("stable-execution.json")) - set(execution_records)
    assert len(recover_records) == 1
    recover_record = recover_records.pop()
    recover_id = str(json.loads(recover_record.read_text())["id"])
    assert (recover_record.parent / "stable-submit-attempts.jsonl").read_text().splitlines() == [
        recover_id
    ]

    recovered_registry = ExecutorRegistry()
    recovered_registry.discover()
    with Factory(paths, executors=recovered_registry) as recovered:
        recovered_run = recovered.execute_run_operation(recover_operation["id"])["result"]
    assert recovered_run["outputs"]["train.mean"] == plugin_run["outputs"]["train.mean"]
    assert len(list(paths.runs.rglob("stable-execution.json"))) == 4
    assert json.loads(recover_record.read_text())["id"] == recover_id
    assert (recover_record.parent / "stable-submit-attempts.jsonl").read_text().splitlines() == [
        recover_id
    ]

    resolved = registry.resolve(
        "stable-test",
        project_root=paths.root,
        state_root=paths.state,
        actor="tester",
        declaration=stable_binding,
    )
    assert resolved.executor.recover(tmp_path / "never-submitted") is None
    completed_dir = execution_records[0].parent
    completed_id = str(json.loads(execution_records[0].read_text())["id"])
    assert resolved.executor.recover(completed_dir) == completed_id

    log_dir = paths.runs / "plugin-logs"
    logging_executor = resolved.executor
    log_id = logging_executor.submit(
        logging_executor.plan(
            argv=[
                "python3",
                "-c",
                "import sys; print('A' * 64); print('B' * 64, file=sys.stderr)",
            ],
            run_dir=log_dir,
            cwd=paths.root,
            requires_result=False,
        )
    )
    while logging_executor.status(log_id).state in {"pending", "running"}:
        time.sleep(0.01)
    log_reader = registry.resolve(
        "stable-test",
        project_root=paths.root,
        state_root=paths.state,
        actor="tester",
        declaration=stable_binding,
    ).executor
    log_reader.attach(log_id, log_dir)
    stdout, stderr = log_reader.read_logs(log_id, tail_bytes=16)
    assert stdout == "A" * 15 + "\n"
    assert stderr == "B" * 15 + "\n"

    indeterminate = paths.runs / "plugin-indeterminate"
    indeterminate.mkdir()
    (indeterminate / "stable-execution.json").write_text(
        json.dumps({"id": "unknown", "state": "launching"})
    )
    with pytest.raises(RuntimeError, match="indeterminate"):
        resolved.executor.recover(indeterminate)

    cancel_dir = paths.runs / "plugin-cancel"
    original = resolved.executor
    execution_id = original.submit(
        original.plan(
            argv=["python3", "-c", "import time; time.sleep(30)"],
            run_dir=cancel_dir,
            cwd=paths.root,
            requires_result=False,
        )
    )
    attached = registry.resolve(
        "stable-test",
        project_root=paths.root,
        state_root=paths.state,
        actor="tester",
        declaration=stable_binding,
    ).executor
    attached.attach(execution_id, cancel_dir)
    assert attached.status(execution_id).state == "running"
    attached.cancel(execution_id)
    assert attached.status(execution_id).state == "canceled"
    assert attached.recover(cancel_dir) == execution_id
    deadline = time.monotonic() + 5
    while original.status(execution_id).state == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert original.status(execution_id).state == "canceled"


def test_opaque_dependency_lock_reaches_provider_without_core_interpretation(paths, tmp_path):
    module_path = paths.root / "modules/examples/statistical/module.yaml"
    lock = b"\x00provider-specific\xff\n"
    (module_path.parent / "requirements.lock").write_bytes(lock)
    module = yaml.safe_load(module_path.read_text())
    module["spec"]["environment"]["dependencyDigest"] = "sha256:" + hashlib.sha256(lock).hexdigest()
    module_path.write_text(yaml.safe_dump(module))
    binding = yaml.safe_load((paths.root / "bindings/local.yaml").read_text())
    binding["spec"]["executor"] = "opaque-lock"
    binding_path = paths.root / "bindings/opaque-lock.yaml"
    binding_path.write_text(yaml.safe_dump(binding))
    observed = []

    class OpaqueLockExecutor(LocalExecutor):
        def prepare_environment(self, *, argv, cwd, dependency, deny_network=False):
            del cwd, deny_network
            observed.append(dependency)
            return {
                "command": list(argv),
                "dependencyDigest": dependency.digest,
                "digest": "sha256:" + "2" * 64,
            }

    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "opaque-lock",
            EXECUTOR_API_VERSION,
            lambda _context: OpaqueLockExecutor(),
            capabilities=MODULE_PROTOCOL_CAPABILITIES | frozenset({"isolation:network-deny"}),
        )
    )
    with Factory(paths, executors=registry) as factory:
        validation = factory.validate_module(module_path)
        report = factory.executor_preflight(
            binding_path, workload_path=paths.root / "workloads/example-statistical.yaml"
        )

    assert validation["dependencyLock"]["size"] == len(lock)
    assert report["ready"]
    assert len(observed) == 2
    assert all(item.relative_path == "requirements.lock" for item in observed)
    assert all(item.contents == lock for item in observed)
    assert all(
        item.digest == module["spec"]["environment"]["dependencyDigest"] for item in observed
    )


@pytest.mark.parametrize(
    ("descriptor", "message"),
    [
        (
            {"command": [], "dependencyDigest": "sha256:x", "digest": "sha256:" + "0" * 64},
            "command argv",
        ),
        (
            {
                "command": ["python3"],
                "dependencyDigest": "sha256:x",
                "digest": "sha256:" + "0" * 64,
            },
            "changed the dependency lock",
        ),
        (
            {"command": ["python3"], "dependencyDigest": "MATCH", "digest": "invalid"},
            "canonical digest",
        ),
        (
            {
                "command": ["python3"],
                "dependencyDigest": "MATCH",
                "digest": "sha256:" + "0" * 64,
                "opaque": b"not-json",
            },
            "canonical JSON",
        ),
    ],
)
def test_provider_environment_descriptor_is_centrally_validated(paths, descriptor, message):
    module_path = paths.root / "modules/examples/statistical/module.yaml"

    class DescriptorExecutor(LocalExecutor):
        def prepare_environment(self, **_kwargs):
            return {
                key: (
                    yaml.safe_load(module_path.read_text())["spec"]["environment"][
                        "dependencyDigest"
                    ]
                    if value == "MATCH"
                    else value
                )
                for key, value in descriptor.items()
            }

    with Factory(paths) as factory:
        manifest, code_root = load_manifest(module_path, paths.root)
        with pytest.raises(IntegrityError, match=message):
            factory._prepare_module_environment(DescriptorExecutor(), manifest, code_root)


def test_run_pins_dataset_revision_before_execution(paths):
    with Factory(paths) as factory:
        first = _add_numbers(factory, paths)
        workload = yaml.safe_load((paths.root / "workloads/example-statistical.yaml").read_text())
        pinned = factory._pin_stage_inputs(project_workload(workload).stages)

        (paths.root / "data/numbers.jsonl").write_text('{"value": 99}\n')
        second = _add_numbers(factory, paths)

        assert first["metadata"]["revision"] != second["metadata"]["revision"]
        assert (
            pinned["dataset/example-numbers"]["metadata"]["revision"]
            == first["metadata"]["revision"]
        )


def test_run_rejects_non_copy_dataset_before_allocation(paths):
    with Factory(paths) as factory:
        factory.add_data(
            paths.root / "data/numbers.jsonl",
            name="example-numbers",
            mode="register",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        with pytest.raises(CapabilityError, match="only copied dataset snapshots"):
            factory.run(*_statistical(paths))
        assert factory.list_resources(kind="Run") == []
        assert list(paths.runs.iterdir()) == []


def test_module_manifest_revision_changes_admitted_source_identity(paths):
    manifest_path = paths.root / "modules/examples/statistical/module.yaml"
    with Factory(paths) as factory:
        first = factory.validate_module(manifest_path)
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["spec"]["extensions"] = {"sourceRef": "repository:modules/examples/statistical-v2"}
        manifest_path.write_text(yaml.safe_dump(manifest))
        second = factory.validate_module(manifest_path)

    assert first["artifactManifest"] != second["artifactManifest"]


def test_module_test_executes_inside_symlink_virtual_environment(paths, tmp_path, monkeypatch):
    environment_path = tmp_path / "venv"
    venv.EnvBuilder(symlinks=True, with_pip=False, system_site_packages=True).create(
        environment_path
    )
    python = environment_path / "bin" / "python3"
    if not python.is_symlink():
        pytest.skip("this platform does not create symlink interpreters")
    module_dir = paths.root / "modules/examples/statistical"
    (module_dir / "main.py").write_text(
        "import json, os, sys\n"
        f"EXPECTED = {str(environment_path)!r}\n"
        "if sys.prefix != EXPECTED:\n"
        "    raise SystemExit(f'wrong interpreter environment: {sys.prefix}')\n"
        "with open(os.environ['OPENFOUNDRY_RESULT_FILE'], 'w') as stream:\n"
        "    json.dump({'protocol': 'openfoundry.module/v1', 'status': 'ok'}, stream)\n"
    )
    monkeypatch.setenv("PATH", f"{environment_path / 'bin'}{os.pathsep}{os.environ['PATH']}")

    with Factory(paths) as factory:
        report = factory.test_module(module_dir / "module.yaml")

    assert report["passed"] == 1


def test_run_realizes_module_dependency_lock_from_binding_wheelhouse(tmp_path):
    from _wheels import build_wheel, lock_for

    paths = _project(tmp_path)
    bootstrap(paths)
    wheelhouse = paths.root / "wheels"
    _wheel, wheel_digest = build_wheel(wheelhouse)
    lock = lock_for("openfoundrytiny", "1.0", wheel_digest)
    module_dir = paths.root / "modules/locked"
    shutil.copytree(paths.root / "modules/examples/statistical", module_dir)
    (module_dir / "requirements.lock").write_bytes(lock)
    manifest = yaml.safe_load((module_dir / "module.yaml").read_text())
    manifest["metadata"]["name"] = "locked"
    manifest["spec"]["environment"]["dependencyDigest"] = (
        "sha256:" + hashlib.sha256(lock).hexdigest()
    )
    manifest["spec"]["extensions"] = {"sourceRef": "repository:modules/locked"}
    (module_dir / "module.yaml").write_text(yaml.safe_dump(manifest))
    (module_dir / "main.py").write_text(
        "import openfoundrytiny\n"
        "from openfoundry.sdk import ProtocolResult, main\n"
        "def validate(_request):\n"
        "    return ProtocolResult(status='ok')\n"
        "def run(_request):\n"
        "    return ProtocolResult(status='ok', "
        "outputs={'openfoundrytiny': openfoundrytiny.VERSION})\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main({'validate': validate, 'run': run}))\n"
    )
    binding = yaml.safe_load((paths.root / "bindings/local.yaml").read_text())
    binding["spec"]["config"] = {
        "dependencyWheelhouse": "wheels",
        "dependencyIndex": False,
    }
    binding_path = paths.root / "bindings/wheelhouse.yaml"
    binding_path.write_text(yaml.safe_dump(binding))
    workload_path = paths.root / "workloads/locked.yaml"
    workload_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "WorkloadSpec",
                "metadata": {"name": "locked", "namespace": "local/test-project"},
                "spec": {
                    "graph": {
                        "stages": [
                            {
                                "name": "train",
                                "module": "modules/locked/module.yaml",
                                "operation": "run",
                                "outputs": ["openfoundrytiny"],
                            }
                        ]
                    },
                },
            }
        )
    )

    with Factory(paths) as factory:
        tested = factory.test_module(module_dir / "module.yaml", binding_path=binding_path)
        result = factory.run(workload_path, binding_path)
        admission = factory._run_resource(result["runId"])["spec"]["extensions"]

    assert tested["passed"] == 1
    assert result["state"] == "Succeeded"
    assert result["outputs"]["train.openfoundrytiny"] == "1.0"
    realization = admission["environments"]["train"]["realization"]
    assert realization["strategy"] == "venv"
    assert realization["options"] == {"index": False, "wheelhouse": str(wheelhouse)}
    assert len(list(paths.environments.glob("*/openfoundry-environment.json"))) == 1


def _scan_for(paths: ProjectPaths, factory: Factory, run: dict) -> Path:
    admission = factory._run_resource(run["runId"])["spec"]["extensions"]
    subjects = [run["outputs"]["train.model"], *admission["moduleDigests"].values()]
    if admission.get("inferenceAdapter"):
        subjects.append(admission["inferenceAdapter"]["sourceDigest"])
    scan_path = paths.root / f"scan-{run['runId']}.yaml"
    scan_path.write_text(
        yaml.safe_dump(
            {
                "scanner": {"name": "test-scanner", "version": "1"},
                "databaseRevision": "test-db-1",
                "generatedAt": "2026-09-01T00:00:00Z",
                "subjects": subjects,
                "findings": [],
                "waivers": [],
            }
        )
    )
    return scan_path


def test_alias_promotion_moves_between_releases(paths):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        run = factory.run(*_statistical(paths))
        factory.evaluate(f"run/{run['runId']}")
        scan_path = _scan_for(paths, factory, run)
        first = factory.create_release(
            run["runId"],
            name="release-one",
            intended_use="test",
            promote=True,
            vulnerability_report=scan_path,
        )
        aliases = AliasRepository(factory.db)
        assert aliases.get("candidate") == (
            first["metadata"]["uid"],
            first["metadata"]["revision"],
            1,
        )
        second = factory.create_release(
            run["runId"],
            name="release-two",
            intended_use="test",
            promote=True,
            vulnerability_report=scan_path,
        )
        assert aliases.get("candidate") == (
            second["metadata"]["uid"],
            second["metadata"]["revision"],
            2,
        )
        with pytest.raises(ConflictError, match="alias version mismatch"):
            promote_alias(
                factory.db,
                factory.events,
                name="candidate",
                uid=first["metadata"]["uid"],
                revision=first["metadata"]["revision"],
                expected_version=1,
                actor="tester",
                policy_decision=PolicyDecision("allow", "sha256:policy", ()),
            )
        assert aliases.get("candidate")[2] == 2
        moved = list(factory.events.query(type="AliasMoved"))
        assert [event.data["version"] for event in moved] == [1, 2]


def _committed_policy_project(tmp_path: Path, *, dirty_worktree: str = "deny") -> ProjectPaths:
    paths = _project(tmp_path)
    (paths.root / ".gitignore").write_text(".openfoundry/\n")
    (paths.root / "policies").mkdir()
    policy = yaml.safe_load(Path("policies/default.yaml").read_text())
    policy["metadata"]["namespace"] = "local/test-project"
    policy["spec"]["rules"][0]["match"]["resource"] = "local/test-project"
    policy["spec"]["config"]["dirtyWorktree"] = dirty_worktree
    (paths.root / "policies/default.yaml").write_text(yaml.safe_dump(policy))
    for command in (
        ["git", "config", "user.name", "t"],
        ["git", "config", "user.email", "t@t"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "Commit the project"],
    ):
        subprocess.run(command, cwd=paths.root, check=True)
    bootstrap(paths)
    return paths


def test_policy_directory_governs_admission_actors_and_worktree(tmp_path):
    paths = _committed_policy_project(tmp_path)
    workload, binding = _statistical(paths)
    with Factory(paths) as factory:
        checks = {item["name"]: item for item in factory.doctor()["checks"]}
        assert checks["policy"]["status"] == "pass"
        assert factory.policy.enforced
        _add_numbers(factory, paths)
        run = factory.run(workload, binding)
        admission = factory._run_resource(run["runId"])["spec"]["extensions"]
        assert admission["policyDigest"] == factory.policy.digest
        assert admission["worktree"]["dirty"] is False
        assert admission["worktree"]["commit"]
        assert admission["worktree"]["policy"] == "deny"
        admitted = next(iter(factory.events.query(run_id=run["runId"], type="RunAdmitted")))
        assert admitted.policy_digest == factory.policy.digest
        assert factory.operations.get(run["operationId"])["request"]["worktree"]["dirty"] is False

        (paths.root / "scratch.txt").write_text("uncommitted\n")
        with pytest.raises(ValidationError, match="dirty worktree"):
            factory.run(workload, binding)
        assert len(factory.operations.list()) == 1

        policy_path = paths.root / "policies/default.yaml"
        policy = yaml.safe_load(policy_path.read_text())
        policy["spec"]["config"]["dirtyWorktree"] = "archive"
        policy_path.write_text(yaml.safe_dump(policy))
        assert factory.policy.dirty_worktree == "archive"
        archived = factory.run(workload, binding)
        worktree = factory._run_resource(archived["runId"])["spec"]["extensions"]["worktree"]
        assert worktree["dirty"] is True
        assert worktree["policy"] == "archive"
        assert "scratch.txt" in worktree["untracked"]
        assert worktree["untrackedCount"] == 1
        patch = factory.local_store.read_manifest(worktree["patchArtifact"])
        assert patch.logical_kind == "worktree-patch"
        assert patch.provenance["commit"] == worktree["commit"]

        policy["spec"]["config"]["retention"] = {"days": 1}
        policy_path.write_text(yaml.safe_dump(policy))
        with pytest.raises(ConfigurationError, match="not enforced"):
            factory.run(workload, binding)
        checks = {item["name"]: item for item in factory.doctor()["checks"]}
        assert checks["policy"]["status"] == "fail"
        del policy["spec"]["config"]["retention"]
        policy_path.write_text(yaml.safe_dump(policy))

    with Factory(paths, actor="stranger") as stranger:
        with pytest.raises(AuthorizationError, match="policy denies actor 'stranger'"):
            stranger.run(workload, binding)
        with pytest.raises(AuthorizationError):
            _add_numbers(stranger, paths, name="more")
        with pytest.raises(AuthorizationError):
            stranger.revoke_data("example-numbers", reason="not allowed")
        denials = list(stranger.events.query(type="PolicyDecisionRecorded"))
        assert denials
        assert denials[-1].data["outcome"] == "deny"
        assert denials[-1].actor == "stranger"
        assert stranger.find_resource("DatasetSnapshot", "example-numbers")["spec"]["rights"][
            "trainingAllowed"
        ]


def _affine_project(tmp_path: Path) -> tuple[ProjectPaths, Path]:
    paths = _project(tmp_path)
    workload_path = paths.root / "workloads/example-from-scratch.yaml"
    bootstrap(paths)
    with Factory(paths) as factory:
        _apply_affine_resources(factory)
        _add_affine(factory)
    return paths, workload_path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("directory_model", [False, True])
def test_service_deployment_serves_release_through_admitted_adapter(tmp_path, directory_model):
    paths, workload_path = _affine_project(tmp_path)
    if directory_model:
        trainer = paths.root / "modules/examples/affine-regression/main.py"
        trainer.write_text(
            trainer.read_text()
            .replace(
                "        return ProtocolResult(\n",
                "        weights = model_path.parent / 'weights'\n"
                "        weights.mkdir()\n"
                "        (weights / 'model.json').write_bytes(model_path.read_bytes())\n"
                "        (weights / 'payload').write_text('directory metadata')\n"
                "        return ProtocolResult(\n",
                1,
            )
            .replace(
                '{"name": "model", "kind": "model", "path": model_path.name}',
                '{"name": "classifier", "kind": "model", "path": "weights"}',
            )
        )
        workload = yaml.safe_load(workload_path.read_text())
        workload["spec"]["graph"]["stages"][0]["outputs"].remove("model")
        workload["spec"]["graph"]["stages"][0]["outputs"].append("classifier")
        workload_path.write_text(yaml.safe_dump(workload))
    port = _free_port()
    with Factory(paths) as factory:
        if directory_model:
            package = yaml.safe_load(Path("model-packages/example-affine.yaml").read_text())
            package["spec"]["adapters"]["inferenceReference"]["stateOutput"] = "train.classifier"
            factory.apply_resource(package)
        run = factory.run(workload_path, paths.root / "bindings/local.yaml")
        evaluation = factory.evaluate(f"run/{run['runId']}")
        assert evaluation["spec"]["extensions"]["compatibilityPassed"]
        release = factory.create_release(
            run["runId"],
            name="affine-v1",
            intended_use="test",
            promote=True,
        )
        (paths.root / "modules/examples/affine-serving/main.py").write_text(
            "raise RuntimeError('live serving source must not execute')\n"
        )
        shutil.rmtree(paths.runs / run["runId"])
        deployment_path = paths.root / "service.yaml"
        deployment_path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "DeploymentSpec",
                    "metadata": {"name": "affine-service", "namespace": "local/test-project"},
                    "spec": {
                        "releaseRef": "release/affine-v1",
                        "extensions": {"form": "service", "port": port},
                    },
                }
            )
        )
        applied = factory.deploy(deployment_path)
        assert applied["state"] == "running"
        status = factory.deployment_status("affine-service")["status"]
        assert status["endpoint"] == f"http://127.0.0.1:{port}"
        serving = json.loads((Path(status["runDirectory"]) / "serving.json").read_text())
        weights = Path(serving["state"]["path"])
        assert weights.is_relative_to(status["runDirectory"])
        assert weights.is_dir() == directory_model
        output_name = "train.classifier" if directory_model else "train.model"
        assert serving["state"]["artifacts"]["payload"] == run["outputs"][output_name]
        model_file = weights / "model.json" if directory_model else weights
        assert json.loads(model_file.read_text()) == run["outputs"]["train.modelState"]
        assert (
            serving["modelPackageRef"]
            == (release["spec"]["extensions"]["manifest"]["modelPackage"]["ref"])
        )
        assert serving["cwd"].startswith(status["runDirectory"])

        with httpx.Client(base_url=status["endpoint"], timeout=5.0) as client:
            health = None
            for _ in range(300):
                try:
                    health = client.get("/healthz")
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                assert factory.deployment_status("affine-service")["status"]["state"] == "running"
                time.sleep(0.1)
            assert health is not None, "endpoint never became healthy"
            assert health.status_code == 200, "endpoint never became healthy"
            assert health.json()["release"] == release["metadata"]["revision"]
            inference = client.post("/v1/infer", json={"inputs": {"input": 3.0}})
            assert inference.status_code == 200, inference.text
            body = inference.json()
            assert body["outputs"]["prediction"] == pytest.approx(7.0, abs=0.01)
            assert body["release"] == release["metadata"]["revision"]
            invalid = client.post("/v1/infer", json={"inputs": {"input": "three"}})
            assert invalid.status_code == 400
            assert "three" not in invalid.text
            unknown = client.post("/v1/infer", json={"inputs": {"input": 1.0, "extra": 2}})
            assert unknown.status_code == 400
            assert client.get("/healthz").json()["requests"] == 1

        canceled = factory.cancel_deployment("affine-service")
        assert canceled["status"]["state"] == "canceled"

        restarted = factory.deploy(deployment_path)
        assert restarted["state"] == "running"
        restarted_status = factory.deployment_status("affine-service")["status"]
        assert restarted_status["runDirectory"] != status["runDirectory"]
        assert restarted_status["previousDeploymentRevision"] is None
        factory.cancel_deployment("affine-service")

        deployment_path.write_text(
            deployment_path.read_text().replace("form: service", "form: batch")
        )
        with pytest.raises(ValidationError, match=r"require extensions\.command"):
            factory.deploy(deployment_path)


def test_copied_dataset_directory_keeps_payload_member(paths):
    source = paths.root / "data/directory"
    source.mkdir()
    (source / "payload").write_text("data")
    (source / "schema.json").write_text('{"type": "text"}')
    with Factory(paths) as factory:
        snapshot = factory.add_data(source, name="directory", mode="copy", rights=dict(_RIGHTS))
        resolved = factory._resolve_stage_input(
            "dataset/directory",
            paths.runs / "probe/inputs",
            {"dataset/directory": snapshot},
            run_id="probe",
            stage_name="consume",
        )
    directory = Path(resolved["path"])
    assert (directory / "payload").read_text() == "data"
    assert json.loads((directory / "schema.json").read_text()) == {"type": "text"}


@pytest.mark.parametrize("base_reference", ["release/affine-v1", "alias/candidate"])
def test_reference_inputs_pin_prior_release_checkpoint_and_artifact(tmp_path, base_reference):
    paths, workload_path = _affine_project(tmp_path)
    probe = paths.root / "modules/probe"
    shutil.copytree(paths.root / "modules/examples/statistical", probe)
    manifest = yaml.safe_load((probe / "module.yaml").read_text())
    manifest["metadata"]["name"] = "probe"
    manifest["spec"]["extensions"] = {"sourceRef": "repository:modules/probe"}
    (probe / "module.yaml").write_text(yaml.safe_dump(manifest))
    (probe / "main.py").write_text(
        "import os\n"
        "from openfoundry.sdk import ProtocolResult, main\n"
        "def validate(_request):\n"
        "    return ProtocolResult(status='ok')\n"
        "def run(request):\n"
        "    seen = {}\n"
        "    for key, value in request.inputs.items():\n"
        "        seen[key] = {\n"
        "            'kind': value['kind'],\n"
        "            'exists': os.path.isfile(value['path']),\n"
        "            'state': value.get('state'),\n"
        "            'resource': value['resource'],\n"
        "        }\n"
        "    return ProtocolResult(status='ok', outputs={'seen': seen})\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main({'validate': validate, 'run': run}))\n"
    )

    def refine_workload(base: str, checkpoint: str, raw: str) -> Path:
        path = paths.root / "workloads/refine.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "WorkloadSpec",
                    "metadata": {"name": "refine", "namespace": "local/test-project"},
                    "spec": {
                        "graph": {
                            "stages": [
                                {
                                    "name": "refine",
                                    "module": "modules/probe/module.yaml",
                                    "operation": "run",
                                    "inputs": {"base": base, "ckpt": checkpoint, "raw": raw},
                                    "outputs": ["seen"],
                                }
                            ]
                        },
                    },
                }
            )
        )
        return path

    with Factory(paths) as factory:
        baseline = factory.run(workload_path, paths.root / "bindings/local.yaml")
        factory.evaluate(f"run/{baseline['runId']}")
        release = factory.create_release(
            baseline["runId"],
            name="affine-v1",
            intended_use="test",
            promote=True,
            vulnerability_report=_scan_for(paths, factory, baseline),
        )
        release_uri = factory._resource_uri(release)
        checkpoint = factory.list_resources(kind="Checkpoint")[0]
        checkpoint_name = checkpoint["metadata"]["name"]
        model_digest = baseline["outputs"]["train.model"]
        operations_before = len(factory.operations.list())

        with pytest.raises(NotFoundError):
            factory.run(
                refine_workload("release/missing", f"checkpoint/{checkpoint_name}", model_digest),
                paths.root / "bindings/local.yaml",
            )
        assert len(factory.operations.list()) == operations_before
        assert len(factory.list_resources(kind="Run")) == 1

        refined = factory.run(
            refine_workload(base_reference, f"checkpoint/{checkpoint_name}", model_digest),
            paths.root / "bindings/local.yaml",
        )
        seen = refined["outputs"]["refine.seen"]
        admission = factory._run_resource(refined["runId"])["spec"]["extensions"]
        stage_lineage = factory.lineage_query(f"run:{refined['runId']}/stage:refine")
        impact = factory.lineage_query(release_uri, direction="downstream")
        materialized = sorted(
            item.name for item in (paths.runs / refined["runId"] / "stages/refine/inputs").iterdir()
        )

    assert refined["state"] == "Succeeded"
    assert seen["base"]["kind"] == "release"
    assert seen["base"]["exists"]
    assert seen["base"]["resource"] == release_uri
    assert seen["base"]["state"]["format"] == "json-affine/v1"
    assert seen["ckpt"]["kind"] == "checkpoint"
    assert seen["ckpt"]["exists"]
    assert seen["ckpt"]["state"]["slope"] == pytest.approx(2.0, abs=0.01)
    assert seen["raw"]["kind"] == "artifact"
    assert seen["raw"]["exists"]
    assert seen["raw"]["state"] is None
    assert materialized == ["base", "ckpt", "raw"]
    assert admission["admittedReferences"] == {
        base_reference: release_uri,
        f"checkpoint/{checkpoint_name}": factory._resource_uri(checkpoint),
        model_digest: f"artifact:{model_digest}",
    }
    used = {edge["source"] for edge in stage_lineage if edge["relation"] == "used"}
    assert {release_uri, factory._resource_uri(checkpoint), f"artifact:{model_digest}"} <= used
    assert any(edge["target"] == f"run:{refined['runId']}/stage:refine" for edge in impact)


def test_copied_dataset_revision_is_relocatable(tmp_path):
    revisions = []
    for name in ("first", "second"):
        project_parent = tmp_path / name
        project_parent.mkdir()
        paths = _project(project_parent)
        bootstrap(paths)
        with Factory(paths) as factory:
            resource = _add_numbers(factory, paths)
            revisions.append(resource["metadata"]["revision"])
            assert resource["spec"]["extensions"]["source"].startswith("sha256:")
    assert revisions[0] == revisions[1]


def test_run_rejects_corrupted_dataset_before_allocation(paths):
    with Factory(paths) as factory:
        resource = _add_numbers(factory, paths)
        digest = resource["spec"]["extensions"]["artifact"]["chunks"][0]["digest"]
        digest_hex = digest.removeprefix("sha256:")
        (paths.store / "blobs" / digest_hex[:2] / digest_hex).write_bytes(b"corrupt")

        with pytest.raises(IntegrityError, match="dataset artifact"):
            factory.run(*_statistical(paths))
        assert factory.list_resources(kind="Run") == []
        assert list(paths.runs.iterdir()) == []


def test_workload_preflight_checks_environment_and_binding_semantics(paths, tmp_path):
    workload_path = paths.root / "workloads/example-statistical.yaml"
    binding_path = paths.root / "bindings/local.yaml"
    module_path = paths.root / "modules/examples/statistical/module.yaml"

    module = yaml.safe_load(module_path.read_text())
    module["spec"]["entryPoint"]["command"][0] = "missing-executable"
    module_path.write_text(yaml.safe_dump(module))
    with Factory(paths) as factory:
        report = factory.executor_preflight(binding_path, workload_path=workload_path)
    assert not report["ready"]
    assert any("unavailable" in issue for issue in report["issues"])


@pytest.mark.parametrize(
    "rights",
    [
        {},
        {"trainingAllowed": False},
        {"trainingAllowed": "true"},
        {"trainingAllowed": True, "revoked": True},
        {"trainingAllowed": True, "revoked": "false"},
    ],
)
def test_run_admission_rejects_data_without_current_training_rights(paths, rights):
    with Factory(paths) as factory:
        factory.add_data(
            paths.root / "data/numbers.jsonl",
            name="example-numbers",
            mode="copy",
            rights=rights,
        )
        with pytest.raises(ValidationError, match="rights do not allow training"):
            factory.create_run_operation(*_statistical(paths))
        assert factory.operations.list() == []
        assert factory.list_resources(kind="Run") == []


def test_revocation_is_current_despite_future_authored_timestamp(paths):
    with Factory(paths) as factory:
        initial = _add_numbers(factory, paths)
        future = {
            "apiVersion": initial["apiVersion"],
            "kind": initial["kind"],
            "metadata": {
                "name": initial["metadata"]["name"],
                "namespace": initial["metadata"]["namespace"],
                "uid": initial["metadata"]["uid"],
                "createdAt": "9999-01-01T00:00:00Z",
            },
            "spec": deepcopy(initial["spec"]),
        }
        future["spec"]["rights"]["rightsRevision"] = "future-authored"
        authorized = factory.apply_resource(future)
        queued = factory.create_run_operation(*_statistical(paths))
        revoked = factory.revoke_data("example-numbers", reason="consent withdrawn")

        assert factory.find_resource("DatasetSnapshot", "example-numbers") == revoked
        assert revoked["metadata"]["revision"] != authorized["metadata"]["revision"]
        with pytest.raises(ValidationError, match="current rights"):
            factory.execute_run_operation(queued["id"])


def test_revocation_before_final_admission_prevents_run_admission(paths, monkeypatch):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        inputs_pinned = threading.Event()
        continue_admission = threading.Event()
        original_pin = factory._pin_stage_inputs

        def pause_after_pin(stages, expected_revisions=None):
            pinned = original_pin(stages, expected_revisions)
            if expected_revisions is not None:
                inputs_pinned.set()
                if not continue_admission.wait(timeout=5):
                    raise RuntimeError("admission test barrier timed out")
            return pinned

        monkeypatch.setattr(factory, "_pin_stage_inputs", pause_after_pin)
        errors = []

        def execute() -> None:
            try:
                factory.execute_run_operation(operation["id"])
            except Exception as error:
                errors.append(error)

        execution = threading.Thread(target=execute)
        execution.start()
        assert inputs_pinned.wait(timeout=5)
        with Factory(paths, actor="rights-operator") as rights_operator:
            rights_operator.revoke_data("example-numbers", reason="consent withdrawn")
        continue_admission.set()
        execution.join(timeout=5)

        assert not execution.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ValidationError)
        assert "current rights" in str(errors[0])
        assert factory.events.query(run_id=operation["id"], type="RunAdmitted") == []
        assert factory.list_resources(kind="Run") == []


def test_revocation_stops_queued_and_recovering_training_without_replay(tmp_path):
    paths = _project(tmp_path)
    binding_path = paths.root / "bindings/local.yaml"
    binding = yaml.safe_load(binding_path.read_text())
    binding["spec"]["executor"] = "rights-test"
    binding_path.write_text(yaml.safe_dump(binding))
    bootstrap(paths)
    observed = {"interrupted": False, "submissions": 0, "attachments": 0}
    recovery_planned = threading.Event()
    continue_recovery = threading.Event()

    class RightsTestExecutor(LocalExecutor):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            if observed["interrupted"] and not recovery_planned.is_set():
                recovery_planned.set()
                if not continue_recovery.wait(timeout=5):
                    raise RuntimeError("recovery test barrier timed out")
            return plan

        def submit(self, plan):
            observed["submissions"] += 1
            return super().submit(plan)

        def status(self, execution_id):
            if not observed["interrupted"]:
                observed["interrupted"] = True
                raise KeyboardInterrupt("interrupt before rights recheck")
            return super().status(execution_id)

        def attach(self, execution_id, run_dir):
            observed["attachments"] += 1
            return super().attach(execution_id, run_dir)

    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "rights-test",
            EXECUTOR_API_VERSION,
            lambda _context: RightsTestExecutor(),
            capabilities=LocalExecutor().capabilities,
            config_contract={"type": "object", "additionalProperties": False},
        )
    )
    with Factory(paths, executors=registry) as factory:
        _add_numbers(factory, paths)
        queued = factory.create_run_operation(
            paths.root / "workloads/example-statistical.yaml", binding_path
        )
        factory.revoke_data("example-numbers", reason="source permission withdrawn")
        with pytest.raises(ValidationError, match="current rights"):
            factory.execute_run_operation(queued["id"])
        assert observed["submissions"] == 0
        assert factory.operations.get(queued["id"])["state"] == "failed"
        assert factory.list_resources(kind="Run") == []

        allowed = _add_numbers(factory, paths, name="second-numbers")
        workload_path = paths.root / "workloads/recovery-rights.yaml"
        workload = yaml.safe_load((paths.root / "workloads/example-statistical.yaml").read_text())
        workload["metadata"]["name"] = "recovery-rights"
        workload["spec"]["graph"]["stages"][0]["inputs"]["dataset"] = "dataset/second-numbers"
        workload_path.write_text(yaml.safe_dump(workload))
        operation = factory.create_run_operation(workload_path, binding_path)
        with pytest.raises(KeyboardInterrupt, match="rights recheck"):
            factory.execute_run_operation(operation["id"])
        assert observed["submissions"] == 1
        assert factory.operations.get(operation["id"])["state"] == "running"

    errors = []

    def recover() -> None:
        try:
            with Factory(paths, executors=registry) as restarted:
                restarted.execute_run_operation(operation["id"])
        except Exception as error:
            errors.append(error)

    recovery = threading.Thread(target=recover)
    recovery.start()
    assert recovery_planned.wait(timeout=5)
    with Factory(paths, executors=registry) as rights_operator:
        revoked = rights_operator.revoke_data("second-numbers", reason="consent withdrawn")
    continue_recovery.set()
    recovery.join(timeout=5)
    assert not recovery.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert "current rights" in str(errors[0])
    assert revoked["metadata"]["uid"] == allowed["metadata"]["uid"]

    with Factory(paths, executors=registry) as restarted:
        assert restarted.operations.get(operation["id"])["state"] == "failed"
        assert restarted.run_status(operation["id"])["status"]["state"] == "Failed"
        assert restarted.list_resources(kind="RunResult") == []
    assert observed == {"interrupted": True, "submissions": 1, "attachments": 0}


def test_revocation_winning_submit_race_prevents_allocation(tmp_path):
    paths = _project(tmp_path)
    binding_path = paths.root / "bindings/local.yaml"
    binding = yaml.safe_load(binding_path.read_text())
    binding["spec"]["executor"] = "submit-race"
    binding_path.write_text(yaml.safe_dump(binding))
    bootstrap(paths)
    planned = threading.Event()
    continue_submit = threading.Event()
    submissions = []

    class SubmitRaceExecutor(LocalExecutor):
        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            planned.set()
            if not continue_submit.wait(timeout=5):
                raise RuntimeError("submit test barrier timed out")
            return plan

        def submit(self, plan):
            submissions.append(plan)
            return super().submit(plan)

    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "submit-race",
            EXECUTOR_API_VERSION,
            lambda _context: SubmitRaceExecutor(),
            capabilities=LocalExecutor().capabilities,
            config_contract={"type": "object", "additionalProperties": False},
        )
    )
    with Factory(paths, executors=registry) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(
            paths.root / "workloads/example-statistical.yaml", binding_path
        )

    errors = []

    def execute() -> None:
        try:
            with Factory(paths, executors=registry) as factory:
                factory.execute_run_operation(operation["id"])
        except Exception as error:
            errors.append(error)

    execution = threading.Thread(target=execute)
    execution.start()
    assert planned.wait(timeout=5)
    with Factory(paths, executors=registry) as rights_operator:
        rights_operator.revoke_data("example-numbers", reason="consent withdrawn")
    continue_submit.set()
    execution.join(timeout=5)

    assert not execution.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValidationError)
    assert "current rights" in str(errors[0])
    assert submissions == []


def test_model_neutral_from_scratch_golden_path(tmp_path, monkeypatch):
    paths = _project(tmp_path)
    workload_path = paths.root / "workloads/example-from-scratch.yaml"
    bootstrap(paths)
    with Factory(paths) as factory:
        package_resource = factory.apply_resource_file("model-packages/example-affine.yaml")
        suite_resource = factory.apply_resource_file("evaluations/example-affine.yaml")
        dataset = _add_affine(factory)
        result = factory.run(workload_path, paths.root / "bindings/local.yaml")
        state = json.loads((paths.runs / result["runId"] / "state.json").read_text())
        state["digests"]["modules"]["train"] = "sha256:" + "0" * 64
        (paths.runs / result["runId"] / "state.json").write_text(json.dumps(state))
        (paths.root / "modules/examples/affine-regression/main.py").write_text(
            "raise RuntimeError('live source must not execute')\n"
        )
        (paths.root / "modules/examples/affine-serving/main.py").write_text(
            "raise RuntimeError('live serving source must not execute')\n"
        )
        evaluation = factory.evaluate(f"run/{result['runId']}")
        admission = factory._run_resource(result["runId"])["spec"]["extensions"]
        scan_path = paths.root / "affine-vulnerability-report.yaml"
        scan = {
            "scanner": {"name": "test-scanner", "version": "1"},
            "databaseRevision": "test-db-1",
            "generatedAt": "2026-09-01T00:00:00Z",
            "subjects": [
                result["outputs"]["train.model"],
                *admission["moduleDigests"].values(),
            ],
            "findings": [],
            "waivers": [],
        }
        scan_path.write_text(yaml.safe_dump(scan))
        with pytest.raises(IntegrityError, match="vulnerabilities"):
            factory.create_release(
                result["runId"],
                name="missing-serving-scan",
                intended_use="test",
                promote=True,
                vulnerability_report=scan_path,
            )
        scan["subjects"].append(admission["inferenceAdapter"]["sourceDigest"])
        scan_path.write_text(yaml.safe_dump(scan))
        revised_data = tmp_path / "revised-affine.jsonl"
        revised_data.write_text(Path("data/fixtures/affine.jsonl").read_text() + "\n")
        current_dataset = factory.add_data(
            revised_data,
            name="example-affine",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        assert current_dataset["metadata"]["uid"] == dataset["metadata"]["uid"]
        unrelated = factory.add_data(
            revised_data,
            name="unrelated",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        factory.revoke_data("unrelated", reason="not part of this run")
        release = factory.create_release(
            result["runId"],
            name="affine-release",
            intended_use="test",
            vulnerability_report=scan_path,
        )
        assert release["spec"]["extensions"]["manifest"]["dataSummary"] == [
            {
                "name": "example-affine",
                "revision": dataset["metadata"]["revision"],
                "rights": dataset["spec"]["rights"],
            }
        ]
        assert unrelated["metadata"]["uid"] != dataset["metadata"]["uid"]
        release_applied = threading.Event()
        continue_promotion = threading.Event()
        original_apply_resource = factory.apply_resource

        def pause_before_final_promotion(value, *, _system=False):
            applied = original_apply_resource(value, _system=_system)
            if value.get("kind") == "Release" and value["metadata"]["name"] == "rights-race":
                release_applied.set()
                if not continue_promotion.wait(timeout=5):
                    raise RuntimeError("promotion test barrier timed out")
            return applied

        monkeypatch.setattr(factory, "apply_resource", pause_before_final_promotion)
        promotion_errors = []

        def promote() -> None:
            try:
                factory.create_release(
                    result["runId"],
                    name="rights-race",
                    intended_use="test",
                    promote=True,
                    vulnerability_report=scan_path,
                )
            except Exception as error:
                promotion_errors.append(error)

        promotion = threading.Thread(target=promote)
        promotion.start()
        assert release_applied.wait(timeout=5)
        with Factory(paths, actor="rights-operator") as rights_operator:
            rights_operator.revoke_data("example-affine", reason="training consent withdrawn")
        continue_promotion.set()
        promotion.join(timeout=5)
        assert not promotion.is_alive()
        assert len(promotion_errors) == 1
        assert isinstance(promotion_errors[0], IntegrityError)
        assert "rights" in str(promotion_errors[0])
        with pytest.raises(NotFoundError):
            AliasRepository(factory.db).get("candidate")
        assert not [
            event
            for event in factory.events.query(type="ReleasePublished")
            if event.subject == "Release/rights-race"
        ]
        experiment = factory.create_experiment(
            name="affine-self-check",
            baseline_ref=f"run/{result['runId']}",
            candidate_ref=factory._resource_uri(evaluation),
            metric="training-loss",
            direction="minimize",
        )
        admitted_evaluation_refs = factory._run_resource(result["runId"])["spec"]["extensions"][
            "evaluationRefs"
        ]
        checkpoints = factory.list_resources(kind="Checkpoint")
        model_manifest = factory.local_store.read_manifest(result["outputs"]["train.model"])
        restored = tmp_path / "restored-model"
        ArtifactBuilder(factory.local_store).restore(model_manifest, restored)

    assert result["state"] == "Succeeded"
    assert result["outputs"]["train.loss"] < 1e-6
    assert result["outputs"]["evaluate.passed"] is True
    assert result["outputs"]["train.model"].startswith("sha256:")
    assert result["outputs"]["train.checkpoint"].startswith("sha256:")
    assert len(checkpoints) == 1
    assert checkpoints[0]["spec"]["artifactRef"] == result["outputs"]["train.checkpoint"]
    assert checkpoints[0]["spec"]["components"]["protocol-state"].startswith("sha256:")
    assert json.loads((restored / "payload").read_text()) == result["outputs"]["train.modelState"]
    assert evaluation["spec"]["extensions"]["compatibilityPassed"] is True
    assert admission["inferenceAdapter"]["sourceDigest"] != admission["moduleDigests"]["train"]
    assert (
        release["spec"]["extensions"]["manifest"]["runtime"]["sources"]["inference"]
        == admission["inferenceAdapter"]["sourceDigest"]
    )
    assert admission["inferenceAdapter"]["sourceDigest"] in release["spec"]["artifacts"]
    assert evaluation["spec"]["scores"]["training-loss"] < 1e-6
    assert experiment["spec"]["decision"] == "tie"
    assert factory._resource_uri(suite_resource) in admitted_evaluation_refs
    assert evaluation["spec"]["extensions"]["modelPackageRef"] == factory._resource_uri(
        package_resource
    )


def test_model_package_admission_rejects_unexecutable_contracts(paths):
    workload = yaml.safe_load((paths.root / "workloads/example-from-scratch.yaml").read_text())
    stages = project_workload(workload).stages
    base = yaml.safe_load(Path("model-packages/example-affine.yaml").read_text())

    with Factory(paths) as factory:
        tolerance = deepcopy(base)
        tolerance["metadata"]["name"] = "invalid-tolerance"
        tolerance["spec"]["compatibilityVectors"][0]["tolerances"]["prediction"]["absolute"] = -1
        factory.apply_resource(tolerance)
        with pytest.raises(ValidationError, match="finite and non-negative"):
            factory._pin_model_package("modelpackage/invalid-tolerance", stages)

        signature = deepcopy(base)
        signature["metadata"]["name"] = "invalid-signature"
        signature["spec"]["signatures"]["state"] = {"type": "string"}
        factory.apply_resource(signature)
        with pytest.raises(ValidationError, match="must describe an object"):
            factory._pin_model_package("modelpackage/invalid-signature", stages)


def test_model_package_admission_rejects_adapter_and_vector_drift(paths):
    workload = yaml.safe_load((paths.root / "workloads/example-from-scratch.yaml").read_text())
    stages = project_workload(workload).stages
    base = yaml.safe_load(Path("model-packages/example-affine.yaml").read_text())

    with Factory(paths) as factory:
        with pytest.raises(IntegrityError, match="does not match the workload"):
            factory._pin_model_package(None, stages, "openfoundry://stale/model-package")
        assert factory._pin_model_package(None, stages) is None
        with pytest.raises(ValidationError, match="modelpackage/<name>"):
            factory._pin_model_package("modelpackage/example@sha256:stale", stages)

        def rejected(name, mutate, match):
            package = deepcopy(base)
            package["metadata"]["name"] = name
            mutate(package)
            factory.apply_resource(package)
            with pytest.raises(ValidationError, match=match):
                factory._pin_model_package(f"modelpackage/{name}", stages)

        rejected(
            "external-contract-ref",
            lambda package: package["spec"]["signatures"]["input"].update(
                {"properties": {"input": {"$ref": "https://example.invalid/input.json"}}}
            ),
            "references",
        )
        rejected(
            "invalid-vector-input",
            lambda package: package["spec"]["compatibilityVectors"][0].update({"inputs": {}}),
            "model package input",
        )
        rejected(
            "invalid-vector-output",
            lambda package: package["spec"]["compatibilityVectors"][0].update({"expected": {}}),
            "model package output",
        )
        rejected(
            "invalid-tolerance-shape",
            lambda package: package["spec"]["compatibilityVectors"][0]["tolerances"].update(
                {"prediction": {"absolute": True}}
            ),
            "finite and non-negative",
        )
        rejected(
            "unknown-training-stage",
            lambda package: package["spec"]["adapters"]["trainingReference"].update(
                {"stage": "missing"}
            ),
            "unknown workload stage",
        )
        rejected(
            "training-operation-drift",
            lambda package: package["spec"]["adapters"]["trainingReference"].update(
                {"operation": "validate"}
            ),
            "does not match the workload stage",
        )
        missing_state = deepcopy(base)
        missing_state["metadata"]["name"] = "missing-state-output"
        missing_state["spec"]["adapters"]["inferenceReference"].pop("stateOutput")
        with pytest.raises(ValidationError, match="resource failed validation"):
            factory.apply_resource(missing_state)
        rejected(
            "invalid-state-output",
            lambda package: package["spec"]["adapters"]["inferenceReference"].update(
                {"stateOutput": "train.missing"}
            ),
            "not declared by the workload",
        )
        rejected(
            "shared-training-inference",
            lambda package: package["spec"]["adapters"]["inferenceReference"].update(
                {"module": "modules/examples/affine-regression/module.yaml"}
            ),
            "independent module",
        )

        admitted = deepcopy(base)
        admitted["metadata"]["name"] = "expected-revision"
        resource = factory.apply_resource(admitted)
        reference = factory._resource_uri(resource)
        assert (
            factory._pin_model_package("modelpackage/expected-revision", stages, reference)
            == resource
        )


def test_resource_pinning_and_resolution_fail_closed(paths):
    workload = yaml.safe_load((paths.root / "workloads/example-from-scratch.yaml").read_text())
    stages = project_workload(workload).stages
    evaluation = yaml.safe_load(Path("evaluations/example-affine.yaml").read_text())

    with Factory(paths) as factory:
        dataset = _add_affine(factory)
        suite = factory.apply_resource(evaluation)

        with pytest.raises(IntegrityError, match="dataset reference was not pinned"):
            factory._pin_stage_inputs(stages, {})
        with pytest.raises(IntegrityError, match="references do not match"):
            factory._pin_named_resources(
                ["evaluationspec/example-affine"], "evaluationspec/", "EvaluationSpec", []
            )
        with pytest.raises(ValidationError, match="reference must use"):
            factory._pin_named_resources(
                ["evaluation/example-affine"], "evaluationspec/", "EvaluationSpec"
            )
        assert factory._pin_named_resources(
            ["evaluationspec/example-affine"],
            "evaluationspec/",
            "EvaluationSpec",
            [factory._resource_uri(suite)],
        ) == [suite]

        assert factory._resolve_output_reference("literal", {}, stages) == "literal"
        with pytest.raises(IntegrityError, match="stage output reference is unavailable"):
            factory._resolve_output_reference("train.model", {}, stages)
        resolve = {"run_id": "run", "stage_name": "train"}
        assert factory._resolve_stage_input(7, paths.runs / "x", {}, **resolve) == 7
        with pytest.raises(IntegrityError, match="not pinned at admission"):
            factory._resolve_stage_input("dataset/missing", paths.runs / "x/y/z", {}, **resolve)
        with pytest.raises(IntegrityError, match="reference input was not pinned"):
            factory._resolve_stage_input("release/missing", paths.runs / "x/y/z", {}, **resolve)

        target_root = paths.runs / "run" / "stages" / "train" / "inputs" / "dataset"
        materialized = factory._resolve_stage_input(
            "dataset/example-affine", target_root, {"dataset/example-affine": dataset}, **resolve
        )
        assert materialized["manifestDigest"].startswith("sha256:")
        assert any(
            edge["source"] == factory._resource_uri(dataset)
            for edge in factory.lineage_query("run:run/stage:train")
        )
        with pytest.raises(IntegrityError, match="target already exists"):
            factory._resolve_stage_input(
                "dataset/example-affine",
                target_root,
                {"dataset/example-affine": dataset},
                **resolve,
            )

        assert factory._verify_stage_outputs({"value": 1})
        assert not factory._verify_stage_outputs({"artifact": "sha256:" + "0" * 64})
        with pytest.raises(IntegrityError, match="immutable result"):
            factory._run_result("missing", {})
        with pytest.raises(IntegrityError, match="identity is ambiguous"):
            factory._run_resource("missing")

    assert EvaluationService._compatibility_equal(True, True, {})
    assert EvaluationService._compatibility_equal(1.0, 1.001, {"absolute": 0.01})
    assert EvaluationService._compatibility_equal([1, 2], [1, 2], {})
    assert not EvaluationService._compatibility_equal([1], [1, 2], {})
    assert EvaluationService._compatibility_equal({"x": 1}, {"x": 1}, {})
    assert not EvaluationService._compatibility_equal({"x": 1}, {"y": 1}, {})
    assert not EvaluationService._compatibility_equal("left", "right", {})


def test_experiment_rejects_different_evaluation_revisions(paths):
    with Factory(paths) as factory:
        baseline = factory.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "EvaluationResult",
                "metadata": {"name": "baseline", "namespace": "local/test-project"},
                "spec": {
                    "evaluationRef": "run/baseline",
                    "scores": {"loss": 1.0},
                    "provenance": {},
                    "uncertainty": {},
                    "failures": [],
                    "extensions": {
                        "evaluationRefs": ["openfoundry://test/evaluationspec/a@sha256:a"]
                    },
                },
            },
            _system=True,
        )
        candidate = factory.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "EvaluationResult",
                "metadata": {"name": "candidate", "namespace": "local/test-project"},
                "spec": {
                    "evaluationRef": "run/candidate",
                    "scores": {"loss": 0.5},
                    "provenance": {},
                    "uncertainty": {},
                    "failures": [],
                    "extensions": {
                        "evaluationRefs": ["openfoundry://test/evaluationspec/b@sha256:b"]
                    },
                },
            },
            _system=True,
        )

        with pytest.raises(ValidationError, match="different evaluation revisions"):
            factory.create_experiment(
                name="invalid-comparison",
                baseline_ref=factory._resource_uri(baseline),
                candidate_ref=factory._resource_uri(candidate),
                metric="loss",
                direction="minimize",
            )

        candidate["spec"]["extensions"]["evaluationRefs"] = baseline["spec"]["extensions"][
            "evaluationRefs"
        ]
        for invalid in (True, "0.5"):
            candidate["spec"]["scores"]["loss"] = invalid
            replacement = factory.apply_resource(
                {
                    **candidate,
                    "metadata": {
                        "name": f"candidate-{str(invalid).lower().replace('.', '-')}",
                        "namespace": "local/test-project",
                    },
                },
                _system=True,
            )
            with pytest.raises(ValidationError, match="non-numeric"):
                factory.create_experiment(
                    name=f"invalid-{invalid!s}",
                    baseline_ref=factory._resource_uri(baseline),
                    candidate_ref=factory._resource_uri(replacement),
                    metric="loss",
                    direction="minimize",
                )


def test_pending_run_operation_executes_after_controller_restart(paths):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))

    with Factory(paths) as restarted:
        completed = restarted.execute_run_operation(operation["id"])
        run_resource = restarted._run_resource(completed["result"]["runId"])

    assert completed["state"] == "succeeded"
    assert completed["result"]["runId"] == operation["id"]
    assert run_resource["spec"]["extensions"]["operationId"] == operation["id"]


def test_execution_plan_digest_covers_every_execution_field(tmp_path):
    plan = ExecutionPlan(
        ("python3", "module.py"),
        tmp_path / "run",
        tmp_path / "source",
        {"MODE": "train"},
        {"cpu": 1},
        30.0,
        True,
        {"provider": {"queue": "a"}},
    )
    digest = _execution_plan_digest(
        plan, request_digest="sha256:request", environment_digest="sha256:environment"
    )
    changes = [
        replace(plan, argv=("python3", "other.py")),
        replace(plan, run_dir=tmp_path / "other-run"),
        replace(plan, cwd=tmp_path / "other-source"),
        replace(plan, env={"MODE": "evaluate"}),
        replace(plan, resources={"cpu": 2}),
        replace(plan, timeout=60.0),
        replace(plan, deny_network=False),
        replace(plan, metadata={"provider": {"queue": "b"}}),
    ]

    assert all(
        _execution_plan_digest(
            changed,
            request_digest="sha256:request",
            environment_digest="sha256:environment",
        )
        != digest
        for changed in changes
    )


@pytest.mark.parametrize("interrupted_event", ["SpecValidated", "RunAdmitted"])
def test_recovery_repairs_run_admission_event_crash_gaps(paths, monkeypatch, interrupted_event):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        append = factory.events.append
        interrupted = False

        def interrupt_event(**kwargs):
            nonlocal interrupted
            matches_run_spec = (
                kwargs["type"] == "SpecValidated" and kwargs["data"].get("kind") == "Run"
            )
            if not interrupted and (
                kwargs["type"] == interrupted_event
                and (interrupted_event == "RunAdmitted" or matches_run_spec)
            ):
                interrupted = True
                raise KeyboardInterrupt(f"controller stopped before {interrupted_event}")
            return append(**kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory.events, "append", interrupt_event)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        completed = factory.execute_run_operation(operation["id"])
        run = factory._run_resource(operation["id"])
        spec_events = factory.events.query(
            resource_uid=run["metadata"]["uid"], type="SpecValidated"
        )
        admission_events = factory.events.query(run_id=operation["id"], type="RunAdmitted")

    assert completed["state"] == "succeeded"
    assert len(spec_events) == 1
    assert len(admission_events) == 1


def test_recovery_does_not_backfill_admission_after_rights_revocation(paths, monkeypatch):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        append = factory.events.append

        def interrupt_admission(**kwargs):
            if kwargs["type"] == "RunAdmitted":
                raise KeyboardInterrupt("controller stopped after run persistence")
            return append(**kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory.events, "append", interrupt_admission)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        factory.revoke_data("example-numbers", reason="consent withdrawn")
        with pytest.raises(ValidationError, match="current rights"):
            factory.execute_run_operation(operation["id"])

        assert factory.events.query(run_id=operation["id"], type="RunAdmitted") == []
        assert factory.operations.get(operation["id"])["state"] == "failed"


def test_recovery_integrity_failure_finalizes_the_durable_run(paths, monkeypatch):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        append = factory.events.append

        def interrupt_admission(**kwargs):
            if kwargs["type"] == "RunAdmitted":
                raise KeyboardInterrupt("controller stopped after run persistence")
            return append(**kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory.events, "append", interrupt_admission)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        run = factory._run_resource(operation["id"])
        source_ref = next(iter(run["spec"]["extensions"]["moduleDigests"].values()))
        source = factory.local_store.read_manifest(source_ref)
        factory.local_store.quarantine_chunk(source.chunks[0].digest)
        with pytest.raises(IntegrityError, match="source failed integrity verification"):
            factory.execute_run_operation(operation["id"])
        status = factory.run_status(operation["id"])
        terminal_events = factory.events.query(
            run_id=operation["id"], resource_uid=operation["id"], type="RunStateChanged"
        )
        failed_operation = factory.operations.get(operation["id"])

    assert failed_operation["state"] == "failed"
    assert status["execution"]["state"] == "Failed"
    assert status["status"]["state"] == "Failed"
    assert [event.data["state"] for event in terminal_events] == ["Failed"]


def test_running_local_operation_reattaches_without_duplicate_stage_work(tmp_path):
    paths = _project(tmp_path)
    workload_path = paths.root / "workloads/example-from-scratch.yaml"
    binding_path = paths.root / "bindings/local.yaml"
    binding = yaml.safe_load(binding_path.read_text())
    binding["spec"]["executor"] = "recoverable-local"
    binding_path.write_text(yaml.safe_dump(binding))
    bootstrap(paths)
    observed = {"interrupted": False, "submissions": 0}

    class RecoverableLocal(LocalExecutor):
        def submit(self, plan):
            observed["submissions"] += 1
            return super().submit(plan)

        def status(self, execution_id):
            if not observed["interrupted"]:
                observed["interrupted"] = True
                raise KeyboardInterrupt("controller interrupted after durable submit")
            return super().status(execution_id)

    registry = ExecutorRegistry()
    registry.register(
        ExecutorProvider(
            "recoverable-local",
            EXECUTOR_API_VERSION,
            lambda _context: RecoverableLocal(),
            capabilities=LocalExecutor().capabilities,
            config_contract={"type": "object", "additionalProperties": False},
        )
    )
    with Factory(paths, executors=registry) as factory:
        _apply_affine_resources(factory)
        _add_affine(factory)
        operation = factory.create_run_operation(workload_path, binding_path)
        with pytest.raises(KeyboardInterrupt, match="controller interrupted"):
            factory.execute_run_operation(operation["id"])
        assert factory.operations.get(operation["id"])["state"] == "running"

    shutil.rmtree(paths.root / "modules/examples/affine-regression")
    shutil.rmtree(paths.root / "modules/examples/affine-serving")
    with Factory(paths, executors=registry) as restarted:
        completed = restarted.execute_run_operation(operation["id"])
        run_submissions = observed["submissions"]
        evaluation = restarted.evaluate(f"run/{operation['id']}")
        admission_events = restarted.events.query(run_id=operation["id"], type="RunAdmitted")

    assert completed["state"] == "succeeded"
    assert completed["result"]["outputs"]["train.loss"] < 1e-6
    assert evaluation["spec"]["extensions"]["compatibilityPassed"] is True
    assert run_submissions == 2
    assert observed["submissions"] == 3
    assert len(admission_events) == 1


def test_recovery_rejects_a_changed_plan_before_executor_attachment(paths):
    module_root = paths.root / "modules/examples/statistical"
    manifest, code_root = load_manifest(module_root / "module.yaml", paths.root)

    class ChangingPlanLocal(LocalExecutor):
        changed = False
        attached = False
        status_calls = 0

        def plan(self, **kwargs):
            plan = super().plan(**kwargs)
            return replace(plan, resources={"cpu": 2}) if self.changed else plan

        def attach(self, execution_id, run_dir):
            self.attached = True
            return super().attach(execution_id, run_dir)

        def status(self, execution_id):
            self.status_calls += 1
            raise KeyboardInterrupt("controller stopped after submit")

    executor = ChangingPlanLocal()
    with Factory(paths) as factory:
        environment = factory._prepare_module_environment(executor, manifest, code_root)
        request = ProtocolRequest(operation="validate")
        stage_dir = paths.runs / "changed-plan"
        with pytest.raises(KeyboardInterrupt, match="controller stopped"):
            factory._execute_module(
                manifest,
                code_root,
                request,
                stage_dir,
                executor=executor,
                executor_config={},
                environment=environment,
            )
        executor.changed = True
        with pytest.raises(IntegrityError, match="plan differs"):
            factory._execute_module(
                manifest,
                code_root,
                request,
                stage_dir,
                executor=executor,
                executor_config={},
                environment=environment,
                recovering=True,
            )

    assert not executor.attached
    assert executor.status_calls == 1


def test_stale_running_operation_fails_closed_without_reexecution(paths, monkeypatch):
    with Factory(paths) as factory:
        operation = factory.operations.create(
            "run",
            {
                "actor": factory.actor,
                "workload": "workloads/unused.yaml",
                "binding": "bindings/unused.yaml",
            },
        )
        operation = factory.operations.get(operation["id"])
        factory.operations.update(
            operation["id"], expected_version=operation["version"], state="running"
        )
        monkeypatch.setattr(
            factory,
            "_run_impl",
            lambda *_args, **_kwargs: pytest.fail("uncertain work must not be executed again"),
        )
        with pytest.raises(IntegrityError, match="automatic replay is disabled"):
            factory.execute_run_operation(operation["id"])
        failed = factory.operations.get(operation["id"])

    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "indeterminate_execution"
    assert not failed["error"]["retryable"]


def test_module_failure_exposes_only_bounded_log_tails(paths):
    module_root = paths.root / "modules/examples/statistical"
    (module_root / "main.py").write_text(
        "import sys\n"
        "print('x' * 2000000)\n"
        "print('y' * 2000000, file=sys.stderr)\n"
        "raise SystemExit(2)\n"
    )
    manifest, code_root = load_manifest(module_root / "module.yaml", paths.root)

    with Factory(paths) as factory:
        executor = LocalExecutor()
        environment = factory._prepare_module_environment(executor, manifest, code_root)
        with pytest.raises(OpenFoundryError) as raised:
            factory._execute_module(
                manifest,
                code_root,
                ProtocolRequest(operation="run"),
                paths.runs / "verbose-failure",
                executor=executor,
                executor_config={},
                environment=environment,
            )

    assert len(raised.value.details["stdout"].encode()) <= 4096
    assert len(raised.value.details["stderr"].encode()) <= 4096


def test_running_operation_reconciles_immutable_completed_result(paths, monkeypatch):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        apply_resource = factory.apply_resource

        def interrupt_completion(value, **kwargs):
            resource = apply_resource(value, **kwargs)
            if value.get("kind") == "RunResult":
                raise KeyboardInterrupt("controller stopped after immutable result publication")
            return resource

        with monkeypatch.context() as patch:
            patch.setattr(factory, "apply_resource", interrupt_completion)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])
        assert factory.operations.get(operation["id"])["state"] == "finalizing"

        completed = factory.execute_run_operation(operation["id"])
        status = factory.run_status(operation["id"])["status"]
        result_lineage = [
            edge
            for edge in factory.lineage.by_run(operation["id"])
            if edge.target == completed["result"]["resultRef"]
        ]
        terminal_events = factory.events.query(
            run_id=operation["id"], resource_uid=operation["id"], type="RunStateChanged"
        )

    assert completed["state"] == "succeeded"
    assert completed["result"]["runId"] == operation["id"]
    assert completed["result"]["outputs"]["train.mean"] == 3.0
    assert status["state"] == "Succeeded"
    assert len(result_lineage) == 1
    assert [event.data["state"] for event in terminal_events] == ["Succeeded"]


def test_running_operation_publishes_result_from_succeeded_run_state(paths, monkeypatch):
    submissions = 0
    submit = LocalExecutor.submit

    def count_submit(executor, plan):
        nonlocal submissions
        submissions += 1
        return submit(executor, plan)

    monkeypatch.setattr(LocalExecutor, "submit", count_submit)
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        apply_resource = factory.apply_resource

        def interrupt_before_result(value, **kwargs):
            if value.get("kind") == "RunResult":
                raise KeyboardInterrupt("controller stopped before result publication")
            return apply_resource(value, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory, "apply_resource", interrupt_before_result)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        assert (
            json.loads((paths.runs / operation["id"] / "state.json").read_text())["state"]
            == "Succeeded"
        )
        completed = factory.execute_run_operation(operation["id"])

    assert completed["state"] == "succeeded"
    assert completed["result"]["outputs"]["train.mean"] == 3.0
    assert submissions == 2


def test_succeeded_run_with_corrupt_output_evidence_fails_closed(paths, tmp_path, monkeypatch):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        apply_resource = factory.apply_resource

        def interrupt_before_result(value, **kwargs):
            if value.get("kind") == "RunResult":
                raise KeyboardInterrupt("controller stopped before result publication")
            return apply_resource(value, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory, "apply_resource", interrupt_before_result)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        state = json.loads((paths.runs / operation["id"] / "state.json").read_text())
        model_ref = state["stages"]["train"]["outputs"]["model"]
        model = factory.local_store.read_manifest(model_ref)
        factory.local_store.quarantine_chunk(model.chunks[0].digest)
        with pytest.raises(IntegrityError, match="output evidence failed verification"):
            factory.execute_run_operation(operation["id"])
        failed = factory.operations.get(operation["id"])
        status = factory.run_status(operation["id"])
        results = factory.resources.list(kind="RunResult")

    assert failed["state"] == "failed"
    assert status["status"]["state"] == "Failed"
    assert results == []


@pytest.mark.parametrize("tamper", ["missing-stage", "failed-stage", "missing-output"])
def test_incomplete_succeeded_run_state_cannot_publish_a_result(paths, monkeypatch, tamper):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        apply_resource = factory.apply_resource

        def interrupt_before_result(value, **kwargs):
            if value.get("kind") == "RunResult":
                raise KeyboardInterrupt("controller stopped before result publication")
            return apply_resource(value, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(factory, "apply_resource", interrupt_before_result)
            with pytest.raises(KeyboardInterrupt, match="controller stopped"):
                factory.execute_run_operation(operation["id"])

        state_path = paths.runs / operation["id"] / "state.json"
        state = json.loads(state_path.read_text())
        if tamper == "missing-stage":
            state["stages"].pop("evaluate")
        elif tamper == "failed-stage":
            state["stages"]["evaluate"] = {"status": "failed", "attempt": 1}
        else:
            state["stages"]["train"]["outputs"].pop("mean")
        state_path.write_text(json.dumps(state))

        with pytest.raises(IntegrityError, match="succeeded run state"):
            factory.execute_run_operation(operation["id"])
        failed = factory.operations.get(operation["id"])
        results = factory.resources.list(kind="RunResult")

    assert failed["state"] == "failed"
    assert results == []


def test_run_operation_execution_lease_rejects_concurrent_worker(paths):
    with Factory(paths) as factory:
        operation = factory.operations.create(
            "run", {"actor": factory.actor, "workload": "unused", "binding": "unused"}
        )
        lock_path = paths.state / "operations" / f"{operation['id']}.lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(ConflictError, match="already executing"):
                factory.execute_run_operation(operation["id"])
        assert factory.operations.get(operation["id"])["state"] == "pending"


def test_queued_run_pins_manifest_outside_code_root(paths):
    module_root = paths.root / "modules/examples/statistical"
    source_root = module_root / "src"
    source_root.mkdir()
    for name in ("main.py", "requirements.lock"):
        (module_root / name).replace(source_root / name)
    manifest_path = module_root / "module.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["spec"]["entryPoint"]["codeRoot"] = "src"
    manifest_path.write_text(yaml.safe_dump(manifest))

    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        manifest["spec"]["extensions"] = {"sourceRef": "repository:changed-after-queue"}
        manifest_path.write_text(yaml.safe_dump(manifest))

        with pytest.raises(IntegrityError, match="module source changed"):
            factory.execute_run_operation(operation["id"])
        assert factory.operations.get(operation["id"])["state"] == "failed"
        assert factory.list_resources(kind="Run") == []


def test_queued_run_rejects_inference_adapter_source_drift(tmp_path):
    paths = _project(tmp_path)
    workload_path = paths.root / "workloads/example-from-scratch.yaml"
    bootstrap(paths)
    with Factory(paths) as factory:
        _apply_affine_resources(factory)
        _add_affine(factory)
        operation = factory.create_run_operation(workload_path, paths.root / "bindings/local.yaml")
        (paths.root / "modules/examples/affine-serving/main.py").write_text(
            "raise RuntimeError('changed after queue')\n"
        )

        with pytest.raises(IntegrityError, match="inference adapter source changed"):
            factory.execute_run_operation(operation["id"])
        runs = factory.list_resources(kind="Run")

    assert runs == []


def test_queued_run_uses_exact_dataset_revision_after_alias_advances(paths):
    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        operation = factory.create_run_operation(*_statistical(paths))
        (paths.root / "data/numbers.jsonl").write_text('{"value": 99}\n')
        _add_numbers(factory, paths)

        completed = factory.execute_run_operation(operation["id"])

    assert completed["result"]["outputs"]["train.mean"] == 3.0


def test_run_operation_records_admission_and_worker_failures(paths, tmp_path):
    with Factory(paths) as factory:
        invalid_kind = factory.operations.create("other", {"actor": factory.actor})
        with pytest.raises(ValidationError, match="not an executable pending run"):
            factory.execute_run_operation(invalid_kind["id"])

        wrong_actor = factory.operations.create("run", {"actor": "another-controller"})
        with pytest.raises(ValidationError, match="actor does not match"):
            factory.execute_run_operation(wrong_actor["id"])

        _add_numbers(factory, paths)
        workload, binding = _statistical(paths)

        def failure(operation_id: str, error: type[Exception], match: str) -> str:
            with pytest.raises(error, match=match):
                factory.execute_run_operation(operation_id)
            return str(factory.operations.get(operation_id)["error"]["code"])

        admission_domain = factory.create_run_operation(workload, binding)
        original = workload.read_text()
        workload.write_text(original.replace("expected: 3.0", "expected: 4.0"))
        assert (
            failure(admission_domain["id"], IntegrityError, "desired state changed")
            == "integrity_error"
        )
        workload.write_text(original)

        admission_generic = factory.create_run_operation(workload, binding)
        binding.rename(paths.root / "bindings/moved.yaml")
        assert (
            failure(admission_generic["id"], FileNotFoundError, "local.yaml")
            == "run_admission_error"
        )
        (paths.root / "bindings/moved.yaml").rename(binding)

        worker_generic = factory.create_run_operation(workload, binding)
        state = paths.runs / worker_generic["id"] / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("not json")
        assert failure(worker_generic["id"], ValueError, "Expecting value") == "run_worker_error"

        worker_domain = factory.create_run_operation(workload, binding)
        factory.revoke_data("example-numbers", reason="consent withdrawn")
        assert failure(worker_domain["id"], ValidationError, "current rights") == "validation_error"


@pytest.mark.parametrize("failure", ["multiple", "state", "declaration"])
def test_checkpoint_publication_rejects_incomplete_stage_results(paths, failure):
    workload_path = paths.root / "workloads/example-from-scratch.yaml"
    workload = yaml.safe_load(workload_path.read_text())
    workload["spec"].pop("modelPackageRef")
    workload["spec"].pop("evaluationRefs")
    workload["spec"]["graph"]["stages"] = workload["spec"]["graph"]["stages"][:1]
    workload_path.write_text(yaml.safe_dump(workload))

    module_root = paths.root / "modules/examples/affine-regression"
    if failure == "multiple":
        source = (module_root / "main.py").read_text()
        source = source.replace(
            '{"name": "checkpoint", "kind": "checkpoint", "path": model_path.name},',
            '{"name": "checkpoint", "kind": "checkpoint", "path": model_path.name},\n'
            '                {"name": "checkpoint-copy", "kind": "checkpoint", '
            '"path": model_path.name},',
            1,
        )
        (module_root / "main.py").write_text(source)
        expected = "only one aggregate checkpoint"
    elif failure == "state":
        source = (module_root / "main.py").read_text().replace("            state=model,\n", "", 1)
        (module_root / "main.py").write_text(source)
        expected = "requires protocol state"
    else:
        manifest_path = module_root / "module.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["spec"]["checkpoint"] = False
        manifest_path.write_text(yaml.safe_dump(manifest))
        expected = "without declaring support"

    with Factory(paths) as factory:
        _add_affine(factory)
        with pytest.raises(ValidationError, match=expected):
            factory.run(workload_path, paths.root / "bindings/local.yaml")
        operation = factory.operations.list()[-1]
        assert operation["state"] == "failed"
        assert (
            factory.run_status(factory.list_resources(kind="Run")[0]["metadata"]["uid"])["status"][
                "state"
            ]
            == "Failed"
        )


def test_release_preservation_and_selection_use_current_requirements(paths):
    def require(**requirements):
        policy_dir = paths.root / "policies"
        policy_dir.mkdir(exist_ok=True)
        (policy_dir / "default.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "Policy",
                    "metadata": {"name": "default"},
                    "spec": {
                        "rules": [
                            {"name": "owner", "effect": "allow", "match": {"actor": "local-user"}}
                        ],
                        "config": {"promotion": requirements},
                    },
                }
            )
        )

    with Factory(paths) as factory:
        _add_numbers(factory, paths)
        run = factory.run(*_statistical(paths))
        unevaluated = factory.create_release(run["runId"], name="early", intended_use="test")
        assert unevaluated["spec"]["extensions"]["manifest"]["evaluations"] == []
        assert factory.show_release("early")["aliases"] == []
        with pytest.raises(IntegrityError, match="evaluation"):
            factory.promote_release("early")
        require(requireEvaluationPass=False)
        assert factory.promote_release("early")["version"] == 1
        with pytest.raises(ConflictError, match="version"):
            factory.promote_release("early", expected_version=99)
        assert factory.show_release("early")["release"] == unevaluated
        require(requireEvaluationPass=True)
        with pytest.raises(IntegrityError, match="evaluation"):
            factory.promote_release("early", alias="stable")

        factory.evaluate(run["runId"])
        measured = factory.create_release(run["runId"], name="measured", intended_use="test")
        assert factory.promote_release("measured", expected_version=1)["version"] == 2
        assert factory.show_release("early")["release"] == unevaluated
        require(requireVulnerabilityScan=True)
        with pytest.raises(IntegrityError, match="vulnerabilities"):
            factory.promote_release("measured", alias="stable")
        require()
        selected_path = paths.root / "selected.yaml"
        selected_path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "DeploymentSpec",
                    "metadata": {"name": "selected"},
                    "spec": {"releaseRef": "alias/candidate", "extensions": {"form": "edge"}},
                }
            )
        )
        first = factory.deploy(selected_path)
        assert first["deployment"]["spec"]["releaseRef"] == factory._resource_uri(measured)
        revised = factory.create_release(run["runId"], name="measured", intended_use="revised")
        assert factory.show_release("alias/candidate")["release"] == measured
        assert factory.show_release("alias/candidate")["aliasVersions"] == {"candidate": 2}
        factory.promote_release("measured", expected_version=2)
        second = factory.deploy(selected_path)
        assert second["deployment"]["spec"]["releaseRef"] == factory._resource_uri(revised)
        version = factory.deployment_status("selected")["statusVersion"]
        rolled_back = factory.rollback_deployment("selected", expected_version=version)
        assert rolled_back["deployment"] == first["deployment"]
        assert rolled_back["status"]["releaseRevision"] == measured["metadata"]["revision"]
        factory.revoke_data("example-numbers", reason="consent withdrawn")
        with pytest.raises(IntegrityError, match="rights"):
            factory.promote_release("measured", alias="stable")
        deployment = paths.root / "revoked-deployment.yaml"
        deployment.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "openfoundry.dev/v1alpha1",
                    "kind": "DeploymentSpec",
                    "metadata": {"name": "revoked"},
                    "spec": {"releaseRef": "release/measured", "extensions": {"form": "edge"}},
                }
            )
        )
        with pytest.raises(IntegrityError, match="current promotion policy"):
            factory.deploy(deployment)
        with pytest.raises(IntegrityError, match="current promotion policy"):
            factory.rollback_deployment("selected", expected_version=rolled_back["statusVersion"])
        assert factory.show_release(factory._resource_uri(measured))["release"] == measured
