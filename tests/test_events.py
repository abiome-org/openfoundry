from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from openfoundry.database import Database
from openfoundry.errors import IntegrityError, NotFoundError
from openfoundry.events import EventStore
from openfoundry.security import SigningIdentity


def _store(tmp_path, name="events"):
    database = Database(tmp_path / f"{name}.db")
    identity = SigningIdentity(tmp_path / f"{name}.key")
    return database, identity, EventStore(database, identity)


def _append(store, **kwargs):
    values = {
        "type": "RunStateChanged",
        "source": "openfoundry://test",
        "subject": "run/one",
        "resource_uid": "resource-one",
        "revision": "sha256:" + "a" * 64,
        "actor": "tester",
        "data": {"state": "Running"},
        "dataschema": "openfoundry.dev/events/run-state/v1",
        "run_id": "run-one",
    }
    values.update(kwargs)
    return store.append(**values)


def test_event_signature_query_and_outbox(tmp_path):
    _database, identity, store = _store(tmp_path)
    event = _append(store)
    assert store.get(event.id, public_key=identity.public_bytes) == event
    assert store.query(run_id="run-one", type="RunStateChanged") == [event]
    assert store.pending() == [event]
    store.mark_published(event.id)
    store.mark_published(event.id)
    assert store.pending() == []


def test_event_and_mutation_are_atomic(tmp_path):
    database, _identity, store = _store(tmp_path)

    def fail(_connection):
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        _append(store, mutation=fail)
    assert database.connection.execute("select count(*) from events").fetchone()[0] == 0
    assert database.connection.execute("select count(*) from event_order").fetchone()[0] == 0
    assert database.connection.execute("select count(*) from outbox").fetchone()[0] == 0


def test_revision_event_deduplication_is_atomic_and_rejects_changed_payload(tmp_path):
    database, _identity, store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda _index: _append(store, dedupe_revision=True), range(16)))
    assert len({event.id for event in events}) == 1
    assert database.connection.execute("select count(*) from events").fetchone()[0] == 1
    assert database.connection.execute("select count(*) from event_order").fetchone()[0] == 1
    with pytest.raises(IntegrityError, match="different payload"):
        _append(store, data={"state": "Changed"}, dedupe_revision=True)


def test_concurrent_sequences_are_unique_and_monotonic(tmp_path):
    _database, _identity, store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda index: _append(store, data={"index": index}), range(24)))
    assert sorted(event.sequence for event in events) == list(range(1, 25))
    assert len({event.id for event in events}) == 24


def test_import_is_idempotent_and_rejects_tamper(tmp_path):
    _source_db, source_identity, source = _store(tmp_path, "source")
    _target_db, _target_identity, target = _store(tmp_path, "target")
    event = _append(source)
    target.import_event(event, source_identity.public_bytes)
    target.import_event(event, source_identity.public_bytes)
    with pytest.raises(IntegrityError):
        target.import_event(
            replace(event, data={"state": "tampered"}), source_identity.public_bytes
        )
    with pytest.raises(NotFoundError):
        target.get("missing")


def test_event_order_migration_backfills_existing_events(tmp_path):
    database, _identity, store = _store(tmp_path)
    event = _append(store)
    database.close()

    import sqlite3

    connection = sqlite3.connect(tmp_path / "events.db")
    connection.executescript(
        """
        DROP TRIGGER event_order_no_update;
        DROP TRIGGER event_order_no_delete;
        DROP TABLE event_order;
        DELETE FROM schema_migrations WHERE version>=4;
        """
    )
    connection.close()

    migrated = Database(tmp_path / "events.db")
    migrated_store = EventStore(migrated, SigningIdentity(tmp_path / "events.key"))
    window = migrated_store.window(limit=1)
    assert [item.id for item in window.items] == [event.id]
    assert window.cursor == event.id
    migrated.close()
