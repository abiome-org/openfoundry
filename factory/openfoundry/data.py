from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openfoundry.artifacts import ArtifactBuilder, ArtifactManifest
from openfoundry.errors import IntegrityError, ValidationError
from openfoundry.stores.base import ArtifactStore

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class DatasetSnapshot:
    name: str
    mode: str
    source: str
    partitions: tuple[dict[str, Any], ...]
    sample_schema: str = "application/octet-stream"
    rights: dict[str, Any] = field(default_factory=dict)
    statistics: dict[str, Any] = field(default_factory=dict)
    cursor_policy: dict[str, Any] = field(default_factory=dict)
    artifact: ArtifactManifest | None = None


def _safe_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if not parsed.scheme or parsed.username or parsed.password or parsed.query:
        raise ValidationError("URI must have a scheme and contain no credentials or query secrets")
    return uri


def _external_files(root: Path, exclude_runtime: bool) -> list[Path]:
    excluded = {".git", ".openfoundry", ".amp"} if exclude_runtime else set()
    result = []
    for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or (not stat.S_ISREG(mode) and not stat.S_ISDIR(mode)):
            raise ValidationError("dataset paths may not contain symlinks or special files")
        if path.is_file():
            result.append(path)
    return result


class DataService:
    def __init__(self, store: ArtifactStore | None = None) -> None:
        self.store = store

    def add(
        self,
        name: str,
        source: str | Path,
        mode: str = "copy",
        *,
        exclude_runtime: bool = False,
        rights: dict[str, Any] | None = None,
        sample_schema: str = "application/octet-stream",
        statistics: dict[str, Any] | None = None,
        cursor_policy: dict[str, Any] | None = None,
    ) -> DatasetSnapshot:
        if not _NAME.fullmatch(name):
            raise ValidationError("invalid dataset name")
        if mode == "stream":
            uri = _safe_uri(str(source))
            if not cursor_policy:
                raise ValidationError("stream mode requires a cursor/version policy")
            return DatasetSnapshot(
                name, mode, uri, (), sample_schema, rights or {}, statistics or {}, cursor_policy
            )
        if mode not in {"copy", "register", "mount"}:
            raise ValidationError("unsupported dataset mode")
        path = Path(source).absolute()
        if not path.exists() or path.is_symlink():
            raise ValidationError("invalid dataset path")
        if mode == "copy":
            if self.store is None:
                raise ValidationError("copy mode requires an artifact store")
            artifact = ArtifactBuilder(self.store).import_path(path)
            parts = tuple(
                {"digest": c.digest, "size": c.size, "offset": c.offset} for c in artifact.chunks
            )
            return DatasetSnapshot(
                name,
                mode,
                str(path),
                parts,
                sample_schema,
                rights or {},
                statistics or {},
                artifact=artifact,
            )
        partitions = []
        files = _external_files(path, exclude_runtime)
        for file in files:
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            info = file.stat()
            relative = file.relative_to(path).as_posix() if path.is_dir() else file.name
            partitions.append(
                {
                    "path": relative,
                    "digest": "sha256:" + digest.hexdigest(),
                    "size": info.st_size,
                    "mtime_ns": str(info.st_mtime_ns),
                    "inode": str(info.st_ino),
                    "mode": stat.S_IMODE(info.st_mode),
                }
            )
        return DatasetSnapshot(
            name, mode, str(path), tuple(partitions), sample_schema, rights or {}, statistics or {}
        )

    def verify(self, snapshot: DatasetSnapshot) -> bool:
        if snapshot.mode == "copy":
            if snapshot.artifact is None or self.store is None:
                return False
            return ArtifactBuilder(self.store).verify(snapshot.artifact)
        if snapshot.mode == "stream":
            return True
        root = Path(snapshot.source)
        for expected in snapshot.partitions:
            path = root / expected["path"] if root.is_dir() else root
            try:
                info = path.stat()
            except FileNotFoundError as exc:
                raise IntegrityError("registered dataset drift detected") from exc
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            if (info.st_size, info.st_mtime_ns, "sha256:" + digest.hexdigest()) != (
                expected["size"],
                int(expected["mtime_ns"]),
                expected["digest"],
            ):
                raise IntegrityError("registered dataset drift detected")
        return True

    def copy(self, name: str, source: str | Path, **kwargs: Any) -> DatasetSnapshot:
        return self.add(name, source, "copy", **kwargs)

    def register(self, name: str, source: str | Path, **kwargs: Any) -> DatasetSnapshot:
        return self.add(name, source, "register", **kwargs)

    def mount(self, name: str, source: str | Path, **kwargs: Any) -> DatasetSnapshot:
        return self.add(name, source, "mount", **kwargs)

    def stream(self, name: str, source: str | Path, **kwargs: Any) -> DatasetSnapshot:
        return self.add(name, source, "stream", **kwargs)
