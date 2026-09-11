from __future__ import annotations

import json
import math
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("JSON object contains duplicate keys")
    return value


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def _read_json(path: Path) -> Any:
    return json.loads(
        path.read_bytes(),
        object_pairs_hook=_unique_object,
        parse_float=_finite_float,
        parse_constant=_finite_float,
    )


def _output_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("script output must be a relative path within its output directory")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("script output escapes its output directory")
    return path


def _arguments(request: dict[str, Any], output: Path) -> list[str]:
    inputs = {
        key: value["path"] if isinstance(value, dict) and "path" in value else value
        for key, value in request["inputs"].items()
    }
    substitutions = {
        "inputs": inputs,
        "parameters": request["config"]["parameters"],
        "output": str(output),
    }
    return [argument.format_map(substitutions) for argument in request["config"]["command"]]


def _metrics(path: Path) -> dict[str, Any]:
    values = _read_json(path)
    if not isinstance(values, dict) or not all(
        isinstance(value, (int, float)) and math.isfinite(value) for value in values.values()
    ):
        raise ValueError("metrics must be a JSON object of finite numbers or boolean checks")
    return values


def run(request: dict[str, Any]) -> dict[str, Any]:
    config = request["config"]
    output = Path(os.environ["OPENFOUNDRY_RESULT_FILE"]).resolve().parent / "outputs"
    output.mkdir(exist_ok=True)
    arguments = _arguments(request, output)
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    environment = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    subprocess.run(arguments, check=True, env=environment)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    elapsed = time.monotonic() - started
    values = _metrics(_output_path(output, config["metrics"])) if config.get("metrics") else {}
    for name in config.get("metricNames", []):
        if name not in values or isinstance(values[name], bool):
            raise ValueError(f"declared metric must be a finite number: {name}")
    artifacts = [
        {
            "name": name,
            "path": str(_output_path(output, path)),
            "kind": "model" if name == "model" else "stage-output",
        }
        for name, path in config.get("artifacts", {}).items()
    ]
    examples = config.get("examples")
    if examples:
        path = _output_path(output, examples)
        records = _read_json(path)
        if not isinstance(records, list) or not all(
            isinstance(item, dict) and isinstance(item.get("id"), (str, int)) for item in records
        ):
            raise ValueError("examples must be a JSON list of records with stable ids")
        if len({str(item["id"]) for item in records}) != len(records):
            raise ValueError("example ids must be unique")
        artifacts.append({"name": "examples", "path": str(path), "kind": "evaluation-examples"})
    measurement = {
        "wallSeconds": elapsed,
        "cpuSeconds": usage.ru_utime + usage.ru_stime - before.ru_utime - before.ru_stime,
        # Local script execution has no accelerator visibility and no cost model;
        # remote executors report actual values. Keys are always present so
        # search can budget on spend, not just elapsed time.
        "gpuSeconds": 0.0,
        "monetaryCostUSD": 0.0,
    }
    measurement_path = _output_path(output.parent, "measurement.json")
    measurement_path.write_text(json.dumps(measurement))
    artifacts.append({"name": "measurement", "path": str(measurement_path), "kind": "measurement"})
    return {"status": "ok", "outputs": values, "artifacts": artifacts}


def main() -> int:
    target = Path(os.environ["OPENFOUNDRY_RESULT_FILE"])
    result: dict[str, Any]
    try:
        request = _read_json(Path(os.environ["OPENFOUNDRY_REQUEST_FILE"]))
        if request["operation"] == "validate":
            result = {"status": "ok"}
        elif request["operation"] == "run":
            result = run(request)
        else:
            raise ValueError(f"unsupported script operation: {request['operation']}")
    except Exception as exc:
        result = {"status": "error", "error": {"code": type(exc).__name__, "message": str(exc)}}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"protocol": "openfoundry.module/v1", **result}, allow_nan=False)
    )
    temporary.replace(target)
    return int(result["status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
