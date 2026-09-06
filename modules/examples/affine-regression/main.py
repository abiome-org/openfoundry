from __future__ import annotations

import json
import os
from pathlib import Path

from openfoundry.sdk import ProtocolRequest, ProtocolResult, main


def validate(_request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"protocol": "openfoundry.module/v1"})


def run(request: ProtocolRequest) -> ProtocolResult:
    action = request.config.get("action", "train")
    if action == "train":
        dataset = Path(request.inputs["dataset"]["path"])
        examples = [json.loads(line) for line in dataset.read_text().splitlines() if line]
        slope = float(request.config.get("initialSlope", 0.0))
        intercept = float(request.config.get("initialIntercept", 0.0))
        rate = float(request.config.get("learningRate", 0.02))
        steps = int(request.config.get("steps", 500))
        for _ in range(steps):
            slope_gradient = 0.0
            intercept_gradient = 0.0
            for example in examples:
                x, y = float(example["input"]), float(example["target"])
                error = slope * x + intercept - y
                slope_gradient += 2.0 * error * x / len(examples)
                intercept_gradient += 2.0 * error / len(examples)
            slope -= rate * slope_gradient
            intercept -= rate * intercept_gradient
        loss = sum(
            (slope * float(item["input"]) + intercept - float(item["target"])) ** 2
            for item in examples
        ) / len(examples)
        model = {"slope": slope, "intercept": intercept, "format": "json-affine/v1"}
        model_path = Path(os.environ["OPENFOUNDRY_RESULT_FILE"]).parent / "model.json"
        model_path.write_text(json.dumps(model, sort_keys=True))
        return ProtocolResult(
            status="ok",
            outputs={"modelState": model, "loss": loss},
            state=model,
            metrics={"training_loss": loss, "steps": steps},
            artifacts=[
                {"name": "model", "kind": "model", "path": model_path.name},
                {"name": "checkpoint", "kind": "checkpoint", "path": model_path.name},
            ],
        )
    if action == "evaluate":
        state = request.inputs["modelState"]
        slope = float(state["slope"])
        intercept = float(state["intercept"])
        tolerance = float(request.config.get("tolerance", 0.01))
        error = max(abs(slope - 2.0), abs(intercept - 1.0))
        return ProtocolResult(
            status="ok",
            outputs={"passed": error <= tolerance, "maximumError": error},
            metrics={"maximum_error": error},
        )
    raise ValueError(f"unsupported action: {action}")


def checkpoint(request: ProtocolRequest) -> ProtocolResult:
    output = Path(os.environ["OPENFOUNDRY_RESULT_FILE"]).parent / "checkpoint.json"
    output.write_text(json.dumps(request.state, sort_keys=True))
    return ProtocolResult(
        status="ok",
        state=request.state,
        artifacts=[{"name": "checkpoint", "kind": "checkpoint", "path": output.name}],
    )


if __name__ == "__main__":
    raise SystemExit(main({"validate": validate, "run": run, "checkpoint": checkpoint}))
