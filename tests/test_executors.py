import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest
from _environments import create_environment, offline_environment
from openfoundry.errors import CapabilityError, ConfigurationError, IntegrityError, ValidationError
from openfoundry.executors import EXECUTOR_API_VERSION, ExecutorProvider, ExecutorRegistry
from openfoundry.executors.base import DependencyLock
from openfoundry.executors.local import LocalExecutor
from openfoundry.executors.registry import default_executor_registry


def _install_plugin(site: Path, dist: str, entry: str, target: str, source: str) -> None:
    info = site / f"{dist}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(f"[openfoundry.executors]\n{entry} = {target}\n")
    (site / f"{target.partition(':')[0]}.py").write_text(source)


def _finish(executor: LocalExecutor, run_dir: Path, argv: list[str]) -> str:
    execution_id = executor.submit(
        executor.plan(argv=argv, run_dir=run_dir, cwd=run_dir.parent, requires_result=False)
    )
    executor._processes[execution_id].wait(timeout=30)
    return execution_id


def test_executor_registry_catalog_duplicates_unknown_and_discovery(tmp_path, monkeypatch):
    registry = default_executor_registry(discover=False)
    catalog = registry.catalog()
    assert [item["name"] for item in catalog["providers"]] == ["local"]
    with pytest.raises(ConfigurationError, match="duplicate"):
        registry.register(
            ExecutorProvider("local", EXECUTOR_API_VERSION, lambda _context: LocalExecutor()),
            source="test",
        )
    with pytest.raises(CapabilityError, match="unknown"):
        registry.resolve(
            "missing",
            project_root=tmp_path,
            state_root=tmp_path / ".openfoundry",
            actor="tester",
            declaration={},
        )

    site = tmp_path / "site"
    _install_plugin(
        site,
        "example-provider",
        "custom",
        "example_provider:provider",
        (
            "from openfoundry.executors import "
            "EXECUTOR_API_VERSION, ExecutorContext, ExecutorProvider\n"
            "from openfoundry.executors.local import LocalExecutor\n\n"
            "provider = ExecutorProvider(\n"
            '    "custom",\n'
            "    EXECUTOR_API_VERSION,\n"
            "    lambda context: LocalExecutor() "
            "if isinstance(context, ExecutorContext) else None,\n"
            '    config_contract={"type": "object", '
            '"additionalProperties": False},\n'
            ")\n"
        ),
    )
    monkeypatch.syspath_prepend(str(site))
    custom = ExecutorRegistry()
    custom.discover()
    provider = importlib.import_module("example_provider").provider
    assert custom.catalog()["providers"][0]["source"].startswith("entry-point:example-provider")

    with pytest.raises(ValidationError, match="provider contract") as invalid:
        custom.resolve(
            "custom",
            project_root=tmp_path,
            state_root=tmp_path / ".openfoundry",
            actor="tester",
            declaration={},
            config={"submitted-secret": "must-not-be-returned"},
        )
    assert "must-not-be-returned" not in repr(invalid.value.as_dict())

    injected = ExecutorRegistry()
    injected.register(provider)
    assert injected.catalog()["providers"][0]["source"] == "runtime"


def test_executor_registry_rejects_invalid_plugins_and_controller_fields(tmp_path, monkeypatch):
    registry = ExecutorRegistry()
    with pytest.raises(ConfigurationError, match="normalized"):
        registry.register(
            ExecutorProvider(" invalid ", EXECUTOR_API_VERSION, lambda _context: LocalExecutor())
        )
    with pytest.raises(ConfigurationError, match="unsupported API version"):
        registry.register(ExecutorProvider("old", "openfoundry.executor/v1alpha1", LocalExecutor))
    with pytest.raises(ConfigurationError, match="invalid config contract"):
        registry.register(
            ExecutorProvider(
                "invalid-schema",
                EXECUTOR_API_VERSION,
                lambda _context: LocalExecutor(),
                config_contract={"type": "not-a-json-schema-type"},
            )
        )

    provider = ExecutorProvider("valid", EXECUTOR_API_VERSION, lambda _context: LocalExecutor())
    registry.register(provider)
    resolve = {
        "project_root": tmp_path,
        "state_root": tmp_path / ".openfoundry",
        "actor": "tester",
        "declaration": {},
    }
    with pytest.raises(ValidationError, match="controller-owned"):
        registry.resolve("valid", config={"argv": ["unexpected"]}, **resolve)

    builtins = default_executor_registry(discover=False)
    with pytest.raises(ValidationError, match="provider contract"):
        builtins.resolve("local", config={"typo": True}, **resolve)

    invalid_executor = ExecutorRegistry()
    invalid_executor.register(
        ExecutorProvider("invalid", EXECUTOR_API_VERSION, lambda _context: object())
    )
    with pytest.raises(ConfigurationError, match="invalid adapter"):
        invalid_executor.resolve("invalid", **resolve)

    broken_site = tmp_path / "broken-site"
    _install_plugin(
        broken_site,
        "broken-provider",
        "broken",
        "broken_provider:provider",
        'raise ImportError("unavailable")\n',
    )
    with monkeypatch.context() as patch:
        patch.syspath_prepend(str(broken_site))
        with pytest.raises(ConfigurationError, match="could not be loaded"):
            ExecutorRegistry().discover()

    wrong_site = tmp_path / "wrong-site"
    _install_plugin(
        wrong_site,
        "wrong-provider",
        "declared-name",
        "wrong_provider:provider",
        "from openfoundry.executors import EXECUTOR_API_VERSION, ExecutorProvider\n"
        "from openfoundry.executors.local import LocalExecutor\n\n"
        'provider = ExecutorProvider("different-name", EXECUTOR_API_VERSION, '
        "lambda _context: LocalExecutor())\n",
    )
    with monkeypatch.context() as patch:
        patch.syspath_prepend(str(wrong_site))
        with pytest.raises(ConfigurationError, match="does not match"):
            ExecutorRegistry().discover()


def test_executor_plugin_wheel_is_discovered_in_an_isolated_environment(tmp_path):
    distributions = tmp_path / "dist"
    distributions.mkdir()
    for source in (Path.cwd(), Path("tests/fixtures/executor_plugin").resolve()):
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(distributions),
            ],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert built.returncode == 0, built.stdout + built.stderr

    environment = tmp_path / "venv"
    create_environment(environment)
    python = environment / "bin/python"
    wheels = sorted(distributions.glob("*.whl"))
    installed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            *[str(wheel) for wheel in wheels],
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=offline_environment(),
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    checked = subprocess.run(
        [str(python), "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=offline_environment(),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    acceptance = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            """
import importlib.metadata
import json
import sys
import time
from pathlib import Path

import openfoundry
import openfoundry_stable_executor
from openfoundry.executors import EXECUTOR_API_VERSION, ExecutorContext, default_executor_registry

prefix = Path(sys.prefix).resolve()
assert Path(openfoundry.__file__).resolve().is_relative_to(prefix)
assert Path(openfoundry_stable_executor.__file__).resolve().is_relative_to(prefix)
requires = (
    importlib.metadata.metadata("openfoundry-stable-executor-test-plugin").get_all(
        "Requires-Dist"
    )
)
assert requires == ["openfoundry<3,>=2"]
registry = default_executor_registry()
catalog = registry.catalog()
stable = next(item for item in catalog["providers"] if item["name"] == "stable-test")
assert catalog["apiVersion"] == EXECUTOR_API_VERSION
assert stable["apiVersion"] == EXECUTOR_API_VERSION
assert stable["source"].startswith("entry-point:openfoundry-stable-executor-test-plugin:")

root = Path("project").resolve()
state = root / ".openfoundry"
state.mkdir(parents=True)
context = ExecutorContext(root, state, "acceptance", {}, {})
executor = openfoundry_stable_executor.create(context)
run_dir = state / "run"
command = (
    "import os\\n"
    "import pathlib\\n"
    "import sys\\n"
    "pathlib.Path(os.environ['OPENFOUNDRY_RESULT_FILE']).write_text(os.environ['OPENFOUNDRY_RUN_ID'])\\n"
    "print('out-tail')\\n"
    "print('err-tail', file=sys.stderr)"
)
execution_id = executor.submit(
    executor.plan(
        argv=[sys.executable, "-c", command],
        run_dir=run_dir,
        cwd=root,
        requires_result=False,
    )
)
while executor.status(execution_id).state in {"pending", "running"}:
    time.sleep(0.01)
attached = openfoundry_stable_executor.create(context)
attached.attach(execution_id, run_dir)
assert attached.status(execution_id).state == "succeeded"
assert attached.recover(run_dir) == execution_id
assert (run_dir / "result.json").read_text() == execution_id
assert attached.read_logs(execution_id, tail_bytes=9) == ("out-tail\\n", "err-tail\\n")
print(json.dumps({"provider": stable["name"], "state": "succeeded"}))
""",
        ],
        cwd=isolated,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert acceptance.returncode == 0, acceptance.stdout + acceptance.stderr
    assert json.loads(acceptance.stdout) == {"provider": "stable-test", "state": "succeeded"}


def test_local_executor_success_failure_logs_and_reconcile(tmp_path):
    executor = LocalExecutor()
    success_dir = tmp_path / "success"
    plan = executor.plan(
        argv=[
            "python3",
            "-c",
            (
                "import os,pathlib; "
                "pathlib.Path(os.environ['OPENFOUNDRY_RESULT_FILE']).write_text('{}')"
            ),
        ],
        run_dir=success_dir,
        cwd=tmp_path,
    )
    execution_id = executor.submit(plan)
    process = executor._processes[execution_id]
    process.wait(timeout=5)
    assert executor.status(execution_id).state == "succeeded"
    assert executor.logs(execution_id) == (
        success_dir / "stdout.log",
        success_dir / "stderr.log",
    )
    assert executor.read_logs(execution_id) == ("", "")
    with pytest.raises(ValueError, match="positive"):
        executor.read_logs(execution_id, tail_bytes=0)
    recovered = LocalExecutor()
    assert recovered.reconcile(success_dir) == execution_id
    assert recovered.status(execution_id).state == "succeeded"

    failure_dir = tmp_path / "failure"
    failed = executor.submit(
        executor.plan(
            argv=["python3", "-c", "raise SystemExit(3)"], run_dir=failure_dir, cwd=tmp_path
        )
    )
    executor._processes[failed].wait(timeout=5)
    assert executor.status(failed).exit_code == 3


def test_local_executor_rejects_an_ambiguous_launch_record(tmp_path):
    run_dir = tmp_path / "ambiguous"
    run_dir.mkdir()
    (run_dir / "execution.json").write_text('{"id":"uncertain","state":"launching","started":0}')

    with pytest.raises(IntegrityError, match="launch outcome is indeterminate"):
        LocalExecutor().recover(run_dir)


def test_local_executor_recovers_a_completed_launch_before_pid_persistence(tmp_path):
    run_dir = tmp_path / "completed"
    run_dir.mkdir()
    (run_dir / "execution.json").write_text('{"id":"completed-id","state":"launching","started":0}')
    (run_dir / "completion.json").write_text('{"exitCode":0,"reason":"exit:0","finished":1}')
    (run_dir / "result.json").write_text("{}")
    executor = LocalExecutor()

    assert executor.recover(run_dir) == "completed-id"
    assert executor.status("completed-id").state == "succeeded"


def test_local_executor_enforces_timeout_without_controller_and_records_plain_command(tmp_path):
    executor = LocalExecutor(limits={"timeoutSeconds": 0.1})
    timed = _finish(executor, tmp_path / "timed", ["python3", "-c", "import time; time.sleep(30)"])
    recovered = LocalExecutor()
    recovered.reconcile(tmp_path / "timed")
    status = recovered.status(timed)
    assert status.state == "failed"
    assert status.reason == "timeout"

    plain = _finish(executor, tmp_path / "plain", ["python3", "-c", "pass"])
    assert executor.status(plain).state == "succeeded"


def test_local_executor_attests_executable_under_network_wrapper(tmp_path):
    if not LocalExecutor._network_namespace_available():
        pytest.skip("unprivileged user namespaces are unavailable on this host")
    executor = LocalExecutor()
    environment = executor.prepare_environment(
        argv=["python3", "-c", "pass"],
        cwd=tmp_path,
        dependency=DependencyLock("requirements.lock", "sha256:test", b""),
        deny_network=True,
    )
    environment["executables"][0]["digest"] = "sha256:" + "0" * 64
    plan = executor.plan(
        argv=environment["command"],
        run_dir=tmp_path / "attested",
        cwd=tmp_path,
        deny_network=True,
        requires_result=False,
        environment=environment,
    )
    assert plan.argv[0] == environment["executables"][0]["path"]
    assert plan.metadata["executables"] == environment["executables"]

    execution_id = executor.submit(plan)
    for _ in range(100):
        status = executor.status(execution_id)
        if status.state != "running":
            break
        time.sleep(0.01)
    assert status.state == "failed"
    assert status.reason == "executable-digest-mismatch"


def test_local_environment_resolves_relative_path_entries(tmp_path, monkeypatch):
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    executable = binary_directory / "tool"
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")

    environment = LocalExecutor().prepare_environment(
        argv=["tool"],
        cwd=tmp_path,
        dependency=DependencyLock("requirements.lock", "sha256:test", b""),
    )
    assert environment["command"][0] == str(executable.resolve())
    assert environment["executables"][0]["path"] == str(executable.resolve())


def test_local_environment_rejects_nonempty_lock_without_environment_root(tmp_path):
    with pytest.raises(CapabilityError, match="no environment cache root"):
        LocalExecutor().prepare_environment(
            argv=["python3", "-c", "pass"],
            cwd=tmp_path,
            dependency=DependencyLock(
                "environment.lock", "sha256:" + "1" * 64, b"\x00provider-specific\xff"
            ),
        )


def test_local_environment_rejects_nonempty_lock_for_non_python_entry_point(tmp_path):
    binary = tmp_path / "tool"
    binary.write_bytes(b"binary")
    binary.chmod(0o755)
    with pytest.raises(CapabilityError, match="requires a Python interpreter entry point"):
        LocalExecutor(environment_root=tmp_path / "environments").prepare_environment(
            argv=["./tool"],
            cwd=tmp_path,
            dependency=DependencyLock("environment.lock", "sha256:" + "1" * 64, b"opaque\n"),
        )


def _wait(executor: LocalExecutor, execution_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while executor.status(execution_id).state in {"pending", "running"}:
        assert time.monotonic() < deadline, "execution did not finish"
        time.sleep(0.05)


def test_local_environment_keeps_virtual_environment_symlink_interpreter(tmp_path, monkeypatch):
    environment_path = tmp_path / "venv"
    venv.EnvBuilder(symlinks=True, with_pip=False, system_site_packages=True).create(
        environment_path
    )
    python = environment_path / "bin" / "python3"
    if not python.is_symlink():
        pytest.skip("this platform does not create symlink interpreters")
    monkeypatch.setenv("PATH", f"{environment_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    executor = LocalExecutor()

    environment = executor.prepare_environment(
        argv=["python3", "-c", "import sys; print(sys.prefix)"],
        cwd=tmp_path,
        dependency=DependencyLock("requirements.lock", "sha256:test", b""),
    )

    assert environment["command"][0] == str(python)
    assert environment["executables"][0]["path"] == str(python)
    assert environment["executables"][0]["target"] == str(python.resolve())
    assert environment["runtime"]["python"]["version"]
    run_dir = tmp_path / "run"
    execution_id = executor.submit(
        executor.plan(
            argv=environment["command"],
            run_dir=run_dir,
            cwd=tmp_path,
            requires_result=False,
            environment=environment,
        )
    )
    _wait(executor, execution_id)
    assert executor.status(execution_id).state == "succeeded"
    assert (run_dir / "stdout.log").read_text().strip() == str(environment_path)


def test_local_executor_realizes_dependency_lock_from_wheelhouse(tmp_path):
    from _wheels import build_wheel, lock_for

    wheelhouse = tmp_path / "wheels"
    _wheel, wheel_digest = build_wheel(wheelhouse)
    lock = lock_for("openfoundrytiny", "1.0", wheel_digest)
    dependency = DependencyLock(
        "requirements.lock", "sha256:" + hashlib.sha256(lock).hexdigest(), lock
    )
    executor = LocalExecutor(
        environment_root=tmp_path / "environments",
        dependency_wheelhouse=wheelhouse,
        dependency_index=False,
    )
    argv = [
        "python3",
        "-c",
        "import openfoundry.sdk, openfoundrytiny; print(openfoundrytiny.VERSION)",
    ]

    environment = executor.prepare_environment(argv=argv, cwd=tmp_path, dependency=dependency)

    realization = environment["realization"]
    assert realization["strategy"] == "venv"
    assert realization["lockDigest"] == dependency.digest
    assert environment["command"][0].startswith(str(tmp_path / "environments"))
    assert Path(environment["command"][0]).is_symlink()
    names = {item["name"] for item in environment["runtime"]["python"]["distributions"]}
    assert {"openfoundrytiny", "openfoundry"} <= names
    again = executor.prepare_environment(argv=argv, cwd=tmp_path, dependency=dependency)
    assert again["digest"] == environment["digest"]
    assert len(list((tmp_path / "environments").glob("*/openfoundry-environment.json"))) == 1

    run_dir = tmp_path / "run"
    execution_id = executor.submit(
        executor.plan(
            argv=environment["command"],
            run_dir=run_dir,
            cwd=tmp_path,
            requires_result=False,
            environment=environment,
        )
    )
    _wait(executor, execution_id)
    assert executor.status(execution_id).state == "succeeded", (run_dir / "stderr.log").read_text()
    assert (run_dir / "stdout.log").read_text().strip() == "1.0"


def test_dependency_cache_distinguishes_venvs_sharing_one_interpreter(tmp_path):
    from _wheels import build_wheel, lock_for

    wheelhouse = tmp_path / "wheels"
    _, digest = build_wheel(wheelhouse)
    contents = lock_for("openfoundrytiny", "1.0", digest)
    dependency = DependencyLock(
        "requirements.lock", "sha256:" + hashlib.sha256(contents).hexdigest(), contents
    )
    executor = LocalExecutor(
        environment_root=tmp_path / "cache",
        dependency_wheelhouse=wheelhouse,
        dependency_index=False,
    )
    commands = []
    for name in ("first", "second"):
        base = tmp_path / name
        venv.EnvBuilder(with_pip=False, symlinks=True).create(base)
        python = base / "bin/python3"
        site = Path(
            subprocess.check_output(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True,
            ).strip()
        )
        (site / "trial_marker.py").write_text(f"VALUE = {name!r}\n")
        environment = executor.prepare_environment(
            argv=[
                str(python),
                "-c",
                "import trial_marker, openfoundrytiny; print(trial_marker.VALUE)",
            ],
            cwd=tmp_path,
            dependency=dependency,
        )
        commands.append(environment["command"])
        assert subprocess.check_output(environment["command"], text=True).strip() == name
    assert commands[0][0] != commands[1][0]


def test_local_executor_reports_unsatisfiable_lock_without_index(tmp_path):
    lock = b"openfoundrymissing==9.9 --hash=sha256:" + b"0" * 64 + b"\n"
    executor = LocalExecutor(environment_root=tmp_path / "environments", dependency_index=False)
    with pytest.raises(CapabilityError, match="dependency installation failed") as excinfo:
        executor.prepare_environment(
            argv=["python3", "-c", "pass"],
            cwd=tmp_path,
            dependency=DependencyLock(
                "requirements.lock", "sha256:" + hashlib.sha256(lock).hexdigest(), lock
            ),
        )
    assert "output" in excinfo.value.details
    assert not list((tmp_path / "environments").glob("*/openfoundry-environment.json"))


def test_local_environment_captures_python_runtime_and_distribution_inventory(tmp_path):
    environment = LocalExecutor().prepare_environment(
        argv=[sys.executable, "-c", "pass"],
        cwd=tmp_path,
        dependency=DependencyLock("requirements.lock", "sha256:test", b""),
    )

    runtime = environment["runtime"]
    assert runtime["system"]
    assert runtime["machine"]
    assert runtime["python"]["version"]
    assert runtime["python"]["implementation"]
    assert runtime["python"]["distributions"]
    assert environment["environmentPolicy"]["inherited"] == ["HOME", "LANG", "PATH", "TZ"]


def test_executor_log_reads_are_tail_bounded(tmp_path):
    executor = LocalExecutor()
    run_dir = tmp_path / "logs"
    execution_id = _finish(
        executor,
        run_dir,
        ["python3", "-c", "print('0123456789'); import sys; print('abcdefghij', file=sys.stderr)"],
    )

    stdout, stderr = executor.read_logs(execution_id, tail_bytes=5)
    assert stdout == "6789\n"
    assert stderr == "ghij\n"


def test_executor_log_error_replacement_remains_byte_bounded(tmp_path):
    executor = LocalExecutor()
    run_dir = tmp_path / "binary-logs"
    execution_id = _finish(
        executor,
        run_dir,
        ["python3", "-c", "import sys; sys.stdout.buffer.write(b'\\xff' * 10000)"],
    )

    stdout, _stderr = executor.read_logs(execution_id, tail_bytes=4096)
    assert stdout
    assert len(stdout.encode()) <= 4096


def test_local_executor_bounds_log_files_without_limiting_artifacts(tmp_path):
    executor = LocalExecutor()
    run_dir = tmp_path / "bounded-logs"
    artifact = run_dir / "model.bin"
    execution_id = _finish(
        executor,
        run_dir,
        [
            "python3",
            "-c",
            (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_bytes(b'a' * 1500000); "
                "print('x' * 1500000); print('y' * 1500000, file=sys.stderr)"
            ),
            str(artifact),
        ],
    )

    assert executor.status(execution_id).state == "succeeded"
    assert (run_dir / "stdout.log").stat().st_size == 1024 * 1024
    assert (run_dir / "stderr.log").stat().st_size == 1024 * 1024
    assert artifact.stat().st_size == 1500000
    assert (
        hashlib.sha256(artifact.read_bytes()).hexdigest()
        == hashlib.sha256(b"a" * 1500000).hexdigest()
    )


def test_local_executor_applies_binding_resource_limits(tmp_path):
    assert LocalExecutor(limits={"gpus": 1}).preflight() == [
        "unsupported local resource limits: gpus"
    ]
    executor = LocalExecutor(limits={"addressSpaceBytes": 1024**3})
    assert executor.preflight() == []
    hungry = _finish(executor, tmp_path / "hungry", ["python3", "-c", "x = bytearray(4 * 1024**3)"])
    assert executor.status(hungry).state == "failed"
    modest = _finish(executor, tmp_path / "modest", ["python3", "-c", "x = bytearray(1024)"])
    assert executor.status(modest).state == "succeeded"
