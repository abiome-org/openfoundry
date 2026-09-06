from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openfoundry.canonical import sha256_digest
from openfoundry.database import Database
from openfoundry.errors import ConflictError, IntegrityError
from openfoundry.events import EventStore
from openfoundry.security import SigningIdentity, verify

_REQUIRED = {"format", "model", "runtime", "provenance", "dataSummary", "evaluations", "assessment"}


@dataclass(frozen=True)
class Release:
    manifest: dict[str, Any]
    digest: str
    key_id: str
    signature: str


class ReleaseBuilder:
    def __init__(self, identity: SigningIdentity) -> None:
        self.identity = identity

    def build(self, manifest: dict[str, Any]) -> Release:
        missing = _REQUIRED - manifest.keys()
        if missing:
            raise IntegrityError(f"incomplete release manifest: {sorted(missing)}")
        if manifest["format"] != "openfoundry.release/v2":
            raise IntegrityError("unsupported release format")
        unsigned = dict(manifest)
        digest = sha256_digest(unsigned)
        return Release(
            unsigned,
            digest,
            self.identity.key_id,
            self.identity.sign({"manifest": unsigned, "digest": digest}),
        )


def verify_release(
    release: Release, public_key: bytes, resolver: Callable[[str], Any] | None = None
) -> None:
    if (
        _REQUIRED - release.manifest.keys()
        or release.manifest.get("format") != "openfoundry.release/v2"
        or sha256_digest(release.manifest) != release.digest
    ):
        raise IntegrityError("release manifest integrity failure")
    verify(public_key, {"manifest": release.manifest, "digest": release.digest}, release.signature)
    if resolver:
        for value in release.manifest.values():
            if isinstance(value, dict) and "digest" in value:
                resolved = resolver(str(value["digest"]))
                if sha256_digest(resolved) != value["digest"]:
                    raise IntegrityError("release reference digest mismatch")


def promote_alias(
    db: Database,
    events: EventStore,
    *,
    name: str,
    uid: str,
    revision: str,
    expected_version: int | None,
    actor: str,
    policy_decision: Any,
) -> int:
    if getattr(policy_decision, "outcome", None) not in {"allow", "warn"}:
        raise IntegrityError("policy denied alias promotion")
    version = 1 if expected_version is None else expected_version + 1
    events.append(
        type="PolicyDecisionRecorded",
        source="openfoundry/release",
        subject=name,
        resource_uid=uid,
        revision=revision,
        actor=actor,
        data={"outcome": policy_decision.outcome, "policyDigest": policy_decision.policy_digest},
        dataschema="openfoundry.dev/PolicyDecision",
    )

    def mutation(connection: Any) -> None:
        row = connection.execute("SELECT version FROM aliases WHERE name=?", (name,)).fetchone()
        if (None if row is None else int(row[0])) != expected_version:
            raise ConflictError("alias version mismatch")
        connection.execute(
            "INSERT INTO aliases VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "uid=excluded.uid,revision=excluded.revision,version=excluded.version",
            (name, uid, revision, version),
        )

    events.append(
        type="AliasMoved",
        source="openfoundry/release",
        subject=name,
        resource_uid=uid,
        revision=revision,
        actor=actor,
        data={"alias": name, "version": version},
        dataschema="openfoundry.dev/AliasMoved",
        mutation=mutation,
    )
    return version
