import json
import subprocess
import sys

import pytest
from openfoundry.script_runner import run
from openfoundry.sdk import ProtocolRequest


def request(tmp_path, monkeypatch, script, **config):
    monkeypatch.setenv("OPENFOUNDRY_RESULT_FILE", str(tmp_path / "result.json"))
    source = tmp_path / "script.py"
    source.write_text(script)
    return ProtocolRequest(
        operation="run",
        inputs={"data": {"path": "/materialized/data"}},
        config={
            "command": [
                sys.executable,
                str(source),
                "{output}",
                "{inputs[data]}",
                "{parameters[rate]}",
            ],
            "parameters": {"rate": 0.25},
            **config,
        },
    ).model_dump(mode="json")


def test_existing_script_adapter_records_artifacts_examples_and_compute(tmp_path, monkeypatch):
    value = request(
        tmp_path,
        monkeypatch,
        """import json, sys
from pathlib import Path
output = Path(sys.argv[1])
assert sys.argv[2:] == ["/materialized/data", "0.25"]
(output / "model").write_text("learned model")
(output / "metrics.json").write_text(json.dumps({"accuracy": 0.9, "passed": True}))
(output / "examples.json").write_text('[{"id":"a","score":1}]')
""",
        metrics="metrics.json",
        artifacts={"model": "model"},
        examples="examples.json",
    )
    result = run(value)
    assert result["outputs"] == {"accuracy": 0.9, "passed": True}
    assert {item["name"] for item in result["artifacts"]} == {"model", "examples", "measurement"}
    measurement = json.loads((tmp_path / "measurement.json").read_text())
    assert measurement["wallSeconds"] > 0
    assert measurement["cpuSeconds"] >= 0
    assert measurement["gpuSeconds"] == 0.0
    assert measurement["monetaryCostUSD"] == 0.0


@pytest.mark.parametrize(
    "metrics",
    [
        '{"score":NaN}',
        '{"score":0,"score":1}',
        '{"score": "perfect"}',
        "[1]",
        "null",
        '{"score":true}',
        "{}",
    ],
)
def test_invalid_metrics_are_errors_not_successful_evaluations(tmp_path, monkeypatch, metrics):
    value = request(
        tmp_path,
        monkeypatch,
        f"from pathlib import Path\nimport sys\n"
        f"(Path(sys.argv[1]) / 'metrics.json').write_text({metrics!r})",
        metrics="metrics.json",
        metricNames=["score"],
    )
    with pytest.raises((ValueError, TypeError)):
        run(value)


@pytest.mark.parametrize("examples", ["{}", "[{}]", '[{"id":"a"},{"id":"a"}]'])
def test_invalid_or_ambiguous_examples_fail_the_stage(tmp_path, monkeypatch, examples):
    value = request(
        tmp_path,
        monkeypatch,
        f"from pathlib import Path\nimport sys\n"
        f"(Path(sys.argv[1]) / 'examples.json').write_text({examples!r})",
        examples="examples.json",
    )
    with pytest.raises(ValueError, match="example"):
        run(value)


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1.0e999"])
def test_nonfinite_example_values_fail_the_stage(tmp_path, monkeypatch, number):
    examples = '[{"id":"a","details":{"score":' + number + "}}]"
    value = request(
        tmp_path,
        monkeypatch,
        f"from pathlib import Path\nimport sys\n"
        f"(Path(sys.argv[1]) / 'examples.json').write_text({examples!r})",
        examples="examples.json",
    )
    with pytest.raises(ValueError, match="finite"):
        run(value)


def test_script_failure_and_escaping_output_propagate(tmp_path, monkeypatch):
    value = request(tmp_path, monkeypatch, "raise SystemExit(7)")
    with pytest.raises(subprocess.CalledProcessError) as error:
        run(value)
    assert error.value.returncode == 7
    outside = tmp_path / "outside.json"
    outside.write_text('{"passed": true}')
    value = request(
        tmp_path,
        monkeypatch,
        f"from pathlib import Path\nimport sys\n"
        f"(Path(sys.argv[1]) / 'metrics.json').symlink_to({str(outside)!r})",
        metrics="metrics.json",
    )
    with pytest.raises(ValueError, match="escapes"):
        run(value)


def test_adapter_measurements_do_not_overwrite_user_outputs(tmp_path, monkeypatch):
    value = request(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nimport sys\n"
        '(Path(sys.argv[1]) / "measurement.json").write_text("model payload")',
        artifacts={"model": "measurement.json"},
    )
    result = run(value)
    paths = {item["name"]: item["path"] for item in result["artifacts"]}
    assert paths["model"] != paths["measurement"]
    assert (tmp_path / "outputs/measurement.json").read_text() == "model payload"


def test_optional_checkpoint_is_emitted_with_state_when_present(tmp_path, monkeypatch):
    value = request(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\nimport sys\n"
        '(Path(sys.argv[1]) / "ckpt.bin").write_text("weights")',
        checkpoint="ckpt.bin",
    )
    result = run(value)
    kinds = {item["name"]: item["kind"] for item in result["artifacts"]}
    assert kinds["checkpoint"] == "checkpoint"
    assert result["state"] == {"checkpoint": "script-checkpoint/v1"}


def test_missing_checkpoint_is_not_an_error(tmp_path, monkeypatch):
    value = request(tmp_path, monkeypatch, "import sys\n", checkpoint="ckpt.bin")
    result = run(value)
    assert "checkpoint" not in {item["name"] for item in result["artifacts"]}
    assert "state" not in result
