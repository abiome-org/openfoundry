import pytest
from openfoundry.canonical import canonical_json, load_document, sha256_digest
from openfoundry.errors import ValidationError


def test_rfc8785_key_order_and_number_form():
    assert canonical_json({"z": 1.0, "a": "é"}) == b'{"a":"\xc3\xa9","z":1}'


@pytest.mark.parametrize(
    "text", ["a: 1\na: 2", "1: value", "x: ${TOKEN}", "x: .nan", "x: 2024-01-01"]
)
def test_strict_yaml_rejects_non_json_or_unsafe_values(text):
    with pytest.raises(ValidationError):
        load_document(text)


def test_digest_is_deterministic_and_prefixed():
    assert sha256_digest({"b": 2, "a": 1}) == sha256_digest({"a": 1, "b": 2})
    assert sha256_digest(None).startswith("sha256:")
