import io

import pytest
from openfoundry.artifacts import ArtifactBuilder, AtomicCheckpointPublisher, TreeEntry
from openfoundry.errors import ConflictError, IntegrityError, ValidationError
from openfoundry.stores.filesystem import FilesystemStore


@pytest.mark.parametrize("logical_kind", ["directory", "model", "checkpoint-shard"])
def test_file_and_tree_roundtrip(tmp_path, logical_kind):
    store = FilesystemStore(tmp_path / "store")
    builder = ArtifactBuilder(store, chunk_size=2)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"abc")
    (source / "nested").mkdir()
    (source / "nested" / "b").write_bytes(b"def")
    manifest = builder.import_path(source, logical_kind=logical_kind)
    assert manifest.logical_kind == logical_kind
    assert builder.verify(manifest)
    builder.restore(manifest, tmp_path / "restored")
    assert builder.verify_restored(manifest, tmp_path / "restored")
    assert (tmp_path / "restored/nested/b").read_bytes() == b"def"
    assert store.read_manifest(manifest.manifest_digest) == manifest
    (tmp_path / "restored/nested/b").write_bytes(b"changed")
    assert not builder.verify_restored(manifest, tmp_path / "restored")


def test_file_restore_uses_payload_name(tmp_path):
    source = tmp_path / "file"
    source.write_bytes(b"content")
    builder = ArtifactBuilder(FilesystemStore(tmp_path / "store"))
    manifest = builder.import_path(source)
    builder.restore(manifest, tmp_path / "out")
    assert (tmp_path / "out/payload").read_bytes() == b"content"
    assert builder.verify_restored(manifest, tmp_path / "out")
    (tmp_path / "out/extra").write_text("unexpected")
    assert not builder.verify_restored(manifest, tmp_path / "out")


def test_empty_directory_roundtrip(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    builder = ArtifactBuilder(FilesystemStore(tmp_path / "store"))
    manifest = builder.import_path(source)
    restored = tmp_path / "restored"
    builder.restore(manifest, restored)
    assert list(restored.iterdir()) == []
    assert builder.verify_restored(manifest, restored)


def test_repeated_chunks_are_valid_and_restore_in_order(tmp_path):
    source = tmp_path / "repeated"
    source.write_bytes(b"abab")
    builder = ArtifactBuilder(FilesystemStore(tmp_path / "store"), chunk_size=2)
    manifest = builder.import_path(source)
    assert manifest.chunks[0].digest == manifest.chunks[1].digest
    builder.restore(manifest, tmp_path / "out")
    assert (tmp_path / "out/payload").read_bytes() == b"abab"


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", ""])
def test_tree_entry_rejects_traversal(path):
    with pytest.raises(ValidationError):
        TreeEntry(path, 0o644, 0, "sha256:" + "0" * 64)


def test_source_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValidationError):
        ArtifactBuilder(FilesystemStore(tmp_path / "store")).import_path(link)


def test_chunk_corruption_and_idempotence(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    digest = "sha256:" + __import__("hashlib").sha256(b"x").hexdigest()
    store.write_chunk(digest, io.BytesIO(b"x"), 1)
    store.write_chunk(digest, io.BytesIO(b"x"), 1)
    path = next((tmp_path / "store/blobs").glob("*/*"))
    path.write_bytes(b"y")
    assert not store.verify_chunk(digest)
    with pytest.raises(ConflictError):
        store.write_chunk(digest, io.BytesIO(b"x"), 1)


def test_restore_rejects_chunk_tampering(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    builder = ArtifactBuilder(store)
    manifest = builder.import_path(source)
    digest_hex = manifest.chunks[0].digest.removeprefix("sha256:")
    (tmp_path / "store/blobs" / digest_hex[:2] / digest_hex).write_bytes(b"tampered")

    with pytest.raises(IntegrityError, match="chunk integrity"):
        builder.restore(manifest, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_directory_restore_rejects_tree_index_tampering(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"payload")
    builder = ArtifactBuilder(store)
    manifest = builder.import_path(source)
    digest_hex = manifest.chunks[0].digest.removeprefix("sha256:")
    (tmp_path / "store/blobs" / digest_hex[:2] / digest_hex).write_bytes(b"tampered")

    with pytest.raises(IntegrityError, match="chunk integrity"):
        builder.restore(manifest, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_quarantine_then_gc(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    digest = "sha256:" + __import__("hashlib").sha256(b"x").hexdigest()
    store.write_chunk(digest, io.BytesIO(b"x"))
    store.quarantine_chunk(digest)
    assert store.garbage_collect([digest]) == 1


def test_atomic_checkpoint_publishes_only_verified_shards(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    builder = ArtifactBuilder(store)
    shard_path = tmp_path / "shard"
    shard_path.write_bytes(b"weights")
    shard = builder.import_path(shard_path, logical_kind="checkpoint-shard")
    checkpoint = AtomicCheckpointPublisher(store).publish(
        {"model-state": shard},
        {"workload": "sha256:" + "1" * 64},
    )
    assert checkpoint.logical_kind == "checkpoint"
    assert builder.verify(checkpoint)
    assert builder.verify_graph(checkpoint)
    assert checkpoint.provenance["components"] == {"model-state": shard.manifest_digest}
    digest_hex = shard.chunks[0].digest.removeprefix("sha256:")
    blob = tmp_path / "store/blobs" / digest_hex[:2] / digest_hex
    blob.write_bytes(b"corrupt")
    assert not builder.verify_graph(checkpoint)
    with pytest.raises(IntegrityError, match="verification failed"):
        AtomicCheckpointPublisher(store).publish(
            {"model-state": shard},
            {},
        )


def test_atomic_checkpoint_rejects_dangling_or_implicit_component_state(tmp_path):
    store = FilesystemStore(tmp_path / "store")
    publisher = AtomicCheckpointPublisher(store)
    with pytest.raises(ValidationError, match="role names"):
        publisher.publish({}, {})
