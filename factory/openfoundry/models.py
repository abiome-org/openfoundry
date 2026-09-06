from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openfoundry.canonical import sha256_digest
from openfoundry.ids import uuid7


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
    namespace: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9./-]{0,126}[a-z0-9])?$")
    uid: str | None = None
    revision: str | None = None
    createdAt: datetime | None = None
    createdBy: str | None = None

    @field_validator("createdAt")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("createdAt must include a timezone")
        return value


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiVersion: str = Field(pattern=r"^openfoundry\.dev/v1alpha1$")
    kind: str
    metadata: Metadata
    spec: dict[str, Any]
    status: dict[str, Any] = Field(default_factory=dict)


def finalize_resource(
    resource: dict[str, Any], *, actor: str, now: datetime | None = None
) -> dict[str, Any]:
    result = deepcopy(resource)
    metadata = result.setdefault("metadata", {})
    digest_content = {
        "apiVersion": result.get("apiVersion"),
        "kind": result.get("kind"),
        "metadata": {"name": metadata.get("name"), "namespace": metadata.get("namespace")},
        "spec": result.get("spec"),
    }
    revision = sha256_digest(digest_content)
    metadata.setdefault("uid", str(uuid7()))
    metadata["revision"] = revision
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    metadata.setdefault("createdAt", instant.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    metadata.setdefault("createdBy", actor)
    result["specDigest"] = sha256_digest(result.get("spec"))
    return result


normalize_resource = finalize_resource
