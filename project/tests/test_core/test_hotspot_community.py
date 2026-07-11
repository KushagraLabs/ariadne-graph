"""Failing tests scoping two hotspot/community quality items (P1 + P2).

These tests describe the *target* behavior the reviewer asked for and are
expected to fail against the current implementation:

* Item 1 (hotspots): each hotspot must expose an internal-vs-external coupling
  split so a large file's INTERNAL self-references are not misread as
  cross-domain coupling. The current ``HotspotInfo`` has no such fields.

* Item 2 (communities): ``detect_communities`` must support FILE granularity
  and must exclude external/library symbols (nodes without a repo ``file_path``).
  Today it only runs over raw SCIP symbol nodes with no granularity switch.

Both tests reuse the in-memory ``FakeSearchableGraphStore`` fixture pattern
from ``test_communities.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ariadne_graph.core.communities import CommunityAnalyzer
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.base import SearchableGraphStore


class FakeSearchableGraphStore(SearchableGraphStore):
    """In-memory searchable store (mirrors test_communities.py)."""

    def __init__(self) -> None:
        self._nodes: dict[str, CodeNode] = {}
        self._edges: list[CodeEdge] = []
        self._communities: dict[int, list[str]] = {}

    async def delete_graph(self, graph_id: str) -> None:
        pass

    async def delete_file_facts(self, graph_id: str, file_path: str) -> None:
        pass

    async def add_nodes_batch(self, graph_id: str, nodes: Sequence[CodeNode]) -> None:
        for node in nodes:
            self._nodes[node.id] = node

    async def add_edges_batch(self, graph_id: str, edges: Sequence[CodeEdge]) -> None:
        self._edges.extend(edges)

    async def query(
        self,
        graph_id: str,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if query == "nodes":
            return [{"n": node.model_dump()} for node in self._nodes.values()]
        if query == "edges":
            return [{"r": edge.model_dump()} for edge in self._edges]
        return []

    async def get_stored_hash(self, graph_id: str, file_path: str) -> str | None:
        return None

    async def update_hash(
        self, graph_id: str, file_path: str, content_hash: str
    ) -> None:
        pass

    async def close(self) -> None:
        pass

    async def upsert_embeddings(self, graph_id: str, rows: Sequence[Any]) -> None:
        pass

    async def search_vector(
        self, graph_id: str, vector: Sequence[float], limit: int
    ) -> list[Any]:
        return []

    async def search_keyword(self, graph_id: str, query: str, limit: int) -> list[Any]:
        return []

    async def set_communities(self, graph_id: str, assignments: dict[str, int]) -> None:
        self._communities = {}
        for node_id, cid in assignments.items():
            self._communities.setdefault(cid, []).append(node_id)

    async def get_communities(self, graph_id: str) -> dict[int, list[str]]:
        return self._communities


@pytest.fixture
def store() -> FakeSearchableGraphStore:
    return FakeSearchableGraphStore()


@pytest.fixture
def analyzer(store: FakeSearchableGraphStore) -> CommunityAnalyzer:
    return CommunityAnalyzer(store)


def _fn(node_id: str, file_path: str) -> CodeNode:
    """A repo-internal function node (has a file_path)."""
    return CodeNode(
        id=node_id,
        graph_id="g",
        labels=["CodeFunction"],
        properties={"name": node_id.split(".")[-1], "file_path": file_path},
    )


def _external(node_id: str) -> CodeNode:
    """A library/external symbol node: no repo file_path."""
    return CodeNode(
        id=node_id,
        graph_id="g",
        labels=["CodeFunction"],
        properties={"name": node_id.split(".")[-1], "file_path": ""},
    )


class TestHotspotInternalExternalSplit:
    """Item 1 (P1): hotspots must separate internal from external coupling."""

    @pytest.mark.asyncio
    async def test_hotspot_exposes_internal_vs_external_split(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        """A big file whose references are mostly INTERNAL (self-references)
        must report the internal/external split so its high raw degree is not
        misread as cross-domain coupling.

        Fixture: ``big.py`` has 6 symbols that call each other heavily
        (internal), plus a single call out to one symbol in ``other.py``
        (external to the file). Internal references far outnumber external.
        """
        big = "big.py"
        other = "other.py"
        # Six symbols all living in big.py.
        nodes = [_fn(f"big.f{i}", big) for i in range(6)]
        nodes.append(_fn("other.g0", other))
        await store.add_nodes_batch("g", nodes)

        # Dense internal call clique inside big.py (12 intra-file edges).
        edges: list[CodeEdge] = []
        for i in range(6):
            for j in range(6):
                if i != j and (i + j) % 2 == 0:
                    edges.append(
                        CodeEdge(
                            source=f"big.f{i}",
                            target=f"big.f{j}",
                            graph_id="g",
                            rel_type="CALLS",
                        )
                    )
        # A single cross-file edge: big.py -> other.py.
        edges.append(
            CodeEdge(
                source="big.f0", target="other.g0", graph_id="g", rel_type="CALLS"
            )
        )
        await store.add_edges_batch("g", edges)

        hotspots = await analyzer.find_hotspots("g", top_n=10, metric="complexity")
        assert hotspots, "expected at least one hotspot"

        top = hotspots[0]
        dumped = top.model_dump()

        # The reviewer's core ask: an internal-vs-external coupling split must be
        # present on the hotspot output. These fields do not exist yet, so this
        # assertion fails.
        assert (
            "internal_refs" in dumped and "external_refs" in dumped
        ), (
            "hotspot must expose internal_refs / external_refs split; "
            f"got fields: {sorted(dumped)}"
        )

        # For a big.py symbol, internal references must dominate external ones —
        # the whole point of the split (1100-vs-249 misread scenario).
        assert dumped["internal_refs"] > dumped["external_refs"], (
            "internal self-references should dominate; "
            f"internal={dumped.get('internal_refs')} "
            f"external={dumped.get('external_refs')}"
        )


class TestCommunityFileGranularity:
    """Item 2 (P2): community detection at FILE granularity, externals excluded."""

    @pytest.mark.asyncio
    async def test_detect_communities_file_granularity_excludes_externals(
        self, analyzer: CommunityAnalyzer, store: FakeSearchableGraphStore
    ) -> None:
        """``detect_communities`` must support running over the FILE graph and
        must drop external/library symbols (no repo ``file_path``).

        Fixture: two files, each with two internal symbols, plus one external
        library symbol that both files call. At file granularity the result
        must be keyed by the two repo files and must NOT contain the external
        symbol id.
        """
        file_a = "pkg/a.py"
        file_b = "pkg/b.py"
        nodes = [
            _fn("pkg.a.f1", file_a),
            _fn("pkg.a.f2", file_a),
            _fn("pkg.b.g1", file_b),
            _fn("pkg.b.g2", file_b),
            _external("library.helper"),
        ]
        await store.add_nodes_batch("g", nodes)
        edges = [
            CodeEdge(source="pkg.a.f1", target="pkg.a.f2", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="pkg.b.g1", target="pkg.b.g2", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="pkg.a.f1", target="pkg.b.g1", graph_id="g", rel_type="CALLS"),
            # Both files reach out to the external library symbol.
            CodeEdge(source="pkg.a.f2", target="library.helper", graph_id="g", rel_type="CALLS"),
            CodeEdge(source="pkg.b.g2", target="library.helper", graph_id="g", rel_type="CALLS"),
        ]
        await store.add_edges_batch("g", edges)

        # New granularity switch — does not exist yet, so this call raises
        # TypeError (unexpected keyword argument) today.
        assignments = await analyzer.detect_communities("g", granularity="file")

        keys = set(assignments.keys())
        # Communities must be keyed by repo files, not raw symbol ids.
        assert file_a in keys and file_b in keys, (
            f"expected file-granularity keys; got {sorted(keys)}"
        )
        # External/library symbol must be excluded entirely.
        assert "library.helper" not in keys, (
            "external/library symbols must be excluded from communities"
        )
        assert not any(k.startswith("pkg.") for k in keys), (
            f"symbol-level ids leaked into file-granularity result: {sorted(keys)}"
        )
