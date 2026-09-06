from __future__ import annotations

import argparse
import json
import os
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Operation = Literal["validate", "prepare", "run", "quiesce", "checkpoint", "restore", "stop"]


class ProtocolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["openfoundry.module/v1"] = "openfoundry.module/v1"
    operation: Operation
    inputs: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ProtocolError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ProtocolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["openfoundry.module/v1"] = "openfoundry.module/v1"
    status: Literal["ok", "error"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    error: ProtocolError | None = None


Handler = Callable[[ProtocolRequest], ProtocolResult | Mapping[str, Any] | None]


def dispatch(handlers: Mapping[str, Handler], request_path: Path, result_path: Path) -> int:
    try:
        request = ProtocolRequest.model_validate_json(request_path.read_bytes())
        handler = handlers.get(request.operation)
        if handler is None:
            raise ValueError(f"operation not implemented: {request.operation}")
        value = handler(request)
        result = (
            value
            if isinstance(value, ProtocolResult)
            else ProtocolResult(status="ok", **(dict(value) if value is not None else {}))
        )
    except Exception as exc:
        result = ProtocolResult(
            status="error",
            error=ProtocolError(
                code=type(exc).__name__,
                message=str(exc),
                details={"traceback": traceback.format_exc()},
            ),
        )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    )
    os.replace(temporary, result_path)
    return 0 if result.status == "ok" else 1


def main(handlers: Mapping[str, Handler]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, default=os.getenv("OPENFOUNDRY_REQUEST_FILE"))
    parser.add_argument("--result", type=Path, default=os.getenv("OPENFOUNDRY_RESULT_FILE"))
    args = parser.parse_args()
    if args.request is None or args.result is None:
        parser.error("request and result paths are required")
    return dispatch(handlers, args.request, args.result)
