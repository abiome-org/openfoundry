from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from openfoundry.canonical import load_document, portable_relative_path
from openfoundry.errors import ConfigurationError, ValidationError
from openfoundry.executors.base import DependencyLock
from openfoundry.schema_registry import default_registry


class ModuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    code_root: str = "."
    argv: list[str]
    schemas: dict[str, Any] = Field(default_factory=dict)
    dependency_lock: str
    dependency_digest: str
    dependency_contents: bytes = Field(repr=False)
    checkpoint: bool = False
    fixtures: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("argv")
    @classmethod
    def argv_valid(cls, value: list[str]) -> list[str]:
        if not value or any(not item or "\x00" in item for item in value):
            raise ValueError("argv must contain non-empty arguments")
        return value


def _within(path: Path, root: Path, message: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(message) from exc


def _code_root(manifest_path: Path, root: Path, code_root: str) -> Path:
    portable_relative_path(code_root, "module code root")
    lexical = manifest_path.parent / code_root
    if lexical.is_symlink():
        raise ValidationError("module code root may not be a symlink")
    code = lexical.resolve()
    _within(code, root, "module code root escapes project root")
    if not code.is_dir():
        raise ValidationError("module code root must be a directory")
    return code


def _lock_contents(code: Path, manifest: ModuleManifest) -> bytes:
    portable_relative_path(manifest.dependency_lock, "module dependency lock")
    lock_path = (code / manifest.dependency_lock).resolve()
    _within(lock_path, code, "module dependency lock escapes code root")
    if not lock_path.is_file() or lock_path.is_symlink():
        raise ValidationError("module dependency lock must be a regular file")
    contents = lock_path.read_bytes()
    if "sha256:" + hashlib.sha256(contents).hexdigest() != manifest.dependency_digest:
        raise ValidationError("module dependency lock digest does not match")
    return contents


def _check_executable(code: Path, executable: str) -> None:
    if executable.startswith("/"):
        raise ValidationError("module executable must not be an absolute path")
    if "/" not in executable:
        return
    portable_relative_path(executable, "module executable")
    target = (code / executable).resolve()
    if code not in target.parents or not target.is_file() or not os.access(target, os.X_OK):
        raise ValidationError("module executable is missing or not executable")


def load_manifest(path: str | Path, project_root: str | Path) -> tuple[ModuleManifest, Path]:
    manifest_path, root = Path(path).resolve(), Path(project_root).resolve()
    _within(manifest_path, root, "manifest is outside project root")
    raw = load_document(manifest_path.read_bytes())
    resource = default_registry.validate_as(raw, "Module")
    spec = resource["spec"]
    entry_point = spec["entryPoint"]
    contracts = spec.get("contracts", {})
    manifest = ModuleManifest.model_validate(
        {
            "name": resource["metadata"]["name"],
            "code_root": entry_point.get("codeRoot", "."),
            "argv": entry_point["command"],
            "schemas": {
                name: contracts.get(name, {"type": "object"})
                for name in ("input", "output", "config", "state")
            },
            "dependency_lock": spec["environment"]["dependencyLock"],
            "dependency_digest": spec["environment"]["dependencyDigest"],
            "dependency_contents": b"",
            "checkpoint": spec.get("checkpoint", False),
            "fixtures": spec.get("fixtures", []),
        }
    )
    code = _code_root(manifest_path, root, manifest.code_root)
    manifest = manifest.model_copy(update={"dependency_contents": _lock_contents(code, manifest)})
    for name, contract in manifest.schemas.items():
        validate_contract_schema(contract, f"module {name}")
    _check_executable(code, manifest.argv[0])
    return manifest, code


_SCAFFOLD_MAIN = """from openfoundry.sdk import ProtocolRequest, ProtocolResult, main


def validate(_request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok")


def run(request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"echo": request.inputs})


if __name__ == "__main__":
    raise SystemExit(main({"validate": validate, "run": run}))
"""


def scaffold_module(directory: str | Path, name: str | None = None) -> Path:
    root = Path(directory)
    if root.exists():
        raise ValidationError(f"module directory already exists: {root}")
    root.mkdir(parents=True)
    (root / "main.py").write_text(_SCAFFOLD_MAIN)
    (root / "requirements.lock").write_bytes(b"")
    manifest = {
        "apiVersion": "openfoundry.dev/v1alpha1",
        "kind": "Module",
        "metadata": {"name": name or root.name},
        "spec": {
            "entryPoint": {"command": ["python3", "main.py"]},
            "environment": {
                "dependencyLock": "requirements.lock",
                "dependencyDigest": "sha256:" + hashlib.sha256(b"").hexdigest(),
            },
        },
    }
    path = root / "module.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def dependency_lock(manifest: ModuleManifest) -> DependencyLock:
    return DependencyLock(
        relative_path=manifest.dependency_lock,
        digest=manifest.dependency_digest,
        contents=manifest.dependency_contents,
    )


_EXCLUDED = {
    ".git",
    ".mypy_cache",
    ".openfoundry",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "secrets",
}


def package_module(
    code_root: str | Path, output: str | Path, *, included_paths: set[str] | None = None
) -> str:
    root, destination = Path(code_root).resolve(), Path(output)
    with (
        destination.open("wb") as raw,
        tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
            relative = path.relative_to(root)
            if any(part in _EXCLUDED for part in relative.parts):
                continue
            if included_paths is not None and relative.as_posix() not in included_paths:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValidationError(f"unsupported package entry: {relative}")
            info = tarfile.TarInfo(relative.as_posix() + ("/" if path.is_dir() else ""))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if path.is_dir() or mode & stat.S_IXUSR else 0o644
            if path.is_file():
                data = path.read_bytes()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    return "sha256:" + hashlib.sha256(destination.read_bytes()).hexdigest()


def extract_module_package(package: str | Path, destination: str | Path) -> Path:
    target = Path(destination)
    if target.exists():
        raise ValidationError("module extraction destination already exists")
    target.mkdir(parents=True)
    try:
        with tarfile.open(package, mode="r:") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or str(path) != member.name.rstrip("/"):
                    raise ValidationError(f"unsafe module package path: {member.name}")
                output = target.joinpath(*path.parts)
                if member.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValidationError(f"unreadable module package entry: {member.name}")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(source.read())
                else:
                    raise ValidationError(f"unsupported module package entry: {member.name}")
                os.chmod(output, member.mode & 0o777)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def worktree_state(root: str | Path) -> dict[str, Any]:
    cwd = Path(root)

    def git(*arguments: str, text: bool = True, check: bool = True) -> Any:
        try:
            return subprocess.run(
                ["git", *arguments], cwd=cwd, check=check, capture_output=True, text=text
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ConfigurationError(
                "the project must be a Git working tree to admit a workload",
                details={"root": str(cwd)},
            ) from exc

    head = git("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    commit = head.stdout.strip() if head.returncode == 0 else None
    patch = git("diff", "--binary", "HEAD", "--", ".", text=False).stdout if commit else b""
    uncommitted = set(
        git("ls-files", "--others", "--exclude-standard", "--", ".").stdout.splitlines()
    )
    if commit is None:
        uncommitted.update(git("ls-files", "--", ".").stdout.splitlines())
    untracked = sorted(uncommitted)
    return {
        "commit": commit,
        "patch": patch,
        "untracked": untracked,
        "dirty": bool(patch or untracked),
        "patchDigest": "sha256:" + hashlib.sha256(patch).hexdigest(),
    }


def git_source(root: str | Path, *, allow_dirty: bool = False) -> dict[str, Any]:
    state = worktree_state(root)
    if state["dirty"] and not allow_dirty:
        raise ValidationError("dirty Git tree requires explicit allow_dirty policy")
    return {
        "commit": state["commit"],
        "patch": state["patch"],
        "untracked": state["untracked"],
        "digest": state["patchDigest"],
    }


def validate_contract(contract: Any, value: Any, name: str) -> None:
    errors = sorted(
        Draft202012Validator(contract).iter_errors(value),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        raise ValidationError(
            f"module {name} contract failed",
            details={
                "errors": [
                    {
                        "path": "$"
                        + "".join(
                            f"[{item}]" if isinstance(item, int) else f".{item}"
                            for item in error.absolute_path
                        ),
                        "constraint": str(error.validator),
                    }
                    for error in errors
                ]
            },
        )


def validate_contract_schema(contract: Any, name: str) -> None:
    reject_schema_references(contract, name)
    try:
        Draft202012Validator.check_schema(contract)
    except Exception as exc:
        raise ValidationError(f"{name} contract is not a valid JSON Schema") from exc


def reject_schema_references(value: Any, name: str) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str) or isinstance(value.get("$dynamicRef"), str):
            raise ValidationError(f"module {name} contract references are not supported")
        for keyword, child in value.items():
            if keyword in {"const", "default", "enum", "examples"}:
                continue
            if keyword in {
                "$defs",
                "dependentSchemas",
                "patternProperties",
                "properties",
            } and isinstance(child, dict):
                for schema in child.values():
                    reject_schema_references(schema, name)
            else:
                reject_schema_references(child, name)
    elif isinstance(value, list):
        for child in value:
            reject_schema_references(child, name)
