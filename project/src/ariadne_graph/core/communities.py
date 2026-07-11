"""Community detection and architecture analysis using Louvain algorithm.

Provides community detection via NetworkX + python-louvain, architecture
summarization, and hotspot identification for code graphs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import networkx as nx  # type: ignore[import-untyped]

from ariadne_graph.core.architecture import PERIPHERAL_ORGANS, _dir_of, _organ
from ariadne_graph.core.models import (
    ArchitectureSummary,
    CommunityInfo,
    HotspotInfo,
)
from ariadne_graph.graphstores.base import SearchableGraphStore

logger = logging.getLogger(__name__)


class CommunityAnalyzer:
    """Detects communities and analyzes architecture of code graphs.

    Uses the Louvain algorithm for community detection on the undirected
    version of the code dependency graph. Provides architecture summaries
    and hotspot identification.

    To avoid reloading large graphs on every analysis call, the analyzer
    keeps an in-memory cache of the NetworkX graph and community assignments
    per graph_id. Synchronous NetworkX/graph algorithms are executed in a
    background thread so they do not block the asyncio event loop.
    """

    def __init__(self, graph_store: SearchableGraphStore) -> None:
        self.graph_store = graph_store
        self._cache: dict[
            str,
            tuple[
                nx.DiGraph,
                dict[str, dict[str, Any]],
                dict[str, int] | None,
            ],
        ] = {}

    def _clear_cache(self, graph_id: str) -> None:
        """Remove cached data for a graph."""
        self._cache.pop(graph_id, None)

    async def _load_graph(
        self, graph_id: str
    ) -> tuple[nx.DiGraph, dict[str, dict[str, Any]]]:
        """Load or retrieve the cached NetworkX graph and node lookup.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            Tuple of (directed graph, node_id -> node_data lookup).
        """
        cached = self._cache.get(graph_id)
        if cached is not None:
            return cached[0], cached[1]

        node_rows = await self.graph_store.query(graph_id, "nodes")
        edge_rows = await self.graph_store.query(graph_id, "edges")

        digraph = nx.DiGraph()
        node_lookup: dict[str, dict[str, Any]] = {}

        for row in node_rows:
            node_data = cast(dict[str, Any], row.get("n", row))
            node_id = node_data.get("id", "")
            if not node_id:
                continue
            node_lookup[node_id] = node_data
            digraph.add_node(
                node_id,
                labels=node_data.get("labels", []),
                properties=node_data.get("properties", {}),
            )

        for row in edge_rows:
            edge_data = cast(dict[str, Any], row.get("r", row))
            source = edge_data.get("source", "")
            target = edge_data.get("target", "")
            rel_type = edge_data.get("rel_type", "")
            if source and target and source in node_lookup and target in node_lookup:
                digraph.add_edge(source, target, rel_type=rel_type)

        self._cache[graph_id] = (digraph, node_lookup, None)
        return digraph, node_lookup

    async def detect_communities(self, graph_id: str) -> dict[str, int]:
        """Detect communities using the Louvain algorithm.

        1. Load the graph (or use cached copy).
        2. Run Louvain on the undirected version in a background thread.
        3. Store assignments via graph_store.set_communities.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            Mapping of node_id -> community_id.
        """
        digraph, _node_lookup = await self._load_graph(graph_id)

        if digraph.number_of_nodes() == 0:
            logger.info("No nodes in graph %s, skipping community detection", graph_id)
            return {}

        if digraph.number_of_edges() == 0:
            logger.info("No edges in graph %s, assigning all to community 0", graph_id)
            assignments = dict.fromkeys(digraph.nodes(), 0)
            await self.graph_store.set_communities(graph_id, assignments)
            self._cache[graph_id] = (digraph, _node_lookup, assignments)
            return assignments

        undirected = digraph.to_undirected()
        try:
            import community as community_louvain  # type: ignore[import-untyped]

            assignments = cast(
                dict[str, int],
                await asyncio.to_thread(community_louvain.best_partition, undirected),
            )
        except ImportError as exc:
            raise RuntimeError(
                "python-louvain is required for community detection. "
                "Install with: pip install python-louvain"
            ) from exc

        await self.graph_store.set_communities(graph_id, assignments)
        self._cache[graph_id] = (digraph, _node_lookup, assignments)

        logger.info(
            "Detected %d communities in graph %s (%d nodes, %d edges)",
            len(set(assignments.values())),
            graph_id,
            digraph.number_of_nodes(),
            digraph.number_of_edges(),
        )

        return assignments

    async def get_community_assignments(self, graph_id: str) -> dict[str, int]:
        """Return node_id -> community_id, detecting communities if needed.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            Mapping of node_id -> community_id.
        """
        cached = self._cache.get(graph_id)
        if cached is not None and cached[2] is not None:
            return cached[2]

        community_groups = await self.graph_store.get_communities(graph_id)
        if community_groups:
            assignments = {
                node_id: cid
                for cid, members in community_groups.items()
                for node_id in members
            }
            if cached is not None:
                self._cache[graph_id] = (cached[0], cached[1], assignments)
            return assignments

        return await self.detect_communities(graph_id)

    @staticmethod
    def _is_test_node(node_data: dict[str, Any]) -> bool:
        """Whether a node's file lives under a peripheral organ (tests/scripts)."""
        file_path = node_data.get("properties", {}).get("file_path", "")
        if not file_path:
            return False
        return _organ(_dir_of(file_path)) in PERIPHERAL_ORGANS

    @staticmethod
    def _enrich_hotspot(
        node_id: str,
        digraph: nx.DiGraph,
        node_lookup: dict[str, dict[str, Any]],
        *,
        metric_type: str,
        score: float,
        community_id: int | None,
    ) -> HotspotInfo:
        """Build a ``HotspotInfo`` with internal/external coupling detail.

        "Internal" edges are those whose neighbor shares this node's
        ``file_path`` (e.g. same-file helper calls); "external" edges cross
        into a different file. Shared by :meth:`find_hotspots` and
        :meth:`get_architecture_summary` so both surface identical semantics.
        """
        node_data = node_lookup.get(node_id, {})
        props = node_data.get("properties", {})
        name = props.get("name", node_id.split(".")[-1] if "." in node_id else node_id)
        file_path = props.get("file_path", "")

        neighbors: list[tuple[str, str]] = []  # (neighbor_id, direction)
        for _src, tgt in digraph.out_edges(node_id):
            neighbors.append((tgt, "out"))
        for src, _tgt in digraph.in_edges(node_id):
            neighbors.append((src, "in"))

        internal_refs = 0
        external_refs = 0
        imported_files: list[str] = []  # files this node's out-edges reach
        importing_files: list[str] = []  # files whose nodes reach into this one
        external_modules: set[str] = set()
        prod_refs = 0
        test_refs = 0

        for neighbor_id, direction in neighbors:
            neighbor_data = node_lookup.get(neighbor_id, {})
            neighbor_fp = neighbor_data.get("properties", {}).get("file_path", "")

            if neighbor_fp and neighbor_fp == file_path:
                internal_refs += 1
            else:
                external_refs += 1
                if neighbor_fp:
                    external_modules.add(neighbor_fp)
                    if direction == "out" and neighbor_fp not in imported_files:
                        imported_files.append(neighbor_fp)
                    if direction == "in" and neighbor_fp not in importing_files:
                        importing_files.append(neighbor_fp)

            if CommunityAnalyzer._is_test_node(neighbor_data):
                test_refs += 1
            else:
                prod_refs += 1

        file_fan_in = digraph.in_degree(node_id) if digraph.has_node(node_id) else 0
        file_fan_out = digraph.out_degree(node_id) if digraph.has_node(node_id) else 0

        return HotspotInfo(
            node_id=node_id,
            node_name=name,
            file_path=file_path,
            metric_type=metric_type,
            score=score,
            community_id=community_id,
            internal_refs=internal_refs,
            external_refs=external_refs,
            imported_files=imported_files,
            importing_files=importing_files,
            external_modules=sorted(external_modules),
            symbol_ref_count=internal_refs + external_refs,
            file_fan_in=int(file_fan_in),
            file_fan_out=int(file_fan_out),
            prod_refs=prod_refs,
            test_refs=test_refs,
            score_formula=(
                f"{metric_type} metric over graph degree/centrality; "
                "internal_refs/external_refs split by shared file_path"
            ),
        )

    async def get_architecture_summary(
        self, graph_id: str
    ) -> ArchitectureSummary:
        """Generate an architecture summary from community detection.

        1. Get community assignments.
        2. For each community: count members, find representative files,
           compute internal density.
        3. Find coupling between communities (cross-community edges).
        4. Identify hotspots: nodes with highest in-degree + out-degree.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            ArchitectureSummary with community info and hotspots.
        """
        assignments = await self.get_community_assignments(graph_id)
        digraph, node_lookup = await self._load_graph(graph_id)

        community_groups: dict[int, list[str]] = {}
        for node_id, cid in assignments.items():
            community_groups.setdefault(cid, []).append(node_id)

        all_community_nodes: set[str] = set(assignments.keys())

        # Calculate degree metrics
        in_degrees = dict(digraph.in_degree())
        out_degrees = dict(digraph.out_degree())
        total_degrees = {
            node: in_degrees.get(node, 0) + out_degrees.get(node, 0)
            for node in all_community_nodes
        }

        # Single-pass edge counting across all communities.
        community_internal_edges: dict[int, int] = {}
        community_external_edges: dict[int, dict[int, int]] = {}
        for src, tgt in digraph.edges():
            src_comm = assignments.get(src)
            tgt_comm = assignments.get(tgt)
            if src_comm is None or tgt_comm is None:
                continue
            if src_comm == tgt_comm:
                community_internal_edges[src_comm] = (
                    community_internal_edges.get(src_comm, 0) + 1
                )
            else:
                external = community_external_edges.setdefault(src_comm, {})
                external[tgt_comm] = external.get(tgt_comm, 0) + 1

        # Build community info
        communities: list[CommunityInfo] = []
        for community_id, members in sorted(community_groups.items()):
            member_set = set(members)
            n = len(member_set)

            internal_edges = community_internal_edges.get(community_id, 0)
            external_edges = community_external_edges.get(community_id, {})

            max_internal = n * (n - 1) if n > 1 else 1
            density = internal_edges / max_internal if max_internal > 0 else 0.0

            # Representative files: most connected nodes in community
            member_degrees = {
                node: total_degrees.get(node, 0)
                for node in member_set
            }
            top_nodes = sorted(
                member_degrees.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:5]
            rep_files = []
            for node_id, _ in top_nodes:
                props = node_lookup.get(node_id, {}).get("properties", {})
                fp = props.get("file_path", "")
                if fp and fp not in rep_files:
                    rep_files.append(fp)

            communities.append(
                CommunityInfo(
                    community_id=community_id,
                    member_count=n,
                    representative_files=rep_files[:5],
                    internal_edge_density=round(density, 4),
                    external_coupling=external_edges,
                )
            )

        # Identify hotspots: highest total degree nodes
        sorted_by_degree = sorted(
            total_degrees.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:20]

        hotspots: list[HotspotInfo] = []
        for node_id, degree in sorted_by_degree:
            hotspots.append(
                self._enrich_hotspot(
                    node_id,
                    digraph,
                    node_lookup,
                    metric_type="complexity",
                    score=float(degree),
                    community_id=assignments.get(node_id),
                )
            )

        # Count unique files
        all_files: set[str] = set()
        for node_data in node_lookup.values():
            fp = node_data.get("properties", {}).get("file_path", "")
            if fp:
                all_files.add(fp)

        return ArchitectureSummary(
            total_communities=len(community_groups),
            total_files=len(all_files),
            total_entities=len(all_community_nodes),
            communities=communities,
            hotspots=hotspots,
        )

    async def find_hotspots(
        self,
        graph_id: str,
        top_n: int = 10,
        metric: str = "complexity",
        include_tests: bool = False,
    ) -> list[HotspotInfo]:
        """Find code hotspots using graph centrality metrics.

        Args:
            graph_id: The repository graph identifier.
            top_n: Number of top hotspots to return.
            metric: Metric to use — "complexity" (degree centrality),
                    "coupling" (approximated betweenness centrality),
                    "fan_in" (in-degree), "fan_out" (out-degree).
            include_tests: When False (default), hotspots whose file lives
                    under a peripheral organ (tests/scripts, per
                    ``architecture.PERIPHERAL_ORGANS``) are excluded.

        Returns:
            List of hotspot info ranked by the chosen metric.
        """
        digraph, node_lookup = await self._load_graph(graph_id)

        if digraph.number_of_nodes() == 0:
            return []

        assignments = await self.get_community_assignments(graph_id)

        # Calculate scores based on metric. CPU-heavy NetworkX calls run in a
        # background thread so they do not block the event loop.
        scores: dict[str, float] = {}

        if metric == "complexity":
            undirected = digraph.to_undirected()
            scores = await asyncio.to_thread(nx.degree_centrality, undirected)
        elif metric == "coupling":
            undirected = digraph.to_undirected()
            n = undirected.number_of_nodes()
            # Use approximate betweenness with a bounded sample size to keep
            # runtime reasonable on large code graphs.
            sample_size = min(100, n)
            try:
                scores = await asyncio.to_thread(
                    nx.betweenness_centrality,
                    undirected,
                    k=sample_size,
                )
            except Exception:
                # Fallback if betweenness fails
                scores = {n_id: float(d) for n_id, d in digraph.degree()}
        elif metric == "fan_in":
            scores = {n_id: float(d) for n_id, d in digraph.in_degree()}
        elif metric == "fan_out":
            scores = {n_id: float(d) for n_id, d in digraph.out_degree()}
        else:
            logger.warning("Unknown metric '%s', defaulting to complexity", metric)
            undirected = digraph.to_undirected()
            scores = await asyncio.to_thread(nx.degree_centrality, undirected)

        # Sort, optionally exclude peripheral organs, then take top_n.
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        if not include_tests:
            sorted_scores = [
                (node_id, score)
                for node_id, score in sorted_scores
                if not self._is_test_node(node_lookup.get(node_id, {}))
            ]

        hotspots: list[HotspotInfo] = []
        for node_id, score in sorted_scores[:top_n]:
            hotspots.append(
                self._enrich_hotspot(
                    node_id,
                    digraph,
                    node_lookup,
                    metric_type=metric,
                    score=round(score, 6),
                    community_id=assignments.get(node_id),
                )
            )

        return hotspots
