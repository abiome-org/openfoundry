import uuid

import pytest
from openfoundry.errors import ValidationError
from openfoundry.ids import parse_digest, uuid7, validate_uuid7


def test_uuid7_is_valid_and_monotonic():
    values = [uuid7() for _ in range(100)]
    assert values == sorted(values)
    assert all(validate_uuid7(v) == v for v in values)


@pytest.mark.parametrize("value", [str(uuid.uuid4()), "garbage"])
def test_uuid7_rejects_other_identifiers(value):
    with pytest.raises(ValidationError):
        validate_uuid7(value)


@pytest.mark.parametrize("value", ["SHA256:" + "a" * 64, "sha256:ABC", "sha256:abc", "missing"])
def test_digest_rejects_noncanonical_or_wrong_length(value):
    with pytest.raises(ValidationError):
        parse_digest(value)


def test_digest_parser_preserves_algorithm_and_value():
    assert parse_digest("sha256:" + "0" * 64) == ("sha256", "0" * 64)
