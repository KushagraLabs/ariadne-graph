"""``code_graph_change_briefing`` — digested pre-edit guidance for a file/symbol.

RED hard case (bead code_hygiene_mcp-3qz): on a fixture file that sits in a
known 2-cycle with 3 known callers, the briefing must contain the cycle path,
callers grouped by module WITH file paths, and ``stale=false``; then, after the
repo is dirtied without a reindex, ``stale=true`` with ``dirty_file_count>0``.
No bare node ids may appear anywhere in the markdown summary, and the existing
analysis tools must be unchanged.

Seeds the graph directly (nodes + SCIP-resolved CALLS edges) rather than running
a real index, because SCIP-python is not invoked in tests and only
``resolved_by=scip-python`` CALLS edges drive the architecture cycle pass — the
same seeding pattern as ``tests/test_core/test_architecture_persist.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import xxhash

from ariadne_graph.core.architecture import persist_architecture_diagnostics
from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.models import CodeEdge, CodeNode, HotspotInfo
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import ChangeBriefingInput, ImpactAnalysisInput
from ariadne_graph.mcp.tools import ToolRegistry, _graph_id_from_repo_path


def _make_registry(store: SQLiteGraphStore, repo_root: Path) -> ToolRegistry:
    config = AnalyzerConfig(repo_root=repo_root)
    return ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"python": PythonLanguageAdapter()},
        config=config,
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=None,
    )


def _write_repo(repo: Path) -> None:
    """A 2-cycle (a<->b) with three distinct callers of a's symbol.

    On-disk files are needed so the freshness mtime prefilter has something to
    stat/hash; the graph itself is seeded directly below.
    """
    (repo / "core").mkdir(parents=True)
    (repo / "callers").mkdir(parents=True)
    (repo / "core" / "a.py").write_text("def alpha():\n    return beta()\n")
    (repo / "core" / "b.py").write_text("def beta():\n    return alpha()\n")
    (repo / "callers" / "c1.py").write_text("def caller_one():\n    return alpha()\n")
    (repo / "callers" / "c2.py").write_text("def caller_two():\n    return alpha()\n")
    (repo / "callers" / "c3.py").write_text("def caller_three():\n    return alpha()\n")


async def _seed(store: SQLiteGraphStore, graph_id: str, root: str) -> None:
    """Seed CodeFile/CodeFunction nodes + SCIP-resolved CALLS edges.

    Cycle: alpha (core/a.py) <-> beta (core/b.py). Callers of alpha:
    caller_one/two/three. The symbol node ids are deliberately chosen NOT to
    appear as tokens inside any file path, so the 'no bare node id in summary'
    assertion catches a genuine leak rather than a filename stem.
    """

    def file_node(rel: str) -> CodeNode:
        return CodeNode(
            id=f"file::{rel}",
            graph_id=graph_id,
            labels=["CodeFile"],
            properties={"file_path": f"{root}/{rel}", "name": rel},
        )

    def fn_node(name: str, rel: str) -> CodeNode:
        return CodeNode(
            id=name,
            graph_id=graph_id,
            labels=["CodeFunction"],
            properties={"file_path": f"{root}/{rel}", "name": name},
        )

    await store.add_nodes_batch(
        graph_id,
        [
            file_node("core/a.py"), file_node("core/b.py"),
            file_node("callers/c1.py"), file_node("callers/c2.py"),
            file_node("callers/c3.py"),
            fn_node("alpha", "core/a.py"), fn_node("beta", "core/b.py"),
            fn_node("caller_one", "callers/c1.py"),
            fn_node("caller_two", "callers/c2.py"),
            fn_node("caller_three", "callers/c3.py"),
        ],
    )

    def calls(src: str, dst: str) -> CodeEdge:
        return CodeEdge(
            source=src, target=dst, graph_id=graph_id, rel_type="CALLS",
            properties={"resolved_by": "scip-python"},
        )

    await store.add_edges_batch(
        graph_id,
        [
            calls("alpha", "beta"), calls("beta", "alpha"),  # the 2-cycle
            calls("caller_one", "alpha"),
            calls("caller_two", "alpha"),
            calls("caller_three", "alpha"),  # 3 callers
        ],
    )

    # Store content hashes so the freshness mtime prefilter has a baseline to
    # detect a later edit against (a real index writes these; direct seeding must
    # too, or dirty detection has nothing to compare to).
    for rel in ("core/a.py", "core/b.py", "callers/c1.py", "callers/c2.py", "callers/c3.py"):
        abs_path = f"{root}/{rel}"
        content_hash = xxhash.xxh3_64_hexdigest(Path(abs_path).read_bytes())
        await store.update_hash(graph_id, abs_path, content_hash)


@pytest.mark.asyncio
async def test_change_briefing_hard_case(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await _seed(store, graph_id, root)
        await persist_architecture_diagnostics(store, graph_id, root)
        # Record freshness so the envelope reports last_indexed/stale honestly.
        await registry.freshness_tracker.mark_indexed(graph_id, root, file_count=5)

        briefing = await registry.handle_change_briefing(
            ChangeBriefingInput(repo_path=root, symbol="alpha")
        )

        # ---- cycle membership: the 2-cycle path is present -------------------
        assert briefing.cycle is not None
        assert briefing.cycle["scc_size"] == 2
        assert set(briefing.cycle["path"]) == {"core/a.py", "core/b.py"}

        # ---- callers grouped by module WITH file paths -----------------------
        caller_files = {
            c["file"] for group in briefing.callers_by_module.values() for c in group
        }
        assert {"callers/c1.py", "callers/c2.py", "callers/c3.py"} <= caller_files
        # grouped by module: 'callers' directory is one module bucket
        assert "callers" in briefing.callers_by_module
        # every listed caller carries a real file path, never a bare node id
        for group in briefing.callers_by_module.values():
            for c in group:
                assert c["file"] and "/" in c["file"]

        # ---- freshness: clean repo -> stale is False -------------------------
        assert briefing.freshness is not None
        assert briefing.freshness["stale"] is False
        assert briefing.freshness["dirty_file_count"] == 0

        # ---- markdown summary: facts, and NO bare node ids -------------------
        summary = briefing.summary
        assert "core/a.py" in summary
        # The graph's symbol node ids ('alpha', 'beta', 'caller_one'...) are
        # chosen to NOT appear inside any file path, so if any shows up as a
        # standalone token it is a genuine bare-node-id leak. File paths (which
        # tokenize to 'core', 'a', 'py', ...) are allowed.
        import re

        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", summary))
        for bare in ("alpha", "beta", "caller_one", "caller_two", "caller_three"):
            assert bare not in tokens, f"bare node id {bare!r} leaked into summary"

        # ---- dirty the repo WITHOUT reindex -> stale flips -------------------
        (repo / "core" / "a.py").write_text("def alpha():\n    return beta() + 999\n")
        briefing2 = await registry.handle_change_briefing(
            ChangeBriefingInput(repo_path=root, symbol="alpha")
        )
        assert briefing2.freshness["stale"] is True
        assert briefing2.freshness["dirty_file_count"] > 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_change_briefing_accepts_file_path_input(tmp_path: Path) -> None:
    """Decision (1): a file_path input resolves to the same owning file as the
    symbol input — either identifier is accepted."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await _seed(store, graph_id, root)
        await persist_architecture_diagnostics(store, graph_id, root)
        await registry.freshness_tracker.mark_indexed(graph_id, root, file_count=5)

        by_file = await registry.handle_change_briefing(
            ChangeBriefingInput(repo_path=root, file_path="core/a.py")
        )
        assert by_file.target_file == "core/a.py"
        assert by_file.cycle is not None
        assert by_file.cycle["scc_size"] == 2
        # file_path input must ALSO populate callers (they trace from the file's
        # own symbol nodes, not just from a single resolved symbol).
        caller_files = {
            c["file"] for group in by_file.callers_by_module.values() for c in group
        }
        assert {"callers/c1.py", "callers/c2.py", "callers/c3.py"} <= caller_files
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_coupling_rank_is_file_level_not_symbol_level(tmp_path: Path) -> None:
    """Hard case for the coupling projection: a file with SEVERAL ranked symbols
    of differing scores must occupy exactly ONE file-level rank, and
    ``ranked_within`` must count FILES, not symbols.

    ``find_hotspots`` ranks symbol nodes, so without the file-level projection a
    file with N symbols would take N positions and ``ranked_within`` would exceed
    the file count. Here core/a.py is given three extra symbols; the five fixture
    files bound ``ranked_within`` at 5 no matter how many symbols exist.
    """
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await _seed(store, graph_id, root)
        # Add extra symbols to core/a.py, wired into the call graph so they get
        # non-trivial coupling scores. Now core/a.py owns 4 symbols but is still
        # one file.
        extra = [
            CodeNode(
                id=f"extra_{i}",
                graph_id=graph_id,
                labels=["CodeFunction"],
                properties={"file_path": f"{root}/core/a.py", "name": f"extra_{i}"},
            )
            for i in range(3)
        ]
        await store.add_nodes_batch(graph_id, extra)
        await store.add_edges_batch(
            graph_id,
            [
                CodeEdge(source=f"extra_{i}", target="alpha", graph_id=graph_id,
                         rel_type="CALLS", properties={"resolved_by": "scip-python"})
                for i in range(3)
            ],
        )
        await registry.freshness_tracker.mark_indexed(graph_id, root, file_count=5)

        rank = await registry._briefing_coupling_rank(graph_id, "core/a.py", root)
        # core/a.py participates in the graph, so it should rank.
        assert rank is not None
        # ranked_within counts FILES (<= 5 fixture files), never the 7+ symbols.
        assert rank["ranked_within"] <= 5
        assert 1 <= rank["rank"] <= rank["ranked_within"]
        # exact pool boundary is not hit, so the pool is not reported capped.
        assert rank["pool_capped"] is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_coupling_rank_excludes_overflow_probe_entry(tmp_path: Path) -> None:
    """Hard case for the pool_capped overflow probe: _briefing_coupling_rank
    fetches pool_size+1 hotspots to DETECT capping, but must project only the
    first pool_size into the ranking. The 1001st (overflow-probe) entry — and any
    file present ONLY through it — must never receive a rank or inflate
    ``ranked_within``.

    Stubs find_hotspots with a controlled 1001-entry list so the boundary is
    exercised deterministically (the real betweenness ranking can't be steered to
    place a chosen file at exactly position 1001).
    """
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        pool_size = 1000
        # 1000 pooled files (descending score) + a 1001st probe entry whose file
        # appears ONLY at the overflow position.
        pooled = [
            HotspotInfo(
                node_id=f"pooled_{i}",
                file_path=f"{root}/pooled/f{i}.py",
                metric_type="coupling",
                score=float(pool_size - i),
            )
            for i in range(pool_size)
        ]
        overflow = HotspotInfo(
            node_id="overflow_only",
            file_path=f"{root}/overflow/only.py",
            metric_type="coupling",
            score=0.0,
        )
        fake = pooled + [overflow]

        async def _fake_find_hotspots(gid, top_n=10, metric="complexity", **kw):
            return fake[:top_n]

        registry.community_analyzer.find_hotspots = _fake_find_hotspots  # type: ignore[assignment]

        # The overflow-only file must NOT rank — it lives past the pool boundary.
        overflow_rank = await registry._briefing_coupling_rank(
            graph_id, "overflow/only.py", root
        )
        assert overflow_rank is None

        # A pooled file DOES rank, ranked_within counts only the 1000 pooled
        # files (not 1001), and pool_capped is True (a 1001st entry came back).
        pooled_rank = await registry._briefing_coupling_rank(
            graph_id, "pooled/f0.py", root
        )
        assert pooled_rank is not None
        assert pooled_rank["rank"] == 1  # highest score
        assert pooled_rank["ranked_within"] == pool_size
        assert pooled_rank["pool_capped"] is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_existing_impact_tool_unchanged(tmp_path: Path) -> None:
    """The new tool must not perturb the existing impact_analysis surface."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_impact_analysis(
            ImpactAnalysisInput(symbol="alpha", graph_id=graph_id)
        )
        # alpha reaches beta (down) and the three callers (up) — unchanged.
        assert result.target_symbol == "alpha"
        assert result.total_affected >= 4
    finally:
        await store.close()
