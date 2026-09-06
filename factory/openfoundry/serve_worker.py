from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as ProtocolValidationError

from openfoundry.canonical import canonical_json
from openfoundry.errors import OpenFoundryError, ValidationError
from openfoundry.modules import validate_contract
from openfoundry.sdk import ProtocolRequest, ProtocolResult

_INHERITED = {"HOME", "LANG", "PATH", "TZ"}


class ServingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment: str
    release: str
    modelPackageRef: str | None = None
    operation: str
    method: str = "predict"
    config: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any]
    signatures: dict[str, Any]
    command: list[str]
    wrapper: list[str] = Field(default_factory=list)
    cwd: str
    host: str
    port: int
    timeoutSeconds: float | None = None


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, Any]
    seed: int | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


def create_app(config: ServingConfig, work_dir: Path) -> FastAPI:
    app = FastAPI(title="openfoundry-serving", docs_url=None, redoc_url=None, openapi_url=None)
    counters = {"requests": 0, "failures": 0}
    requests_dir = work_dir / "requests"

    @app.exception_handler(OpenFoundryError)
    async def openfoundry_error(_request: Request, exc: OpenFoundryError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "deployment": config.deployment,
            "release": config.release,
            "modelPackageRef": config.modelPackageRef,
            "protocol": "openfoundry.module/v1",
            "method": config.method,
            "requests": counters["requests"],
            "failures": counters["failures"],
        }

    @app.post("/v1/infer")
    def infer(request: InferenceRequest) -> dict[str, Any]:
        validate_contract(config.signatures["input"], request.inputs, "inference input")
        counters["requests"] += 1
        request_id = uuid.uuid4().hex
        directory = requests_dir / request_id
        directory.mkdir(parents=True, exist_ok=True)
        protocol = ProtocolRequest(
            operation=config.operation,  # type: ignore[arg-type]
            inputs=request.inputs,
            state=config.state,
            config=config.config,
            context={
                "deployment": config.deployment,
                "requestId": request_id,
                "inference": {
                    "method": config.method,
                    "seed": request.seed,
                    "parameters": request.parameters,
                },
            },
        )
        request_path = directory / "request.json"
        result_path = directory / "result.json"
        request_path.write_bytes(canonical_json(protocol.model_dump(mode="json")))
        environment = {key: value for key, value in os.environ.items() if key in _INHERITED}
        environment.update(
            {
                "OPENFOUNDRY_REQUEST_FILE": str(request_path),
                "OPENFOUNDRY_RESULT_FILE": str(result_path),
                "OPENFOUNDRY_RUN_ID": request_id,
            }
        )
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [*config.wrapper, *config.command],
                cwd=config.cwd,
                env=environment,
                capture_output=True,
                check=False,
                timeout=config.timeoutSeconds,
            )
        except subprocess.TimeoutExpired:
            counters["failures"] += 1
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(
                status_code=504, detail={"code": "inference_timeout", "requestId": request_id}
            ) from None
        try:
            if not result_path.exists():
                counters["failures"] += 1
                sys.stderr.write(
                    f"request {request_id}: adapter exited {completed.returncode} without a "
                    f"result: {completed.stderr[-2000:].decode(errors='replace')}\n"
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "no_result",
                        "requestId": request_id,
                        "exitCode": completed.returncode,
                    },
                )
            result = ProtocolResult.model_validate_json(result_path.read_bytes())
            if result.status != "ok" or completed.returncode:
                counters["failures"] += 1
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": result.error.code if result.error else "adapter_failed",
                        "requestId": request_id,
                        "exitCode": completed.returncode,
                    },
                )
            validate_contract(config.signatures["output"], result.outputs, "inference output")
        except (ProtocolValidationError, ValidationError):
            counters["failures"] += 1
            raise HTTPException(
                status_code=502, detail={"code": "invalid_result", "requestId": request_id}
            ) from None
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return {
            "outputs": result.outputs,
            "release": config.release,
            "modelPackageRef": config.modelPackageRef,
            "requestId": request_id,
            "durationMs": round((time.perf_counter() - started) * 1000, 3),
        }

    return app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = ServingConfig.model_validate(json.loads(args.config.read_bytes()))
    uvicorn.run(
        create_app(config, args.config.parent),
        host=config.host,
        port=config.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
