import hashlib
import io

import pytest
from openfoundry.artifacts import ArtifactBuilder
from openfoundry.errors import IntegrityError
from openfoundry.stores.filesystem import FilesystemStore
from openfoundry.sync import SyncEngine


def test_sync_plans_missing_content_and_preserves_identity(tmp_path):
    source = FilesystemStore(tmp_path / "source")
    destination = FilesystemStore(tmp_path / "destination")
    payload = tmp_path / "payload"
    payload.write_bytes(b"abcdefgh")
    manifest = ArtifactBuilder(source, chunk_size=2).import_path(payload)
    first_chunk = manifest.chunks[0]
    with source.read_chunk(first_chunk.digest) as stream:
        destination.write_chunk(first_chunk.digest, stream, first_chunk.size)
    plan = SyncEngine().plan(source, destination, manifest.manifest_digest, concurrency=2)
    assert first_chunk.digest not in {chunk.digest for chunk in plan.missing_chunks}
    assert not list(destination.list_manifests())
    result = SyncEngine().execute(plan, source, destination)
    assert result.manifest_digest == manifest.manifest_digest
    assert destination.read_manifest(manifest.manifest_digest).digest == manifest.digest
    assert SyncEngine().plan(source, destination, manifest.manifest_digest).missing_chunks == ()


def test_interrupted_sync_never_publishes_manifest_and_resumes(tmp_path, monkeypatch):
    source = FilesystemStore(tmp_path / "source")
    destination = FilesystemStore(tmp_path / "destination")
    payload = tmp_path / "payload"
    payload.write_bytes(b"abcdefgh")
    manifest = ArtifactBuilder(source, chunk_size=2).import_path(payload)
    plan = SyncEngine().plan(source, destination, manifest.manifest_digest, concurrency=1)
    original = destination.write_chunk
    calls = 0

    def interrupted(digest, stream, size=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        return original(digest, stream, size)

    monkeypatch.setattr(destination, "write_chunk", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        SyncEngine().execute(plan, source, destination)
    assert not list(destination.list_manifests())
    monkeypatch.setattr(destination, "write_chunk", original)
    resumed = SyncEngine().plan(source, destination, manifest.manifest_digest, concurrency=1)
    assert len(resumed.missing_chunks) < len(plan.missing_chunks)
    SyncEngine().execute(resumed, source, destination)
    assert list(destination.list_manifests()) == [manifest.manifest_digest]


def test_sync_detects_source_corruption(tmp_path):
    source = FilesystemStore(tmp_path / "source")
    destination = FilesystemStore(tmp_path / "destination")
    payload = tmp_path / "payload"
    payload.write_bytes(b"abc")
    manifest = ArtifactBuilder(source).import_path(payload)
    digest_hex = manifest.chunks[0].digest.split(":", 1)[1]
    (tmp_path / "source" / "blobs" / digest_hex[:2] / digest_hex).write_bytes(b"corrupt")
    with pytest.raises(IntegrityError):
        SyncEngine().sync(source, destination, manifest.manifest_digest)
    assert not list(destination.list_manifests())


def test_sync_does_not_delete_unrelated_destination_content(tmp_path):
    source = FilesystemStore(tmp_path / "source")
    destination = FilesystemStore(tmp_path / "destination")
    unrelated = b"unrelated"
    digest = "sha256:" + hashlib.sha256(unrelated).hexdigest()
    destination.write_chunk(digest, io.BytesIO(unrelated))
    payload = tmp_path / "payload"
    payload.write_bytes(b"payload")
    manifest = ArtifactBuilder(source).import_path(payload)
    SyncEngine().sync(source, destination, manifest.manifest_digest)
    assert destination.verify_chunk(digest)
