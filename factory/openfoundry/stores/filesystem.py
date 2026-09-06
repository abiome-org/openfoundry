from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from openfoundry.artifacts import ArtifactManifest
from openfoundry.canonical import canonical_json
from openfoundry.errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from openfoundry.ids import parse_digest
from openfoundry.stores.base import StoreCapabilities


class FilesystemStore:
    capabilities = StoreCapabilities()

    def __init__(self, root: str | Path, default_chunk_size: int = 8 * 1024 * 1024) -> None:
        self.root = Path(root).absolute()
        if default_chunk_size <= 0:
            raise ValidationError("default_chunk_size must be positive")
        self.default_chunk_size = default_chunk_size
        for name in ("blobs", "manifests", "staging", "quarantine"):
            path = self.root / name
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise ValidationError("store directories may not be symlinks")

    def _path(self, area: str, digest: str, suffix: str = "") -> Path:
        algorithm, value = parse_digest(digest)
        if algorithm != "sha256":
            raise ValidationError("store supports sha256 only")
        path = self.root / area / value[:2] / (value + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = path.parent.resolve()
        if self.root.resolve() not in (resolved_parent, *resolved_parent.parents):
            raise ValidationError("store path escapes root")
        if path.exists() and path.is_symlink():
            raise IntegrityError("symlink encountered in store")
        return path

    def has_chunk(self, digest: str, size: int | None = None) -> bool:
        path = self._path("blobs", digest)
        return path.is_file() and (size is None or path.stat().st_size == size)

    def read_chunk(self, digest: str, offset: int = 0, length: int | None = None) -> BinaryIO:
        if offset < 0 or (length is not None and length < 0):
            raise ValidationError("invalid byte range")
        path = self._path("blobs", digest)
        if not path.is_file():
            raise NotFoundError(f"chunk not found: {digest}")
        stream = path.open("rb")
        stream.seek(offset)
        if length is None:
            return stream
        try:
            data = stream.read(length)
        finally:
            stream.close()
        return io.BytesIO(data)

    def write_chunk(self, digest: str, source: BinaryIO, size: int | None = None) -> None:
        target = self._path("blobs", digest)
        if target.exists():
            if self.verify_chunk(digest, size):
                return
            raise ConflictError(f"existing chunk conflicts with {digest}")
        fd, temporary_name = tempfile.mkstemp(dir=self.root / "staging")
        calculated, written = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as sink:
                while block := source.read(1024 * 1024):
                    sink.write(block)
                    calculated.update(block)
                    written += len(block)
                sink.flush()
                os.fsync(sink.fileno())
            actual = "sha256:" + calculated.hexdigest()
            if actual != digest or (size is not None and size != written):
                raise IntegrityError("chunk digest or size mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                if not self.verify_chunk(digest, size):
                    raise ConflictError(f"concurrent chunk conflicts with {digest}") from None
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def verify_chunk(self, digest: str, size: int | None = None) -> bool:
        path = self._path("blobs", digest)
        if not path.is_file() or (size is not None and path.stat().st_size != size):
            return False
        calculated = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                calculated.update(block)
        return digest == "sha256:" + calculated.hexdigest()

    def publish_manifest(self, manifest: ArtifactManifest) -> str:
        digest, data = manifest.manifest_digest, canonical_json(manifest.to_dict())
        target = self._path("manifests", digest, ".json")
        if target.exists():
            if target.read_bytes() != data:
                raise ConflictError("immutable manifest conflict")
            return digest
        fd, name = tempfile.mkstemp(dir=self.root / "staging")
        try:
            with os.fdopen(fd, "wb") as sink:
                sink.write(data)
                sink.flush()
                os.fsync(sink.fileno())
            try:
                os.link(name, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ConflictError("immutable manifest conflict") from None
        finally:
            Path(name).unlink(missing_ok=True)
        return digest

    def read_manifest(self, digest: str) -> ArtifactManifest:
        path = self._path("manifests", digest, ".json")
        if not path.is_file():
            raise NotFoundError(f"manifest not found: {digest}")
        manifest = ArtifactManifest.from_dict(json.loads(path.read_bytes()))
        if manifest.manifest_digest != digest:
            raise IntegrityError("manifest digest mismatch")
        return manifest

    def list_manifests(self) -> Iterable[str]:
        for path in sorted((self.root / "manifests").glob("*/*.json")):
            yield "sha256:" + path.stem

    def quarantine_chunk(self, digest: str) -> None:
        source = self._path("blobs", digest)
        if source.exists():
            os.replace(source, self._path("quarantine", digest))

    def garbage_collect(self, digests: Iterable[str]) -> int:
        count = 0
        for digest in digests:
            path = self._path("quarantine", digest)
            if path.exists():
                path.unlink()
                count += 1
        return count
