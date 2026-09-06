from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from openfoundry.database import Database
from openfoundry.errors import ConflictError, ValidationError
from openfoundry.lineage import LineageEdge, LineageStore


def test_traversal_depth_impact_and_run(tmp_path):
    store = LineageStore(Database(tmp_path / "db"))
    a = LineageEdge("a", "b", "used", "entity", "activity", "run")
    b = LineageEdge("b", "c", "generated", "activity", "entity", "run")
    store.add(a)
    store.add(b)
    assert store.downstream("a", max_depth=1) == [a]
    assert store.upstream("c") == [a, b]
    assert store.impact("a") == ["b", "c"]
    assert store.by_run("run") == [a, b]


def test_cycle_and_invalid_depth_rejected(tmp_path):
    store = LineageStore(Database(tmp_path / "db"))
    store.add(LineageEdge("a", "b", "used", "entity", "activity"))
    with pytest.raises(ConflictError):
        store.add(LineageEdge("b", "a", "generated", "activity", "entity"))
    with pytest.raises(ValidationError):
        store.downstream("a", max_depth=0)


def test_concurrent_opposite_edges_cannot_create_cycle(tmp_path):
    store = LineageStore(Database(tmp_path / "db"))
    barrier = Barrier(2)

    def add(edge):
        barrier.wait()
        try:
            store.add(edge)
            return "added"
        except ConflictError:
            return "rejected"

    edges = [
        LineageEdge("a", "b", "used", "entity", "activity"),
        LineageEdge("b", "a", "generated", "activity", "entity"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, edges))
    assert sorted(results) == ["added", "rejected"]
