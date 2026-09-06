from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from openfoundry.artifacts import ArtifactManifest, ChunkDescriptor
from openfoundry.errors import IntegrityError, ValidationError
from openfoundry.stores.base import ArtifactStore


@dataclass(frozen=True)
class SyncPlan:
    manifest_digest: str
    direction: str = "push"
    concurrency: int = 4
    missing_chunks: tuple[ChunkDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in {"push", "pull"}:
            raise ValidationError(
                "only push and pull are supported; mirror/bidirectional needs explicit "
                "conflict and deletion policy"
            )
        if self.concurrency < 1:
            raise ValidationError("concurrency must be positive")


class SyncEngine:
    def plan(
        self,
        source: ArtifactStore,
        destination: ArtifactStore,
        manifest_digest: str,
        *,
        direction: str = "push",
        concurrency: int = 4,
    ) -> SyncPlan:
        if direction == "pull":
            source, destination = destination, source
        manifest = source.read_manifest(manifest_digest)
        all_chunks = list(manifest.chunks) + [c for e in manifest.entries for c in e.chunks]
        missing = tuple(c for c in all_chunks if not destination.has_chunk(c.digest, c.size))
        return SyncPlan(manifest_digest, direction, concurrency, missing)

    def execute(
        self,
        plan: SyncPlan,
        source: ArtifactStore,
        destination: ArtifactStore,
        *,
        dry_run: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ArtifactManifest:
        if plan.direction == "pull":
            source, destination = destination, source
        manifest = source.read_manifest(plan.manifest_digest)
        if dry_run:
            return manifest

        def transfer(chunk: ChunkDescriptor) -> str:
            with source.read_chunk(chunk.digest) as stream:
                destination.write_chunk(chunk.digest, stream, chunk.size)
            if not destination.verify_chunk(chunk.digest, chunk.size):
                raise IntegrityError(f"destination verification failed: {chunk.digest}")
            return chunk.digest

        completed, total = 0, len(plan.missing_chunks)
        with ThreadPoolExecutor(max_workers=plan.concurrency) as pool:
            futures = [pool.submit(transfer, chunk) for chunk in plan.missing_chunks]
            for future in as_completed(futures):
                digest = future.result()
                completed += 1
                if progress:
                    progress(completed, total, digest)
        required = list(manifest.chunks) + [c for e in manifest.entries for c in e.chunks]
        if not all(destination.verify_chunk(c.digest, c.size) for c in required):
            raise IntegrityError("artifact incomplete; manifest was not published")
        destination.publish_manifest(manifest)
        return manifest

    def sync(
        self,
        source: ArtifactStore,
        destination: ArtifactStore,
        manifest_digest: str,
        *,
        direction: str = "push",
        concurrency: int = 4,
        dry_run: bool = False,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> ArtifactManifest:
        plan = self.plan(
            source, destination, manifest_digest, direction=direction, concurrency=concurrency
        )
        return self.execute(plan, source, destination, dry_run=dry_run, progress=progress)
