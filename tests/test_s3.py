import hashlib
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from botocore.config import Config
from openfoundry.artifacts import ArtifactManifest, ChunkDescriptor
from openfoundry.errors import ConflictError, IntegrityError, NotFoundError
from openfoundry.stores.s3 import S3Store


class _ObjectStore(BaseHTTPRequestHandler):
    objects: ClassVar[dict[str, bytes]] = {}

    def log_message(self, *_arguments):
        return

    def _respond(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _xml(self, status, body):
        self._respond(
            status, f"<?xml version='1.0'?>{body}".encode(), {"Content-Type": "application/xml"}
        )

    def _error(self, status, code):
        self._xml(status, f"<Error><Code>{code}</Code><Message>{code}</Message></Error>")

    def _key(self):
        parts = urlsplit(self.path)
        return unquote(parts.path).split("/", 2)[2] if parts.path.count("/") >= 2 else "", parts

    def do_HEAD(self):
        key, _parts = self._key()
        if key not in self.objects:
            return self._error(404, "NotFound")
        self._respond(200, self.objects[key])

    def do_GET(self):
        key, parts = self._key()
        if not key:
            prefix = parse_qs(parts.query).get("prefix", [""])[0]
            keys = "".join(
                f"<Contents><Key>{item}</Key></Contents>"
                for item in sorted(self.objects)
                if item.startswith(prefix)
            )
            listing = f"<ListBucketResult><IsTruncated>false</IsTruncated>{keys}</ListBucketResult>"
            return self._xml(200, listing)
        if key not in self.objects:
            return self._error(404, "NoSuchKey")
        body = self.objects[key]
        if "Range" not in self.headers:
            return self._respond(200, body)
        start, _, end = self.headers["Range"].removeprefix("bytes=").partition("-")
        self._respond(206, body[int(start) : int(end) + 1 if end else None])

    def do_PUT(self):
        key, _parts = self._key()
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("If-None-Match") == "*" and key in self.objects:
            return self._error(412, "PreconditionFailed")
        self.objects[key] = body
        self._respond(200, headers={"ETag": '"' + hashlib.md5(body).hexdigest() + '"'})


@pytest.fixture
def s3():
    _ObjectStore.objects = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ObjectStore)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            S3Store(
                "bucket",
                "prefix",
                endpoint_url=f"http://127.0.0.1:{server.server_port}",
                aws_access_key_id="test",
                aws_secret_access_key="test",
                region_name="us-east-1",
                config=Config(
                    s3={"addressing_style": "path"},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            ),
            _ObjectStore.objects,
        )
    finally:
        server.shutdown()
        server.server_close()


def _manifest(payload=b"payload"):
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return ArtifactManifest(
        "application/octet-stream",
        len(payload),
        digest,
        (ChunkDescriptor(digest, len(payload), 0),),
    )


def test_s3_chunk_manifest_range_listing_and_idempotence(s3):
    store, objects = s3
    manifest = _manifest()
    chunk = manifest.chunks[0]
    assert not store.has_chunk(chunk.digest)
    store.write_chunk(chunk.digest, io.BytesIO(b"payload"), chunk.size)
    store.write_chunk(chunk.digest, io.BytesIO(b"ignored"), chunk.size)
    assert store.verify_chunk(chunk.digest, chunk.size)
    assert store.read_chunk(chunk.digest, 1, 3).read() == b"ayl"
    assert store.publish_manifest(manifest) == manifest.manifest_digest
    assert store.publish_manifest(manifest) == manifest.manifest_digest
    assert store.read_manifest(manifest.manifest_digest) == manifest
    assert list(store.list_manifests()) == [manifest.manifest_digest]
    assert objects[store._key("blobs", chunk.digest)] == b"payload"
    assert "credential" not in repr(store)
    assert store.config()["endpoint_url"].startswith("http://127.0.0.1:")


def test_s3_rejects_corruption_conflict_and_missing(s3):
    store, objects = s3
    manifest = _manifest()
    with pytest.raises(IntegrityError):
        store.write_chunk(manifest.digest, io.BytesIO(b"wrong"), len(b"wrong"))
    with pytest.raises(NotFoundError):
        store.read_chunk(manifest.digest)
    store.write_chunk(manifest.digest, io.BytesIO(b"payload"))
    objects[store._key("blobs", manifest.digest)] = b"corrupt"
    assert not store.verify_chunk(manifest.digest)
    store.publish_manifest(manifest)
    manifest_key = store._key("manifests", manifest.manifest_digest, ".json")
    objects[manifest_key] = json.dumps({"different": True}).encode()
    with pytest.raises(ConflictError):
        store.publish_manifest(manifest)


def test_s3_optional_dependency_error():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\nsys.modules['boto3'] = None\n"
                "from openfoundry.stores.s3 import S3Store\nS3Store('bucket')"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 1
    assert "requires installation" in probe.stderr
