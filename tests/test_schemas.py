from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from openfoundry.errors import ValidationError
from openfoundry.models import Metadata, finalize_resource
from openfoundry.schema_registry import SchemaRegistry


def _object_value(schema, root):
    value = {
        key: _value(schema.get("properties", {})[key], root) for key in schema.get("required", [])
    }
    for key in schema.get("oneOf", [{}])[0].get("required", []):
        value[key] = _value(schema["properties"][key], root)
    if not value and schema.get("minProperties", 0):
        value["component"] = _value(schema.get("additionalProperties", {}), root)
    return value


def _string_value(schema):
    pattern = schema.get("pattern", "")
    if schema.get("format") == "uuid":
        return "00000000-0000-4000-8000-000000000000"
    if pattern.startswith("^sha256:") or pattern.endswith(":[0-9a-f]+$"):
        return "sha256:" + "0" * 64
    return "x"


def _value(schema, root):
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].split("/")[-1]]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    typ = schema.get("type", "object" if "properties" in schema else None)
    if "oneOf" in schema and typ != "object":
        return _value(schema["oneOf"][0], root)
    if typ == "object":
        return _object_value(schema, root)
    if typ == "array":
        return [_value(schema.get("items", {}), root)] * schema.get("minItems", 0)
    scalars = {"integer": max(1, schema.get("minimum", 0)), "number": 1.0, "boolean": True}
    return scalars[typ] if typ in scalars else _string_value(schema)


def _minimal(registry, kind):
    schema = registry.schema_for(kind)
    return _value(schema, schema)


@pytest.mark.parametrize("kind", SchemaRegistry().kinds)
def test_every_schema_kind_accepts_minimal_resource(kind):
    registry = SchemaRegistry()
    value = _minimal(registry, kind)
    assert registry.validate(value)["kind"] == kind


def test_checked_in_evaluation_spec_example_is_valid_and_carries_no_thresholds():
    registry = SchemaRegistry()
    spec = registry.load(Path("evaluations/example-affine.yaml"))
    assert spec["kind"] == "EvaluationSpec"
    with_thresholds = deepcopy(spec)
    with_thresholds["spec"]["metrics"][0]["minimum"] = 0.0
    with pytest.raises(ValidationError):
        registry.validate(with_thresholds)


def test_schema_rejects_wrong_kind_top_level_and_naive_time():
    registry = SchemaRegistry()
    value = _minimal(registry, registry.kinds[0])
    value["kind"] = "NoSuchKind"
    with pytest.raises(ValidationError):
        registry.validate(value)
    Metadata(name="x", namespace="x", createdAt=datetime.now(UTC))
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - deliberately test rejection
    with pytest.raises(ValueError, match="timezone"):
        Metadata(name="x", namespace="x", createdAt=naive)


def test_finalization_excludes_status_and_does_not_mutate():
    source = {
        "apiVersion": "openfoundry.dev/v1alpha1",
        "kind": "X",
        "metadata": {"name": "x", "namespace": "n"},
        "spec": {"a": 1},
        "status": {"phase": "old"},
    }
    original = deepcopy(source)
    first = finalize_resource(source, actor="a", now=datetime(2026, 1, 1, tzinfo=UTC))
    source2 = deepcopy(source)
    source2["status"] = {"phase": "new"}
    second = finalize_resource(source2, actor="a", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert source == original
    assert first["metadata"]["revision"] == second["metadata"]["revision"]


def test_checked_in_deployment_manifest_is_valid():
    value = SchemaRegistry().load(Path("deployments/example-edge.yaml"))
    assert value["kind"] == "DeploymentSpec"


def test_checked_in_workload_and_modules_use_canonical_resources():
    registry = SchemaRegistry()
    workload = registry.load(Path("workloads/example-statistical.yaml"))
    statistical = registry.load(Path("modules/examples/statistical/module.yaml"))
    text_frequency = registry.load(Path("modules/examples/text-frequency/module.yaml"))
    from_scratch = registry.load(Path("workloads/example-from-scratch.yaml"))
    affine = registry.load(Path("modules/examples/affine-regression/module.yaml"))
    serving = registry.load(Path("modules/examples/affine-serving/module.yaml"))
    model_package = registry.load(Path("model-packages/example-affine.yaml"))
    assert workload["kind"] == "WorkloadSpec"
    assert from_scratch["kind"] == "WorkloadSpec"
    assert statistical["kind"] == text_frequency["kind"] == affine["kind"] == "Module"
    assert serving["kind"] == "Module"
    assert model_package["kind"] == "ModelPackage"
    assert model_package["spec"]["adapters"]["inferenceReference"]["module"].endswith(
        "affine-serving/module.yaml"
    )

    with pytest.raises(ValidationError, match="unsupported apiVersion"):
        registry.validate({"stages": []})


def test_module_and_workload_references_are_typed_and_relocatable():
    registry = SchemaRegistry()
    module = registry.load(Path("modules/examples/statistical/module.yaml"))
    workload = registry.load(Path("workloads/example-statistical.yaml"))

    invalid_module = deepcopy(module)
    invalid_module["spec"]["entryPoint"]["codeRoot"] = "/tmp/module"
    with pytest.raises(ValidationError):
        registry.validate(invalid_module)

    invalid_environment = deepcopy(module)
    invalid_environment["spec"]["environment"] = {}
    with pytest.raises(ValidationError):
        registry.validate(invalid_environment)

    invalid_workload = deepcopy(workload)
    invalid_workload["spec"]["graph"]["stages"][0]["module"] = "../module.yaml"
    with pytest.raises(ValidationError):
        registry.validate(invalid_workload)

    with pytest.raises(ValidationError, match="expected Module"):
        registry.validate_as(workload, "Module")
