from __future__ import annotations

import json
import os
from pathlib import Path

from openfoundry.sdk import ProtocolRequest, ProtocolResult, main


def validate(_request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"module": "statistical", "protocol": "v1"})


def run(request: ProtocolRequest) -> ProtocolResult:
    action = request.config.get("action", "train")
    if action == "train":
        dataset = request.inputs["dataset"]
        path = Path(dataset["path"])
        values = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    values.append(float(json.loads(line)["value"]))
        if not values:
            raise ValueError("training dataset contains no values")
        model = {
            "kind": "statistical-mean",
            "count": len(values),
            "mean": sum(values) / len(values),
            "minimum": min(values),
            "maximum": max(values),
        }
        output = Path(os.environ["OPENFOUNDRY_RESULT_FILE"]).parent / "model.json"
        output.write_text(json.dumps(model, sort_keys=True), encoding="utf-8")
        return ProtocolResult(
            status="ok",
            outputs={"mean": model["mean"], "count": model["count"]},
            metrics={"samples": len(values)},
            artifacts=[{"name": "model", "kind": "model", "path": output.name}],
        )
    if action == "evaluate":
        actual = float(request.inputs["mean"])
        expected = float(request.config["expected"])
        error = abs(actual - expected)
        passed = error <= float(request.config.get("tolerance", 1e-12))
        report = {"passed": passed, "absoluteError": error, "expected": expected, "actual": actual}
        output = Path(os.environ["OPENFOUNDRY_RESULT_FILE"]).parent / "evaluation.json"
        output.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        return ProtocolResult(
            status="ok",
            outputs={"passed": passed, "compatibilityPassed": passed, "absoluteError": error},
            metrics={"absolute_error": error},
            artifacts=[{"name": "evaluation", "kind": "evaluation", "path": output.name}],
        )
    raise ValueError(f"unsupported action: {action}")


if __name__ == "__main__":
    raise SystemExit(main({"validate": validate, "run": run}))
