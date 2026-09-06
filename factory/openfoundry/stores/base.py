from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable

from openfoundry.artifacts import ArtifactManifest


@dataclass(frozen=True)
class StoreCapabilities:
    read: bool = True
    write: bool = True
    list: bool = True
    range_read: bool = True
    multipart: bool = False
    atomic_publish: bool = True
    versioning: bool = False
    server_side_copy: bool = False


@runtime_checkable
class ArtifactStore(Protocol):
    capabilities: StoreCapabilities

    def has_chunk(self, digest: str, size: int | None = None) -> bool: ...
    def read_chunk(self, digest: str, offset: int = 0, length: int | None = None) -> BinaryIO: ...
    def write_chunk(self, digest: str, source: BinaryIO, size: int | None = None) -> None: ...
    def verify_chunk(self, digest: str, size: int | None = None) -> bool: ...
    def publish_manifest(self, manifest: ArtifactManifest) -> str: ...
    def read_manifest(self, digest: str) -> ArtifactManifest: ...
    def list_manifests(self) -> Iterable[str]: ...
