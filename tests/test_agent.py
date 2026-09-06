import subprocess
from datetime import UTC, datetime

import pytest
from openfoundry.agent import capability_catalog, initial_context
from openfoundry.api import create_app
from openfoundry.canonical import canonical_json
from openfoundry.config import ProjectPaths, bootstrap
from openfoundry.errors import ValidationError
from openfoundry.factory import Factory


def _project(tmp_path):
    root = tmp_path / "agent-project"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "openfoundry.yaml").write_text(
        """apiVersion: openfoundry.dev/v1alpha1
kind: Project
metadata: {name: agent-test, namespace: local/agent-test}
spec: {owners: [local-user], extensions: {}}
"""
    )
    return ProjectPaths(root)


def _event(factory, subject="probe"):
    return factory.events.append(
        type="Probe",
        source="openfoundry://local/agent-test",
        subject=subject,
        resource_uid="probe",
        revision="sha256:" + "a" * 64,
        actor="tester",
        data={},
        dataschema="openfoundry.dev/events/probe/v1",
    )


def test_prebootstrap_context_and_capabilities_are_stable_and_actionable(tmp_path):
    paths = _project(tmp_path)
    first_catalog = capability_catalog()
    second_catalog = capability_catalog()
    assert first_catalog == second_catalog
    assert len({item["action"] for item in first_catalog["actions"]}) == len(
        first_catalog["actions"]
    )
    assert all(
        {"action", "description", "interfaces", "requiredScope", "mutates"} <= item.keys()
        for item in first_catalog["actions"]
    )

    first = initial_context(paths)
    second = initial_context(paths)
    assert not first["readiness"]["ready"]
    assert first["bootstrapPlan"]["actions"]
    assert first["viewDigest"] == second["viewDigest"]
    assert first["generatedAt"] != first["viewDigest"]


def test_capability_catalog_covers_every_versioned_http_operation(tmp_path):
    paths = _project(tmp_path)
    bootstrap(paths)
    app = create_app(paths)
    try:
        openapi = app.openapi()
    finally:
        app.state.factory.close()
    methods = {"get", "post", "put", "patch", "delete"}
    api_operations = {
        (method.upper(), path)
        for path, operations in openapi["paths"].items()
        if path.startswith("/v1/")
        for method in operations
        if method in methods
    }
    catalog_operations = {
        (interface["method"], interface["path"])
        for action in capability_catalog()["actions"]
        if (interface := action["interfaces"].get("http")) is not None
    }
    assert api_operations == catalog_operations
    for action in capability_catalog()["actions"]:
        if interface := action["interfaces"].get("http"):
            operation = openapi["paths"][interface["path"]][interface["method"].lower()]
            assert operation["operationId"] == action["action"]
            assert operation["x-openfoundry-action"]["requiredScope"] == action["requiredScope"]


def test_agent_boundaries_fail_closed_with_actionable_validation(tmp_path):
    paths = _project(tmp_path)
    with pytest.raises(ValidationError, match="cursor"):
        initial_context(paths, since="unknown-event")
    with pytest.raises(ValidationError, match="max_bytes"):
        initial_context(paths, max_bytes=1)

    bootstrap(paths)
    with Factory(paths) as factory, pytest.raises(ValidationError, match="max_bytes"):
        factory.agent.context(max_bytes=1)


def test_context_is_bounded_deterministic_incremental_and_redacted(tmp_path):
    paths = _project(tmp_path)
    bootstrap(paths)
    with Factory(paths) as factory:
        for index in range(3):
            factory.operations.create(f"operation-{index}", {})
        factory.operations.create("sensitive", {"token": "must-not-escape"})
        factory.events.append(
            type="SensitiveProbe",
            source="openfoundry://local/agent-test",
            subject="probe",
            resource_uid="probe",
            revision="sha256:" + "a" * 64,
            actor="tester",
            data={"prompt": "must-not-escape"},
            dataschema="openfoundry.dev/events/probe/v1",
        )
        at_one = datetime(2026, 9, 1, 12, tzinfo=UTC)
        at_two = datetime(2026, 9, 1, 13, tzinfo=UTC)
        first = factory.agent.context(limit=1, max_bytes=16_384, at=at_one)
        second = factory.agent.context(limit=1, max_bytes=16_384, at=at_two)
        assert len(canonical_json(first)) <= 16_384
        assert {item["name"] for item in first["inventory"]["executors"]} >= {"local"}
        assert first["activity"]["operations"]["returned"] == 1
        assert first["activity"]["operations"]["total"] == 4
        assert first["activity"]["operations"]["truncated"]
        assert first["viewDigest"] == second["viewDigest"]
        assert first["generatedAt"] != second["generatedAt"]
        assert "must-not-escape" not in canonical_json(first).decode()

        cursor = first["recentEvents"]["cursor"]
        _event(factory, "incremental")
        incremental = factory.agent.context(since=cursor, at=at_two)
        assert incremental["recentEvents"]["since"] == cursor
        assert {item["type"] for item in incremental["recentEvents"]["items"]} >= {"Probe"}


def test_empty_inventory_does_not_invent_new_work(tmp_path):
    paths = _project(tmp_path)
    bootstrap(paths)
    payload = paths.root / "samples.jsonl"
    payload.write_text('{"value":1}\n')
    with Factory(paths) as factory:
        initial = factory.agent.context()
        assert not {"goals", "knowledge", "recommendations", "blockers"} & initial.keys()
        factory.add_data(
            payload,
            name="samples",
            mode="copy",
            rights={"license": "CC0-1.0", "trainingAllowed": True},
        )
        advanced = factory.agent.context()
        assert advanced["inventory"]["resources"] != initial["inventory"]["resources"]


def test_incremental_context_preserves_events_and_makes_progress_under_byte_pressure(tmp_path):
    paths = _project(tmp_path)
    bootstrap(paths)
    with Factory(paths) as factory:
        _event(factory, "baseline")
        cursor = factory.agent.context()["recentEvents"]["cursor"]
        for index in range(5):
            factory.events.append(
                type="LargeEvent",
                source="openfoundry://local/agent-test",
                subject=f"{index}-" + "x" * 6000,
                resource_uid="probe",
                revision="sha256:" + "a" * 64,
                actor="tester",
                data={},
                dataschema="openfoundry.dev/events/probe/v1",
            )
        expected = [event.id for event in factory.events.window(limit=100, after=cursor).items]
        received = []
        for _ in range(len(expected) + 1):
            context = factory.agent.context(since=cursor, max_bytes=16_384)
            assert len(canonical_json(context)) <= 16_384
            page = context["recentEvents"]
            received.extend(item["id"] for item in page["items"])
            if not page["truncated"]:
                break
            assert page["items"], "a fitting event must advance an incremental cursor"
            assert page["cursor"] == page["items"][-1]["id"]
            assert page["cursor"] != cursor
            cursor = page["cursor"]
        assert received == expected


def test_context_operation_focus_uses_only_metadata_and_reports_exact_totals(tmp_path):
    paths = _project(tmp_path)
    bootstrap(paths)
    with Factory(paths) as factory:
        oldest = factory.operations.create("matching", {"token": "private-needle"})
        for _ in range(5):
            factory.operations.create("other", {"token": "private-needle"})
        focused = factory.agent.context(focus="matching", limit=1)["activity"]["operations"]
        assert [item["id"] for item in focused["items"]] == [oldest["id"]]
        assert focused["total"] == 1
        assert factory.agent.context(focus="private-needle")["activity"]["operations"]["total"] == 0
        page = factory.agent.context(limit=1)["activity"]["operations"]
        assert page["total"] == 6
        assert page["returned"] == 1
        assert page["truncated"]
