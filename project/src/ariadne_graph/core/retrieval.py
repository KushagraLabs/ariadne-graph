"""Graph retrieval — node lookup, dependency tracing, and impact analysis.

Provides BFS traversal of the code dependency graph, transitive closure
computation, and impact analysis for change planning.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, cast

from ariadne_graph.core.models import ImpactAnalysisResult
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import SearchableGraphStore

logger = logging.getLogger(__name__)


class GraphRetriever:
    """Retrieves graph data with dependency tracing and impact analysis.

    Combines graph store queries with snippet extraction to provide
    rich, contextual views of code entities and their relationships.
    """

    def __init__(
        self,
        graph_store: SearchableGraphStore,
        snippet_extractor: SnippetExtractor,
    ) -> None:
        self.graph_store = graph_store
        self.snippet_extractor = snippet_extractor

    async def retrieve_node(
        self,
        graph_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Find a node by name or ID and return it with edges and snippets.

        Args:
            graph_id: The repository graph identifier.
            query: Node name, partial name, or deterministic ID to look up.

        Returns:
            Dictionary with node data, edges, and code snippet.
        """
        # Use _resolve_symbol which has fallback to full node scan
        node_data = await self._resolve_symbol(graph_id, query)

        if node_data is None:
            return {
                "found": False,
                "query": query,
                "node": None,
                "edges": {"outgoing": [], "incoming": []},
                "snippet": "",
            }

        node_id = node_data.get("id", "")

        # Fetch connected edges
        outgoing = await self._get_edges(graph_id, node_id, direction="outgoing")
        incoming = await self._get_edges(graph_id, node_id, direction="incoming")

        # Get snippet
        snippet = ""
        file_path = node_data.get("properties", {}).get("file_path", "")
        line_start = node_data.get("properties", {}).get("line_start", 0)
        line_end = node_data.get("properties", {}).get("line_end", 0)
        if file_path and line_start:
            snippet = self.snippet_extractor.get_snippet(
                file_path=file_path,
                line_start=line_start,
                line_end=line_end or line_start,
                context_lines=5,
            )

        return {
            "found": True,
            "query": query,
            "node": node_data,
            "edges": {
                "outgoing": outgoing,
                "incoming": incoming,
            },
            "snippet": snippet,
        }

    async def _get_edges(
        self,
        graph_id: str,
        node_id: str,
        direction: str = "outgoing",
    ) -> list[dict[str, Any]]:
        """Fetch edges connected to a node.

        Args:
            graph_id: The repository graph identifier.
            node_id: The node ID to query edges for.
            direction: "outgoing", "incoming", or "both".

        Returns:
            List of edge dictionaries.
        """
        if direction == "outgoing":
            query_names = ["node_outgoing_edges", "node_edges"]
        elif direction == "incoming":
            query_names = ["node_incoming_edges", "node_edges"]
        else:
            query_names = ["node_all_edges", "node_edges"]

        rows: list[dict[str, Any]] = []
        for query_name in query_names:
            try:
                rows = await self.graph_store.query(
                    graph_id,
                    query_name,
                    params={"node_id": node_id},
                )
            except Exception as exc:
                logger.debug("Edge query %s failed for %s: %s", query_name, node_id, exc)
                continue
            if rows:
                break

        # Fallback: scan all edges and filter
        if not rows:
            try:
                all_edges = await self.graph_store.query(graph_id, "edges")
                for row in all_edges:
                    edge_data = row.get("r", row)
                    if not edge_data:
                        continue
                    src = edge_data.get("source", "")
                    tgt = edge_data.get("target", "")
                    if direction == "outgoing" and src == node_id or direction == "incoming" and tgt == node_id or direction == "both" and (src == node_id or tgt == node_id):
                        rows.append(row)
            except Exception as exc:
                logger.warning("Fallback edge scan failed for %s: %s", node_id, exc)

        edges: list[dict[str, Any]] = []
        for row in rows:
            edge_data = row.get("r", row)
            if edge_data:
                edges.append(edge_data)

        return edges

    async def trace_dependencies(
        self,
        graph_id: str,
        symbol: str,
        direction: str = "both",
        max_depth: int = 3,
    ) -> list[dict[str, Any]]:
        """BFS traversal of the dependency graph from a starting symbol.

        Args:
            graph_id: The repository graph identifier.
            symbol: Starting symbol name or node ID.
            direction: "up" (callers/dependents), "down" (callees/dependencies),
                       or "both" (bidirectional).
            max_depth: Maximum traversal depth.

        Returns:
            List of path entries, each with node and path context.
        """
        # Resolve starting node
        start_node = await self._resolve_symbol(graph_id, symbol)
        if start_node is None:
            return []

        start_id = start_node.get("id", "")

        # BFS traversal
        visited: set[str] = {start_id}
        queue: deque[tuple[str, int, list[str]]] = deque([(start_id, 0, [start_id])])
        results: list[dict[str, Any]] = []

        while queue:
            current_id, depth, path = queue.popleft()

            # Get node details
            node_data = await self._get_node_data(graph_id, current_id)
            if node_data is None:
                continue

            results.append({
                "node_id": current_id,
                "node": node_data,
                "depth": depth,
                "path": list(path),
            })

            if depth >= max_depth:
                continue

            # Find neighbors based on direction
            neighbors = await self._get_neighbors(graph_id, current_id, direction)
            for neighbor_id in neighbors:
                if neighbor_id not in visited:
                    visited.add(neighbor_id)
                    new_path = path + [neighbor_id]
                    queue.append((neighbor_id, depth + 1, new_path))

        return results

    async def impact_analysis(
        self,
        graph_id: str,
        symbol: str,
    ) -> ImpactAnalysisResult:
        """Find all nodes reachable from a symbol (transitive closure).

        Computes the full set of nodes that depend on or are depended upon
        by the given symbol, ranked by coupling strength (number of paths).

        Args:
            graph_id: The repository graph identifier.
            symbol: The starting symbol name or node ID.

        Returns:
            ImpactAnalysisResult with affected nodes and coupling scores.
        """
        # Resolve starting node
        start_node = await self._resolve_symbol(graph_id, symbol)
        if start_node is None:
            return ImpactAnalysisResult(
                target_symbol=symbol,
                total_affected=0,
                direct_dependencies=[],
                transitive_affected=[],
                coupling_scores={},
            )

        start_id = start_node.get("id", "")

        # Forward BFS: what does this symbol depend on (downstream)
        downstream = await self._bfs_reachable(graph_id, start_id, direction="down")

        # Reverse BFS: what depends on this symbol (upstream)
        upstream = await self._bfs_reachable(graph_id, start_id, direction="up")

        # Direct dependencies: immediate downstream neighbors
        direct = await self._get_neighbors(graph_id, start_id, "down")

        # Transitive: everything reachable excluding self
        all_affected = (downstream.keys() | upstream.keys()) - {start_id}

        # Coupling scores: count paths to each node
        coupling_scores: dict[str, float] = {}
        for node_id in all_affected:
            down_score = downstream.get(node_id, 0)
            up_score = upstream.get(node_id, 0)
            coupling_scores[node_id] = float(down_score + up_score)

        # Sort transitive affected by coupling score
        sorted_affected = sorted(
            all_affected,
            key=lambda nid: coupling_scores.get(nid, 0),
            reverse=True,
        )

        return ImpactAnalysisResult(
            target_symbol=start_id,
            total_affected=len(all_affected),
            direct_dependencies=list(direct),
            transitive_affected=sorted_affected,
            coupling_scores=coupling_scores,
        )

    async def _resolve_symbol(
        self, graph_id: str, symbol: str
    ) -> dict[str, Any] | None:
        """Resolve a symbol string to a node dictionary."""
        # Try exact ID
        rows = await self.graph_store.query(
            graph_id,
            "node_by_id",
            params={"node_id": symbol},
        )
        if rows:
            return cast(dict[str, Any], rows[0].get("n", rows[0]))

        # Try name match
        rows = await self.graph_store.query(
            graph_id,
            "node_by_name",
            params={"name": symbol},
        )
        if rows:
            return cast(dict[str, Any], rows[0].get("n", rows[0]))

        # Try fuzzy
        rows = await self.graph_store.query(
            graph_id,
            "node_name_fuzzy",
            params={"name": symbol},
        )
        if rows:
            return cast(dict[str, Any], rows[0].get("n", rows[0]))

        # Fallback: scan all nodes and match by id suffix or name property
        try:
            all_nodes = await self.graph_store.query(graph_id, "nodes")
            for row in all_nodes:
                node_data = row.get("n", row)
                nid = node_data.get("id", "")
                if nid == symbol or nid.endswith(f".{symbol}") or nid.endswith(f":{symbol}"):
                    return cast(dict[str, Any], node_data)
                props = node_data.get("properties", {})
                name = props.get("name", "")
                qualname = props.get("qualname", "")
                if name == symbol or qualname == symbol:
                    return cast(dict[str, Any], node_data)
        except Exception:
            pass

        return None

    async def _get_node_data(
        self, graph_id: str, node_id: str
    ) -> dict[str, Any] | None:
        """Fetch node data by ID."""
        rows = await self.graph_store.query(
            graph_id,
            "node_by_id",
            params={"node_id": node_id},
        )
        if rows:
            return cast(dict[str, Any], rows[0].get("n", rows[0]))

        return None

    async def _get_neighbors(
        self,
        graph_id: str,
        node_id: str,
        direction: str = "both",
    ) -> list[str]:
        """Get neighbor node IDs based on direction.

        Args:
            graph_id: The repository graph identifier.
            node_id: The center node ID.
            direction: "up" (callers/dependents), "down" (callees),
                       "both" (both directions).

        Returns:
            List of neighbor node IDs.
        """
        neighbors: list[str] = []

        if direction in ("down", "both"):
            # Outgoing edges: this node -> others
            outgoing = await self._get_edges(graph_id, node_id, "outgoing")
            for edge in outgoing:
                target = edge.get("target", "")
                if target:
                    canonical = await self._canonicalize_neighbor(graph_id, target)
                    if canonical:
                        neighbors.append(canonical)

        if direction in ("up", "both"):
            # Incoming edges: others -> this node
            incoming = await self._get_edges(graph_id, node_id, "incoming")
            for edge in incoming:
                source = edge.get("source", "")
                if source:
                    canonical = await self._canonicalize_neighbor(graph_id, source)
                    if canonical:
                        neighbors.append(canonical)

        return neighbors

    async def _canonicalize_neighbor(self, graph_id: str, neighbor_id: str) -> str:
        """Redirect a dangling cross-extractor stub to its concrete node.

        When a file is SCIP-indexed, edges into it target SCIP symbol strings;
        when a file falls back to Tree-sitter, that same symbol exists under a
        legacy module-based id. The SCIP translator emits a stub node
        (``is_external=True``, no ``file_path``) for the symbol-string target,
        so a SCIP -> Tree-sitter edge lands on the stub and the trace stops
        there. If exactly one concrete (non-stub) node shares the stub's
        ``name``, redirect to it so the boundary resolves. Ambiguous or
        non-stub targets are returned unchanged (no behaviour change for the
        SCIP-only or single-language paths).
        """
        node = await self._get_node_data(graph_id, neighbor_id)
        if node is None:
            return neighbor_id
        props = node.get("properties", {}) or {}
        if not props.get("is_external"):
            return neighbor_id
        # External node with a real file_path is a node_modules / stdlib symbol;
        # don't let BFS escape the project.
        if props.get("file_path"):
            return ""
        name = props.get("name")
        if not name:
            return neighbor_id

        # Use the indexed name lookup instead of scanning every node.
        try:
            rows = await self.graph_store.query(
                graph_id, "node_by_name", params={"name": name}
            )
        except Exception:
            return neighbor_id

        concrete: list[str] = []
        for row in rows:
            cand = row.get("n", row)
            if not cand:
                continue
            cprops = cand.get("properties", {}) or {}
            if cprops.get("is_external"):
                continue
            if cprops.get("name") != name:
                continue
            # Concrete nodes carry a file_path; stubs do not.
            if not cprops.get("file_path"):
                continue
            cid = cand.get("id", "")
            if cid and cid != neighbor_id:
                concrete.append(cid)

        if len(concrete) == 1:
            return concrete[0]

        # No unique concrete target: if the neighbor is an external stub
        # without a project file_path, drop it so BFS doesn't escape into
        # stdlib / node_modules.
        if not props.get("file_path"):
            return ""
        return neighbor_id

    async def _bfs_reachable(
        self,
        graph_id: str,
        start_id: str,
        direction: str = "down",
        max_depth: int = 10,
    ) -> dict[str, int]:
        """BFS to count paths/depths to all reachable nodes.

        Args:
            graph_id: The repository graph identifier.
            start_id: Starting node ID.
            direction: "down" for outgoing, "up" for incoming.
            max_depth: Maximum traversal depth.

        Returns:
            Mapping of node_id -> minimum depth from start.
        """
        visited: dict[str, int] = {start_id: 0}
        queue: deque[tuple[str, int]] = deque([(start_id, 0)])

        while queue:
            current_id, depth = queue.popleft()
            if depth >= max_depth:
                continue

            neighbors = await self._get_neighbors(graph_id, current_id, direction)
            for neighbor_id in neighbors:
                if neighbor_id not in visited:
                    visited[neighbor_id] = depth + 1
                    queue.append((neighbor_id, depth + 1))

        return visited
