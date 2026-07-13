"""Embedding provider abstraction and service layer."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.models import CodeNode, EmbeddingPayload
from ariadne_graph.graphstores.base import SearchableGraphStore

logger = logging.getLogger(__name__)

# Bumped whenever _build_node_text changes what it feeds the embedder. Stored
# per-embedding in the `model` column (as "{model_name}#v{N}") so a later index
# can detect stale vectors and flag re-indexing — see FreshnessTracker / the
# index_status embedding_version report. v1 = name+type+module+first-doc-line;
# v2 (bead 42i) = adds signature + parameter names + full docstring.
EMBEDDING_TEXT_VERSION = 2


def embedding_version_tag(model_name: str) -> str:
    """Per-vector provenance tag: ``"{model_name}#v{EMBEDDING_TEXT_VERSION}"``.

    Stored in the ``embeddings.model`` column so a later index can tell whether
    a stored vector was produced by the current model + text schema; a mismatch
    means the vectors are stale and the repo should be re-indexed.
    """
    return f"{model_name}#v{EMBEDDING_TEXT_VERSION}"


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers.

    Implementations must support batch encoding of text into
    dense vector representations.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (one per input text).
        """
        ...

    @property
    def dimensions(self) -> int:
        """Dimensionality of the embedding vectors."""
        ...

    @property
    def model_name(self) -> str:
        """Name of the embedding model being used."""
        ...


class LocalEmbeddingProvider:
    """Local embedding provider using sentence-transformers.

    Lazy-loads the model on first use to avoid heavy import
    overhead when embeddings are not needed.
    """

    def __init__(
        self,
        model_name: str | None = None,
        batch_size: int = 32,
        config: AnalyzerConfig | None = None,
    ) -> None:
        """Initialize the local embedding provider.

        Args:
            model_name: sentence-transformers model name. Falls back to
                config.embedding_model or the default "all-MiniLM-L6-v2".
            batch_size: Number of texts to encode in each batch.
            config: AnalyzerConfig for default values.
        """
        if config is not None:
            self._model_name = model_name or config.embedding_model
            self._batch_size = config.embedding_batch_size
            self._dimensions = config.embedding_dimensions
        else:
            self._model_name = model_name or "all-MiniLM-L6-v2"
            self._batch_size = batch_size
            self._dimensions = 384  # Default for all-MiniLM-L6-v2

        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _load_model(self) -> Any:
        """Lazy-load the SentenceTransformer model (thread-safe)."""
        if self._model is not None:
            return self._model

        async with self._lock:
            if self._model is not None:
                return self._model

            # Lazy import to avoid heavy dependency when not needed
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is required for local embeddings. "
                    "Install with: pip install -e \".[semantic]\""
                ) from exc

            logger.info("Loading embedding model: %s", self._model_name)
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(self._model_name)
            )
            # Update dimensions from actual model if different. The method was
            # renamed in sentence-transformers 5.x; fall back for older versions.
            get_dim = getattr(
                self._model,
                "get_embedding_dimension",
                self._model.get_sentence_embedding_dimension,
            )
            self._dimensions = get_dim()
            logger.info(
                "Embedding model loaded: %s (dim=%d)",
                self._model_name,
                self._dimensions,
            )
            return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts in batches using the local model.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors.
        """
        if not texts:
            return []

        model = await self._load_model()
        all_embeddings: list[np.ndarray] = []

        loop = asyncio.get_event_loop()

        def _encode(b: list[str]) -> Any:
            return model.encode(
                b,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            embeddings = await loop.run_in_executor(None, _encode, batch)
            all_embeddings.extend(embeddings)

        return [emb.tolist() for emb in all_embeddings]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return self._model_name


def _build_node_text(node: CodeNode) -> str:
    """Build a text representation of a node for embedding.

    Format (EMBEDDING_TEXT_VERSION v2): "{name} {type} in {module}
    {signature} ({param names}): {FULL docstring or first source line}".

    v2 (bead 42i) enriches the thin v1 text (name+type+module+first-doc-line)
    with the signature, parameter names, and the FULL docstring so behavioural
    duplicates — two helpers that do the same thing under different names — land
    near each other in vector space. Bumping EMBEDDING_TEXT_VERSION forces a
    re-embed; stale vectors are surfaced via index_status, not silently reused.

    Args:
        node: The code node to represent as text.

    Returns:
        A descriptive text string suitable for embedding.
    """
    props = node.properties
    name = props.get("name", node.id.split(".")[-1] if "." in node.id else node.id)
    node_type = _infer_node_type(node)
    module = props.get("module", "")

    parts = [name, node_type]
    if module:
        parts.append(f"in {module}")

    signature = str(props.get("signature", "")).strip()
    if signature:
        parts.append(signature)

    # Parameter names (when captured separately from the signature) add the
    # domain vocabulary that distinguishes, e.g., a date helper from a money one.
    params = props.get("parameters") or props.get("params")
    if isinstance(params, list) and params:
        names = [_param_name(p) for p in params]
        names = [n for n in names if n]
        if names:
            parts.append(f"({', '.join(names)})")

    # FULL docstring (v1 used only its first line). Python nodes carry it in
    # ``docstring``; SCIP/TypeScript nodes carry JSDoc in ``documentation`` (the
    # ScipGraphTranslator's field) — read both so TS equivalence expressed in
    # JSDoc is embedded too. Fall back to the node's ``snippet`` (every extractor
    # — Python classes, Tree-sitter TS — emits one) so those kinds get behavioural
    # text too, then the first ``source`` line.
    description = (
        str(props.get("docstring", "")).strip()
        or str(props.get("documentation", "")).strip()
        or str(props.get("snippet", "")).strip()
    )
    if not description:
        source_lines = str(props.get("source", "")).splitlines()
        if source_lines:
            description = source_lines[0].strip()
    if description:
        parts.append(f": {description}")

    return " ".join(parts)


def _param_name(param: Any) -> str:
    """Best-effort parameter name from a str or a dict-shaped param record."""
    if isinstance(param, str):
        return param.strip()
    if isinstance(param, dict):
        return str(param.get("name", "")).strip()
    return ""


def _infer_node_type(node: CodeNode) -> str:
    """Infer a human-readable type string from node labels."""
    if not node.labels:
        return "entity"
    # Map common labels to readable types
    label_map = {
        "CodeFunction": "function",
        "CodeClass": "class",
        "CodeModule": "module",
        "CodeMethod": "method",
        "CodeVariable": "variable",
        "CodeImport": "import",
        "CodeParameter": "parameter",
        "CodeReturn": "return",
        "KnowledgeNode": "",
    }
    for label in node.labels:
        if label in label_map:
            return label_map[label] or "entity"
    return node.labels[0].lower().removeprefix("code") or "entity"


class EmbeddingService:
    """Service layer for computing and storing node embeddings."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        graph_store: SearchableGraphStore,
    ) -> None:
        self.provider = provider
        self.graph_store = graph_store

    async def embed_nodes(
        self,
        graph_id: str,
        nodes: list[CodeNode],
    ) -> None:
        """Build text representations and embed a batch of nodes.

        Args:
            graph_id: The repository graph identifier.
            nodes: List of code nodes to embed.
        """
        if not nodes:
            return

        texts = [_build_node_text(node) for node in nodes]
        logger.debug("Embedding %d nodes for graph %s", len(nodes), graph_id)

        embeddings = await self.provider.embed(texts)

        # Provenance tag: which model + which text schema produced these vectors.
        model_tag = embedding_version_tag(self.provider.model_name)

        rows: list[EmbeddingPayload] = []
        for node, text, embedding in zip(nodes, texts, embeddings, strict=False):
            rows.append(
                EmbeddingPayload(
                    node_id=node.id,
                    graph_id=graph_id,
                    text=text,
                    embedding=embedding,
                    model=model_tag,
                )
            )

        await self.graph_store.upsert_embeddings(graph_id, rows)
        logger.debug(
            "Stored %d embeddings for graph %s", len(rows), graph_id
        )

    async def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for nodes by semantic similarity.

        Args:
            graph_id: The repository graph identifier.
            query: The search query text.
            limit: Maximum number of results.

        Returns:
            List of search hits with scores and node data.
        """
        query_vectors = await self.provider.embed([query])
        if not query_vectors:
            return []

        hits = await self.graph_store.search_vector(
            graph_id, query_vectors[0], limit=limit
        )

        # Convert SearchHit to dict for caller convenience
        return [
            {
                "node_id": hit.node_id,
                "score": hit.score,
                "node": hit.node.model_dump() if hit.node is not None else None,
            }
            for hit in hits
        ]
