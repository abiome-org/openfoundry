from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath
from typing import Any

import rfc8785
import yaml

from openfoundry.errors import ValidationError

_ENV = re.compile(r"\$\{[^}]+\}")


class _StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValidationError("mapping keys must be strings")
        if key in result:
            raise ValidationError(f"duplicate mapping key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"non-finite number at {path}")
    if isinstance(value, str) and _ENV.search(value):
        raise ValidationError(f"environment interpolation is prohibited at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"mapping key at {path} must be a string")
            _check(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check(child, f"{path}[{index}]")


def canonical_json(value: Any) -> bytes:
    _check(value)
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError, rfc8785.CanonicalizationError) as exc:
        raise ValidationError(f"value is not canonical JSON: {exc}") from exc


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def load_document(data: str | bytes) -> Any:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        value = yaml.load(text, Loader=_StrictLoader)
    except ValidationError:
        raise
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ValidationError(f"invalid YAML/JSON: {exc}") from exc
    _check(value)
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"document is not JSON-compatible: {exc}") from exc
    return value


def portable_relative_path(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValidationError(f"{field} must be a repository-relative POSIX path")
    if value != "." and path.as_posix() != value:
        raise ValidationError(f"{field} must be normalized")
    return path


canonicalize = canonical_json
digest = sha256_digest
load_yaml = load_document
