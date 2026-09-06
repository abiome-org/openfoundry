import json
import subprocess
import sys

from openfoundry.sdk import ProtocolResult, dispatch


def test_protocol_dispatch_success_error_and_unsupported(tmp_path):
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(json.dumps({"operation": "run", "inputs": {"value": 2}}))
    code = dispatch(
        {"run": lambda value: {"outputs": {"value": value.inputs["value"] * 2}}},
        request,
        result,
    )
    assert code == 0
    assert ProtocolResult.model_validate_json(result.read_bytes()).outputs["value"] == 4

    request.write_text(json.dumps({"operation": "stop"}))
    assert dispatch({}, request, result) == 1
    error = ProtocolResult.model_validate_json(result.read_bytes())
    assert error.status == "error"
    assert error.error is not None
    assert "not implemented" in error.error.message

    request.write_text(json.dumps({"operation": "run"}))

    def fail(_request):
        raise RuntimeError("worker failed")

    assert dispatch({"run": fail}, request, result) == 1
    assert "worker failed" in result.read_text()

    worker = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from openfoundry.sdk import ProtocolResult, main\n"
                "raise SystemExit(main({'run': lambda _request: ProtocolResult(status='ok')}))"
            ),
            "--request",
            str(request),
            "--result",
            str(result),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert worker.returncode == 0, worker.stderr
    assert json.loads(result.read_text())["status"] == "ok"
