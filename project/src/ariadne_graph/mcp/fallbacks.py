"""Fallback implementations for ToolRegistry handlers.

These helpers run against any :class:`GraphStore` backend when the
SearchableGraphStore-based services (GraphRetriever, CommunityAnalyzer) are
not available. They keep the manual scan logic in one place instead of
duplicating it across every handler.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from ariadne_graph.graphstores.base import GraphStore
from ariadne_graph.mcp.schemas import (
    ArchitectureOutput,
    FindHotspotsOutput,
    ImpactAnalysisOutput,
    RetrieveOutput,
    TraceDependenciesOutput,
)

logger = logging.getLogger(__name__)


class GraphStoreFallbacks:
    """Minimal graph operations that work with a plain GraphStore backend."""

    @staticmethod
    async def retrieve(
        graph_store: GraphStore,
        graph_id: str,
        query: str,
    ) -> RetrieveOutput:
        """Retrieve a node by id/name plus its neighbors and edges."""
        results: list[dict[str, Any]] = []

        try:
            nodes = await graph_store.query(graph_id, "nodes")
            for row in nodes:
                node_data = row.get("n", row)
                if node_data.get("id") == query:
                    results.append({"type": "node", "data": node_data})
        except Exception as exc:
            logger.warning("Fallback retrieve: nodes query failed for %s: %s", graph_id, exc)

        try:
            neighbors = await graph_store.query(
                graph_id, "node_neighbors", {"node_id": query}
            )
            for row in neighbors:
                node_data = row.get("n", row)
                if not any(
                    r.get("data", {}).get("id") == node_data.get("id") for r in results
                ):
                    results.append({"type": "neighbor", "data": node_data})
        except Exception as exc:
            logger.warning(
                "Fallback retrieve: neighbors query failed for %s: %s", query, exc
            )

        try:
            edges = await graph_store.query(graph_id, "edges")
            for row in edges:
                edge_data = row.get("r", row)
                if edge_data.get("source") == query or edge_data.get("target") == query:
                    results.append({"type": "edge", "data": edge_data})
        except Exception as exc:
            logger.warning("Fallback retrieve: edges query failed for %s: %s", query, exc)

        return RetrieveOutput(results=results)

    @staticmethod
    async def trace_dependencies(
        graph_store: GraphStore,
        graph_id: str,
        symbol: str,
        direction: str,
        max_depth: int,
    ) -> TraceDependenciesOutput:
        """BFS trace of call/import dependencies from a symbol."""
        try:
            edges = await graph_store.query(graph_id, "edges")
        except Exception as exc:
            logger.warning("Fallback trace: edges query failed for %s: %s", graph_id, exc)
            return TraceDependenciesOutput(paths=[])

        outgoing: dict[str, list[str]] = {}
        incoming: dict[str, list[str]] = {}
        for row in edges:
            edge_data = row.get("r", row)
            src = edge_data.get("source", "")
            tgt = edge_data.get("target", "")
            if src and tgt:
                outgoing.setdefault(src, []).append(tgt)
                incoming.setdefault(tgt, []).append(src)

        all_paths: list[list[str]] = []
        visited: set[tuple[str, ...]] = set()
        queue: deque[tuple[str, list[str], int]] = deque()
        queue.append((symbol, [symbol], 0))

        while queue:
            current, path, depth = queue.popleft()
            if depth >= max_depth:
                continue

            neighbors: list[str] = []
            if direction in ("both", "downstream"):
                neighbors.extend(outgoing.get(current, []))
            if direction in ("both", "upstream"):
                neighbors.extend(incoming.get(current, []))

            for neighbor in neighbors:
                new_path = path + [neighbor]
                path_key = tuple(new_path)
                if path_key not in visited:
                    visited.add(path_key)
                    all_paths.append(new_path)
                    queue.append((neighbor, new_path, depth + 1))

        return TraceDependenciesOutput(paths=all_paths)

    @staticmethod
    async def impact_analysis(
        graph_store: GraphStore,
        graph_id: str,
        symbol: str,
    ) -> ImpactAnalysisOutput:
        """Transitive closure impact analysis from a symbol."""
        try:
            edges = await graph_store.query(graph_id, "edges")
        except Exception:
            return ImpactAnalysisOutput(
                target_symbol=symbol,
                total_affected=0,
                message="Failed to query graph for impact analysis",
            )

        outgoing: dict[str, list[str]] = {}
        incoming: dict[str, list[str]] = {}
        for row in edges:
            edge_data = row.get("r", row)
            src = edge_data.get("source", "")
            tgt = edge_data.get("target", "")
            outgoing.setdefault(src, []).append(tgt)
            incoming.setdefault(tgt, []).append(src)

        direct = set(outgoing.get(symbol, []))
        direct.update(incoming.get(symbol, []))

        visited: set[str] = set()
        queue: deque[str] = deque([symbol])
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for nxt in outgoing.get(current, []):
                if nxt not in visited:
                    queue.append(nxt)
            for nxt in incoming.get(current, []):
                if nxt not in visited:
                    queue.append(nxt)

        visited.discard(symbol)
        transitive = sorted(visited)

        coupling_scores: dict[str, float] = {}
        for dep in transitive:
            dep_out = set(outgoing.get(dep, []))
            dep_in = set(incoming.get(dep, []))
            sym_out = set(outgoing.get(symbol, []))
            sym_in = set(incoming.get(symbol, []))
            shared = len(dep_out & sym_out) + len(dep_in & sym_in)
            coupling_scores[dep] = round(float(shared) + 1.0, 2)

        return ImpactAnalysisOutput(
            target_symbol=symbol,
            total_affected=len(transitive),
            direct_dependencies=sorted(direct),
            transitive_affected=transitive,
            coupling_scores=coupling_scores,
        )

    @staticmethod
    async def find_hotspots(
        graph_store: GraphStore,
        graph_id: str,
        top_n: int,
        metric: str,
    ) -> FindHotspotsOutput:
        """Rank symbols by fan-in, fan-out, coupling, or combined degree."""
        try:
            edges = await graph_store.query(graph_id, "edges")
            nodes = await graph_store.query(graph_id, "nodes")
        except Exception as exc:
            return FindHotspotsOutput(
                hotspots=[],
                message=f"Failed to query graph: {exc}",
            )

        fan_in: dict[str, int] = {}
        fan_out: dict[str, int] = {}
        coupling: dict[str, set[str]] = {}

        for row in edges:
            edge_data = row.get("r", row)
            src = edge_data.get("source", "")
            tgt = edge_data.get("target", "")
            if src and tgt:
                fan_out[src] = fan_out.get(src, 0) + 1
                fan_in[tgt] = fan_in.get(tgt, 0) + 1
                coupling.setdefault(src, set()).add(tgt)
                coupling.setdefault(tgt, set()).add(src)

        node_info: dict[str, dict[str, Any]] = {}
        for row in nodes:
            node_data = row.get("n", row)
            nid = node_data.get("id", "")
            props = node_data.get("properties", {})
            node_info[nid] = {
                "name": props.get("name", nid),
                "file_path": props.get("file_path", ""),
                "labels": node_data.get("labels", []),
            }

        all_symbols = set(fan_in.keys()) | set(fan_out.keys())
        scored: list[tuple[str, float]] = []

        for sym in all_symbols:
            if metric == "fan_in":
                score = float(fan_in.get(sym, 0))
            elif metric == "fan_out":
                score = float(fan_out.get(sym, 0))
            elif metric == "coupling":
                score = float(len(coupling.get(sym, set())))
            else:  # complexity — combine fan_in + fan_out
                score = float(fan_in.get(sym, 0) + fan_out.get(sym, 0))
            scored.append((sym, score))

        scored.sort(key=lambda x: x[1], reverse=True)

        hotspots: list[dict[str, Any]] = []
        for sym, score in scored[:top_n]:
            info = node_info.get(sym, {})
            hotspots.append({
                "node_id": sym,
                "node_name": info.get("name", sym),
                "file_path": info.get("file_path", ""),
                "metric_type": metric,
                "score": score,
            })

        return FindHotspotsOutput(
            hotspots=hotspots,
            message=f"Top {len(hotspots)} hotspots by {metric}",
        )

    @staticmethod
    async def get_architecture(
        graph_store: GraphStore,
        graph_id: str,
    ) -> ArchitectureOutput:
        """Connected-component architecture summary."""
        communities_data: dict[int, list[str]] = {}
        try:
            edges = await graph_store.query(graph_id, "edges")
            nodes = await graph_store.query(graph_id, "nodes")

            parent: dict[str, str] = {}

            def find(x: str) -> str:
                if x not in parent:
                    parent[x] = x
                if parent[x] != x:
                    parent[x] = find(parent[x])
                return parent[x]

            def union(x: str, y: str) -> None:
                px, py = find(x), find(y)
                if px != py:
                    parent[px] = py

            for row in edges:
                edge_data = row.get("r", row)
                src = edge_data.get("source", "")
                tgt = edge_data.get("target", "")
                if src and tgt:
                    union(src, tgt)

            groups: dict[str, list[str]] = {}
            for row in nodes:
                node_data = row.get("n", row)
                nid = node_data.get("id", "")
                root = find(nid)
                groups.setdefault(root, []).append(nid)

            communities_data = dict(enumerate(groups.values()))
        except Exception as exc:
            return ArchitectureOutput(
                summary={},
                message=f"Failed to compute architecture: {exc}",
            )

        try:
            nodes = await graph_store.query(graph_id, "nodes")
            total_entities = len(nodes)
            total_files = len({
                row.get("n", {}).get("properties", {}).get("file_path")
                for row in nodes
                if row.get("n", {}).get("properties", {}).get("file_path")
            })
        except Exception:
            total_entities = 0
            total_files = 0

        community_summaries: list[dict[str, Any]] = []
        for comm_id, members in communities_data.items():
            community_summaries.append({
                "community_id": comm_id,
                "member_count": len(members),
                "representative_files": [],
            })

        summary = {
            "total_communities": len(communities_data),
            "total_files": total_files,
            "total_entities": total_entities,
            "communities": community_summaries,
        }

        return ArchitectureOutput(
            summary=summary,
            message=(
                f"Architecture: {len(communities_data)} communities, "
                f"{total_files} files, {total_entities} entities"
            ),
        )
