from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

from openfoundry.canonical import canonical_json
from openfoundry.database import Database
from openfoundry.errors import ConflictError, ValidationError

NodeKind = Literal["entity", "activity", "agent"]


@dataclass(frozen=True)
class LineageEdge:
    source: str
    target: str
    relation: str
    source_kind: NodeKind
    target_kind: NodeKind
    run_id: str | None = None
    attributes: dict[str, Any] | None = None


class LineageStore:
    def __init__(self, database: Database) -> None:
        self.db = database

    def add(self, edge: LineageEdge) -> LineageEdge:
        raw = canonical_json(asdict(edge))
        try:
            with self.db.transaction(immediate=True) as connection:
                if edge.source == edge.target or self._reachable_in_transaction(
                    connection, edge.target, edge.source
                ):
                    raise ConflictError("lineage edge would create a cycle")
                connection.execute(
                    "INSERT INTO lineage VALUES(?,?,?,?,?)",
                    (edge.source, edge.target, edge.relation, edge.run_id, raw),
                )
        except sqlite3.IntegrityError as exc:
            row = self.db.connection.execute(
                "SELECT data FROM lineage WHERE source=? AND target=? AND relation=?",
                (edge.source, edge.target, edge.relation),
            ).fetchone()
            if row is None or bytes(row[0]) != raw:
                raise ConflictError("lineage edge identity has different content") from exc
        return edge

    add_edge = add

    @staticmethod
    def _reachable_in_transaction(connection: sqlite3.Connection, start: str, goal: str) -> bool:
        row = connection.execute(
            """
            WITH RECURSIVE reachable(node) AS (
              SELECT target FROM lineage WHERE source=?
              UNION
              SELECT lineage.target FROM lineage
              JOIN reachable ON lineage.source=reachable.node
            )
            SELECT 1 FROM reachable WHERE node=? LIMIT 1
            """,
            (start, goal),
        ).fetchone()
        return row is not None

    def _traverse(self, node: str, *, downstream: bool, max_depth: int) -> list[LineageEdge]:
        if max_depth < 1:
            raise ValidationError("max_depth must be positive")
        result: dict[tuple[str, str, str], LineageEdge] = {}
        frontier, seen = {node}, {node}
        for _ in range(max_depth):
            if not frontier:
                break
            column = "source" if downstream else "target"
            placeholders = ",".join("?" for _ in frontier)
            rows = self.db.connection.execute(
                f"SELECT data FROM lineage WHERE {column} IN ({placeholders})", sorted(frontier)
            )
            following: set[str] = set()
            for row in rows:
                edge = LineageEdge(**json.loads(row[0]))
                result[(edge.source, edge.target, edge.relation)] = edge
                other = edge.target if downstream else edge.source
                if other not in seen:
                    seen.add(other)
                    following.add(other)
            frontier = following
        return sorted(result.values(), key=lambda edge: (edge.source, edge.target, edge.relation))

    def upstream(self, node: str, *, max_depth: int = 100) -> list[LineageEdge]:
        return self._traverse(node, downstream=False, max_depth=max_depth)

    def downstream(self, node: str, *, max_depth: int = 100) -> list[LineageEdge]:
        return self._traverse(node, downstream=True, max_depth=max_depth)

    def impact(self, node: str, *, max_depth: int = 100) -> list[str]:
        return sorted({edge.target for edge in self.downstream(node, max_depth=max_depth)})

    revocation_impact = impact

    def by_run(self, run_id: str) -> list[LineageEdge]:
        rows = self.db.connection.execute(
            "SELECT data FROM lineage WHERE run_id=? ORDER BY source,target,relation", (run_id,)
        )
        return [LineageEdge(**json.loads(row[0])) for row in rows]
