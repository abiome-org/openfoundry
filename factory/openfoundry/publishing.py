from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.canonical import load_document
from openfoundry.database import AliasRepository
from openfoundry.errors import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    ValidationError,
)
from openfoundry.lineage import LineageEdge
from openfoundry.policy import PolicyDecision, promotion_gate
from openfoundry.releases import Release, ReleaseBuilder, promote_alias, verify_release
from openfoundry.workloads import DataUse, dataset_uses, project_workload

if TYPE_CHECKING:
    from openfoundry.factory import Factory


def _denied_rules(decision: PolicyDecision) -> str:
    return ", ".join(
        str(item["rule"]) for item in decision.explanations if item.get("effect") == "deny"
    )


class PublishingService:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory

    def _release_aliases(self, release: dict[str, Any]) -> dict[str, int]:
        metadata = release["metadata"]
        return {
            alias["name"]: alias["version"]
            for alias in AliasRepository(self.factory.db).list()
            if alias["uid"] == metadata["uid"] and alias["revision"] == metadata["revision"]
        }

    def list_releases(self) -> list[dict[str, Any]]:
        return [
            {
                "name": release["metadata"]["name"],
                "revision": release["metadata"]["revision"],
                "evaluationPassed": release["spec"]
                .get("extensions", {})
                .get("manifest", {})
                .get("assessment", {})
                .get("evaluation_passed"),
                "aliases": list(self._release_aliases(release)),
                "createdAt": release["metadata"]["createdAt"],
            }
            for release in self.factory.resources.latest(kind="Release")
        ]

    def resolve_release(self, reference: str) -> dict[str, Any]:
        if reference.startswith("openfoundry://"):
            return self.factory._resource_by_uri("Release", reference)
        if reference.startswith("alias/"):
            uid, revision, _version = AliasRepository(self.factory.db).get(reference[6:])
            release = self.factory.resources.get(uid, revision)
            if release["kind"] != "Release":
                raise IntegrityError("alias does not reference a release")
            return release
        return self.factory.find_resource("Release", reference.removeprefix("release/"))

    def show_release(self, name: str) -> dict[str, Any]:
        release = self.resolve_release(name)
        aliases = self._release_aliases(release)
        return {"release": release, "aliases": list(aliases), "aliasVersions": aliases}

    def create_release(
        self,
        run_id: str,
        *,
        name: str,
        intended_use: str,
        limitations: list[str] | None = None,
        promote: bool = False,
        alias: str = "candidate",
        vulnerability_report: str | Path | None = None,
        evaluation_ref: str | None = None,
    ) -> dict[str, Any]:
        self.factory._authorize("release.create")
        if promote:
            self.factory._authorize("release.promote")
        run_id = run_id.removeprefix("run/")
        run = self.factory.run_status(run_id)
        status = run["status"]
        if status.get("state") != "Succeeded":
            raise ValidationError("only a succeeded run can produce a release")
        run_resource = self.factory._run_resource(run_id)
        run_result = self.factory._run_result(run_id, status)
        model_package_ref = run_resource["spec"]["extensions"].get("modelPackageRef")
        evaluation = self._release_evaluation(
            run_id, run_resource, run_result, model_package_ref, evaluation_ref
        )
        evaluation_spec = evaluation["spec"] if evaluation else {}
        evaluation_extensions = evaluation_spec.get("extensions", {})
        evaluation_revision = evaluation["metadata"]["revision"] if evaluation else None
        artifact_digests, model_digest, state_digest = self._release_artifacts(run_result)
        admission = run_resource["spec"]["extensions"]
        source_digests = self._release_sources(run_resource, model_package_ref)
        release_artifacts = sorted({*artifact_digests, *source_digests.values()})
        required_scan_subjects = {model_digest, *source_digests.values()}
        vulnerability_summary, vulnerability_artifact, vulnerabilities_valid = (
            self._load_vulnerability_report(vulnerability_report, required_scan_subjects)
        )
        datasets = self._release_datasets(admission)
        workload = self.factory._resource_by_uri(
            "WorkloadSpec", run_resource["spec"]["workloadRef"]
        )
        data_uses = dataset_uses(project_workload(workload).stages)
        rights_valid = self.factory._input_rights_valid(datasets, data_uses)
        compatibility_passed = bool(evaluation_extensions.get("compatibilityPassed"))
        metric_scores = {
            name: value
            for name, value in dict(evaluation_spec.get("scores", {})).items()
            if not isinstance(value, bool) and isinstance(value, (int, float))
        }
        evidence = {
            "evaluation_passed": bool(evaluation_extensions.get("passed")),
            "lineage_complete": bool(self.factory.lineage.by_run(run_id)),
            "rights_valid": rights_valid,
            "compatibility_passed": compatibility_passed,
            "vulnerabilities_valid": vulnerabilities_valid,
            "vulnerabilities_present": vulnerability_report is not None,
            "metric_scores": metric_scores,
        }
        decision = promotion_gate(evidence, self.factory.policy.config.get("promotion"))
        if promote and decision.outcome == "deny":
            raise IntegrityError(f"promotion denied by gates: {_denied_rules(decision)}")
        manifest = {
            "format": "openfoundry.release/v2",
            "model": {"digest": model_digest},
            "modelPackage": {"ref": model_package_ref},
            "state": {"digest": state_digest},
            "runtime": {"name": "openfoundry.module/v1", "sources": source_digests},
            "workload": {"runId": run_id},
            "binding": {"digest": admission["bindingDigest"]},
            "dataSummary": [
                {
                    "name": item["metadata"]["name"],
                    "revision": item["metadata"]["revision"],
                    "rights": item["spec"].get("rights", {}),
                }
                for item in datasets.values()
            ],
            "evaluations": [evaluation_revision] if evaluation_revision else [],
            "limitations": limitations or [],
            "assessment": evidence,
            "intendedUse": intended_use,
            "compatibility": {
                "moduleProtocol": "openfoundry.module/v1",
                "passed": compatibility_passed,
                "evaluationRevision": evaluation_revision,
                "vectors": evaluation_extensions.get("compatibilityVectors", 0),
            },
            "provenance": {
                "runId": run_id,
                "runRef": self.factory._resource_uri(run_resource),
                "resultRef": self.factory._resource_uri(run_result),
            },
            "vulnerabilities": vulnerability_summary,
        }
        signed = ReleaseBuilder(self.factory.identity).build(manifest)
        verify_release(signed, self.factory.identity.public_bytes)
        resource = self.factory.apply_resource(
            {
                "apiVersion": "openfoundry.dev/v1alpha1",
                "kind": "Release",
                "metadata": {"name": name, "namespace": self.factory.namespace},
                "spec": {
                    "artifacts": release_artifacts,
                    "evidence": [
                        *([evaluation_revision] if evaluation_revision else []),
                        *([vulnerability_artifact] if vulnerability_artifact else []),
                    ],
                    "signatures": [signed.signature],
                    "extensions": {
                        "manifest": signed.manifest,
                        "digest": signed.digest,
                        "keyId": signed.key_id,
                    },
                },
            },
            _system=True,
        )
        metadata = resource["metadata"]
        self._record_release_lineage(
            resource,
            run_id,
            [*release_artifacts, *filter(None, [vulnerability_artifact])],
            evaluation,
        )
        if promote:
            self._promote_release(resource, alias)
        self.factory.events.append(
            type="ReleasePublished",
            source=f"openfoundry://{self.factory.namespace}",
            subject=f"Release/{name}",
            resource_uid=metadata["uid"],
            revision=metadata["revision"],
            actor=self.factory.actor,
            run_id=run_id,
            data={"releaseDigest": signed.digest, "promoted": promote},
            dataschema="https://schemas.openfoundry.dev/events/release-published/v1",
        )
        return resource

    def _release_evaluation(
        self,
        run_id: str,
        run_resource: dict[str, Any],
        run_result: dict[str, Any],
        model_package_ref: str | None,
        evaluation_ref: str | None,
    ) -> dict[str, Any] | None:
        evaluations = [
            item
            for item in self.factory.resources.list(kind="EvaluationResult")
            if item["spec"].get("evaluationRef") == f"run/{run_id}"
            and item["spec"].get("provenance", {}).get("runId") == run_id
            and item["spec"].get("provenance", {}).get("runRef")
            == self.factory._resource_uri(run_resource)
            and item["spec"].get("provenance", {}).get("runResultRef")
            == self.factory._resource_uri(run_result)
            and item["spec"].get("extensions", {}).get("runId") == run_id
            and item["spec"].get("extensions", {}).get("modelPackageRef") == model_package_ref
            and item["spec"].get("extensions", {}).get("evaluationRefs")
            == run_resource["spec"]["extensions"].get("evaluationRefs", [])
        ]
        if evaluation_ref is not None:
            evaluations = [
                item
                for item in evaluations
                if evaluation_ref
                in {item["metadata"]["revision"], self.factory._resource_uri(item)}
            ]
            if not evaluations:
                raise ValidationError("requested evaluation revision is not eligible for this run")
        if not evaluations:
            return None
        if len(evaluations) != 1:
            raise ValidationError("multiple evaluations exist; select an exact evaluation revision")
        evaluation = evaluations[0]
        return evaluation

    def _release_datasets(self, admission: dict[str, Any]) -> dict[str, dict[str, Any]]:
        admitted_inputs = admission.get("admittedInputs", {})
        if not isinstance(admitted_inputs, dict):
            raise IntegrityError("run has invalid admitted dataset references")
        return {
            name: self.factory._resource_by_uri("DatasetSnapshot", str(reference))
            for name, reference in admitted_inputs.items()
        }

    def _record_release_lineage(
        self,
        resource: dict[str, Any],
        run_id: str,
        artifacts: list[str],
        evaluation: dict[str, Any] | None,
    ) -> None:
        release_uri = self.factory._resource_uri(resource)
        self.factory.lineage.add(
            LineageEdge(
                f"run:{run_id}", release_uri, "generated", "activity", "entity", run_id=run_id
            )
        )
        for source in [
            *(f"artifact:{digest}" for digest in artifacts),
            *([self.factory._resource_uri(evaluation)] if evaluation else []),
        ]:
            self.factory.lineage.add(
                LineageEdge(
                    source, release_uri, "wasDerivedFrom", "entity", "entity", run_id=run_id
                )
            )

    @contextmanager
    def selection(self, release: dict[str, Any]) -> Iterator[PolicyDecision]:
        datasets, uses, evidence = self._promotion_inputs(release)
        with self.factory._dataset_rights_locks(list(datasets.values())):
            decision = promotion_gate(
                {**evidence, "rights_valid": self.factory._input_rights_valid(datasets, uses)},
                self.factory.policy.config.get("promotion"),
            )
            if decision.outcome == "deny":
                raise IntegrityError(f"promotion denied by gates: {_denied_rules(decision)}")
            yield decision

    def _promote_release(
        self, release: dict[str, Any], alias: str, expected_version: int | None = None
    ) -> int:
        metadata = release["metadata"]
        with self.selection(release) as decision:
            try:
                current_alias_version: int | None = AliasRepository(self.factory.db).get(alias)[2]
            except NotFoundError:
                current_alias_version = None
            if expected_version is not None and expected_version != current_alias_version:
                raise ConflictError("alias version mismatch")
            return promote_alias(
                self.factory.db,
                self.factory.events,
                name=alias,
                uid=metadata["uid"],
                revision=metadata["revision"],
                expected_version=current_alias_version,
                actor=self.factory.actor,
                policy_decision=decision,
            )

    def _promotion_inputs(
        self, release: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, list[DataUse]], dict[str, Any]]:
        extensions = release["spec"]["extensions"]
        signatures = release["spec"]["signatures"]
        if len(signatures) != 1 or extensions["keyId"] != self.factory.identity.key_id:
            raise IntegrityError("release signing identity mismatch")
        manifest = extensions["manifest"]
        verify_release(
            Release(manifest, extensions["digest"], extensions["keyId"], signatures[0]),
            self.factory.identity.public_bytes,
        )
        run = self.factory._resource_by_uri("Run", manifest["provenance"]["runRef"])
        datasets = self._release_datasets(run["spec"]["extensions"])
        workload = self.factory._resource_by_uri("WorkloadSpec", run["spec"]["workloadRef"])
        return datasets, dataset_uses(project_workload(workload).stages), manifest["assessment"]

    def promotion_decision(self, release: dict[str, Any]) -> PolicyDecision:
        datasets, uses, evidence = self._promotion_inputs(release)
        return promotion_gate(
            {**evidence, "rights_valid": self.factory._input_rights_valid(datasets, uses)},
            self.factory.policy.config.get("promotion"),
        )

    def promote_release(
        self, name: str, *, alias: str = "candidate", expected_version: int | None = None
    ) -> dict[str, Any]:
        self.factory._authorize("release.promote")
        release = self.resolve_release(name)
        version = self._promote_release(release, alias, expected_version)
        return {
            "releaseRef": self.factory._resource_uri(release),
            "alias": alias,
            "version": version,
        }

    def _release_artifacts(self, run_result: dict[str, Any]) -> tuple[list[str], str, str]:
        artifacts = [
            (output_name, value, self.factory.local_store.read_manifest(value))
            for output_name, value in run_result["spec"]["outputs"].items()
            if isinstance(value, str) and value.startswith("sha256:")
        ]
        artifact_digests = sorted({digest for _name, digest, _manifest in artifacts})
        if not artifact_digests:
            raise ValidationError("a release requires at least one model or output artifact")
        model_candidates = sorted(
            {
                digest
                for output_name, digest, artifact in artifacts
                if artifact.logical_kind in {"model", "model-package", "weights"}
                or output_name.lower().endswith((".model", ".modelpackage", ".weights"))
            }
        )
        if len(model_candidates) != 1:
            raise ValidationError(
                "a release requires exactly one aggregate model artifact with role model, "
                "model-package, or weights"
            )
        state_candidates = sorted(
            {
                digest
                for output_name, digest, artifact in artifacts
                if artifact.logical_kind in {"checkpoint", "model-state", "state"}
                or output_name.lower().endswith((".checkpoint", ".state"))
            }
        )
        if len(state_candidates) > 1:
            raise ValidationError("a release may reference only one aggregate state artifact")
        return artifact_digests, model_candidates[0], (state_candidates or model_candidates)[0]

    @staticmethod
    def _release_sources(
        run_resource: dict[str, Any], model_package_ref: str | None
    ) -> dict[str, str]:
        admission = run_resource["spec"]["extensions"]
        source_digests: dict[str, str] = dict(admission["moduleDigests"])
        if model_package_ref is not None:
            inference_admission = admission.get("inferenceAdapter")
            if not isinstance(inference_admission, dict) or not isinstance(
                inference_admission.get("sourceDigest"), str
            ):
                raise IntegrityError(
                    "legacy model compatibility evidence is ineligible for release; run again "
                    "with an independently admitted inference adapter"
                )
            source_digests["inference"] = inference_admission["sourceDigest"]
        return source_digests

    def release_evidence(self, run_id: str) -> dict[str, Any]:
        run_id = run_id.removeprefix("run/")
        status = self.factory.run_status(run_id)["status"]
        if status.get("state") != "Succeeded":
            raise ValidationError("only a succeeded run can produce release evidence")
        run_resource = self.factory._run_resource(run_id)
        _artifacts, model_digest, _state_digest = self._release_artifacts(
            self.factory._run_result(run_id, status)
        )
        sources = self._release_sources(
            run_resource, run_resource["spec"]["extensions"].get("modelPackageRef")
        )
        return {
            "scanner": {"name": "", "version": ""},
            "databaseRevision": "",
            "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "subjects": sorted({model_digest, *sources.values()}),
            "findings": [],
            "waivers": [],
        }

    def _load_vulnerability_report(
        self, report_path: str | Path | None, required_subjects: set[str]
    ) -> tuple[dict[str, Any], str | None, bool]:
        if report_path is None:
            return {"status": "not-scanned"}, None, False
        path = Path(report_path)
        report = load_document(path.read_bytes())
        if not isinstance(report, dict):
            raise ValidationError("vulnerability report must be an object")
        for field, kind in {
            "scanner": dict,
            "databaseRevision": str,
            "generatedAt": str,
            "subjects": list,
            "findings": list,
            "waivers": list,
        }.items():
            if not isinstance(report.get(field), kind):
                raise ValidationError(f"vulnerability report requires {field}")
        generated = datetime.fromisoformat(str(report["generatedAt"]).replace("Z", "+00:00"))
        if generated.tzinfo is None or generated.utcoffset() is None:
            raise ValidationError("vulnerability report time must include a timezone")
        covered = {str(item) for item in report["subjects"]}
        missing = sorted(required_subjects - covered)
        waived = {str(item) for item in report["waivers"]}
        blocking: list[str] = []
        for finding in report["findings"]:
            if not isinstance(finding, dict):
                raise ValidationError("vulnerability findings must be objects")
            identifier = str(finding.get("id", ""))
            severity = str(finding.get("severity", "unknown")).lower()
            status = str(finding.get("status", "open")).lower()
            if severity in {"critical", "high"} and status != "fixed" and identifier not in waived:
                blocking.append(identifier or "unnamed-finding")
        passed = bool(report["databaseRevision"]) and not missing and not blocking
        artifact = ArtifactBuilder(self.factory.local_store).import_path(
            path,
            logical_kind="vulnerability-report",
            provenance={
                "scanner": report["scanner"],
                "databaseRevision": report["databaseRevision"],
            },
        )
        return (
            {
                "status": "passed" if passed else "failed",
                "scanner": report["scanner"],
                "databaseRevision": report["databaseRevision"],
                "generatedAt": report["generatedAt"],
                "reportArtifact": artifact.manifest_digest,
                "missingSubjects": missing,
                "blockingFindings": sorted(blocking),
            },
            artifact.manifest_digest,
            passed,
        )
