from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import IO, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.canonical import canonical_json, sha256_digest
from openfoundry.config import ProjectPaths, load_project
from openfoundry.database import Database
from openfoundry.errors import ConflictError, IntegrityError, ValidationError
from openfoundry.events import EventStore
from openfoundry.security import SecretStore, SigningIdentity
from openfoundry.stores.filesystem import FilesystemStore

_FORMAT = "openfoundry.backup/v1"
_MANIFEST = "manifest.json"
_REQUIRED = {"metadata.db", "identity/signing.key", "identity/secrets.key"}
_STORE_PREFIXES = ("store/blobs/", "store/manifests/")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_RUNTIME_DIRECTORIES = ("runs", "packages", "operations", "environments")


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return "sha256:" + digest.hexdigest(), size


def _copy_regular(source: Path, destination: Path) -> None:
    mode = source.lstat().st_mode
    if source.is_symlink() or not stat.S_ISREG(mode):
        raise IntegrityError(f"backup source is not a regular file: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    os.chmod(destination, stat.S_IMODE(mode))


def _copy_store_area(paths: ProjectPaths, staging: Path, area: str) -> None:
    source_root = paths.store / area
    if source_root.is_symlink() or not source_root.is_dir():
        raise IntegrityError(f"artifact store area is invalid: {area}")
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise IntegrityError("artifact store backup does not follow symbolic links")
        if source.is_dir():
            continue
        if not source.is_file():
            raise IntegrityError("artifact store contains a non-regular file")
        _copy_regular(source, staging / "store" / area / source.relative_to(source_root))


def _inventory(staging: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        relative = path.relative_to(staging).as_posix()
        digest, size = _digest_file(path)
        files.append(
            {
                "path": relative,
                "digest": digest,
                "size": size,
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        )
    return files


def _tar_info(name: str, size: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def _write_archive(destination: Path, staging: Path, manifest: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            with tarfile.open(fileobj=output, mode="w") as archive:
                archive.addfile(_tar_info(_MANIFEST, len(manifest), 0o600), io.BytesIO(manifest))
                for record in _inventory(staging):
                    path = staging / record["path"]
                    with path.open("rb") as source:
                        archive.addfile(
                            _tar_info(record["path"], record["size"], record["mode"]),
                            source,
                        )
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ConflictError("backup destination already exists") from exc
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_state(root: Path) -> dict[str, int]:
    database = Database.inspect(root / "metadata.db")
    try:
        if not database.integrity_check():
            raise IntegrityError("backup database integrity check failed")
        database.verify_migrations()
        identity = SigningIdentity(root / "identity" / "signing.key")
        resources = list(database.connection.execute("SELECT data,digest FROM resources"))
        for row in resources:
            if sha256_digest(json.loads(row[0])) != str(row[1]):
                raise IntegrityError("backup resource digest check failed")
        events = EventStore(database, identity)
        event_items = events.query()
        for event in event_items:
            if event.payload_digest != sha256_digest(event.data):
                raise IntegrityError("backup event payload digest check failed")
            if event.key_id == identity.key_id:
                events._verify(event, identity.public_bytes)
        secrets = SecretStore(database, root / "identity" / "secrets.key")
        secret_items = secrets.list()
        for item in secret_items:
            secrets.get(str(item["name"]), str(item["purpose"]))
        store = FilesystemStore(root / "store")
        artifact_count = 0
        builder = ArtifactBuilder(store)
        for digest in store.list_manifests():
            artifact_count += 1
            if not builder.verify(store.read_manifest(digest)):
                raise IntegrityError("backup artifact integrity check failed")
        return {
            "resources": len(resources),
            "events": len(event_items),
            "secrets": len(secret_items),
            "artifacts": artifact_count,
        }
    finally:
        database.close()


def create_backup(
    paths: ProjectPaths,
    database: Database,
    identity: SigningIdentity,
    destination: str | Path,
) -> dict[str, Any]:
    target = Path(destination).resolve()
    if target == paths.state.resolve() or paths.state.resolve() in target.parents:
        raise ValidationError("backup destination must be outside .openfoundry")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ConflictError("backup destination already exists")
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".openfoundry-backup-") as name:
        staging = Path(name)
        database.backup(staging / "metadata.db")
        os.chmod(staging / "metadata.db", 0o600)
        _copy_regular(paths.signing_key, staging / "identity" / "signing.key")
        _copy_regular(paths.secret_key, staging / "identity" / "secrets.key")
        for area in ("blobs", "manifests"):
            _copy_store_area(paths, staging, area)
        counts = _verify_state(staging)
        project = load_project(paths)
        unsigned = {
            "format": _FORMAT,
            "project": project["metadata"]["namespace"],
            "projectDigest": sha256_digest(project),
            "keyId": identity.key_id,
            "files": _inventory(staging),
        }
        envelope = {**unsigned, "signature": identity.sign(unsigned)}
        manifest = canonical_json(envelope)
        if len(manifest) > _MAX_MANIFEST_BYTES:
            raise ValidationError("backup manifest exceeds the supported size")
        _write_archive(target, staging, manifest)
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "files": len(unsigned["files"]),
        "keyId": identity.key_id,
        "integrity": True,
        **counts,
    }


def _validated_manifest(
    value: Any, project: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "format",
        "project",
        "projectDigest",
        "keyId",
        "files",
        "signature",
    }:
        raise IntegrityError("backup manifest has an invalid shape")
    if (
        value["format"] != _FORMAT
        or value["project"] != project["metadata"]["namespace"]
        or value["projectDigest"] != sha256_digest(project)
    ):
        raise IntegrityError("backup does not belong to this project")
    if not isinstance(value["keyId"], str) or not isinstance(value["signature"], str):
        raise IntegrityError("backup signature metadata is invalid")
    if not isinstance(value["files"], list):
        raise IntegrityError("backup file inventory is invalid")
    records: dict[str, Any] = {}
    for record in value["files"]:
        path = _validated_record(record)
        if path in records:
            raise IntegrityError("backup file path is invalid or duplicated")
        records[path] = record
    if not records.keys() >= _REQUIRED:
        raise IntegrityError("backup is missing required durable state")
    unsigned = {key: value[key] for key in ("format", "project", "projectDigest", "keyId", "files")}
    return unsigned, records


def _validated_record(record: Any) -> str:
    if not isinstance(record, dict) or set(record) != {"path", "digest", "size", "mode"}:
        raise IntegrityError("backup file record is invalid")
    path = record["path"]
    portable = PurePosixPath(path) if isinstance(path, str) else PurePosixPath("/")
    if (
        not isinstance(path, str)
        or portable.is_absolute()
        or ".." in portable.parts
        or portable.as_posix() != path
    ):
        raise IntegrityError("backup file path is invalid or duplicated")
    if path not in _REQUIRED and not path.startswith(_STORE_PREFIXES):
        raise IntegrityError("backup contains an unsupported state path")
    digest, size, mode = record["digest"], record["size"], record["mode"]
    if (
        not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(mode, int)
        or isinstance(mode, bool)
        or not 0 <= mode <= 0o777
    ):
        raise IntegrityError("backup file metadata is invalid")
    return path


def _extract_archive(
    archive_path: Path, project: dict[str, Any], temporary: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with tarfile.open(archive_path, mode="r:") as archive:
        members = _archive_members(archive)
        manifest_member = members.get(_MANIFEST)
        if manifest_member is None or manifest_member.size > _MAX_MANIFEST_BYTES:
            raise IntegrityError("backup manifest is missing or too large")
        manifest_source = archive.extractfile(manifest_member)
        if manifest_source is None:
            raise IntegrityError("backup manifest cannot be read")
        value = json.loads(manifest_source.read())
        unsigned, records = _validated_manifest(value, project)
        if set(members) != {_MANIFEST, *records}:
            raise IntegrityError("backup archive and file inventory differ")
        for path, record in sorted(records.items()):
            member = members[path]
            if member.size != record["size"]:
                raise IntegrityError("backup member size differs from its inventory")
            member_source = archive.extractfile(member)
            if member_source is None:
                raise IntegrityError("backup member cannot be read")
            _extract_file(member_source, temporary / path, record)
    return value, unsigned, records


def _verified_identity(
    temporary: Path, value: dict[str, Any], unsigned: dict[str, Any], expected_key_id: str | None
) -> SigningIdentity:
    identity = SigningIdentity(temporary / "identity" / "signing.key")
    if identity.key_id != value["keyId"]:
        raise IntegrityError("backup signing identity does not match its manifest")
    if expected_key_id is not None and identity.key_id != expected_key_id:
        raise IntegrityError("backup signing identity does not match the expected identity")
    identity.verify(unsigned, value["signature"])
    return identity


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != member.name
            or member.name in members
            or not member.isfile()
        ):
            raise IntegrityError("backup archive contains an unsafe member")
        members[member.name] = member
    return members


def _extract_file(source: IO[bytes], destination: Path, record: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        while block := source.read(1024 * 1024):
            output.write(block)
            digest.update(block)
            size += len(block)
        output.flush()
        os.fsync(output.fileno())
    if size != record["size"] or "sha256:" + digest.hexdigest() != record["digest"]:
        raise IntegrityError("backup file digest or size mismatch")
    os.chmod(destination, record["mode"])


def restore_backup(
    paths: ProjectPaths,
    source: str | Path,
    *,
    expected_key_id: str | None = None,
) -> dict[str, Any]:
    supplied_archive = Path(source)
    if supplied_archive.is_symlink():
        raise ValidationError("backup archive must be a regular file")
    archive_path = supplied_archive.resolve()
    if not archive_path.is_file():
        raise ValidationError("backup archive must be a regular file")
    if paths.state.exists():
        raise ConflictError(
            "restore requires .openfoundry to be absent; retain or move existing state first"
        )
    project = load_project(paths)
    temporary = Path(tempfile.mkdtemp(dir=paths.root, prefix=".openfoundry-restore-"))
    os.chmod(temporary, 0o700)
    try:
        value, unsigned, records = _extract_archive(archive_path, project, temporary)
        identity = _verified_identity(temporary, value, unsigned, expected_key_id)
        counts = _verify_state(temporary)
        for relative in _RUNTIME_DIRECTORIES:
            directory = temporary / relative
            directory.mkdir(mode=0o700)
        os.replace(temporary, paths.state)
        descriptor = os.open(paths.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "path": str(paths.state),
        "source": str(archive_path),
        "files": len(records),
        "keyId": identity.key_id,
        "integrity": True,
        **counts,
    }
