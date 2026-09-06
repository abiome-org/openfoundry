import pytest
from openfoundry.database import Database
from openfoundry.errors import ConflictError, NotFoundError
from openfoundry.operations import OperationStore


def test_operation_lifecycle_compare_and_set_filter_and_restart(tmp_path):
    database = Database(tmp_path / "db")
    operations = OperationStore(database)
    created = operations.create("run", {"workload": "train"})
    assert created["state"] == "pending"
    assert operations.get(created["id"])["version"] == 1
    running = operations.update(
        created["id"], expected_version=1, state="running", result={"execution": "one"}
    )
    assert running["version"] == 2
    assert operations.list(state="running") == [running]
    with pytest.raises(ConflictError):
        operations.update(created["id"], expected_version=1, state="failed")
    completed = operations.update(
        created["id"], expected_version=2, state="succeeded", result={"runId": "one"}
    )
    assert OperationStore(database).get(created["id"]) == completed
    assert operations.list(state="running") == []
    with pytest.raises(NotFoundError):
        operations.get("missing")
