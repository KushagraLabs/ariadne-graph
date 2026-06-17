"""Community detection and architecture analysis using Louvain algorithm.

Provides community detection via NetworkX + python-louvain, architecture
summarization, and hotspot identification for code graphs.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import networkx as nx  # type: ignore[import-untyped]

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
    """

    def __init__(self, graph_store: SearchableGraphStore) -> None:
        self.graph_store = graph_store

    async def detect_communities(self, graph_id: str) -> dict[str, int]:
        """Detect communities using the Louvain algorithm.

        1. Query all nodes and edges for the graph.
        2. Build a NetworkX DiGraph from edges.
        3. Run Louvain on the undirected version.
        4. Store assignments via graph_store.set_communities.

        Args:
            graph_id: The repository graph identifier.

        Returns:
            Mapping of node_id -> community_id.
        """
        # Fetch nodes and edges
        node_rows = await self.graph_store.query(graph_id, "nodes")
        edge_rows = await self.graph_store.query(graph_id, "edges")

        # Build NetworkX directed graph
        digraph = nx.DiGraph()

        node_ids: set[str] = set()
        for row in node_rows:
            node_data = row.get("n", row)
            node_id = node_data.get("id", "")
            if node_id:
                node_ids.add(node_id)
                digraph.add_node(
                    node_id,
                    labels=node_data.get("labels", []),
                    properties=node_data.get("properties", {}),
                )

        for row in edge_rows:
            edge_data = row.get("r", row)
            source = edge_data.get("source", "")
            target = edge_data.get("target", "")
            rel_type = edge_data.get("rel_type", "")
            if source and target and source in node_ids and target in node_ids:
                digraph.add_edge(source, target, rel_type=rel_type)

        if digraph.number_of_nodes() == 0:
            logger.info("No nodes in graph %s, skipping community detection", graph_id)
            return {}

        if digraph.number_of_edges() == 0:
            logger.info("No edges in graph %s, assigning all to community 0", graph_id)
            assignments = dict.fromkeys(digraph.nodes(), 0)
            await self.graph_store.set_communities(graph_id, assignments)
            return assignments

        # Run Louvain on the undirected version
        undirected = digraph.to_undirected()
        try:
            import community as community_louvain  # type: ignore[import-untyped]

            assignments = cast(dict[str, int], community_louvain.best_partition(undirected))
        except ImportError as exc:
            raise RuntimeError(
                "python-louvain is required for community detection. "
                "Install with: pip install python-louvain"
            ) from exc

        # Store assignments
        await self.graph_store.set_communities(graph_id, assignments)

        logger.info(
            "Detected %d communities in graph %s (%d nodes, %d edges)",
            len(set(assignments.values())),
            graph_id,
            digraph.number_of_nodes(),
            digraph.number_of_edges(),
        )

        return assignments

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
        # Get or detect communities
        community_groups = await self.graph_store.get_communities(graph_id)
        if not community_groups:
            assignments = await self.detect_communities(graph_id)
            rebuilt_groups: dict[int, list[str]] = {}
            for node_id, cid in assignments.items():
                rebuilt_groups.setdefault(cid, []).append(node_id)
            community_groups = rebuilt_groups

        # Fetch nodes and edges
        node_rows = await self.graph_store.query(graph_id, "nodes")
        edge_rows = await self.graph_store.query(graph_id, "edges")

        # Build lookup tables
        node_lookup: dict[str, dict[str, Any]] = {}
        for row in node_rows:
            node_data = row.get("n", row)
            node_id = node_data.get("id", "")
            if node_id:
                node_lookup[node_id] = node_data

        # Build directed graph for metrics
        digraph = nx.DiGraph()
        for row in edge_rows:
            edge_data = row.get("r", row)
            source = edge_data.get("source", "")
            target = edge_data.get("target", "")
            if source and target:
                digraph.add_edge(source, target)

        # Get all node IDs that are in communities
        all_community_nodes: set[str] = set()
        for members in community_groups.values():
            all_community_nodes.update(members)

        # Calculate degree metrics
        in_degrees = dict(digraph.in_degree())
        out_degrees = dict(digraph.out_degree())
        total_degrees = {
            node: in_degrees.get(node, 0) + out_degrees.get(node, 0)
            for node in all_community_nodes
        }

        # Build community info
        communities: list[CommunityInfo] = []
        for community_id, members in sorted(community_groups.items()):
            member_set = set(members)

            # Count internal and external edges
            internal_edges = 0
            external_edges: dict[int, int] = {}
            for edge in digraph.edges():
                src, tgt = edge
                src_comm = None
                tgt_comm = None
                for cid, mems in community_groups.items():
                    if src in mems:
                        src_comm = cid
                    if tgt in mems:
                        tgt_comm = cid

                if src_comm == community_id and tgt_comm == community_id:
                    internal_edges += 1
                elif src_comm == community_id and tgt_comm is not None:
                    external_edges[tgt_comm] = external_edges.get(tgt_comm, 0) + 1

            # Internal edge density: internal / possible_internal
            n = len(member_set)
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
            node_data = node_lookup.get(node_id, {})
            props = node_data.get("properties", {})
            name = props.get("name", node_id.split(".")[-1] if "." in node_id else node_id)
            file_path = props.get("file_path", "")

            # Find community
            comm_id: int | None = None
            for cid, mems in community_groups.items():
                if node_id in mems:
                    comm_id = cid
                    break

            hotspots.append(
                HotspotInfo(
                    node_id=node_id,
                    node_name=name,
                    file_path=file_path,
                    metric_type="complexity",
                    score=float(degree),
                    community_id=comm_id,
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
    ) -> list[HotspotInfo]:
        """Find code hotspots using graph centrality metrics.

        Args:
            graph_id: The repository graph identifier.
            top_n: Number of top hotspots to return.
            metric: Metric to use — "complexity" (degree centrality),
                    "coupling" (betweenness centrality),
                    "fan_in" (in-degree), "fan_out" (out-degree).

        Returns:
            List of hotspot info ranked by the chosen metric.
        """
        # Fetch edges
        edge_rows = await self.graph_store.query(graph_id, "edges")
        node_rows = await self.graph_store.query(graph_id, "nodes")

        # Build directed graph
        digraph = nx.DiGraph()
        node_lookup: dict[str, dict[str, Any]] = {}

        for row in node_rows:
            node_data = row.get("n", row)
            node_id = node_data.get("id", "")
            if node_id:
                node_lookup[node_id] = node_data
                digraph.add_node(node_id)

        for row in edge_rows:
            edge_data = row.get("r", row)
            source = edge_data.get("source", "")
            target = edge_data.get("target", "")
            if source and target:
                digraph.add_edge(source, target)

        if digraph.number_of_nodes() == 0:
            return []

        # Get community assignments for context
        community_groups = await self.graph_store.get_communities(graph_id)
        node_community: dict[str, int] = {}
        if community_groups:
            for cid, members in community_groups.items():
                for node_id in members:
                    node_community[node_id] = cid

        # Calculate scores based on metric
        scores: dict[str, float] = {}

        if metric == "complexity":
            # Degree centrality (undirected)
            undirected = digraph.to_undirected()
            scores = nx.degree_centrality(undirected)
        elif metric == "coupling":
            # Betweenness centrality
            try:
                undirected = digraph.to_undirected()
                scores = nx.betweenness_centrality(undirected)
            except Exception:
                # Fallback if betweenness fails
                scores = {n: float(d) for n, d in digraph.degree()}
        elif metric == "fan_in":
            scores = {n: float(d) for n, d in digraph.in_degree()}
        elif metric == "fan_out":
            scores = {n: float(d) for n, d in digraph.out_degree()}
        else:
            logger.warning("Unknown metric '%s', defaulting to complexity", metric)
            undirected = digraph.to_undirected()
            scores = nx.degree_centrality(undirected)

        # Sort and build results
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        hotspots: list[HotspotInfo] = []
        for node_id, score in sorted_scores[:top_n]:
            node_data = node_lookup.get(node_id, {})
            props = node_data.get("properties", {})
            name = props.get("name", node_id.split(".")[-1] if "." in node_id else node_id)
            file_path = props.get("file_path", "")

            hotspots.append(
                HotspotInfo(
                    node_id=node_id,
                    node_name=name,
                    file_path=file_path,
                    metric_type=metric,
                    score=round(score, 6),
                    community_id=node_community.get(node_id),
                )
            )

        return hotspots
