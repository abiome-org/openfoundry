#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "factory/openfoundry/_version.py"
SOURCE_URL = "https://github.com/abiome-org/openfoundry"


def _version() -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', VERSION_FILE.read_text(), re.MULTILINE)
    if match is None:
        raise RuntimeError("could not read the distribution version")
    return match.group(1)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_build(destination: Path, source_date_epoch: int) -> list[Path]:
    environment = os.environ | {
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": str(source_date_epoch),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(destination),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"distribution build failed:\n{completed.stdout}{completed.stderr}")
    artifacts = sorted(destination.iterdir())
    if len(artifacts) != 2 or {item.suffix for item in artifacts} != {".whl", ".gz"}:
        raise RuntimeError("distribution build did not produce exactly one wheel and one sdist")
    return artifacts


def _build_reproducibly(destination: Path, source_date_epoch: int) -> list[Path]:
    with tempfile.TemporaryDirectory(prefix="openfoundry-release-build-") as temporary_name:
        temporary = Path(temporary_name)
        first = _run_build(temporary / "first", source_date_epoch)
        second = _run_build(temporary / "second", source_date_epoch)
        first_by_name = {item.name: item for item in first}
        second_by_name = {item.name: item for item in second}
        if first_by_name.keys() != second_by_name.keys():
            raise RuntimeError("repeated builds produced different artifact names")
        for name, artifact in first_by_name.items():
            if artifact.read_bytes() != second_by_name[name].read_bytes():
                raise RuntimeError(f"distribution artifact is not reproducible: {name}")
            shutil.copyfile(artifact, destination / name)
    return sorted(destination.glob("openfoundry-*"))


def _spdx(version: str, artifacts: list[Path], created: str) -> dict[str, Any]:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    dependencies = []
    for index, requirement in enumerate(metadata["dependencies"], start=1):
        name = re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0]
        dependencies.append(
            {
                "SPDXID": f"SPDXRef-Dependency-{index}",
                "name": name,
                "versionInfo": requirement.removeprefix(name),
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
    distribution_packages = [
        {
            "SPDXID": f"SPDXRef-Distribution-{index}",
            "name": artifact.name,
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "Apache-2.0",
            "licenseDeclared": "Apache-2.0",
            "copyrightText": "NOASSERTION",
            "checksums": [{"algorithm": "SHA256", "checksumValue": _digest(artifact)}],
        }
        for index, artifact in enumerate(artifacts, start=1)
    ]
    checksums = [package["checksums"][0] for package in distribution_packages]
    namespace = hashlib.sha256(
        json.dumps(checksums, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"openfoundry-{version}",
        "documentNamespace": f"https://openfoundry.dev/spdx/distribution/{namespace}",
        "creationInfo": {"created": created, "creators": [f"Tool: OpenFoundry {version}"]},
        "packages": [*distribution_packages, *dependencies],
        "relationships": [
            {
                "spdxElementId": package["SPDXID"],
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": dependency["SPDXID"],
            }
            for package in distribution_packages
            for dependency in dependencies
        ],
    }


def _provenance(
    artifacts: list[Path],
    *,
    revision: str,
    source_url: str,
    source_date_epoch: int,
    builder_id: str,
    source_patch_digest: str | None,
) -> dict[str, Any]:
    external_parameters: dict[str, Any] = {"sourceDateEpoch": source_date_epoch}
    if source_patch_digest is not None:
        external_parameters["sourcePatch"] = {"sha256": source_patch_digest}
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": item.name, "digest": {"sha256": _digest(item)}} for item in artifacts],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://pypa.io/build",
                "externalParameters": external_parameters,
                "internalParameters": {},
                "resolvedDependencies": [
                    {"uri": f"git+{source_url}@{revision}", "digest": {"gitCommit": revision}}
                ],
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {"invocationId": revision},
            },
        },
    }


def _validate_vulnerability_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("vulnerability report must be readable JSON") from exc
    if (
        not isinstance(report, dict)
        or not {
            "scanner",
            "databaseRevision",
            "generatedAt",
            "findings",
        }
        <= report.keys()
    ):
        raise RuntimeError("vulnerability report is missing required fields")
    scanner = report["scanner"]
    if (
        not isinstance(scanner, dict)
        or not isinstance(scanner.get("name"), str)
        or not isinstance(scanner.get("version"), str)
        or not isinstance(report["databaseRevision"], str)
        or not isinstance(report["generatedAt"], str)
        or not isinstance(report["findings"], list)
    ):
        raise RuntimeError("vulnerability report has invalid fields")
    waivers = report.get("waivers", [])
    if not isinstance(waivers, list):
        raise RuntimeError("vulnerability report waivers must be a list")
    waived = {
        waiver.get("findingId")
        for waiver in waivers
        if isinstance(waiver, dict) and isinstance(waiver.get("findingId"), str)
    }
    blocked = []
    for finding in report["findings"]:
        if not isinstance(finding, dict):
            raise RuntimeError("vulnerability report has an invalid finding")
        if (
            str(finding.get("severity", "")).lower() in {"high", "critical"}
            and str(finding.get("status", "open")).lower() == "open"
            and finding.get("id") not in waived
        ):
            blocked.append(str(finding.get("id", "unknown")))
    if blocked:
        raise RuntimeError(f"unwaived high or critical vulnerabilities: {', '.join(blocked)}")
    return report


def _sign(path: Path, command: str) -> Path:
    arguments = shlex.split(command)
    if not arguments:
        raise RuntimeError("release signer command is empty")
    completed = subprocess.run([*arguments, str(path)], capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"release signer failed: {completed.stderr.strip()}")
    signature = path.with_name(path.name + ".sig")
    if not signature.is_file() or not signature.read_bytes():
        raise RuntimeError(f"release signer did not create {signature.name}")
    return signature


def _source_patch_digest() -> str | None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if status.returncode:
        raise RuntimeError("could not inspect the source checkout")
    if not status.stdout:
        return None
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if patch.returncode or untracked.returncode:
        raise RuntimeError("could not capture the candidate source patch")
    digest = hashlib.sha256(b"git-diff\0" + patch.stdout)
    for raw_name in sorted(name for name in untracked.stdout.split(b"\0") if name):
        path = ROOT / os.fsdecode(raw_name)
        digest.update(b"\0untracked\0" + raw_name + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        else:
            raise RuntimeError(f"unsupported untracked source entry: {os.fsdecode(raw_name)}")
    return digest.hexdigest()


def _verify_source(revision: str, version: str, *, candidate: bool) -> str | None:
    observed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if observed.returncode or observed.stdout.strip() != revision:
        raise RuntimeError("source revision does not match the checked-out Git revision")
    source_patch_digest = _source_patch_digest()
    if source_patch_digest is not None and not candidate:
        raise RuntimeError("a final release requires a clean checkout")
    if candidate:
        return source_patch_digest
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if tags.returncode or f"v{version}" not in tags.stdout.splitlines():
        raise RuntimeError(f"a final release must build tag v{version}")
    return None


def prepare(arguments: argparse.Namespace) -> dict[str, Any]:
    destination = arguments.output.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("release output directory must be absent or empty")
    if not re.fullmatch(r"[0-9a-f]{40,64}", arguments.source_revision):
        raise RuntimeError("source revision must be a full hexadecimal commit ID")
    if arguments.source_date_epoch < 0:
        raise RuntimeError("source date epoch must be non-negative")
    version = _version()
    vulnerability_report = (
        _validate_vulnerability_report(arguments.vulnerability_report)
        if arguments.vulnerability_report is not None
        else None
    )
    if vulnerability_report is None and not arguments.candidate:
        raise RuntimeError("a final release requires a vulnerability report")
    signer = arguments.sign_command or os.environ.get("OPENFOUNDRY_RELEASE_SIGN_COMMAND", "")
    if not signer and not arguments.candidate:
        raise RuntimeError("a final release requires OPENFOUNDRY_RELEASE_SIGN_COMMAND")
    source_patch_digest = _verify_source(
        arguments.source_revision, version, candidate=arguments.candidate
    )
    builder_id = arguments.builder_id or (
        f"{arguments.source_url}/blob/{arguments.source_revision}/tools/release.py"
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"openfoundry-release-{destination.name}-"))
    try:
        artifacts = _build_reproducibly(staging, arguments.source_date_epoch)
        if _source_patch_digest() != source_patch_digest:
            raise RuntimeError("source checkout changed during the release build")
        created = (
            datetime.fromtimestamp(arguments.source_date_epoch, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        sbom_path = staging / f"openfoundry-{version}.spdx.json"
        _write_json(sbom_path, _spdx(version, artifacts, created))
        provenance_path = staging / f"openfoundry-{version}.provenance.json"
        _write_json(
            provenance_path,
            _provenance(
                artifacts,
                revision=arguments.source_revision,
                source_url=arguments.source_url,
                source_date_epoch=arguments.source_date_epoch,
                builder_id=builder_id,
                source_patch_digest=source_patch_digest,
            ),
        )
        inventoried = [*artifacts, sbom_path, provenance_path]
        if vulnerability_report is not None:
            vulnerability_path = staging / f"openfoundry-{version}.vulnerabilities.json"
            _write_json(vulnerability_path, vulnerability_report)
            inventoried.append(vulnerability_path)
        checksums = staging / "SHA256SUMS"
        checksums.write_text(
            "".join(f"{_digest(item)}  {item.name}\n" for item in sorted(inventoried)),
            encoding="utf-8",
        )
        signature = _sign(checksums, signer) if signer else None
        if destination.exists():
            destination.rmdir()
        shutil.move(str(staging), str(destination))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "version": version,
        "reproducible": True,
        "artifacts": [item.name for item in artifacts],
        "sbom": sbom_path.name,
        "provenance": provenance_path.name,
        "checksums": checksums.name,
        "signature": None if signature is None else signature.name,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--builder-id")
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--vulnerability-report", type=Path)
    parser.add_argument("--sign-command")
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="Allow an unsigned candidate without a vulnerability report.",
    )
    return parser


def main() -> int:
    try:
        report = prepare(_parser().parse_args())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
