from __future__ import annotations

from collections import Counter

from openfoundry.sdk import ProtocolRequest, ProtocolResult, main


def validate(_request: ProtocolRequest) -> ProtocolResult:
    return ProtocolResult(status="ok", outputs={"module": "text-frequency", "protocol": "v1"})


def run(request: ProtocolRequest) -> ProtocolResult:
    text = str(request.inputs["text"])
    counts = Counter(text.lower().split())
    return ProtocolResult(status="ok", outputs={"counts": dict(sorted(counts.items()))})


if __name__ == "__main__":
    raise SystemExit(main({"validate": validate, "run": run}))
