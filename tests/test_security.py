import os
from datetime import UTC, datetime, timedelta

import pytest
from openfoundry.database import Database
from openfoundry.errors import ConflictError, IntegrityError
from openfoundry.security import (
    ApiTokenStore,
    SecretStore,
    SigningIdentity,
    import_trust_bundle,
    verify,
)


def test_sign_verify_bundle_and_tamper(tmp_path):
    identity = SigningIdentity(tmp_path / "sign.key")
    signature = identity.sign({"x": 1})
    identity.verify({"x": 1}, signature)
    key_id, public = import_trust_bundle(identity.export_trust_bundle())
    assert key_id == identity.key_id
    verify(public, {"x": 1}, signature)
    with pytest.raises(IntegrityError):
        verify(public, {"x": 2}, signature)
    assert os.stat(tmp_path / "sign.key").st_mode & 0o777 == 0o600


def test_trust_bundle_rejects_algorithm_tamper(tmp_path):
    bundle = SigningIdentity(tmp_path / "key").export_trust_bundle()
    bundle["algorithm"] = "RSA"
    with pytest.raises(IntegrityError):
        import_trust_bundle(bundle)


def test_secret_roundtrip_purpose_cas_and_no_values_in_list(tmp_path):
    store = SecretStore(Database(tmp_path / "db"), tmp_path / "secret.key")
    assert store.put("api", "super-secret", "sync") == 1
    assert store.get("api", "sync") == b"super-secret"
    with pytest.raises(IntegrityError):
        store.get("api", "other")
    with pytest.raises(ConflictError) as stale:
        store.put("api", "new", "sync")
    assert stale.value.details == {"expectedVersion": None, "currentVersion": 1}
    assert store.put("api", "new", "sync", expected_version=1) == 2
    assert store.get("api", "sync") == b"new"
    assert "super-secret" not in repr(store.list())


def test_secret_ciphertext_tamper_is_detected(tmp_path):
    db = Database(tmp_path / "db")
    store = SecretStore(db, tmp_path / "key")
    store.put("x", b"value", "test")
    db.connection.execute("update secrets set ciphertext=? where name='x'", (b"bad",))
    with pytest.raises(IntegrityError):
        store.get("x", "test")


def test_api_tokens_are_hashed_scoped_expiring_and_revocable(tmp_path):
    db = Database(tmp_path / "db")
    store = ApiTokenStore(db)
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    token, principal = store.create(actor="alice", scopes={"read"}, expires_at=expiry)
    assert principal.allows("read")
    assert not principal.allows("write")
    assert token not in repr(store.list())
    assert store.authenticate(token) == principal
    store.revoke(principal.token_id)
    assert store.authenticate(token) is None
    with pytest.raises(ValueError, match="future"):
        store.create(
            actor="alice",
            scopes={"read"},
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
