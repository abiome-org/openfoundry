import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import openfoundry.database as database_module
from _environments import create_environment

SOURCE_DATE_EPOCH = "1700000000"


def _run(command, *, cwd=None, env=None, timeout=120):
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def _environment(path: Path) -> tuple[Path, Path]:
    return create_environment(path), path / "bin/openfoundry"


def test_release_bundle_and_candidate_install_upgrade_backup_restore(tmp_path):
    revision = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    vulnerability_report = tmp_path / "vulnerabilities.json"
    vulnerability_report.write_text(
        json.dumps(
            {
                "scanner": {"name": "release-test-scanner", "version": "1"},
                "databaseRevision": "test-db-1",
                "generatedAt": "2023-11-14T22:13:20Z",
                "findings": [],
                "waivers": [],
            }
        )
    )
    signer = tmp_path / "signer.py"
    signer.write_text(
        """import hashlib
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
source.with_name(source.name + ".sig").write_text(hashlib.sha256(source.read_bytes()).hexdigest())
"""
    )
    distribution = tmp_path / "release"
    source_marker = Path("release-source-state-test.tmp")
    source_marker.write_text("candidate-only source\n")
    try:
        release = _run(
            [
                sys.executable,
                "tools/release.py",
                "--candidate",
                "--output",
                distribution,
                "--source-revision",
                revision,
                "--source-date-epoch",
                SOURCE_DATE_EPOCH,
                "--vulnerability-report",
                vulnerability_report,
                "--sign-command",
                f"{sys.executable} {signer}",
            ]
        )
    finally:
        source_marker.unlink(missing_ok=True)
    report = json.loads(release.stdout)
    assert report == {
        "version": "2.0.0",
        "reproducible": True,
        "artifacts": [
            "openfoundry-2.0.0-py3-none-any.whl",
            "openfoundry-2.0.0.tar.gz",
        ],
        "sbom": "openfoundry-2.0.0.spdx.json",
        "provenance": "openfoundry-2.0.0.provenance.json",
        "checksums": "SHA256SUMS",
        "signature": "SHA256SUMS.sig",
    }

    checksums = (distribution / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 5
    for line in checksums:
        expected, name = line.split("  ", 1)
        assert hashlib.sha256((distribution / name).read_bytes()).hexdigest() == expected
    assert (distribution / "SHA256SUMS.sig").read_text() == hashlib.sha256(
        (distribution / "SHA256SUMS").read_bytes()
    ).hexdigest()

    sbom = json.loads((distribution / report["sbom"]).read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert {package["versionInfo"] for package in sbom["packages"][:2]} == {"2.0.0"}
    assert {package["downloadLocation"] for package in sbom["packages"][:2]} == {"NOASSERTION"}
    assert all("externalRefs" not in package for package in sbom["packages"][:2])
    provenance = json.loads((distribution / report["provenance"]).read_text())
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    assert provenance["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"] == {
        "gitCommit": revision
    }
    assert provenance["predicate"]["runDetails"]["builder"]["id"] == (
        f"https://github.com/abiome-org/openfoundry/blob/{revision}/tools/release.py"
    )
    source_patch = provenance["predicate"]["buildDefinition"]["externalParameters"]["sourcePatch"]
    assert set(source_patch) == {"sha256"}
    assert len(source_patch["sha256"]) == 64

    wheel = distribution / "openfoundry-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    assert metadata["Version"] == "2.0.0"
    assert metadata["Requires-Python"] == "<3.13,>=3.11"

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    python, openfoundry_command = _environment(tmp_path / "wheel-environment")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"} | {
        "PIP_NO_INDEX": "1"
    }
    _run(
        [python, "-m", "pip", "install", "--no-index", "--no-deps", wheel],
        cwd=isolated,
        env=environment,
    )
    _run([python, "-m", "pip", "check"], cwd=isolated, env=environment)
    installed_probe = (
        "import json,openfoundry,pathlib,sys; print(json.dumps({'version':openfoundry.__version__, "
        "'isolated':pathlib.Path(openfoundry.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.prefix).resolve())}))"
    )
    installed = _run(
        [
            python,
            "-I",
            "-c",
            installed_probe,
        ],
        cwd=isolated,
        env=environment,
    )
    assert json.loads(installed.stdout) == {"version": "2.0.0", "isolated": True}

    project = tmp_path / "candidate-project"
    project.mkdir()
    (project / "openfoundry.yaml").write_text(
        """apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata: {name: candidate, namespace: local/candidate}
spec: {owners: [release-test], extensions: {}}
"""
    )
    _run(["git", "init", "-q"], cwd=project)
    _run(["git", "config", "user.name", "OpenFoundry distribution test"], cwd=project)
    _run(["git", "config", "user.email", "distribution-test@openfoundry.invalid"], cwd=project)
    _run(["git", "add", "openfoundry.yaml"], cwd=project)
    _run(["git", "commit", "-qm", "Initialize candidate project"], cwd=project)
    bootstrap = _run(
        [openfoundry_command, "--project", project, "--output", "json", "bootstrap"],
        cwd=isolated,
        env=environment,
    )
    assert json.loads(bootstrap.stdout)["ready"]
    assert json.loads(
        _run(
            [openfoundry_command, "--project", project, "--output", "json", "doctor"],
            cwd=isolated,
            env=environment,
        ).stdout
    )["ready"]
    backup = tmp_path / "candidate.openfoundry-backup"
    backup_report = json.loads(
        _run(
            [
                openfoundry_command,
                "--project",
                project,
                "--output",
                "json",
                "admin",
                "backup",
                backup,
            ],
            cwd=isolated,
            env=environment,
        ).stdout
    )
    restored = tmp_path / "restored-project"
    restored.mkdir()
    shutil.copy2(project / "openfoundry.yaml", restored / "openfoundry.yaml")
    restore_report = json.loads(
        _run(
            [
                openfoundry_command,
                "--project",
                restored,
                "--output",
                "json",
                "admin",
                "restore",
                backup,
                "--expected-key-id",
                backup_report["keyId"],
            ],
            cwd=isolated,
            env=environment,
        ).stdout
    )
    assert restore_report["integrity"]
    assert restore_report["keyId"] == backup_report["keyId"]

    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO schema_migrations VALUES(?)", ((1,), (2,), (3,)))
    connection.executescript(
        database_module._SCHEMA + database_module._SCHEMA_V2 + database_module._SCHEMA_V3
    )
    connection.close()
    upgrade_probe = (
        "from openfoundry.database import Database; import sys; db=Database(sys.argv[1]); "
        "print(db.connection.execute('select max(version) from schema_migrations')"
        ".fetchone()[0]); db.close()"
    )
    upgraded = _run(
        [
            python,
            "-I",
            "-c",
            upgrade_probe,
            legacy,
        ],
        cwd=isolated,
        env=environment,
    )
    assert upgraded.stdout.strip() == "6"

    sdist = distribution / "openfoundry-2.0.0.tar.gz"
    with tarfile.open(sdist, "r:gz") as archive:
        members = {item.name.split("/", 1)[1] for item in archive if "/" in item.name}
    assert {"README.md", "CHANGELOG.md", "docs/walkthrough.md", "install.sh"} <= members
    sdist_python, _ = _environment(tmp_path / "sdist-environment")
    _run(
        [
            sdist_python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-build-isolation",
            "--no-deps",
            sdist,
        ],
        cwd=isolated,
        env=environment,
    )
    _run([sdist_python, "-m", "pip", "check"], cwd=isolated, env=environment)
    assert (
        _run(
            [sdist_python, "-I", "-c", "import openfoundry; print(openfoundry.__version__)"],
            cwd=isolated,
            env=environment,
        ).stdout.strip()
        == "2.0.0"
    )


def test_release_refuses_blocking_vulnerabilities_before_writing_output(tmp_path):
    revision = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    report = tmp_path / "blocked.json"
    report.write_text(
        json.dumps(
            {
                "scanner": {"name": "scanner", "version": "1"},
                "databaseRevision": "db-1",
                "generatedAt": "2023-11-14T22:13:20Z",
                "findings": [{"id": "CVE-test", "severity": "critical", "status": "open"}],
            }
        )
    )
    output = tmp_path / "blocked-release"
    completed = subprocess.run(
        [
            sys.executable,
            "tools/release.py",
            "--output",
            output,
            "--source-revision",
            revision,
            "--source-date-epoch",
            SOURCE_DATE_EPOCH,
            "--vulnerability-report",
            report,
            "--sign-command",
            "unused",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "unwaived high or critical vulnerabilities" in completed.stderr
    assert not output.exists()
