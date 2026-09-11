from __future__ import annotations

import difflib
import html
import json
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openfoundry.artifacts import ArtifactBuilder
from openfoundry.errors import ValidationError
from openfoundry.experiment_definition import ExperimentDefinition

if TYPE_CHECKING:
    from openfoundry.experiments import ExperimentService


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": name, "before": before.get(name), "after": after.get(name)}
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    ]


def _measurements(service: ExperimentService, outputs: dict[str, Any]) -> dict[str, Any]:
    stages = {
        stage: service.artifact_json(outputs[f"{stage}.measurement"])
        for stage in ("train", "evaluate")
        if f"{stage}.measurement" in outputs
    }
    return {
        "stages": stages,
        "wallSeconds": sum(item["wallSeconds"] for item in stages.values()) if stages else None,
        "cpuSeconds": sum(item["cpuSeconds"] for item in stages.values()) if stages else None,
        "gpuSeconds": sum(float(item.get("gpuSeconds", 0.0)) for item in stages.values())
        if stages
        else None,
        "monetaryCostUSD": sum(float(item.get("monetaryCostUSD", 0.0)) for item in stages.values())
        if stages
        else None,
        # Legacy alias kept for one release; prefer monetaryCostUSD.
        "monetaryCost": sum(float(item.get("monetaryCostUSD", 0.0)) for item in stages.values())
        if stages
        else None,
    }


def _subject(service: ExperimentService, run_id: str) -> dict[str, Any]:
    run_id = run_id.removeprefix("run/")
    metadata = service.metadata(run_id)
    if not metadata:
        raise ValidationError("run is not a script experiment")
    status = service.factory.run_status(run_id)
    result = service.factory._run_result(run_id, status["status"])
    evaluation = service.factory._evaluation_result(f"run/{run_id}")
    definition = metadata["definition"]
    candidate = metadata["candidate"]
    outputs = result["spec"]["outputs"]
    run = service.factory._run_resource(run_id)
    return {
        "runId": run_id,
        "name": candidate,
        "experiment": definition["name"],
        "rationale": definition["candidates"][candidate]["rationale"],
        "parameters": definition["candidates"][candidate]["parameters"],
        "scores": evaluation["spec"]["scores"],
        "evaluationRefs": evaluation["spec"]["extensions"]["evaluationRefs"],
        "sources": metadata["sources"],
        "outputs": outputs,
        "datasets": run["spec"]["extensions"]["admittedInputs"],
        "evaluationInputs": _evaluation_inputs(service, run),
        "measurement": _measurements(service, outputs),
        "reproduce": {
            "runRef": service.factory._resource_uri(run),
            "resultRef": service.factory._resource_uri(result),
            "sourceArtifacts": run["spec"]["extensions"]["moduleDigests"],
            "environments": result["spec"]["admission"]["environments"],
            "definitionDigest": metadata["definitionDigest"],
        },
    }


def _evaluation_inputs(service: ExperimentService, run: dict[str, Any]) -> dict[str, Any]:
    workload = service.factory._resource_by_uri("WorkloadSpec", run["spec"]["workloadRef"])
    stage = next(item for item in workload["spec"]["graph"]["stages"] if item["name"] == "evaluate")
    admitted = run["spec"]["extensions"]["admittedInputs"]
    config = stage["config"]
    substitutions = {
        "inputs": dict.fromkeys(stage["inputs"], "input"),
        "parameters": config["parameters"],
        "output": "output",
    }
    return {
        "datasets": {
            key: admitted[reference]
            for key, reference in stage["inputs"].items()
            if reference in admitted
        },
        "arguments": [value.format_map(substitutions) for value in config["command"]],
    }


def _examples(service: ExperimentService, subject: dict[str, Any]) -> dict[str, dict[str, Any]]:
    digest = subject["outputs"].get("evaluate.examples")
    if not digest:
        return {}
    examples = service.artifact_json(digest)
    indexed = {str(item["id"]): item for item in examples}
    if len(indexed) != len(examples):
        raise ValidationError("evaluation example ids must be unique")
    return indexed


def _example_changes(
    service: ExperimentService, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    left, right = _examples(service, before), _examples(service, after)
    changes = []
    for identity in sorted(left.keys() & right.keys()):
        old, new = left[identity], right[identity]
        if old.get("prediction") != new.get("prediction") or old.get("score") != new.get("score"):
            comparable = old.get("expected") == new.get("expected") and old.get("input") == new.get(
                "input"
            )
            delta = (
                (float(new["score"]) - float(old["score"]))
                if comparable
                and all(isinstance(item.get("score"), (float, int)) for item in (old, new))
                else None
            )
            changes.append({"id": identity, "before": old, "after": new, "delta": delta})
    return {
        "items": changes[:50],
        "total": len(changes),
        "truncated": len(changes) > 50,
        "added": len(right.keys() - left.keys()),
        "removed": len(left.keys() - right.keys()),
    }


def _source_text(service: ExperimentService, subject: dict[str, Any], stage: str, name: str) -> str:
    digest = subject["reproduce"]["sourceArtifacts"][stage]
    builder = ArtifactBuilder(service.factory.local_store)
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "source"
        builder.restore(service.factory.local_store.read_manifest(digest), target)
        with tarfile.open(target / "payload") as archive:
            try:
                member = archive.extractfile(name)
            except KeyError:
                return ""
            return member.read(65536).decode(errors="replace") if member else ""


def _source_changes(
    service: ExperimentService, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    total = 0
    for stage in ("train", "evaluate"):
        for change in _changes(before["sources"][stage], after["sources"][stage]):
            total += 1
            if len(changes) == 25:
                continue
            name = change["name"]
            diff = "".join(
                difflib.unified_diff(
                    _source_text(service, before, stage, name).splitlines(keepends=True),
                    _source_text(service, after, stage, name).splitlines(keepends=True),
                    fromfile=f"baseline/{stage}/{name}",
                    tofile=f"candidate/{stage}/{name}",
                )
            )
            changes.append(
                {"stage": stage, **change, "diff": diff[:16000], "truncated": len(diff) > 16000}
            )
    return {"items": changes, "total": total, "truncated": total > 25}


def _comparison(
    definition: ExperimentDefinition, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    reasons = []
    if before["evaluationRefs"] != after["evaluationRefs"]:
        reasons.append("evaluation specification changed")
    if before["sources"]["evaluate"] != after["sources"]["evaluate"]:
        reasons.append("evaluation source changed")
    if before["evaluationInputs"] != after["evaluationInputs"]:
        reasons.append("evaluation data or arguments changed")
    comparable = not reasons
    metrics: list[dict[str, Any]] = []
    regressions = []
    for name, metric in definition.metrics.items():
        left, right = before["scores"].get(name), after["scores"].get(name)
        delta = improvement = None
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in (left, right)
        ):
            comparable = False
            reasons.append(f"metric unavailable in one run: {name}")
        else:
            delta = right - left
            improvement = delta if metric.direction == "maximize" else -delta
            if metric.maxRegression is not None and improvement < -metric.maxRegression:
                regressions.append(name)
        metrics.append(
            {
                "name": name,
                "baseline": left,
                "candidate": right,
                "delta": delta,
                "improvement": improvement,
                "direction": metric.direction,
            }
        )
    primary = next(item for item in metrics if item["name"] == definition.primaryMetric)
    acceptable = bool(after["scores"].get("passed")) and not regressions
    decision = "review" if not comparable else "baseline"
    if comparable and acceptable:
        decision = (
            "candidate"
            if primary["improvement"] > 0
            else "tie"
            if primary["improvement"] == 0
            else "baseline"
        )
    return {
        "comparable": comparable,
        "reasons": reasons,
        "decision": decision,
        "acceptable": acceptable,
        "regressions": regressions,
        "metrics": metrics,
    }


def review(
    service: ExperimentService, run_id: str, baseline: str | None = None, *, details: bool = False
) -> dict[str, Any]:
    after = _subject(service, run_id)
    definition = ExperimentDefinition.model_validate(service.metadata(after["runId"])["definition"])
    if baseline is None:
        baseline = next(
            (
                item["id"]
                for item in service.list(definition.name)
                if item["candidate"] == definition.baseline
                and item["state"] == "succeeded"
                and item["id"] < after["runId"]
            ),
            None,
        )
    report: dict[str, Any] = {
        "objective": definition.objective,
        "modelCard": service.metadata(after["runId"])["modelCard"],
        "candidate": after,
        "limits": {
            "scope": "each-stage",
            "configured": definition.limits.model_dump(exclude_none=True),
            "executor": definition.executor,
        },
        "baseline": None,
        "comparison": None,
    }
    if baseline:
        before = _subject(service, baseline)
        if before["experiment"] != after["experiment"]:
            raise ValidationError("comparison runs belong to different experiments")
        report.update(
            {
                "baseline": before,
                "comparison": _comparison(definition, before, after),
                "changes": {
                    "parameters": _changes(before["parameters"], after["parameters"]),
                    "data": _changes(before["datasets"], after["datasets"]),
                    "source": _source_changes(service, before, after),
                },
                "examples": _example_changes(service, before, after),
            }
        )
    return report if details else summarize_review(report)


def summarize_review(report: dict[str, Any]) -> dict[str, Any]:
    summary = {key: value for key, value in report.items() if key != "modelCard"}
    for name in ("candidate", "baseline"):
        subject = report[name]
        summary[name] = (
            {
                key: subject[key]
                for key in (
                    "runId",
                    "name",
                    "experiment",
                    "rationale",
                    "parameters",
                    "scores",
                    "measurement",
                )
            }
            if subject
            else None
        )
    if "examples" in report:
        summary["examples"] = {
            key: value for key, value in report["examples"].items() if key != "items"
        }
        source = report["changes"]["source"]
        summary["changes"] = {
            **report["changes"],
            "source": {key: value for key, value in source.items() if key != "items"},
        }
    return summary


def write_review(report: dict[str, Any], path: Path) -> None:
    candidate = report["candidate"]

    def escape(value: Any) -> str:
        return html.escape(str(value))

    comparison = report.get("comparison") or {}
    metrics = comparison.get("metrics", [])
    rows = "".join(
        f"<tr><td>{escape(row['name'])}</td><td>{escape(row['baseline'])}</td>"
        f"<td>{escape(row['candidate'])}</td>"
        f"<td>{escape(round(row['delta'], 6) if row['delta'] is not None else '—')}</td></tr>"
        for row in metrics
    )
    examples = "".join(
        f"<details><summary>Example {escape(item['id'])} · change {escape(item['delta'])}</summary>"
        f"<pre>{escape(json.dumps(item, indent=2, ensure_ascii=False))}</pre></details>"
        for item in report.get("examples", {}).get("items", [])
    )
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenFoundry candidate review</title>
<style>
body{{font:16px/1.6 system-ui;margin:48px auto;padding:0 24px;max-width:1000px;
color:#18232c;background:#f9faf9}}h1{{line-height:1.2}}
table{{border-collapse:collapse;width:100%;background:white}}
td,th{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef1ee;padding:16px}}
details{{margin:12px 0}}small{{color:#566}}</style>
<small>OPENFOUNDRY · CANDIDATE REVIEW</small><h1>{escape(candidate["name"])}</h1>
<p>{escape(report["objective"])}</p><p>{escape(candidate["rationale"])}</p>
<p>Decision: <strong>{escape(comparison.get("decision", "uncompared"))}</strong>
 · Run {escape(candidate["runId"])}</p>
<table><thead><tr><th>Metric</th><th>Baseline</th><th>Candidate</th><th>Delta</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Measured execution</h2><pre>{escape(json.dumps(candidate["measurement"], indent=2))}</pre>
<h2>Changes</h2><pre>{escape(json.dumps(report.get("changes", {}), indent=2))}</pre>
<h2>Changed behavior</h2>{examples}<details><summary>Complete evidence</summary>
<pre>{escape(json.dumps(report, indent=2, ensure_ascii=False))}</pre></details></html>"""
    path.write_text(content)
