import io
import shutil
import subprocess
import tarfile

import pytest
import yaml
from openfoundry.errors import ConfigurationError, ValidationError
from openfoundry.modules import (
    extract_module_package,
    git_source,
    load_manifest,
    package_module,
    worktree_state,
)


def test_worktree_state_tracks_commits_patches_and_untracked_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    (root / "a.txt").write_text("a\n")

    uncommitted = worktree_state(root)
    assert uncommitted["commit"] is None
    assert uncommitted["dirty"]
    assert uncommitted["untracked"] == ["a.txt"]

    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
    clean = worktree_state(root)
    assert clean["commit"]
    assert not clean["dirty"]
    assert clean["patch"] == b""
    assert git_source(root)["commit"] == clean["commit"]

    (root / "a.txt").write_text("b\n")
    patched = worktree_state(root)
    assert patched["dirty"]
    assert patched["patch"]
    assert patched["patchDigest"] != clean["patchDigest"]
    with pytest.raises(ValidationError, match="allow_dirty"):
        git_source(root)

    subprocess.run(["git", "checkout", "--", "a.txt"], cwd=root, check=True)
    (root / "new.txt").write_text("n\n")
    assert git_source(root, allow_dirty=True)["untracked"] == ["new.txt"]

    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    with pytest.raises(ConfigurationError, match="Git working tree"):
        worktree_state(outside)


def test_reproducible_package_and_links_rejected(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "x").write_text("x")
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    assert package_module(root, a) == package_module(root, b)
    (root / "link").symlink_to("x")
    with pytest.raises(ValidationError, match="unsupported"):
        package_module(root, tmp_path / "c.tar")


def test_safe_module_extraction_and_traversal_rejection(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    (root / "entry.py").write_text("print('ok')")
    package = tmp_path / "module.tar"
    package_module(root, package)
    destination = extract_module_package(package, tmp_path / "extracted")
    assert (destination / "entry.py").read_text() == "print('ok')"
    with pytest.raises(ValidationError, match="already exists"):
        extract_module_package(package, destination)

    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe, "w") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValidationError, match="unsafe"):
        extract_module_package(unsafe, tmp_path / "unsafe-out")
    assert not (tmp_path / "escape").exists()


def test_module_dependency_lock_is_verified(tmp_path):
    project = tmp_path / "project"
    module = project / "modules/example"
    shutil.copytree("modules/examples/statistical", module)
    load_manifest(module / "module.yaml", project)
    (module / "requirements.lock").write_text("tampered\n")
    with pytest.raises(ValidationError, match="digest does not match"):
        load_manifest(module / "module.yaml", project)


def test_module_contract_dynamic_references_are_rejected(tmp_path):
    project = tmp_path / "project"
    module = project / "modules/example"
    shutil.copytree("modules/examples/statistical", module)
    manifest_path = module / "module.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["spec"]["contracts"] = {"input": {"$dynamicRef": "#input"}}
    manifest_path.write_text(yaml.safe_dump(manifest))

    with pytest.raises(ValidationError, match="references are not supported"):
        load_manifest(manifest_path, project)


def test_module_contract_allows_instance_property_named_ref(tmp_path):
    project = tmp_path / "project"
    module = project / "modules/example"
    shutil.copytree("modules/examples/statistical", module)
    manifest_path = module / "module.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["spec"]["contracts"] = {
        "input": {"type": "object", "properties": {"$ref": {"type": "string"}}}
    }
    manifest_path.write_text(yaml.safe_dump(manifest))

    loaded, _root = load_manifest(manifest_path, project)
    assert "$ref" in loaded.schemas["input"]["properties"]
