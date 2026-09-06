from fastapi.testclient import TestClient
from openfoundry.serve_worker import ServingConfig, create_app

SIGNATURES = {
    "input": {
        "type": "object",
        "required": ["input"],
        "properties": {"input": {"type": "number"}},
        "additionalProperties": False,
    },
    "output": {
        "type": "object",
        "required": ["prediction"],
        "properties": {"prediction": {"type": "number"}},
        "additionalProperties": False,
    },
}
OK_SCRIPT = (
    "import json, os\n"
    "request = json.load(open(os.environ['OPENFOUNDRY_REQUEST_FILE']))\n"
    "prediction = request['state']['slope'] * request['inputs']['input']\n"
    "prediction += request['state']['intercept']\n"
    "json.dump({'protocol': 'openfoundry.module/v1', 'status': 'ok',\n"
    "           'outputs': {'prediction': prediction}},\n"
    "          open(os.environ['OPENFOUNDRY_RESULT_FILE'], 'w'))\n"
)
ERROR_SCRIPT = (
    "import json, os\n"
    "json.dump({'protocol': 'openfoundry.module/v1', 'status': 'error',\n"
    "           'error': {'code': 'Boom', 'message': 'secret value 41.5'}},\n"
    "          open(os.environ['OPENFOUNDRY_RESULT_FILE'], 'w'))\n"
    "raise SystemExit(1)\n"
)
BAD_OUTPUT_SCRIPT = (
    "import json, os\n"
    "json.dump({'protocol': 'openfoundry.module/v1', 'status': 'ok',\n"
    "           'outputs': {'prediction': 'seven'}},\n"
    "          open(os.environ['OPENFOUNDRY_RESULT_FILE'], 'w'))\n"
)


def _client(tmp_path, script, timeout=None):
    config = ServingConfig(
        deployment="demo",
        release="sha256:release",
        modelPackageRef="openfoundry://local/demo/modelpackage/affine@sha256:package",
        operation="run",
        config={},
        state={"slope": 2.0, "intercept": 1.0, "format": "json-affine/v1"},
        signatures=SIGNATURES,
        command=["python3", "-c", script],
        wrapper=[],
        cwd=str(tmp_path),
        host="127.0.0.1",
        port=1,
        timeoutSeconds=timeout,
    )
    return TestClient(create_app(config, tmp_path))


def test_serving_worker_runs_one_protocol_exchange_per_request(tmp_path):
    client = _client(tmp_path, OK_SCRIPT)
    health = client.get("/healthz").json()
    assert health["requests"] == 0
    assert health["release"] == "sha256:release"

    response = client.post("/v1/infer", json={"inputs": {"input": 3.0}, "seed": 7})
    assert response.status_code == 200
    body = response.json()
    assert body["outputs"] == {"prediction": 7.0}
    assert body["modelPackageRef"].endswith("@sha256:package")
    assert body["requestId"]
    assert not any((tmp_path / "requests").iterdir())
    health = client.get("/healthz").json()
    assert health["requests"] == 1
    assert health["failures"] == 0


def test_serving_worker_reports_failures_without_echoing_values(tmp_path):
    invalid = _client(tmp_path, OK_SCRIPT).post("/v1/infer", json={"inputs": {"input": "41.5"}})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "validation_error"
    assert "41.5" not in invalid.text

    no_result = _client(tmp_path, "import sys; sys.exit(3)").post(
        "/v1/infer", json={"inputs": {"input": 41.5}}
    )
    assert no_result.status_code == 502
    assert no_result.json()["detail"]["code"] == "no_result"
    assert no_result.json()["detail"]["exitCode"] == 3
    assert "41.5" not in no_result.text

    errored = _client(tmp_path, ERROR_SCRIPT).post("/v1/infer", json={"inputs": {"input": 41.5}})
    assert errored.status_code == 502
    assert errored.json()["detail"]["code"] == "Boom"
    assert "secret" not in errored.text

    bad_output = _client(tmp_path, BAD_OUTPUT_SCRIPT).post(
        "/v1/infer", json={"inputs": {"input": 41.5}}
    )
    assert bad_output.status_code == 502
    assert bad_output.json()["detail"]["code"] == "invalid_result"
    assert "seven" not in bad_output.text

    slow = _client(tmp_path, "import time; time.sleep(5)", timeout=0.2)
    timed_out = slow.post("/v1/infer", json={"inputs": {"input": 41.5}})
    assert timed_out.status_code == 504
    assert timed_out.json()["detail"]["code"] == "inference_timeout"
    assert slow.get("/healthz").json()["failures"] == 1

    unknown_field = _client(tmp_path, OK_SCRIPT).post(
        "/v1/infer", json={"inputs": {"input": 1.0}, "unexpected": True}
    )
    assert unknown_field.status_code == 422


def test_serving_worker_reports_malformed_result_as_adapter_failure(tmp_path):
    client = _client(
        tmp_path,
        "import os; open(os.environ['OPENFOUNDRY_RESULT_FILE'], 'w').write('not json')",
    )
    response = client.post("/v1/infer", json={"inputs": {"input": 3.0}})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_result"
    assert client.get("/healthz").json()["failures"] == 1
    assert not any((tmp_path / "requests").iterdir())


def test_serving_worker_rejects_success_result_from_failed_process(tmp_path):
    client = _client(tmp_path, OK_SCRIPT + "raise SystemExit(3)\n")
    response = client.post("/v1/infer", json={"inputs": {"input": 3.0}})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "adapter_failed"
    assert response.json()["detail"]["exitCode"] == 3
    assert client.get("/healthz").json()["failures"] == 1
    assert not any((tmp_path / "requests").iterdir())
