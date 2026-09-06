from __future__ import annotations

import json
from pathlib import Path

from openfoundry.sdk import ProtocolRequest, ProtocolResult, main


def validate(_request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"protocol": "openfoundry.module/v1"})


def run(request: ProtocolRequest) -> ProtocolResult:
    weights = Path(request.state["path"])
    if weights.is_dir():
        weights /= "model.json"
    state = json.loads(weights.read_text())
    value = float(request.inputs["input"])
    prediction = float(state["slope"]) * value + float(state["intercept"])
    return ProtocolResult(status="ok", outputs={"prediction": prediction}, state=request.state)


if __name__ == "__main__":
    raise SystemExit(main({"validate": validate, "run": run}))
