# ruff: noqa: E501

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openfoundry.canonical import canonical_json
from openfoundry.errors import ConflictError, IntegrityError, NotFoundError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources(uid TEXT NOT NULL, revision TEXT NOT NULL, kind TEXT NOT NULL,
 data BLOB NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(uid,revision));
CREATE TABLE IF NOT EXISTS statuses(uid TEXT PRIMARY KEY, data BLOB NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(name TEXT PRIMARY KEY, uid TEXT NOT NULL, revision TEXT NOT NULL,
 version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, type TEXT NOT NULL, source TEXT NOT NULL,
 subject TEXT NOT NULL, time TEXT NOT NULL, resource_uid TEXT NOT NULL, revision TEXT NOT NULL,
 run_id TEXT, sequence INTEGER NOT NULL, data BLOB NOT NULL, digest TEXT NOT NULL,
 signature TEXT NOT NULL, UNIQUE(resource_uid,sequence));
CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY REFERENCES events(id), published_at TEXT);
CREATE TABLE IF NOT EXISTS lineage(source TEXT NOT NULL, target TEXT NOT NULL, relation TEXT NOT NULL,
 run_id TEXT, data BLOB NOT NULL, PRIMARY KEY(source,target,relation));
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
 data BLOB NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, purpose TEXT NOT NULL, nonce BLOB NOT NULL,
 ciphertext BLOB NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS federation_peers(id TEXT PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS federation_inbox(peer_id TEXT NOT NULL, event_id TEXT NOT NULL,
 data BLOB NOT NULL, PRIMARY KEY(peer_id,event_id));
CREATE TABLE IF NOT EXISTS federation_outbox(peer_id TEXT NOT NULL, event_id TEXT NOT NULL,
 data BLOB NOT NULL, published_at TEXT, PRIMARY KEY(peer_id,event_id));
CREATE INDEX IF NOT EXISTS events_resource_idx ON events(resource_uid,sequence);
CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id);
CREATE INDEX IF NOT EXISTS events_type_time_idx ON events(type,time);
CREATE INDEX IF NOT EXISTS lineage_target_idx ON lineage(target);
CREATE TRIGGER IF NOT EXISTS resources_no_update BEFORE UPDATE ON resources
 BEGIN SELECT RAISE(ABORT,'immutable resource'); END;
CREATE TRIGGER IF NOT EXISTS resources_no_delete BEFORE DELETE ON resources
 BEGIN SELECT RAISE(ABORT,'immutable resource'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS lineage_no_update BEFORE UPDATE ON lineage
 BEGIN SELECT RAISE(ABORT,'immutable lineage'); END;
CREATE TRIGGER IF NOT EXISTS lineage_no_delete BEFORE DELETE ON lineage
 BEGIN SELECT RAISE(ABORT,'immutable lineage'); END;
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS api_tokens(token_hash TEXT PRIMARY KEY, actor TEXT NOT NULL,
 scopes BLOB NOT NULL, expires_at TEXT, created_at TEXT NOT NULL, revoked_at TEXT);
CREATE INDEX IF NOT EXISTS api_tokens_actor_idx ON api_tokens(actor,revoked_at);
"""

_SCHEMA_V3 = """
CREATE INDEX IF NOT EXISTS resources_kind_uid_created_idx
 ON resources(kind,uid,created_at,revision);
CREATE INDEX IF NOT EXISTS resources_created_idx ON resources(created_at,uid);
CREATE INDEX IF NOT EXISTS operations_state_id_idx ON operations(state,id);
"""

_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS event_order(position INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE REFERENCES events(id));
INSERT OR IGNORE INTO event_order(event_id) SELECT id FROM events ORDER BY time,id;
CREATE TRIGGER IF NOT EXISTS event_order_no_update BEFORE UPDATE ON event_order
 BEGIN SELECT RAISE(ABORT,'immutable event order'); END;
CREATE TRIGGER IF NOT EXISTS event_order_no_delete BEFORE DELETE ON event_order
 BEGIN SELECT RAISE(ABORT,'immutable event order'); END;
"""

_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS resource_order(position INTEGER PRIMARY KEY AUTOINCREMENT,
 uid TEXT NOT NULL, revision TEXT NOT NULL, UNIQUE(uid,revision),
 FOREIGN KEY(uid,revision) REFERENCES resources(uid,revision));
INSERT OR IGNORE INTO resource_order(uid,revision)
 SELECT uid,revision FROM resources ORDER BY rowid;
CREATE TRIGGER IF NOT EXISTS resources_record_order AFTER INSERT ON resources
 BEGIN INSERT INTO resource_order(uid,revision) VALUES(NEW.uid,NEW.revision); END;
CREATE TRIGGER IF NOT EXISTS resource_order_no_update BEFORE UPDATE ON resource_order
 BEGIN SELECT RAISE(ABORT,'immutable resource order'); END;
CREATE TRIGGER IF NOT EXISTS resource_order_no_delete BEFORE DELETE ON resource_order
 BEGIN SELECT RAISE(ABORT,'immutable resource order'); END;
"""

_SCHEMA_V6 = """
DROP TABLE IF EXISTS federation_inbox;
DROP TABLE IF EXISTS federation_outbox;
DROP TABLE IF EXISTS federation_peers;
"""


@dataclass(frozen=True)
class _Migration:
    version: int
    name: str
    checksum: str
    sql: str


_MIGRATIONS = (
    _Migration(
        1,
        "legacy-0.1-baseline",
        "b4b91aa0e91c72692e4d4c1649d3e9c04814454ea56dc51d2a55f7ce35b22278",
        _SCHEMA,
    ),
    _Migration(
        2,
        "api-tokens",
        "d90790324ed34f4c64038f22b5ea04e396a33c3121c12bfcaacaee5c157815d1",
        _SCHEMA_V2,
    ),
    _Migration(
        3,
        "query-indices",
        "45eee84933d2ca28e50b499b2d784f610ba445f69c43b86b03591a2f79049c0a",
        _SCHEMA_V3,
    ),
    _Migration(
        4,
        "event-order",
        "b83be71c941c1822f395d7aca4478dbd3f2b456b2b58419d49e1a4c69a2cc149",
        _SCHEMA_V4,
    ),
    _Migration(
        5,
        "resource-order",
        "b48b2309058ad6ce0a4f34dfdba3a14352092ed61493b7efe23db85607ba1eae",
        _SCHEMA_V5,
    ),
    _Migration(
        6,
        "drop-federation",
        "7e9e81339aa73fea140e86b2ce9a958e9d0d5d917b7f6312a8b28ad0de1d3b2f",
        _SCHEMA_V6,
    ),
)


def _validate_migration_registry() -> None:
    versions = [migration.version for migration in _MIGRATIONS]
    if versions != list(range(1, len(_MIGRATIONS) + 1)):
        raise IntegrityError("bundled migration versions are not contiguous and ordered")
    names = [migration.name for migration in _MIGRATIONS]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise IntegrityError("bundled migration names must be non-empty and unique")
    for migration in _MIGRATIONS:
        if hashlib.sha256(migration.sql.encode()).hexdigest() != migration.checksum:
            raise IntegrityError(f"bundled migration checksum drift at version {migration.version}")


def _execute_sql(connection: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise IntegrityError("migration contains an incomplete SQL statement")


def _prepare_migration_table(connection: sqlite3.Connection) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    columns = (
        {str(row[1]) for row in connection.execute("PRAGMA table_info(schema_migrations)")}
        if exists
        else set()
    )
    legacy = columns == {"version"}
    if not exists:
        connection.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL)"
        )
    elif legacy:
        connection.execute("ALTER TABLE schema_migrations ADD COLUMN name TEXT")
        connection.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
    elif columns != {"version", "name", "checksum"}:
        raise IntegrityError("schema migration table has an unsupported shape")
    return legacy


def _verify_migration_history(connection: sqlite3.Connection, legacy: bool) -> None:
    rows = connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = [int(row[0]) for row in rows]
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise IntegrityError("schema migration history contains a version gap")
    if versions and versions[-1] > len(_MIGRATIONS):
        raise IntegrityError("database was created by a newer schema version")
    for row in rows:
        migration = _MIGRATIONS[int(row[0]) - 1]
        if legacy:
            connection.execute(
                "UPDATE schema_migrations SET name=?,checksum=? WHERE version=?",
                (migration.name, migration.checksum, migration.version),
            )
        elif row[1] != migration.name or row[2] != migration.checksum:
            raise IntegrityError(f"schema migration metadata drift at version {migration.version}")


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout: int = 5000,
        migrate: bool = True,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path)
        if read_only and migrate:
            raise ValueError("a read-only database cannot run migrations")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout = busy_timeout
        self.read_only = read_only
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        if migrate:
            self.migrate()

    @classmethod
    def inspect(cls, path: str | Path, *, busy_timeout: int = 5000) -> Database:
        return cls(path, busy_timeout=busy_timeout, migrate=False, read_only=True)

    def connect(self) -> sqlite3.Connection:
        target = (
            f"{self.path.resolve().as_uri()}?mode=ro&immutable=1" if self.read_only else self.path
        )
        connection = sqlite3.connect(
            target,
            timeout=self.busy_timeout / 1000,
            isolation_level=None,
            check_same_thread=False,
            uri=self.read_only,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout}")
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
        with self._connections_lock:
            self._connections.add(connection)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self.connect()
            self._local.connection = connection
        return connection

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def migrate(self) -> None:
        if self.read_only:
            raise IntegrityError("cannot migrate a read-only database")
        _validate_migration_registry()
        connection = self.connection
        with self.transaction(immediate=True):
            _verify_migration_history(connection, _prepare_migration_table(connection))
        for migration in _MIGRATIONS:
            with self.transaction(immediate=True):
                if connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (migration.version,)
                ).fetchone():
                    continue
                _execute_sql(connection, migration.sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,checksum) VALUES(?,?,?)",
                    (migration.version, migration.name, migration.checksum),
                )

    def rebuild_indices(self) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute("REINDEX")

    def verify_migrations(self) -> bool:
        _validate_migration_registry()
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(schema_migrations)")
        }
        if columns != {"version", "name", "checksum"}:
            raise IntegrityError("schema migration table has an unsupported shape")
        rows = self.connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        recorded = [(int(row[0]), str(row[1]), str(row[2])) for row in rows]
        expected = [
            (migration.version, migration.name, migration.checksum) for migration in _MIGRATIONS
        ]
        if recorded != expected:
            raise IntegrityError("schema migration history does not match this installed build")
        return True

    def integrity_check(self) -> bool:
        result = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        return bool(result == "ok")

    def backup(self, destination: str | Path) -> None:
        target = sqlite3.connect(destination)
        try:
            self.connection.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        if hasattr(self._local, "connection"):
            del self._local.connection


class ResourceRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def put(
        self, uid: str, revision: str, kind: str, value: Any, *, created_at: str
    ) -> dict[str, Any]:
        from openfoundry.canonical import sha256_digest

        raw, digest = canonical_json(value), sha256_digest(value)
        try:
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    "INSERT INTO resources VALUES(?,?,?,?,?,?)",
                    (uid, revision, kind, raw, digest, created_at),
                )
        except sqlite3.IntegrityError as exc:
            row = self.db.connection.execute(
                "SELECT data FROM resources WHERE uid=? AND revision=?", (uid, revision)
            ).fetchone()
            if row is None or bytes(row[0]) != raw:
                raise ConflictError(
                    "immutable resource identity already has different content"
                ) from exc
        return self.get(uid, revision)

    def get(self, uid: str, revision: str) -> dict[str, Any]:
        row = self.db.connection.execute(
            "SELECT data FROM resources WHERE uid=? AND revision=?", (uid, revision)
        ).fetchone()
        if row is None:
            raise NotFoundError("resource not found")
        value: dict[str, Any] = json.loads(row[0])
        return value

    def list(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT data FROM resources"
        args: tuple[Any, ...] = ()
        if kind is not None:
            query += " WHERE kind=?"
            args = (kind,)
        return [
            json.loads(row[0])
            for row in self.db.connection.execute(query + " ORDER BY uid,revision", args)
        ]

    def latest(
        self, *, kind: str | None = None, limit: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        if limit is not None and limit < 1:
            return []
        where = "WHERE resources.kind=?" if kind is not None else ""
        args: builtins.list[Any] = [kind] if kind is not None else []
        query = f"""
            WITH ranked AS (
              SELECT resources.data,resource_order.position AS commit_position,resources.uid,
                ROW_NUMBER() OVER (
                  PARTITION BY resources.uid ORDER BY resource_order.position DESC
                ) AS revision_rank
              FROM resources JOIN resource_order USING(uid,revision) {where}
            )
            SELECT data FROM ranked WHERE revision_rank=1
            ORDER BY commit_position DESC,uid DESC
        """
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        return [json.loads(row[0]) for row in self.db.connection.execute(query, args)]

    def inventory(self) -> builtins.list[dict[str, Any]]:
        rows = self.db.connection.execute(
            """
            SELECT kind,COUNT(DISTINCT uid),COUNT(*),MAX(created_at)
            FROM resources GROUP BY kind ORDER BY kind
            """
        )
        return [
            {
                "kind": str(row[0]),
                "objects": int(row[1]),
                "revisions": int(row[2]),
                "latestAt": str(row[3]),
            }
            for row in rows
        ]

    def get_status(self, uid: str) -> tuple[dict[str, Any], int]:
        row = self.db.connection.execute(
            "SELECT data,version FROM statuses WHERE uid=?", (uid,)
        ).fetchone()
        if row is None:
            raise NotFoundError("status not found")
        return json.loads(row[0]), int(row[1])

    def set_status(self, uid: str, value: Any, *, expected_version: int | None) -> int:
        raw = canonical_json(value)
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute("SELECT version FROM statuses WHERE uid=?", (uid,)).fetchone()
            current = None if row is None else int(row[0])
            if current != expected_version:
                raise ConflictError(
                    "status version mismatch",
                    details={"expectedVersion": expected_version, "currentVersion": current},
                )
            version = 1 if current is None else current + 1
            connection.execute(
                "INSERT INTO statuses VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET data=excluded.data,version=excluded.version",
                (uid, raw, version),
            )
        return version


class AliasRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    def move(self, name: str, uid: str, revision: str, *, expected_version: int | None) -> int:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute("SELECT version FROM aliases WHERE name=?", (name,)).fetchone()
            current = None if row is None else int(row[0])
            if current != expected_version:
                raise ConflictError(
                    "alias version mismatch",
                    details={"expectedVersion": expected_version, "currentVersion": current},
                )
            version = 1 if current is None else current + 1
            connection.execute(
                "INSERT INTO aliases VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET uid=excluded.uid,revision=excluded.revision,version=excluded.version",
                (name, uid, revision, version),
            )
            return version

    def get(self, name: str) -> tuple[str, str, int]:
        row = self.db.connection.execute(
            "SELECT uid,revision,version FROM aliases WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            raise NotFoundError("alias not found")
        return str(row[0]), str(row[1]), int(row[2])

    def list(self) -> builtins.list[dict[str, Any]]:
        rows = self.db.connection.execute(
            "SELECT name,uid,revision,version FROM aliases ORDER BY name"
        )
        return [
            {
                "name": str(row[0]),
                "uid": str(row[1]),
                "revision": str(row[2]),
                "version": int(row[3]),
            }
            for row in rows
        ]
