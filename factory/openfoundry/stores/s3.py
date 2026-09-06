from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
from collections.abc import Iterable
from typing import Any

from openfoundry.artifacts import ArtifactManifest
from openfoundry.canonical import canonical_json
from openfoundry.errors import CapabilityError, ConflictError, IntegrityError, NotFoundError
from openfoundry.ids import parse_digest
from openfoundry.stores.base import StoreCapabilities


class S3Store:
    capabilities = StoreCapabilities(multipart=True, atomic_publish=True, versioning=True)

    def __init__(
        self,
        bucket: str,
        prefix: str = "openfoundry",
        endpoint_url: str | None = None,
        credential_reference: str | None = None,
        **client_options: Any,
    ) -> None:
        try:
            boto3 = importlib.import_module("boto3")
        except ImportError as exc:
            raise CapabilityError(
                "S3 support requires installation with `pip install openfoundry[s3]`"
            ) from exc
        self.bucket, self.prefix, self.endpoint_url = bucket, prefix.strip("/"), endpoint_url
        self.credential_reference = credential_reference
        self._client = boto3.client("s3", endpoint_url=endpoint_url, **client_options)
        self._client_error = importlib.import_module("botocore.exceptions").ClientError

    def __repr__(self) -> str:
        return (
            f"S3Store(bucket={self.bucket!r}, prefix={self.prefix!r}, "
            f"endpoint_url={self.endpoint_url!r})"
        )

    def config(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint_url": self.endpoint_url,
            "credential_reference": self.credential_reference,
        }

    def _key(self, area: str, digest: str, suffix: str = "") -> str:
        algorithm, value = parse_digest(digest)
        if algorithm != "sha256":
            raise IntegrityError("S3 artifact store supports sha256 content identity")
        parts = [part for part in (self.prefix, area, value[:2], value + suffix) if part]
        return "/".join(parts)

    def _not_found(self, exc: Exception) -> bool:
        response = getattr(exc, "response", {})
        return str(response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}

    def has_chunk(self, digest: str, size: int | None = None) -> bool:
        try:
            value = self._client.head_object(Bucket=self.bucket, Key=self._key("blobs", digest))
        except self._client_error as exc:
            if self._not_found(exc):
                return False
            raise
        return size is None or int(value["ContentLength"]) == size

    def read_chunk(self, digest: str, offset: int = 0, length: int | None = None) -> Any:
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._key("blobs", digest),
        }
        if offset or length is not None:
            end = "" if length is None else str(offset + length - 1)
            parameters["Range"] = f"bytes={offset}-{end}"
        try:
            return self._client.get_object(**parameters)["Body"]
        except self._client_error as exc:
            if self._not_found(exc):
                raise NotFoundError(f"chunk not found: {digest}") from exc
            raise

    def write_chunk(self, digest: str, source: Any, size: int | None = None) -> None:
        if self.has_chunk(digest, size) and self.verify_chunk(digest, size):
            return
        calculated, written = hashlib.sha256(), 0
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as staged:
            while block := source.read(1024 * 1024):
                calculated.update(block)
                staged.write(block)
                written += len(block)
            actual = "sha256:" + calculated.hexdigest()
            if actual != digest or (size is not None and size != written):
                raise IntegrityError("chunk digest or size mismatch")
            staged.seek(0)
            self._client.upload_fileobj(
                staged,
                self.bucket,
                self._key("blobs", digest),
                ExtraArgs={
                    "Metadata": {"openfoundry-sha256": digest.removeprefix("sha256:")},
                    "ContentType": "application/octet-stream",
                },
            )
        if not self.verify_chunk(digest, written):
            raise IntegrityError("S3 chunk failed independent post-upload verification")

    def verify_chunk(self, digest: str, size: int | None = None) -> bool:
        try:
            body = self.read_chunk(digest)
        except NotFoundError:
            return False
        calculated, observed = hashlib.sha256(), 0
        try:
            while block := body.read(1024 * 1024):
                calculated.update(block)
                observed += len(block)
        finally:
            body.close()
        return digest == "sha256:" + calculated.hexdigest() and (size is None or size == observed)

    def publish_manifest(self, manifest: ArtifactManifest) -> str:
        digest = manifest.manifest_digest
        data = canonical_json(manifest.to_dict())
        key = self._key("manifests", digest, ".json")
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType="application/vnd.openfoundry.artifact-manifest.v1+json",
                Metadata={"openfoundry-sha256": digest.removeprefix("sha256:")},
                IfNoneMatch="*",
            )
        except self._client_error as exc:
            response = getattr(exc, "response", {})
            if str(response.get("Error", {}).get("Code")) not in {
                "PreconditionFailed",
                "412",
            }:
                raise
            existing = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            if existing != data:
                raise ConflictError("immutable S3 manifest conflict") from exc
        return digest

    def read_manifest(self, digest: str) -> ArtifactManifest:
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=self._key("manifests", digest, ".json")
            )
        except self._client_error as exc:
            if self._not_found(exc):
                raise NotFoundError(f"manifest not found: {digest}") from exc
            raise
        body = response["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        manifest = ArtifactManifest.from_dict(json.loads(data))
        if manifest.manifest_digest != digest:
            raise IntegrityError("S3 manifest digest mismatch")
        return manifest

    def list_manifests(self) -> Iterable[str]:
        prefix = "/".join(part for part in (self.prefix, "manifests") if part) + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                if key.endswith(".json"):
                    yield "sha256:" + key.rsplit("/", 1)[-1].removesuffix(".json")
