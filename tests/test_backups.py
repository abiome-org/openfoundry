import os
import shutil
import tarfile
from pathlib import Path

import openfoundry.backups as backups_module
import pytest
import yaml
from openfoundry.artifacts import ArtifactBuilder
from openfoundry.backups import restore_backup
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.errors import ConflictError, IntegrityError, ValidationError
from openfoundry.factory import Factory


def _project(root: Path) -> ProjectPaths:
    root.mkdir()
    (root / "openfoundry.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Project",
                "metadata": {"name": "backup-test", "namespace": "local/backup-test"},
                "spec": {"owners": ["local-user"], "extensions": {}},
            }
        )
    )
    shutil.copytree("modules/examples/statistical", root / "modules/examples/statistical")
    return ProjectPaths(root)


def _backup(tmp_path: Path) -> tuple[ProjectPaths, Path, str, str, list[str], list[str]]:
    paths = _project(tmp_path / "source")
    bootstrap(paths)
    data = paths.root / "numbers.jsonl"
    data.write_text('{"value": 1}\n{"value": 2}\n')
    archive = tmp_path / "factory.openfoundry-backup"
    with Factory(paths) as factory:
        factory.add_data(
            data,
            name="numbers",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        factory.secrets.put("registry", "credential", "test")
        token = factory.secrets.get("local-api-token", "api-authentication").decode()
        key_id = factory.identity.key_id
        resources = [resource["metadata"]["revision"] for resource in factory.list_resources()]
        events = [event.id for event in factory.events.query()]
        report = factory.backup(archive)
        assert report["integrity"]
        assert report["artifacts"] >= 1
    return paths, archive, key_id, token, resources, events


def test_complete_backup_restores_identity_secrets_metadata_and_artifacts(tmp_path):
    source, archive, key_id, token, resources, events = _backup(tmp_path)
    assert archive.stat().st_mode & 0o777 == 0o600
    target = _project(tmp_path / "restored")

    report = restore_backup(target, archive, expected_key_id=key_id)

    assert report["integrity"]
    assert report["keyId"] == key_id
    assert target.signing_key.stat().st_mode & 0o777 == 0o600
    assert target.secret_key.stat().st_mode & 0o777 == 0o600
    with Factory(target) as restored:
        assert restored.identity.key_id == key_id
        assert restored.authenticate(token)
        assert restored.secrets.get("registry", "test") == b"credential"
        assert restored.verify_data("numbers")
        assert [
            resource["metadata"]["revision"] for resource in restored.list_resources()
        ] == resources
        assert [restored.events.get(event_id).id for event_id in events] == events
        manifests = list(restored.local_store.list_manifests())
        assert manifests
        assert all(
            ArtifactBuilder(restored.local_store).verify(restored.local_store.read_manifest(digest))
            for digest in manifests
        )
        assert restored.validate_module(target.root / "modules/examples/statistical/module.yaml")[
            "valid"
        ]
    assert source.state.exists()


def test_restore_rejects_tampering_atomically(tmp_path):
    source, archive, _key_id, _token, _resources, _events = _backup(tmp_path)
    tampered = tmp_path / "tampered.openfoundry-backup"
    contents = bytearray(archive.read_bytes())
    secret_key = source.secret_key.read_bytes()
    assert contents.count(secret_key) == 1
    offset = contents.index(secret_key)
    contents[offset] ^= 1
    tampered.write_bytes(contents)
    target = _project(tmp_path / "tampered-restore")

    with pytest.raises(IntegrityError, match="digest or size mismatch"):
        restore_backup(target, tampered)

    assert not target.state.exists()
    assert not list(target.root.glob(".openfoundry-restore-*"))


def test_restore_checks_external_identity_and_refuses_existing_state(tmp_path):
    _source, archive, _key_id, _token, _resources, _events = _backup(tmp_path)
    target = _project(tmp_path / "wrong-identity")
    with pytest.raises(IntegrityError, match="expected identity"):
        restore_backup(target, archive, expected_key_id="sha256:" + "0" * 64)
    assert not target.state.exists()

    bootstrap(target)
    with pytest.raises(ConflictError, match=r"requires \.openfoundry to be absent"):
        restore_backup(target, archive)


def test_restore_requires_the_exact_project_configuration(tmp_path):
    _source, archive, _key_id, _token, _resources, _events = _backup(tmp_path)
    target = _project(tmp_path / "changed-project")
    project = yaml.safe_load(target.config.read_text())
    project["spec"]["owners"] = ["different-owner"]
    target.config.write_text(yaml.safe_dump(project))

    with pytest.raises(IntegrityError, match="does not belong to this project"):
        restore_backup(target, archive)

    assert not target.state.exists()


def test_backup_creation_and_restore_share_the_manifest_size_limit(tmp_path, monkeypatch):
    source, archive, _key_id, _token, _resources, _events = _backup(tmp_path)
    monkeypatch.setattr(backups_module, "_MAX_MANIFEST_BYTES", 1)
    with Factory(source) as factory:
        oversized = tmp_path / "oversized.openfoundry-backup"
        with pytest.raises(ValidationError, match="manifest exceeds"):
            factory.backup(oversized)
        assert not oversized.exists()

    target = _project(tmp_path / "limited-restore")
    with pytest.raises(IntegrityError, match="manifest is missing or too large"):
        restore_backup(target, archive)
    assert not target.state.exists()


def test_restore_rejects_unsafe_archive_members(tmp_path):
    archive = tmp_path / "unsafe.openfoundry-backup"
    with tarfile.open(archive, "w") as value:
        path = tmp_path / "payload"
        path.write_bytes(b"x")
        value.add(path, arcname="../escape")
    target = _project(tmp_path / "unsafe-restore")

    with pytest.raises(IntegrityError, match="unsafe member"):
        restore_backup(target, archive)

    assert not (tmp_path / "escape").exists()
    assert not target.state.exists()
    assert not list(target.root.glob(".openfoundry-restore-*"))


def test_backup_refuses_store_symlinks_and_existing_destination(tmp_path):
    paths = _project(tmp_path / "source")
    bootstrap(paths)
    archive = tmp_path / "existing.openfoundry-backup"
    archive.write_bytes(b"keep")
    with Factory(paths) as factory:
        with pytest.raises(ConflictError, match="already exists"):
            factory.backup(archive)

        outside = tmp_path / "outside"
        outside.write_bytes(b"content")
        link = paths.store / "manifests" / "link"
        os.symlink(outside, link)
        with pytest.raises(IntegrityError, match="symbolic links"):
            factory.backup(tmp_path / "symlink.openfoundry-backup")
