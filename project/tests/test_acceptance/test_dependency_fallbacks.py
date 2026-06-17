"""Acceptance tests for graceful degradation when optional extras are missing.

These tests monkeypatch away optional dependencies so they exercise the
fallback code paths even in an environment where the `[all]` extras are
installed.
"""

from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import LocalEmbeddingProvider
from ariadne_graph.core.models import EmbeddingPayload
from ariadne_graph.graphstores.memory import MemoryGraphStore
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.languages.typescript import extractor as ts_extractor
from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
from ariadne_graph.mcp.schemas import SearchSemanticInput
from ariadne_graph.mcp.tools import ToolRegistry


def test_typescript_adapter_stub_without_tree_sitter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without tree-sitter-typescript the adapter emits a stub plus a diagnostic."""
    monkeypatch.setattr(
        "ariadne_graph.languages.typescript.adapter.HAS_TREE_SITTER",
        False,
    )
    monkeypatch.setattr(ts_extractor, "HAS_TREE_SITTER", False)

    adapter = TypeScriptLanguageAdapter()
    file_path = tmp_path / "module.ts"
    file_path.write_text("const answer: number = 42;\n")

    context = ExtractionContext(graph_id="g-test", repo_root=tmp_path)
    delta = adapter.extract_file(file_path, context)

    assert delta.parser_version.endswith(":stub")

    file_nodes = [n for n in delta.nodes if "CodeFile" in n.labels]
    assert len(file_nodes) == 1
    assert file_nodes[0].properties["file_path"] == "module.ts"

    diagnostic_nodes = [n for n in delta.nodes if "CodeDiagnostic" in n.labels]
    assert len(diagnostic_nodes) == 1
    assert diagnostic_nodes[0].properties["rule"] == "missing_dependency"
    assert "tree-sitter-typescript" in diagnostic_nodes[0].properties["message"]

    assert len(delta.edges) == 1
    assert delta.edges[0].rel_type == "HAS_DIAGNOSTIC"
    assert delta.edges[0].source == file_nodes[0].id
    assert delta.edges[0].target == diagnostic_nodes[0].id


@pytest.mark.asyncio
async def test_sqlite_vector_search_disabled_without_sqlite_vec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without sqlite-vec the store still returns vector hits via brute force."""
    monkeypatch.setattr(
        "ariadne_graph.graphstores.sqlite._HAS_SQLITE_VEC",
        False,
    )

    store = SQLiteGraphStore(
        str(tmp_path / "graph.db"),
        embedding_dimensions=4,
    )
    try:
        embedding = [1.0, 0.0, 0.0, 0.0]
        await store.upsert_embeddings(
            "g1",
            [
                EmbeddingPayload(
                    node_id="node.a",
                    graph_id="g1",
                    text="alpha",
                    embedding=embedding,
                )
            ],
        )

        hits = await store.search_vector("g1", embedding, limit=5)
        assert len(hits) == 1
        assert hits[0].node_id == "node.a"
        assert store._has_sqlite_vec is False
    finally:
        await store.close()


def test_local_embedding_provider_fails_without_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local embedding provider raises a clear error when the extra is missing."""
    real_import = builtins.__import__

    def _fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    provider = LocalEmbeddingProvider(model_name="test-model")
    with pytest.raises(RuntimeError, match=r"\[semantic\]"):
        asyncio.run(provider.embed(["query"]))


@pytest.mark.asyncio
async def test_semantic_search_handler_reports_missing_embedding_provider(
    tmp_path: Path,
) -> None:
    """Semantic search returns an empty result with an install hint when disabled."""
    config = AnalyzerConfig(repo_root=tmp_path)
    registry = ToolRegistry(
        graph_store=MemoryGraphStore(),
        searchable_store=MemoryGraphStore(),
        adapters={"python": PythonLanguageAdapter()},
        config=config,
        embedding_provider=None,
    )

    result = await registry.handle_search_semantic(
        SearchSemanticInput(query_text="authentication")
    )

    assert result.hits == []
    assert "[semantic]" in result.message
