from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from typing import Any

from openfoundry.canonical import canonical_json
from openfoundry.database import Database
from openfoundry.errors import ConflictError, NotFoundError, OperationCanceled
from openfoundry.ids import uuid7


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class OperationStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def create(
        self, kind: str, request: dict[str, Any], *, operation_id: str | None = None
    ) -> dict[str, Any]:
        operation = {
            "id": operation_id or str(uuid7()),
            "kind": kind,
            "state": "pending",
            "request": request,
            "result": None,
            "error": None,
            "createdAt": _now(),
            "updatedAt": _now(),
        }
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO operations(id,kind,state,data,version) VALUES(?,?,?,?,?)",
                (
                    operation["id"],
                    kind,
                    operation["state"],
                    canonical_json(operation),
                    1,
                ),
            )
        return operation

    def get(self, operation_id: str) -> dict[str, Any]:
        row = self.db.connection.execute(
            "SELECT data,version FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("operation not found")
        value: dict[str, Any] = json.loads(row[0])
        value["version"] = int(row[1])
        return value

    def update(
        self,
        operation_id: str,
        *,
        expected_version: int,
        state: str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT data,version FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("operation not found")
            if int(row[1]) != expected_version:
                raise ConflictError("operation version mismatch")
            value = json.loads(row[0])
            value.update({"state": state, "result": result, "error": error, "updatedAt": _now()})
            version = expected_version + 1
            cursor = connection.execute(
                "UPDATE operations SET state=?,data=?,version=? WHERE id=? AND version=?",
                (state, canonical_json(value), version, operation_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("operation compare-and-set failed")
        return self.get(operation_id)

    def list(self, *, state: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id FROM operations"
        args: tuple[str, ...] = ()
        if state is not None:
            query += " WHERE state=?"
            args = (state,)
        query += " ORDER BY id"
        return [self.get(str(row[0])) for row in self.db.connection.execute(query, args)]

    def advance(
        self,
        operation_id: str,
        *,
        state: str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # The execution lease owns transitions; a concurrent cancellation only adds intent.
        with self.db.transaction(immediate=True) as connection:
            value = self.get(operation_id)
            if value.get("cancelRequest") and state in {"running", "recovering", "finalizing"}:
                raise OperationCanceled("run cancellation requested")
            version = value.pop("version")
            value.update(state=state, result=result, error=error, updatedAt=_now())
            connection.execute(
                "UPDATE operations SET state=?,data=?,version=? WHERE id=? AND version=?",
                (state, canonical_json(value), version + 1, operation_id, version),
            )
        return self.get(operation_id)

    def request_cancel(self, operation_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as connection:
            value = self.get(operation_id)
            if value.get("cancelRequest") or value["state"] in {
                "succeeded",
                "failed",
                "canceled",
                "error",
                "finalizing",
            }:
                return value
            version = value.pop("version")
            value["cancelRequest"] = {"actor": actor, "reason": reason, "requestedAt": _now()}
            value["updatedAt"] = _now()
            connection.execute(
                "UPDATE operations SET data=?,version=? WHERE id=? AND version=?",
                (canonical_json(value), version + 1, operation_id, version),
            )
        return self.get(operation_id)

    def summaries(
        self, *, states: set[str] | None = None, focus: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        clauses: builtins.list[str] = []
        args: builtins.list[Any] = []
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            args.extend(sorted(states))
        if focus:
            escaped = focus.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(id LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\' OR state LIKE ? ESCAPE '\\')"
            )
            args.extend([f"%{escaped}%"] * 3)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = int(
            self.db.connection.execute("SELECT count(*) FROM operations" + where, args).fetchone()[
                0
            ]
        )
        rows = self.db.connection.execute(
            "SELECT id,kind,state,version,"
            "json_extract(CAST(data AS TEXT),'$.createdAt') AS created_at,"
            "json_extract(CAST(data AS TEXT),'$.updatedAt') AS updated_at,"
            "json_type(CAST(data AS TEXT),'$.error') != 'null' AS has_error "
            "FROM operations" + where + " ORDER BY id DESC LIMIT ?",
            [*args, limit],
        )
        items = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "state": row["state"],
                "version": row["version"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "hasError": bool(row["has_error"]),
            }
            for row in rows
        ]
        return {
            "items": items,
            "returned": len(items),
            "total": total,
            "truncated": total > len(items),
        }
