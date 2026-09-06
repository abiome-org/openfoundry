import sqlite3

import openfoundry.database as database_module
import pytest
from openfoundry.database import AliasRepository, Database, ResourceRepository
from openfoundry.errors import ConflictError, IntegrityError, NotFoundError


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    yield database
    database.close()


def test_migration_idempotence_and_integrity(db):
    db.migrate()
    assert db.integrity_check()
    assert db.connection.execute("select count(*) from schema_migrations").fetchone()[0] == 6
    assert all(
        row[0] and row[1]
        for row in db.connection.execute(
            "select name,checksum from schema_migrations order by version"
        )
    )


def test_legacy_migration_table_is_upgraded(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO schema_migrations VALUES(?)", ((1,), (2,), (3,)))
    connection.executescript(
        database_module._SCHEMA + database_module._SCHEMA_V2 + database_module._SCHEMA_V3
    )
    connection.close()

    migrated = Database(path)
    rows = migrated.connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (item.version, item.name, item.checksum) for item in database_module._MIGRATIONS
    ]
    migrated.close()


@pytest.mark.parametrize("versions", [(1, 3), (1, 2, 3, 4, 5, 6, 7)])
def test_rejects_migration_gaps_and_future_versions(tmp_path, versions):
    path = tmp_path / "invalid.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO schema_migrations VALUES(?)", ((version,) for version in versions)
    )
    connection.commit()
    connection.close()

    with pytest.raises(IntegrityError):
        Database(path)


def test_rejects_migration_checksum_drift(db):
    db.connection.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=2")
    with pytest.raises(IntegrityError, match="drift at version 2"):
        db.migrate()


@pytest.mark.parametrize(
    ("name", "checksum"),
    [(None, None), ("api-tokens", None), (None, database_module._MIGRATIONS[1].checksum)],
)
def test_rejects_missing_modern_migration_metadata(tmp_path, name, checksum):
    path = tmp_path / "modern.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT,checksum TEXT)"
    )
    connection.executemany(
        "INSERT INTO schema_migrations VALUES(?,?,?)",
        ((item.version, item.name, item.checksum) for item in database_module._MIGRATIONS),
    )
    connection.execute(
        "UPDATE schema_migrations SET name=?,checksum=? WHERE version=2", (name, checksum)
    )
    connection.commit()
    connection.close()

    with pytest.raises(IntegrityError, match="drift at version 2"):
        Database(path)


def _create_partial_then_fail(database):
    with database.transaction(immediate=True) as connection:
        connection.execute("CREATE TABLE partial(value TEXT)")
        connection.execute("INSERT INTO missing VALUES(1)")


def test_failed_transaction_rolls_back_every_statement(tmp_path):
    database = Database(tmp_path / "retry.db")
    with pytest.raises(sqlite3.OperationalError):
        _create_partial_then_fail(database)
    tables = database.connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial'")
    assert tables.fetchone() is None
    database.close()


def test_inspection_is_read_only_non_migrating_and_does_not_touch_wal(tmp_path):
    path = tmp_path / "inspect.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE marker(value TEXT)")
    connection.commit()
    sidecars = [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]
    assert all(item.exists() for item in sidecars)
    before = {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in sidecars}

    inspected = Database.inspect(path)
    assert inspected.connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        inspected.connection.execute("CREATE TABLE changed(value TEXT)")
    inspected.close()
    assert {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in sidecars} == before
    connection.close()


def test_resource_immutable_and_idempotent(db):
    repo = ResourceRepository(db)
    assert repo.put("u", "r", "K", {"x": 1}, created_at="now") == {"x": 1}
    repo.put("u", "r", "K", {"x": 1}, created_at="now")
    with pytest.raises(ConflictError):
        repo.put("u", "r", "K", {"x": 2}, created_at="now")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.connection.execute("update resources set kind='Z'")


def test_latest_resource_uses_commit_order_not_authored_timestamp(db):
    repo = ResourceRepository(db)
    repo.put("u", "future", "K", {"revision": "future"}, created_at="9999-01-01T00:00:00Z")
    repo.put("u", "later", "K", {"revision": "later"}, created_at="2000-01-01T00:00:00Z")

    assert repo.latest(kind="K") == [{"revision": "later"}]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.connection.execute("UPDATE resource_order SET position=99")


def test_status_compare_and_swap(db):
    repo = ResourceRepository(db)
    assert repo.set_status("u", {"phase": "a"}, expected_version=None) == 1
    with pytest.raises(ConflictError):
        repo.set_status("u", {}, expected_version=None)
    assert repo.set_status("u", {"phase": "b"}, expected_version=1) == 2


def test_alias_compare_and_swap_and_missing(db):
    repo = AliasRepository(db)
    assert repo.move("prod", "u", "r", expected_version=None) == 1
    with pytest.raises(ConflictError):
        repo.move("prod", "u", "r2", expected_version=None)
    assert repo.get("prod") == ("u", "r", 1)
    with pytest.raises(NotFoundError):
        repo.get("none")


def test_backup_is_valid_and_contains_data(db, tmp_path):
    ResourceRepository(db).put("u", "r", "K", {"x": 1}, created_at="now")
    target = tmp_path / "backup.db"
    db.backup(target)
    copied = Database(target)
    assert ResourceRepository(copied).get("u", "r") == {"x": 1}
    assert copied.integrity_check()
    copied.close()
