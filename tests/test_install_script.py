import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from _environments import offline_environment
from openfoundry.install_support import (
    STARTER,
    copy_starter,
    render_template,
    upsert_managed_section,
    validate_managed_file,
)
from openfoundry.install_support import main as install_support_main
from openfoundry.modules import load_manifest
from openfoundry.schema_registry import SchemaRegistry

AGENTS_BEGIN = "<!-- BEGIN OpenFoundry OPERATOR GUIDE -->"
AGENTS_END = "<!-- END OpenFoundry OPERATOR GUIDE -->"
IGNORE_BEGIN = "# BEGIN OpenFoundry MANAGED IGNORE"
IGNORE_END = "# END OpenFoundry MANAGED IGNORE"


def test_install_script_help_and_plan_are_non_mutating(tmp_path):
    assert Path("install.sh").stat().st_mode & stat.S_IXUSR
    help_result = subprocess.run(
        ["bash", "install.sh", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--plan" in help_result.stdout
    assert "AGENTS.md" in help_result.stdout
    assert "MODEL_CARD.md" in help_result.stdout

    target = tmp_path / "My Model Project"
    plan = subprocess.run(
        ["bash", "install.sh", "--plan", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"target: {target}" in plan.stdout
    assert "project name if created: my-model-project" in plan.stdout
    assert "Preserve existing MODEL_CARD.md" in plan.stdout
    assert "No changes made." in plan.stdout
    assert not target.exists()


def test_install_script_rejects_invalid_project_name(tmp_path):
    result = subprocess.run(
        ["bash", "install.sh", "--plan", "--name", "Not Valid", str(tmp_path / "target")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "project name must satisfy" in result.stderr
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "content",
    [
        "<!-- BEGIN OpenFoundry OPERATOR GUIDE -->\n",
        "<!-- END OpenFoundry OPERATOR GUIDE -->\n",
        (
            "<!-- BEGIN OpenFoundry OPERATOR GUIDE -->\n"
            "<!-- END OpenFoundry OPERATOR GUIDE -->\n"
            "<!-- BEGIN OpenFoundry OPERATOR GUIDE -->\n"
            "<!-- END OpenFoundry OPERATOR GUIDE -->\n"
        ),
        (
            "> <!-- BEGIN OpenFoundry OPERATOR GUIDE -->\n"
            "> <!-- END OpenFoundry OPERATOR GUIDE -->\n"
        ),
    ],
)
def test_install_plan_rejects_malformed_managed_markers_without_mutation(tmp_path, content):
    target = tmp_path / "target"
    target.mkdir()
    agents = target / "AGENTS.md"
    agents.write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["bash", "install.sh", "--plan", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "managed markers are malformed" in result.stderr
    assert agents.read_text(encoding="utf-8") == content
    assert not (target / ".venv").exists()


def test_install_plan_resolves_target_before_root_guard(tmp_path):
    target = tmp_path / "root-link"
    target.symlink_to("/", target_is_directory=True)
    result = subprocess.run(
        ["bash", "install.sh", "--plan", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "filesystem root" in result.stderr


def test_install_rejects_fake_venv_python_without_executing_it(tmp_path):
    target = tmp_path / "target"
    binary = target / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    marker = tmp_path / "fake-python-executed"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    binary.chmod(0o755)
    (target / ".venv/pyvenv.cfg").write_text("home = /tmp\n", encoding="utf-8")
    (target / ".venv/.openfoundry-managed").write_text("forged marker\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", "install.sh", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "is not OpenFoundry-managed" in result.stderr
    assert not marker.exists()


def test_install_rejects_symlinked_managed_directory_before_creating_venv(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "bindings").symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        ["bash", "install.sh", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "symbolic-link directory" in result.stderr
    assert not (target / ".venv").exists()


@pytest.mark.parametrize(
    ("template", "destination_name", "begin", "end"),
    [
        (Path("templates/project/AGENTS.md"), "AGENTS.md", AGENTS_BEGIN, AGENTS_END),
        (Path("templates/project/gitignore"), ".gitignore", IGNORE_BEGIN, IGNORE_END),
    ],
)
def test_managed_sections_upgrade_once_and_preserve_project_content(
    tmp_path, template, destination_name, begin, end
):
    destination = tmp_path / destination_name
    destination.write_text(
        (
            f"project content before\n\n{begin}\n"
            f"obsolete OpenFoundry text\n{end}\n\nproject content after\n"
        ),
        encoding="utf-8",
    )
    destination.chmod(0o640)

    assert upsert_managed_section(template, destination, begin, end)
    updated = destination.read_text(encoding="utf-8")
    metadata = destination.stat()
    assert updated.startswith("project content before\n\n")
    assert updated.endswith("\n\nproject content after\n")
    assert "obsolete OpenFoundry text" not in updated
    assert updated.count(begin) == 1
    assert updated.count(end) == 1
    assert stat.S_IMODE(metadata.st_mode) == 0o640

    assert not upsert_managed_section(template, destination, begin, end)
    assert destination.read_text(encoding="utf-8") == updated
    assert destination.stat().st_ino == metadata.st_ino


@pytest.mark.parametrize(
    ("destination_name", "begin", "end"),
    [
        ("AGENTS.md", AGENTS_BEGIN, AGENTS_END),
        (".gitignore", IGNORE_BEGIN, IGNORE_END),
    ],
)
def test_managed_file_validation_rejects_non_standalone_markers(
    tmp_path, destination_name, begin, end
):
    destination = tmp_path / destination_name
    destination.write_text(f"> {begin}\n> {end}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="standalone lines"):
        validate_managed_file(destination, begin, end)


@pytest.mark.parametrize(
    ("destination_name", "begin", "end"),
    [
        ("AGENTS.md", AGENTS_BEGIN, AGENTS_END),
        (".gitignore", IGNORE_BEGIN, IGNORE_END),
    ],
)
def test_managed_file_validation_rejects_incomplete_and_duplicate_markers(
    tmp_path, destination_name, begin, end
):
    destination = tmp_path / destination_name
    malformed = [f"{begin}\n", f"{begin}\n{end}\n{begin}\n{end}\n"]

    for content in malformed:
        destination.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="exactly one ordered"):
            validate_managed_file(destination, begin, end)


def test_install_support_creates_and_preserves_templates_and_managed_files(tmp_path, capsys):
    section = tmp_path / "section"
    section.write_text(f"{IGNORE_BEGIN}\ngenerated\n{IGNORE_END}\n", encoding="utf-8")
    destination = tmp_path / ".gitignore"

    assert install_support_main(["validate", str(destination), IGNORE_BEGIN, IGNORE_END]) == 0
    assert (
        install_support_main(["upsert", str(section), str(destination), IGNORE_BEGIN, IGNORE_END])
        == 0
    )
    assert destination.read_text(encoding="utf-8") == section.read_text(encoding="utf-8")

    destination.write_text("project-ignore", encoding="utf-8")
    assert upsert_managed_section(section, destination, IGNORE_BEGIN, IGNORE_END)
    assert destination.read_text(encoding="utf-8").startswith("project-ignore\n\n")

    rendered = tmp_path / "openfoundry.yaml"
    manifest_template = Path("templates/project/openfoundry.yaml")
    assert (
        install_support_main(
            ["render", str(manifest_template), str(rendered), "factory", "local/factory"]
        )
        == 0
    )
    content = rendered.read_text(encoding="utf-8")
    assert "name: factory" in content
    assert "namespace: local/factory" in content
    assert not render_template(manifest_template, rendered, "changed", "local/changed")
    assert rendered.read_text(encoding="utf-8") == content

    model_card = tmp_path / "MODEL_CARD.md"
    model_card_template = Path("templates/project/MODEL_CARD.md")
    assert render_template(model_card_template, model_card, "factory", "local/factory")
    model_card_content = model_card.read_text(encoding="utf-8")
    assert model_card_content.startswith("# factory model card\n")
    assert "`local/factory`" in model_card_content
    assert "__OPENFOUNDRY_PROJECT_NAME__" not in model_card_content
    assert not render_template(model_card_template, model_card, "changed", "local/changed")
    assert model_card.read_text(encoding="utf-8") == model_card_content

    malformed = tmp_path / "malformed"
    malformed.write_text("no managed markers\n", encoding="utf-8")
    assert (
        install_support_main(["upsert", str(malformed), str(destination), IGNORE_BEGIN, IGNORE_END])
        == 1
    )
    assert "template markers do not match" in capsys.readouterr().err


def test_starter_copy_is_complete_and_never_overwrites(tmp_path):
    target = tmp_path / "project"
    assert copy_starter(Path.cwd(), target) == list(STARTER)
    manifest_path = target / "modules/examples/affine-regression/module.yaml"
    _manifest, code_root = load_manifest(manifest_path, target)
    assert code_root == manifest_path.parent.resolve()
    assert not list(target.rglob("__pycache__"))
    workload = target / "workloads/example-from-scratch.yaml"
    workload.write_text("edited\n", encoding="utf-8")
    assert copy_starter(Path.cwd(), target) == []
    assert workload.read_text(encoding="utf-8") == "edited\n"


def test_install_support_writes_through_an_inherited_target_after_path_swap(tmp_path):
    target_path = tmp_path / "target"
    target_path.mkdir()
    relocated = tmp_path / "relocated"
    outside = tmp_path / "outside"
    outside.mkdir()
    descriptor = os.open(target_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        target_path.rename(relocated)
        target_path.symlink_to(outside, target_is_directory=True)
        target = Path(f"/proc/self/fd/{descriptor}")
        rendered = target / "openfoundry.yaml"
        assert render_template(
            Path("templates/project/openfoundry.yaml"), rendered, "anchored", "local/anchored"
        )
    finally:
        os.close(descriptor)

    assert "name: anchored" in (relocated / "openfoundry.yaml").read_text(encoding="utf-8")
    assert not (outside / "openfoundry.yaml").exists()


def test_render_template_never_replaces_a_concurrently_created_manifest(tmp_path, monkeypatch):
    destination = tmp_path / "openfoundry.yaml"
    original_link = os.link

    def create_competing_manifest(*args, **kwargs):
        destination.write_text("created concurrently\n", encoding="utf-8")
        return original_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", create_competing_manifest)
    with pytest.raises(FileExistsError):
        render_template(
            Path("templates/project/openfoundry.yaml"), destination, "factory", "local/factory"
        )

    assert destination.read_text(encoding="utf-8") == "created concurrently\n"
    assert not list(tmp_path.glob(".openfoundry.yaml.openfoundry-*"))


def test_render_template_does_not_publish_a_partial_manifest(tmp_path, monkeypatch):
    destination = tmp_path / "openfoundry.yaml"

    def fail_sync(_descriptor):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="simulated storage failure"):
        render_template(
            Path("templates/project/openfoundry.yaml"), destination, "factory", "local/factory"
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".openfoundry.yaml.openfoundry-*"))


def test_managed_file_validation_rejects_non_regular_destination(tmp_path):
    destination = tmp_path / "AGENTS.md"
    destination.mkdir()

    with pytest.raises(ValueError, match="regular non-symbolic-link file"):
        validate_managed_file(destination, AGENTS_BEGIN, AGENTS_END)


def test_directory_installer_is_idempotent_and_rebuilds_only_its_managed_venv(tmp_path):
    target = tmp_path / "installed-factory"
    target.mkdir()
    (target / "AGENTS.md").write_text(
        f"project agent rule\n\n{AGENTS_BEGIN}\nold guide\n{AGENTS_END}\n",
        encoding="utf-8",
    )
    (target / ".gitignore").write_text(
        f"project-ignore\n\n{IGNORE_BEGIN}\nold ignore\n{IGNORE_END}\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "python-for-offline-install"
    fail_cleanup = tmp_path / "fail-cleanup"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ -f "{fail_cleanup}" ] && [ "$1" = "-" ]; then\n'
        '  case "${3:-}" in root:*|target:*) exit 70;; esac\n'
        "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        "  shift 2\n"
        f'exec "{sys.executable}" -m venv "$@"\n'
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    wrapper_argument = os.path.relpath(wrapper, Path.cwd())
    environment = offline_environment()

    first = subprocess.run(
        ["bash", "install.sh", "--python", wrapper_argument, str(target)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    gitignore = (target / ".gitignore").read_text(encoding="utf-8")
    model_card = (target / "MODEL_CARD.md").read_text(encoding="utf-8")
    first_venv_inode = (target / ".venv").stat().st_ino
    assert "OpenFoundry is ready" in first.stdout
    assert agents.startswith("project agent rule\n\n")
    assert "old guide" not in agents
    assert gitignore.startswith("project-ignore\n\n")
    assert "old ignore" not in gitignore
    assert model_card.startswith("# installed-factory model card\n")
    assert "`local/installed-factory`" in model_card
    assert (target / ".venv").is_symlink()
    assert (target / ".venv/.openfoundry-managed").is_file()
    assert (target / "modules/examples/affine-regression/module.yaml").is_file()
    git = ["git", "-C", str(target)]
    history = subprocess.run(
        [*git, "log", "--format=%s"], check=True, capture_output=True, text=True
    ).stdout
    assert history.splitlines() == ["Initialize OpenFoundry project"]
    status = subprocess.run(
        [*git, "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    assert status == ""
    entrypoint = (target / ".venv/bin/openfoundry").read_text(encoding="utf-8").splitlines()[0]
    assert "/proc/self/fd/" not in entrypoint
    assert "/dev/fd/" not in entrypoint

    second = subprocess.run(
        ["bash", "install.sh", "--python", wrapper_argument, str(target)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "OpenFoundry is ready" in second.stdout
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == agents
    assert (target / ".gitignore").read_text(encoding="utf-8") == gitignore
    assert (target / "MODEL_CARD.md").read_text(encoding="utf-8") == model_card
    assert (target / ".venv").stat().st_ino != first_venv_inode
    assert len(list((target / ".openfoundry-venvs").iterdir())) == 1
    assert not list(target.glob(".venv.openfoundry-link-*"))
    assert not list(target.glob(".venv.openfoundry-old-*"))

    fail_cleanup.touch()
    third = subprocess.run(
        ["bash", "install.sh", "--python", wrapper_argument, str(target)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert "previous OpenFoundry environment cleanup was deferred" in third.stderr
    assert (target / ".venv").is_symlink()
    assert (target / ".venv/.openfoundry-managed").is_file()
    assert len(list((target / ".openfoundry-venvs").iterdir())) == 2


def test_directory_installer_never_follows_a_swapped_target_path(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    relocated = tmp_path / "relocated"
    outside = tmp_path / "outside"
    outside.mkdir()
    wrapper = tmp_path / "python-that-swaps-target"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        f'  mv "{target}" "{relocated}"\n'
        f'  ln -s "{outside}" "{target}"\n'
        "  shift 2\n"
        f'  exec "{sys.executable}" -m venv "$@"\n'
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", "install.sh", "--python", str(wrapper), str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=offline_environment(),
    )

    assert result.returncode == 1
    assert "target pathname changed during installation" in result.stderr
    assert not list(outside.iterdir())
    assert (relocated / "openfoundry.yaml").is_file()
    assert (relocated / ".venv").is_symlink()
    assert (relocated / ".venv/.openfoundry-managed").is_file()
    entrypoint = (relocated / ".venv/bin/openfoundry").read_text(encoding="utf-8").splitlines()[0]
    assert "/proc/self/fd/" not in entrypoint
    assert "/dev/fd/" not in entrypoint


def test_interruption_after_venv_switch_never_deletes_active_environment(tmp_path):
    target = tmp_path / "target"
    activation_script = tmp_path / "activation.py"
    wrapper = tmp_path / "python-that-interrupts-activation"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        "  shift 2\n"
        f'  exec "{sys.executable}" -m venv "$@"\n'
        "fi\n"
        'if [ "$1" = "-" ]; then\n'
        '  case "${3:-}" in\n'
        "    venv.*)\n"
        f'      cat >"{activation_script}"\n'
        f"      sed -i '/^    committed = True$/a\\    os.kill(os.getpid(), 15)' "
        f'"{activation_script}"\n'
        f'      exec "{sys.executable}" "{activation_script}" "$2" "$3"\n'
        "      ;;\n"
        "  esac\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", "install.sh", "--python", str(wrapper), str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=offline_environment(),
    )

    assert result.returncode == 1
    assert "could not atomically activate" in result.stderr
    assert (target / ".venv").is_symlink()
    assert (target / ".venv/.openfoundry-managed").is_file()
    assert len(list((target / ".openfoundry-venvs").iterdir())) == 1


def test_rendered_project_templates_match_resource_contracts():
    registry = SchemaRegistry()
    replacements = {
        "__OPENFOUNDRY_PROJECT_NAME__": "installed-factory",
        "__OPENFOUNDRY_PROJECT_NAMESPACE__": "local/installed-factory",
    }
    templates = [
        Path("templates/project/openfoundry.yaml"),
        Path("templates/project/bindings/local.yaml"),
        Path("templates/project/policies/default.yaml"),
    ]

    for template in templates:
        content = template.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, value)
        registry.load(content)


def test_operator_guide_is_bounded_and_actionable():
    root_guide = Path("AGENTS.md")
    guide = Path("templates/project/AGENTS.md").read_text(encoding="utf-8")
    root_names = {path.name for path in Path.cwd().iterdir()}
    assert root_guide.name == "AGENTS.md"
    assert root_guide.is_file()
    assert "AGENTS.md" in root_names
    assert "agents.md" not in root_names
    assert guide.count("<!-- BEGIN OpenFoundry OPERATOR GUIDE -->") == 1
    assert guide.count("<!-- END OpenFoundry OPERATOR GUIDE -->") == 1
    assert len(guide.splitlines()) <= 100


def test_installer_locks_build_backend_and_disables_build_isolation():
    script = Path("install.sh").read_text(encoding="utf-8")
    build_lock = Path("requirements.build.lock").read_text(encoding="utf-8")
    assert "requirements.build.lock" in script
    assert "--only-binary=:all:" in script
    assert "--no-build-isolation --no-deps" in script
    assert "editables==0.5" in build_lock
    assert "hatchling==1.32.0" in build_lock
