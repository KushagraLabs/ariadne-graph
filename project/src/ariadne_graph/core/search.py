"""Hybrid search combining semantic vectors, keyword search, and graph traversal."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ariadne_graph.core.embeddings import EmbeddingProvider
from ariadne_graph.core.models import SearchHit
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.base import SearchableGraphStore

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant
_RRF_K = 60


class HybridSearcher:
    """Hybrid search combining semantic, keyword, and graph-based retrieval.

    Supports four search modes:
    - "semantic": Vector similarity search only.
    - "keyword": FTS/BM25 keyword search only.
    - "hybrid": Combined semantic + keyword with Reciprocal Rank Fusion.
    - "symbol": Exact symbol name match via graph node lookup.
    """

    def __init__(
        self,
        graph_store: SearchableGraphStore,
        embedding_provider: EmbeddingProvider | None = None,
        snippet_extractor: SnippetExtractor | None = None,
    ) -> None:
        """Initialize the hybrid searcher.

        Args:
            graph_store: The searchable graph store backend.
            embedding_provider: Optional embedding provider for semantic search.
            snippet_extractor: Optional snippet extractor for result formatting.
        """
        self.graph_store = graph_store
        self.embedding_provider = embedding_provider
        self.snippet_extractor = snippet_extractor

    async def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        search_type: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Execute a search query using the specified strategy.

        Args:
            graph_id: The repository graph identifier.
            query: The search query text or symbol name.
            limit: Maximum number of results to return.
            search_type: One of "semantic", "keyword", "hybrid", "symbol".

        Returns:
            List of ranked search results as dictionaries.
        """
        if not query.strip():
            return []

        if search_type == "semantic":
            return await self._search_semantic(graph_id, query, limit)
        elif search_type == "keyword":
            return await self._search_keyword(graph_id, query, limit)
        elif search_type == "hybrid":
            return await self._search_hybrid(graph_id, query, limit)
        elif search_type == "symbol":
            return await self._search_symbol(graph_id, query, limit)
        else:
            logger.warning("Unknown search_type '%s', falling back to hybrid", search_type)
            return await self._search_hybrid(graph_id, query, limit)

    async def _search_semantic(
        self, graph_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Vector similarity search."""
        if self.embedding_provider is None:
            logger.warning("No embedding provider configured for semantic search")
            return []

        query_vectors = await self.embedding_provider.embed([query])
        if not query_vectors:
            return []

        hits = await self.graph_store.search_vector(
            graph_id, query_vectors[0], limit=limit
        )
        return self._hits_to_dicts(hits)

    async def _search_keyword(
        self, graph_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Keyword/BM25 search."""
        hits = await self.graph_store.search_keyword(graph_id, query, limit=limit)
        return self._hits_to_dicts(hits)

    async def _search_hybrid(
        self, graph_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Hybrid search using Reciprocal Rank Fusion of semantic + keyword.

        RRF formula: score = sum(1.0 / (k + rank)) for each list containing the item.
        k = 60 (tuning constant).
        """
        # Gather results from both search modalities concurrently
        semantic_task = self._search_semantic(graph_id, query, limit)
        keyword_task = self._search_keyword(graph_id, query, limit)

        raw_semantic: list[dict[str, Any]] | Exception
        raw_keyword: list[dict[str, Any]] | Exception
        raw_semantic, raw_keyword = await asyncio.gather(
            semantic_task, keyword_task, return_exceptions=True
        )

        semantic_results: list[dict[str, Any]]
        if isinstance(raw_semantic, Exception):
            logger.error("Semantic search failed: %s", raw_semantic)
            semantic_results = []
        else:
            semantic_results = raw_semantic

        keyword_results: list[dict[str, Any]]
        if isinstance(raw_keyword, Exception):
            logger.error("Keyword search failed: %s", raw_keyword)
            keyword_results = []
        else:
            keyword_results = raw_keyword

        # RRF scoring
        rrf_scores: dict[str, float] = {}
        rrf_nodes: dict[str, dict[str, Any]] = {}

        # Process semantic results
        for rank, result in enumerate(semantic_results, start=1):
            node_id = result["node_id"]
            rrf_scores[node_id] = rrf_scores.get(node_id, 0.0) + 1.0 / (_RRF_K + rank)
            if node_id not in rrf_nodes:
                rrf_nodes[node_id] = result

        # Process keyword results
        for rank, result in enumerate(keyword_results, start=1):
            node_id = result["node_id"]
            rrf_scores[node_id] = rrf_scores.get(node_id, 0.0) + 1.0 / (_RRF_K + rank)
            if node_id not in rrf_nodes:
                rrf_nodes[node_id] = result

        # Sort by RRF score descending
        sorted_node_ids = sorted(rrf_scores.keys(), key=lambda nid: rrf_scores[nid], reverse=True)

        results: list[dict[str, Any]] = []
        for node_id in sorted_node_ids[:limit]:
            entry = dict(rrf_nodes[node_id])
            entry["score"] = rrf_scores[node_id]
            entry["rrf_score"] = rrf_scores[node_id]
            results.append(entry)

        return results

    async def _search_symbol(
        self, graph_id: str, query: str, limit: int
    ) -> list[dict[str, Any]]:
        """Exact symbol match — look up node by deterministic ID or name."""
        rows: list[dict[str, Any]] = []

        # Try exact node ID match first
        with contextlib.suppress(Exception):
            rows = await self.graph_store.query(
                graph_id,
                "node_by_id",
                params={"node_id": query},
            )

        # If no exact match, try name-based lookup
        if not rows:
            with contextlib.suppress(Exception):
                rows = await self.graph_store.query(
                    graph_id,
                    "node_by_name",
                    params={"name": query},
                )

        # If still no match, try partial/fuzzy name match via query
        if not rows:
            with contextlib.suppress(Exception):
                rows = await self.graph_store.query(
                    graph_id,
                    "node_name_fuzzy",
                    params={"name": query},
                )

        # Fallback: scan all nodes to find matches by id suffix or name
        if not rows:
            with contextlib.suppress(Exception):
                all_nodes = await self.graph_store.query(graph_id, "nodes")
                query_lower = query.lower()
                matched: list[dict[str, Any]] = []
                for row in all_nodes:
                    node_data = row.get("n", row)
                    nid = node_data.get("id", "")
                    props = node_data.get("properties", {})
                    name = props.get("name", "")
                    qualname = props.get("qualname", "")
                    if (
                        nid == query
                        or nid.endswith(f".{query}")
                        or name == query
                        or qualname == query
                        or query_lower in nid.lower()
                        or query_lower in name.lower()
                    ):
                        matched.append(row)
                rows = matched

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if len(results) >= limit:
                break
            node_data = row.get("n", row)
            node_id = node_data.get("id", "")
            if node_id in seen:
                continue
            seen.add(node_id)
            results.append({
                "node_id": node_id,
                "score": 1.0,
                "node": node_data,
                "match_type": "symbol",
            })

        return results

    @staticmethod
    def _hits_to_dicts(hits: list[SearchHit]) -> list[dict[str, Any]]:
        """Convert SearchHit objects to plain dictionaries."""
        return [
            {
                "node_id": hit.node_id,
                "score": hit.score,
                "node": hit.node.model_dump() if hit.node is not None else None,
            }
            for hit in hits
        ]

    async def expand_results(
        self,
        graph_id: str,
        node_ids: list[str],
        depth: int = 1,
    ) -> dict[str, list[dict[str, Any]]]:
        """Expand search results by fetching graph neighbors.

        For each node, retrieves connected nodes (callers, callees, imports)
        up to the specified depth.

        Args:
            graph_id: The repository graph identifier.
            node_ids: Starting node IDs to expand from.
            depth: Number of hops to traverse (default 1 = direct neighbors).

        Returns:
            Mapping of node_id -> list of neighbor dictionaries.
        """
        if depth <= 0 or not node_ids:
            return {}

        all_neighbors: dict[str, list[dict[str, Any]]] = {}
        visited: set[str] = set()
        current_level = list(node_ids)

        for _ in range(depth):
            next_level: list[str] = []
            for node_id in current_level:
                if node_id in visited:
                    continue
                visited.add(node_id)

                rows = await self.graph_store.query(
                    graph_id,
                    "node_neighbors",
                    params={"node_id": node_id},
                )

                neighbors: list[dict[str, Any]] = []
                for row in rows:
                    neighbor_data = row.get("n", row)
                    neighbor_id = neighbor_data.get("id", "")
                    if neighbor_id and neighbor_id not in visited:
                        neighbors.append(neighbor_data)
                        next_level.append(neighbor_id)

                if node_id in all_neighbors:
                    existing_ids = {n.get("id") for n in all_neighbors[node_id]}
                    for n in neighbors:
                        if n.get("id") not in existing_ids:
                            all_neighbors[node_id].append(n)
                else:
                    all_neighbors[node_id] = neighbors

            current_level = next_level
            if not current_level:
                break

        return all_neighbors

    async def format_context(
        self,
        graph_id: str,
        node_ids: list[str],
    ) -> str:
        """Format nodes + neighbors + snippets into a prompt-friendly context string.

        Produces a structured text suitable for inclusion in LLM prompts,
        with code snippets and relationship context.

        Args:
            graph_id: The repository graph identifier.
            node_ids: Node IDs to include in the context.

        Returns:
            Formatted context string with snippets and neighbor info.
        """
        parts: list[str] = []

        # Build node lookup if direct queries fail
        node_lookup: dict[str, dict[str, Any]] | None = None

        for node_id in node_ids:
            # Fetch node details
            rows: list[dict[str, Any]] = []
            with contextlib.suppress(Exception):
                rows = await self.graph_store.query(
                    graph_id,
                    "node_by_id",
                    params={"node_id": node_id},
                )

            # Fallback: build lookup from all nodes
            if not rows:
                if node_lookup is None:
                    node_lookup = {}
                    with contextlib.suppress(Exception):
                        all_nodes = await self.graph_store.query(graph_id, "nodes")
                        for row in all_nodes:
                            nd = row.get("n", row)
                            nid = nd.get("id", "")
                            if nid:
                                node_lookup[nid] = nd
                if node_lookup and node_id in node_lookup:
                    rows = [{"n": node_lookup[node_id]}]

            if not rows:
                continue

            node_data = rows[0].get("n", rows[0])
            name = node_data.get("properties", {}).get("name", node_id)
            node_type = node_data.get("labels", ["entity"])[0]
            file_path = node_data.get("properties", {}).get("file_path", "")
            line_start = node_data.get("properties", {}).get("line_start", 0)
            line_end = node_data.get("properties", {}).get("line_end", 0)

            parts.append(f"### {name} ({node_type})")
            parts.append(f"**File:** {file_path}")

            # Add snippet if extractor is available
            if self.snippet_extractor and file_path and line_start:
                snippet = self.snippet_extractor.get_snippet(
                    file_path=file_path,
                    line_start=line_start,
                    line_end=line_end or line_start,
                    context_lines=3,
                )
                if snippet:
                    parts.append("```")
                    parts.append(snippet)
                    parts.append("```")

            # Add neighbor context
            neighbors = await self.expand_results(graph_id, [node_id], depth=1)
            node_neighbors = neighbors.get(node_id, [])
            if node_neighbors:
                parts.append("**Related:**")
                for neighbor in node_neighbors[:5]:  # Limit to top 5
                    neighbor_name = neighbor.get("properties", {}).get("name", "")
                    neighbor_id = neighbor.get("id", "")
                    display_name = neighbor_name or neighbor_id
                    parts.append(f"  - {display_name}")

            parts.append("")  # Blank line between entries

        return "\n".join(parts)


