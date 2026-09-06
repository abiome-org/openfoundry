from pathlib import Path

import pytest
import yaml
from openfoundry.errors import IntegrityError, ValidationError
from openfoundry.workloads import AdmittedWorkload, RunState, Stage, StateStore, project_workload


def test_cycle_retry_and_state(tmp_path):
    with pytest.raises(ValueError, match="cycle"):
        AdmittedWorkload(
            source_digest="sha256:" + "0" * 64,
            stages=[
                Stage(name="a", module="m", needs=["b"]),
                Stage(name="b", module="m", needs=["a"]),
            ],
        )
    spec = AdmittedWorkload(
        source_digest="sha256:" + "0" * 64, stages=[Stage(name="a", module="m")]
    )
    assert spec.digest == "sha256:a912106504415e3def9190427211043125daaa0d849fcc36289c852d04fde414"
    assert (
        spec.model_copy(
            update={"stages": [Stage(name="a", module="m", dataUse="evaluation")]}
        ).digest
        != spec.digest
    )
    store = StateStore(tmp_path / "state.json")
    store.initialize(spec)
    assert store.transition(RunState.DRAFT, RunState.VALIDATED)["state"] == "Validated"
    assert store.verify(spec)["digests"]["workload"] == spec.digest
    value = store.read()
    value["stages"]["unknown"] = {"status": "succeeded", "attempt": 1, "outputs": {}}
    store._write(value)
    with pytest.raises(IntegrityError, match="unknown stage"):
        store.verify(spec)


def test_canonical_workload_projection_enforces_semantics():
    path = Path("workloads/example-statistical.yaml")
    workload = yaml.safe_load(path.read_text())
    workload["spec"]["graph"]["stages"][0]["needs"] = ["evaluate"]
    with pytest.raises(ValidationError, match="semantic"):
        project_workload(workload)

    workload = yaml.safe_load(path.read_text())
    workload["spec"]["graph"]["stages"][1]["needs"] = ["missing"]
    with pytest.raises(ValidationError, match="semantic"):
        project_workload(workload)
