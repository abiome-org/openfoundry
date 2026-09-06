# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from openfoundry.canonical import canonical_json, sha256_digest
from openfoundry.database import Database
from openfoundry.errors import ConflictError, IntegrityError, NotFoundError

_TOKEN_SCOPES = frozenset({"read", "write", "admin", "*"})


@dataclass(frozen=True)
class ApiPrincipal:
    token_id: str
    actor: str
    scopes: frozenset[str]
    expires_at: str | None

    def allows(self, scope: str) -> bool:
        return "*" in self.scopes or "admin" in self.scopes or scope in self.scopes


class ApiTokenStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _digest(token: str) -> str:
        return (
            "sha256:" + hashlib.sha256(("openfoundry-api-token-v1:" + token).encode()).hexdigest()
        )

    @staticmethod
    def _validate(actor: str, scopes: set[str] | frozenset[str], expires_at: str | None) -> None:
        if not actor.strip():
            raise ValueError("API token actor is required")
        if not scopes or not scopes <= _TOKEN_SCOPES:
            raise ValueError("API token scopes must contain read, write, admin, or *")
        if expires_at is not None:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError("API token expiry must include a timezone")
            if expiry <= datetime.now(UTC):
                raise ValueError("API token expiry must be in the future")

    def register(
        self,
        token: str,
        *,
        actor: str,
        scopes: set[str] | frozenset[str],
        expires_at: str | None = None,
    ) -> ApiPrincipal:
        self._validate(actor, scopes, expires_at)
        token_id = self._digest(token)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO api_tokens VALUES(?,?,?,?,?,NULL)",
                (token_id, actor, json.dumps(sorted(scopes)), expires_at, now),
            )
        principal = self.authenticate(token)
        if principal is None:
            raise IntegrityError("API token registration failed")
        return principal

    def create(
        self, *, actor: str, scopes: set[str], expires_at: str | None = None
    ) -> tuple[str, ApiPrincipal]:
        token = secrets.token_urlsafe(32)
        return token, self.register(token, actor=actor, scopes=scopes, expires_at=expires_at)

    def authenticate(self, token: str) -> ApiPrincipal | None:
        if not token:
            return None
        token_id = self._digest(token)
        row = self.db.connection.execute(
            "SELECT actor,scopes,expires_at,revoked_at FROM api_tokens WHERE token_hash=?",
            (token_id,),
        ).fetchone()
        if row is None or row[3] is not None:
            return None
        expires_at = None if row[2] is None else str(row[2])
        if expires_at is not None and datetime.fromisoformat(
            expires_at.replace("Z", "+00:00")
        ) <= datetime.now(UTC):
            return None
        return ApiPrincipal(token_id, str(row[0]), frozenset(json.loads(row[1])), expires_at)

    def revoke(self, token_id: str) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self.db.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,?) WHERE token_hash=?",
                (now, token_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("API token not found")

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "tokenId": str(row[0]),
                "actor": str(row[1]),
                "scopes": json.loads(row[2]),
                "expiresAt": row[3],
                "createdAt": str(row[4]),
                "revokedAt": row[5],
            }
            for row in self.db.connection.execute(
                "SELECT token_hash,actor,scopes,expires_at,created_at,revoked_at "
                "FROM api_tokens ORDER BY created_at,token_hash"
            )
        ]


def _atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SigningIdentity:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            private = Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(private)
        os.chmod(self.path, 0o600)
        self._private = Ed25519PrivateKey.from_private_bytes(self.path.read_bytes())

    @property
    def public_bytes(self) -> bytes:
        return self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def key_id(self) -> str:
        return sha256_digest({"publicKey": base64.b64encode(self.public_bytes).decode()})

    def sign(self, value: Any) -> str:
        return base64.b64encode(self._private.sign(canonical_json(value))).decode()

    def verify(self, value: Any, signature: str) -> None:
        verify(self.public_bytes, value, signature)

    def export_trust_bundle(self) -> dict[str, str]:
        return {
            "keyId": self.key_id,
            "algorithm": "Ed25519",
            "publicKey": base64.b64encode(self.public_bytes).decode(),
        }


def import_trust_bundle(bundle: dict[str, str]) -> tuple[str, bytes]:
    try:
        public = base64.b64decode(bundle["publicKey"], validate=True)
        Ed25519PublicKey.from_public_bytes(public)
    except (KeyError, ValueError) as exc:
        raise IntegrityError("invalid trust bundle") from exc
    key_id = sha256_digest({"publicKey": bundle["publicKey"]})
    if bundle.get("algorithm") != "Ed25519" or bundle.get("keyId") != key_id:
        raise IntegrityError("trust bundle identity mismatch")
    return key_id, public


def verify(public_key: bytes, value: Any, signature: str) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            base64.b64decode(signature, validate=True), canonical_json(value)
        )
    except (InvalidSignature, ValueError) as exc:
        raise IntegrityError("signature verification failed") from exc


class SecretStore:
    def __init__(self, database: Database, key_path: str | Path) -> None:
        self.db, self.key_path = database, Path(key_path)
        if not self.key_path.exists():
            _atomic_private(self.key_path, AESGCM.generate_key(bit_length=256))
        os.chmod(self.key_path, 0o600)
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise IntegrityError("invalid secret master key")
        self._cipher = AESGCM(key)

    @staticmethod
    def _aad(name: str, purpose: str) -> bytes:
        return canonical_json({"name": name, "purpose": purpose})

    def put(
        self, name: str, value: str | bytes, purpose: str, *, expected_version: int | None = None
    ) -> int:
        plaintext = value.encode() if isinstance(value, str) else value
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad(name, purpose))
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT version,created_at FROM secrets WHERE name=?", (name,)
            ).fetchone()
            current = None if row is None else int(row[0])
            if current != expected_version:
                raise ConflictError(
                    "secret version mismatch",
                    details={"expectedVersion": expected_version, "currentVersion": current},
                )
            version, created = (1, now) if row is None else (int(row[0]) + 1, str(row[1]))
            connection.execute(
                "INSERT INTO secrets VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET purpose=excluded.purpose,nonce=excluded.nonce,ciphertext=excluded.ciphertext,updated_at=excluded.updated_at,version=excluded.version",
                (name, purpose, nonce, ciphertext, created, now, version),
            )
            return version

    def get(self, name: str, purpose: str) -> bytes:
        row = self.db.connection.execute(
            "SELECT purpose,nonce,ciphertext FROM secrets WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise NotFoundError("secret not found")
        if row[0] != purpose:
            raise IntegrityError("secret purpose mismatch")
        try:
            return self._cipher.decrypt(row[1], row[2], self._aad(name, purpose))
        except InvalidTag as exc:
            raise IntegrityError("secret ciphertext failed authentication") from exc

    def delete(self, name: str, *, expected_version: int | None = None) -> None:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute("SELECT version FROM secrets WHERE name=?", (name,)).fetchone()
            if row is None:
                raise NotFoundError("secret not found")
            if expected_version is not None and int(row[0]) != expected_version:
                raise ConflictError(
                    "secret version mismatch",
                    details={"expectedVersion": expected_version, "currentVersion": int(row[0])},
                )
            connection.execute("DELETE FROM secrets WHERE name=?", (name,))

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.connection.execute(
            "SELECT name,purpose,created_at,updated_at,version FROM secrets ORDER BY name"
        )
        return [dict(row) for row in rows]
