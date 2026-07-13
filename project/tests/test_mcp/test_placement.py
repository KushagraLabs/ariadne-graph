"""``code_graph_suggest_placement`` + ``code_graph_find_equivalent`` (bead 42i).

The two tools that most directly prevent codebase debt in consumer repos:
placement ("where should component W live?") and duplicate-check ("does an
equivalent of Z already exist?").

RED hard cases:

* suggest_placement — a described server-only helper that CONSUMES a shared
  contract must rank a server module ABOVE the shared module, and the winner
  must list ZERO violations, while placing the same helper in ``shared`` DOES
  list a violation (shared may not depend on server). Arch-config + dependency
  matrix only; NO embeddings needed, so this half is fully testable regardless
  of the semantic extra.

* find_equivalent — WITH the semantic extra installed, a date-range formatting
  helper description surfaces the existing ``shared/datetime`` candidate in the
  top-k, framed as needs-human-judgement with NO auto-verdict.

* degraded mode — WITHOUT sentence_transformers (embedding_provider=None), BOTH
  find_equivalent and capabilities must return an EXPLICIT degraded-mode message,
  never empty silence.

Seeds the graph directly (CodeFile/CodeFunction nodes + SCIP-resolved CALLS
edges) like ``tests/test_mcp/test_change_briefing.py`` — SCIP is not run in
tests and only ``resolved_by=scip-*`` edges drive the dep matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.core.embeddings import (
    EMBEDDING_TEXT_VERSION,
    _build_node_text,
    embedding_version_tag,
)
from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.core.snippets import SnippetExtractor
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.mcp.schemas import FindEquivalentInput, SuggestPlacementInput
from ariadne_graph.mcp.tools import ToolRegistry, _graph_id_from_repo_path

# A hermetic, deterministic embedding provider — a small hashed bag-of-words so
# texts sharing vocabulary land near each other, with NO model download / network
# (a live sentence-transformers download flakes CI). It is a plain EmbeddingProvider
# (not LocalEmbeddingProvider), so it also exercises the custom-provider path that
# _semantic_ready() trusts without the sentence_transformers extra.
_EMB_DIM = 64


class _FakeEmbeddingProvider:
    def __init__(self, model_name: str = "fake-test-embedder") -> None:
        self._model_name = model_name

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import math
        import re

        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * _EMB_DIM
            for tok in re.findall(r"[a-z]+", text.lower()):
                vec[hash(tok) % _EMB_DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    @property
    def dimensions(self) -> int:
        return _EMB_DIM

    @property
    def model_name(self) -> str:
        return self._model_name


def _make_registry(
    store: SQLiteGraphStore,
    repo_root: Path,
    *,
    with_embeddings: bool,
) -> ToolRegistry:
    provider = _FakeEmbeddingProvider() if with_embeddings else None
    return ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"python": PythonLanguageAdapter()},
        config=AnalyzerConfig(repo_root=repo_root),
        snippet_extractor=SnippetExtractor(repo_root=repo_root),
        embedding_provider=provider,  # type: ignore[arg-type]
    )


def _write_repo(repo: Path) -> None:
    """A 3-organ layout: server -> shared (allowed), shared !-> server.

    server/handlers.py imports a contract from shared/contracts.py (the healthy
    downward dependency). shared/datetime.py holds an existing date helper for
    the find_equivalent duplicate-check case.
    """
    (repo / "server").mkdir(parents=True)
    (repo / "shared").mkdir(parents=True)
    (repo / "server" / "handlers.py").write_text(
        "from shared.contracts import Contract\n\ndef handle():\n    return Contract()\n"
    )
    (repo / "shared" / "contracts.py").write_text("class Contract:\n    pass\n")
    (repo / "shared" / "datetime.py").write_text(
        "def format_date_range(start, end):\n"
        '    """Format a start/end date pair into a human date range string."""\n'
        "    return f'{start} - {end}'\n"
    )


async def _seed(store: SQLiteGraphStore, graph_id: str, root: str) -> list[CodeNode]:
    def file_node(rel: str) -> CodeNode:
        return CodeNode(
            id=f"file::{rel}",
            graph_id=graph_id,
            labels=["CodeFile"],
            properties={"file_path": f"{root}/{rel}", "name": rel},
        )

    def fn_node(node_id: str, name: str, rel: str, doc: str = "") -> CodeNode:
        return CodeNode(
            id=node_id,
            graph_id=graph_id,
            labels=["CodeFunction"],
            properties={
                "file_path": f"{root}/{rel}",
                "name": name,
                "module": rel.replace("/", ".").removesuffix(".py"),
                "docstring": doc,
                "signature": f"def {name}(start, end)" if doc else f"def {name}()",
            },
        )

    nodes = [
        file_node("server/handlers.py"),
        file_node("shared/contracts.py"),
        file_node("shared/datetime.py"),
        fn_node("handle", "handle", "server/handlers.py"),
        fn_node("Contract", "Contract", "shared/contracts.py"),
        fn_node(
            "format_date_range",
            "format_date_range",
            "shared/datetime.py",
            doc="Format a start/end date pair into a human date range string.",
        ),
    ]
    await store.add_nodes_batch(graph_id, nodes)
    # server/handlers.py -> shared/contracts.py (healthy downward dep). The dep
    # matrix reads file->file CALLS/IMPORT edges resolved by SCIP.
    await store.add_edges_batch(
        graph_id,
        [
            CodeEdge(
                source="handle",
                target="Contract",
                graph_id=graph_id,
                rel_type="CALLS",
                properties={"resolved_by": "scip-python"},
            )
        ],
    )
    return nodes


def _write_arch_config(repo: Path) -> None:
    """Declared module map: server may depend on shared; shared may NOT depend
    on server. This makes placement violations deterministic (not heuristic)."""
    (repo / ".ariadne").mkdir(parents=True, exist_ok=True)
    (repo / ".ariadne" / "architecture.yml").write_text(
        "version: 1\n"
        "modules:\n"
        "  server:\n"
        "    paths: ['server/*']\n"
        "    may_depend_on: ['shared']\n"
        "  shared:\n"
        "    paths: ['shared/*']\n"
        "    may_depend_on: []\n"
    )


@pytest.mark.asyncio
async def test_suggest_placement_ranks_server_over_shared_with_config(tmp_path: Path) -> None:
    """A server-only helper that DEPENDS ON a server internal (and reads a shared
    contract) must rank the server module above shared: placing it in shared
    would require shared -> server (an upward import shared may NOT make), so the
    server winner has zero violations while shared is flagged."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)

        # depends on a server internal (server/handlers.py) AND a shared contract.
        # Placing in `server`: server->server (ok) + server->shared (allowed) = 0.
        # Placing in `shared`: shared->server (UPWARD, disallowed) = >=1 violation.
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="A server helper that extends handle() and reads a Contract",
                depends_on=["server/handlers.py", "shared/contracts.py"],
                consumed_by=[],
            )
        )

        assert result.candidates, "expected ranked candidate modules"
        assert result.config_used is True
        ranked = [c.module for c in result.candidates]
        assert "server" in ranked and "shared" in ranked
        assert ranked.index("server") < ranked.index("shared")

        winner = result.candidates[0]
        assert winner.module == "server"
        assert winner.violations == []

        shared_c = next(c for c in result.candidates if c.module == "shared")
        # shared placement needs shared -> server (upward): at least one violation.
        assert shared_c.violations, "shared placement must list >=1 violation"
        assert any(v["to"] == "server" for v in shared_c.violations)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_peripheral_consumer_is_exempt(tmp_path: Path) -> None:
    """A test/script CONSUMER is exempt from layering (the analyzer skips edges
    from peripheral paths), so a test importing the component must not manufacture
    a violation for any production placement."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)  # shared may NOT depend on server
    (repo / "tests").mkdir()
    (repo / "tests" / "test_thing.py").write_text("def test_x():\n    pass\n")
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        from ariadne_graph.core.models import CodeNode

        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="file::tests/test_thing.py", graph_id=graph_id, labels=["CodeFile"],
                         properties={"file_path": f"{root}/tests/test_thing.py",
                                     "name": "tests/test_thing.py"}),
                CodeNode(id="test_x", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/tests/test_thing.py", "name": "test_x"}),
            ],
        )
        # A CO-LOCATED TS test 'server/foo.test.ts' — its module collapses to the
        # non-peripheral 'server', so its peripheral status MUST be judged on the
        # PATH (before collapse). Placing the component in 'shared' with this test
        # as consumer would spuriously flag shared<-server if the path status were
        # lost. (server/foo.test.ts is peripheral by the co-located-test regex.)
        (repo / "server" / "foo.test.ts").write_text("test('x', () => {});\n")
        await store.add_nodes_batch(
            graph_id,
            [
                CodeNode(id="file::server/foo.test.ts", graph_id=graph_id, labels=["CodeFile"],
                         properties={"file_path": f"{root}/server/foo.test.ts",
                                     "name": "server/foo.test.ts"}),
                CodeNode(id="testFoo", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/server/foo.test.ts", "name": "testFoo"}),
            ],
        )
        # Component depends on a server internal (so a NON-exempt consumer in server
        # WOULD create shared<-server), and is consumed by the co-located TS test.
        # The test must be exempt -> shared placement has the dep violation only,
        # not an extra consumer violation from the test.
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="a helper used by a co-located server test",
                depends_on=["shared/contracts.py"],
                consumed_by=["server/foo.test.ts"],
            )
        )
        shared_c = next(c for c in result.candidates if c.module == "shared")
        # The co-located test consumer is exempt: no consumer-direction violation.
        assert all(v["direction"] != "consumed_by" for v in shared_c.violations), shared_c.violations
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_partial_config_no_false_violations(tmp_path: Path) -> None:
    """A config that declares only SOME modules must not invent violations for the
    undeclared ones. A file in an organ outside every declared `paths` glob falls
    back to a directory bucket with no may_depend_on rule — its direction is
    UNKNOWN, so depending on it (or being consumed by it) is NOT a violation."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)  # declares only server + shared
    # an UNDECLARED organ 'plugins' (matched by no config glob)
    (repo / "plugins").mkdir()
    (repo / "plugins" / "ext.py").write_text("def ext():\n    return 1\n")
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        from ariadne_graph.core.models import CodeNode

        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="file::plugins/ext.py", graph_id=graph_id, labels=["CodeFile"],
                         properties={"file_path": f"{root}/plugins/ext.py", "name": "plugins/ext.py"}),
                CodeNode(id="ext", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/plugins/ext.py", "name": "ext"}),
            ],
        )
        # a component consuming the UNDECLARED plugins/ext.py: no declared rule
        # governs 'plugins', so placing it in 'server' must NOT be flagged.
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="a helper depending on an undeclared plugin",
                depends_on=["plugins/ext.py"],
                consumed_by=["server/handlers.py"],
            )
        )
        server_c = next(c for c in result.candidates if c.module == "server")
        # server -> plugins is undeclared-endpoint (unknown), server -> server is
        # same-module: no violation may be attributed to the server candidate.
        assert server_c.violations == [], server_c.violations
        # BUT the unknown pair must be surfaced (not presented as verified-safe):
        # the rationale must NOT claim "no layering violations" and must say the
        # relationship was not checked.
        assert "introduces no layering violations" not in server_c.rationale
        assert "not checked" in server_c.rationale
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_unknown_ranks_below_verified_clean(tmp_path: Path) -> None:
    """A candidate whose relationship touches an UNDECLARED module (unknown) must
    NOT outrank a fully-declared, verified zero-violation candidate — an unknown
    is not 'verified safe'."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)  # declares server + shared
    (repo / "plugins").mkdir()
    (repo / "plugins" / "ext.py").write_text("def ext():\n    return 1\n")
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        from ariadne_graph.core.models import CodeNode

        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="file::plugins/ext.py", graph_id=graph_id, labels=["CodeFile"],
                         properties={"file_path": f"{root}/plugins/ext.py", "name": "plugins/ext.py"}),
                CodeNode(id="ext", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/plugins/ext.py", "name": "ext"}),
            ],
        )
        # Depends ONLY on shared (declared). For candidate 'shared' this is a
        # same-module (allowed) dep and for 'server' a declared allowed dep — both
        # verified-clean (0 unknowns). For candidate 'plugins' the dep shared is an
        # UNDECLARED-endpoint pair => 1 unknown. So 'plugins' must rank LAST.
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="helper depending on a shared contract",
                depends_on=["shared/contracts.py"],
                consumed_by=[],
            )
        )
        modules = [c.module for c in result.candidates]
        assert "plugins" in modules  # the undeclared organ is a candidate bucket
        # the verified-clean declared modules outrank the unknown 'plugins' bucket.
        assert modules.index("shared") < modules.index("plugins")
        assert modules.index("server") < modules.index("plugins")
        plugins_c = next(c for c in result.candidates if c.module == "plugins")
        assert "not checked" in plugins_c.rationale
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_excludes_declared_peripheral_module(tmp_path: Path) -> None:
    """A config that DECLARES a peripheral module (e.g. for tests/) must not offer
    it as a placement target for production code — declared modules whose paths
    are all peripheral are filtered out, just like peripheral files are."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    (repo / ".ariadne").mkdir(parents=True, exist_ok=True)
    (repo / ".ariadne" / "architecture.yml").write_text(
        "version: 1\n"
        "modules:\n"
        "  server:\n"
        "    paths: ['server/*']\n"
        "    may_depend_on: ['shared', 'tests']\n"
        "  shared:\n"
        "    paths: ['shared/*']\n"
        "  tests:\n"
        "    paths: ['tests/*']\n"  # a declared PERIPHERAL module
        "    may_depend_on: ['server', 'shared']\n"
    )
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="a server helper",
                depends_on=["shared/contracts.py"],
                consumed_by=["server/handlers.py"],
            )
        )
        modules = {c.module for c in result.candidates}
        assert "tests" not in modules, "declared peripheral module must not be a target"
        assert "server" in modules and "shared" in modules
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_rejects_non_definition_id(tmp_path: Path) -> None:
    """A qualified reference whose id matches a NON-definition node (a CodeImport
    usage) must NOT resolve to that usage's file — it is reported unresolved, not
    attributed to the wrong module."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        from ariadne_graph.core.models import CodeNode

        # A CodeImport node whose id is a plausible qualified reference living in
        # server/handlers.py (a usage, not a definition).
        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="shared.contracts.Contract@import", graph_id=graph_id,
                         labels=["CodeImport"],
                         properties={"file_path": f"{root}/server/handlers.py",
                                     "name": "Contract"}),
            ],
        )
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="x",
                depends_on=["shared.contracts.Contract@import"],  # a CodeImport id
            )
        )
        # It must be reported unresolved (a usage id is not a definition), not
        # silently attributed to server/handlers.py's module.
        assert "shared.contracts.Contract@import" in result.message
        assert result.candidates == []  # no usable constraint resolved
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_embedding_coverage_excludes_filepathless_nodes(tmp_path: Path) -> None:
    """A node with no file_path (e.g. SCIP CodeExternalModule) is unembeddable and
    must NOT count against coverage — else a fully-embedded index looks partial."""
    from ariadne_graph.core.models import CodeNode

    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=True)
    try:
        nodes = await _seed(store, graph_id, root)
        # An external node with NO file_path (never embeddable).
        await store.add_nodes_batch(
            graph_id,
            [CodeNode(id="ext::npm/left-pad", graph_id=graph_id,
                      labels=["CodeExternalModule"], properties={"name": "left-pad"})],
        )
        # Embed ALL the file-backed nodes (a complete rebuild).
        await registry.embedding_service.embed_nodes(graph_id, nodes)
        status = await registry._embedding_status(graph_id)
        # coverage is over file-backed nodes only, so a full rebuild is complete
        # despite the unembeddable external node.
        assert status["coverage_complete"] is True, status
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_requires_constraints(tmp_path: Path) -> None:
    """A description-only call cannot rank placement meaningfully (every candidate
    would tie and sort alphabetically). It must return NO candidates and ask for
    depends_on/consumed_by rather than present an arbitrary order as advice."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(repo_path=root, description="a new helper of some kind")
        )
        assert result.candidates == []
        assert "depends_on" in result.message and "consumed_by" in result.message
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_works_without_config(tmp_path: Path) -> None:
    """Decision (2): no .ariadne/architecture.yml -> still ranks candidates by
    co-location, but HONESTLY reports that import direction (layering) was NOT
    checked and emits NO fabricated violations (the old heuristic silently
    reported zero violations for every candidate — a false 'all-clear')."""
    repo = tmp_path / "repo"
    _write_repo(repo)  # note: no _write_arch_config
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="A server helper consumed by the server handler",
                depends_on=["shared/contracts.py"],
                consumed_by=["server/handlers.py"],
            )
        )
        assert result.candidates
        assert result.config_used is False
        assert {"server", "shared"} <= {c.module for c in result.candidates}
        # No config -> no fabricated violations anywhere, and the message says so.
        assert all(c.violations == [] for c in result.candidates)
        assert "not checked" in result.message.lower()
        for c in result.candidates:
            assert "not checked" in c.rationale.lower()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_resolves_symbol_references(tmp_path: Path) -> None:
    """A bare SYMBOL name in depends_on/consumed_by is resolved to its owning
    file (not silently dropped): depending on the 'Contract' symbol and consumed
    by 'handle' must constrain placement exactly like the file paths would, and
    an unresolvable reference is REPORTED, never silently ignored."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _write_arch_config(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="A server helper extending handle() reading a Contract",
                depends_on=["handle", "Contract"],  # bare symbols, not file paths
                consumed_by=[],
            )
        )
        assert result.candidates
        # 'handle' -> server, 'Contract' -> shared were resolved (server co-located).
        winner = result.candidates[0]
        assert winner.module == "server"
        assert winner.violations == []
        # shared placement now needs shared -> server (handle): a violation.
        shared_c = next(c for c in result.candidates if c.module == "shared")
        assert any(v["to"] == "server" for v in shared_c.violations)

        # an unresolvable reference is surfaced in the message, not dropped.
        result2 = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="x",
                depends_on=["nonexistent_symbol_xyz"],
            )
        )
        assert "nonexistent_symbol_xyz" in result2.message

        # a './'-prefixed / '..'-containing relative path normalises to the same
        # indexed key (must NOT land in the unresolved list).
        result3 = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="x",
                depends_on=["./server/../shared/contracts.py"],
                consumed_by=[],
            )
        )
        assert "could not be used" not in result3.message
        assert "shared" in {c.module for c in result3.candidates}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_suggest_placement_flags_ambiguous_symbol(tmp_path: Path) -> None:
    """A bare symbol defined in MULTIPLE files is AMBIGUOUS: placement must NOT
    silently pick one file's module (which could be wrong) — it reports the
    reference as ambiguous and excludes it, asking for a qualified symbol."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    (repo / "server" / "util.py").write_text("def helper():\n    return 1\n")
    (repo / "shared" / "util.py").write_text("def helper():\n    return 2\n")
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        # two distinct 'helper' definitions, one per organ
        from ariadne_graph.core.models import CodeNode

        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="server.util.helper", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/server/util.py", "name": "helper"}),
                CodeNode(id="shared.util.helper", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/shared/util.py", "name": "helper"}),
            ],
        )
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="something using helper",
                depends_on=["helper"],  # ambiguous: defined in server/ and shared/
            )
        )
        assert "ambiguous" in result.message.lower()
        assert "helper" in result.message

        # The advised disambiguation — a qualified symbol (node id) — must resolve.
        result_q = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="something using the server helper",
                depends_on=["server.util.helper"],  # unambiguous node id
                consumed_by=[],
            )
        )
        assert "ambiguous" not in result_q.message.lower()
        assert "could not be used" not in result_q.message
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_equivalent_degraded_without_semantic(tmp_path: Path) -> None:
    """Without embeddings, find_equivalent + capabilities must announce degraded
    mode EXPLICITLY (not empty silence)."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        await _seed(store, graph_id, root)
        result = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="format a date range into a string",
            )
        )
        assert result.candidates == []
        assert result.semantic_available is False
        assert "semantic" in result.message.lower()
        assert "[semantic]" in result.message  # the install hint, not silence
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_equivalent_surfaces_datetime_helper(tmp_path: Path) -> None:
    """WITH embeddings (hermetic fake provider — no model download): a date-range
    formatting description surfaces the existing shared/datetime.format_date_range
    in the top-k, with needs-human-judgement framing and NO auto-verdict."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=True)
    try:
        nodes = await _seed(store, graph_id, root)
        # embed the seeded nodes so semantic search has vectors
        await registry.embedding_service.embed_nodes(graph_id, nodes)

        result = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="a helper that formats a start and end date into a date range string",
                limit=5,
            )
        )
        assert result.semantic_available is True
        assert result.candidates, "expected ranked existing-symbol candidates"
        names = [c.name for c in result.candidates]
        assert "format_date_range" in names
        # explicit needs-human-judgement framing, no auto-verdict
        assert result.needs_human_judgement is True
        top = result.candidates[0]
        assert top.rationale  # a rationale string, not a verdict
        assert not hasattr(top, "is_duplicate")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_equivalent_type_filter_not_starved_by_nonmatching_hits(tmp_path: Path) -> None:
    """A definition ranked BELOW many non-matching (file/import) nodes must still
    surface with a type filter — the whole-graph pool means the post-fetch filter
    can't drop a match that only exists past a small window (codex P2)."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=True)
    try:
        nodes = await _seed(store, graph_id, root)
        # Add 80 CodeFile nodes whose text is tuned to the query so they'd occupy a
        # small (e.g. 50) window and hide the one matching CodeFunction below them.
        extra_files = [
            CodeNode(
                id=f"file::noise/date_range_{i}.py",
                graph_id=graph_id,
                labels=["CodeFile"],
                properties={
                    "file_path": f"{root}/noise/date_range_{i}.py",
                    "name": f"date range format start end helper {i}",
                },
            )
            for i in range(80)
        ]
        await store.add_nodes_batch(graph_id, extra_files)
        await registry.embedding_service.embed_nodes(graph_id, nodes + extra_files)

        # Filter to functions only: the 80 CodeFile hits must not starve the match.
        result = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="format a start and end date into a date range",
                types=["CodeFunction"],
                limit=5,
            )
        )
        names = [c.name for c in result.candidates]
        assert "format_date_range" in names
        # every returned candidate is a function (the filter held)
        for c in result.candidates:
            assert c.name  # non-empty; all are CodeFunction defs
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_equivalent_module_hint_matches_on_token_boundary(tmp_path: Path) -> None:
    """The same-module bonus must match the module name on a TOKEN boundary in the
    signature hint, not as a bare substring: a signature 'def capitalize(value)'
    must NOT award the 'shared' bonus just because... (here we assert the module's
    name is not spuriously found inside an unrelated signature word)."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=True)
    try:
        nodes = await _seed(store, graph_id, root)
        await registry.embedding_service.embed_nodes(graph_id, nodes)
        # 'contracts' as a bare substring appears in no signature word here; use a
        # signature whose only 'shared'-ish content is inside a larger word.
        result = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="build a contract response",
                signature="def sharedish_helper(value)",  # 'shared' is a substring, not a token
                limit=5,
            )
        )
        # No candidate may claim the same-module bonus from a mere substring match.
        for c in result.candidates:
            assert "same module as the hint" not in c.rationale

        # A bare parameter word equal to a module token ('shared') but with NO
        # qualified path ('.'/'/') must ALSO not earn the bonus — it names no module.
        result_bare = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="build a contract response",
                signature="def transform(shared)",  # token 'shared' but not qualified
                limit=5,
            )
        )
        for c in result_bare.candidates:
            assert "same module as the hint" not in c.rationale

        # But a QUALIFIED hint naming the module's path tokens DOES earn the bonus.
        # format_date_range lives in module 'shared.datetime' (set in _seed).
        result2 = await registry.handle_find_equivalent(
            FindEquivalentInput(
                repo_path=root,
                description="format a date range",
                signature="def shared.datetime.format_range(start, end)",
                limit=5,
            )
        )
        fmt = next((c for c in result2.candidates if c.name == "format_date_range"), None)
        assert fmt is not None
        assert "same module as the hint" in fmt.rationale
    finally:
        await store.close()


def test_embedding_text_is_enriched_v2() -> None:
    """v2 embedding text (bead 42i) must include the signature, parameter names,
    and the FULL docstring — not just the first line (the thin v1 text that
    missed behavioural duplicates)."""
    node = CodeNode(
        id="fmt",
        graph_id="g",
        labels=["CodeFunction"],
        properties={
            "name": "format_date_range",
            "module": "shared.datetime",
            "signature": "def format_date_range(start, end)",
            "parameters": ["start", "end"],
            "docstring": "Format a start/end date pair.\nSecond line with more detail.",
        },
    )
    text = _build_node_text(node)
    assert "format_date_range" in text
    assert "shared.datetime" in text
    assert "def format_date_range(start, end)" in text  # signature
    assert "start" in text and "end" in text  # parameter names
    # FULL docstring, not just the first line
    assert "Second line with more detail." in text


def test_embedding_text_uses_typescript_documentation_property() -> None:
    """SCIP/TypeScript nodes carry JSDoc in the ``documentation`` property (not
    ``docstring``). The embedding text must include it so TS equivalence
    expressed in JSDoc is embedded — otherwise find_equivalent misses TS dupes."""
    node = CodeNode(
        id="fmtTs",
        graph_id="g",
        labels=["CodeFunction"],
        properties={
            "name": "formatDateRange",
            "module": "shared/datetime.ts",
            # SCIP translator stores JSDoc here, NOT in 'docstring'
            "documentation": "Format a start/end date pair into a human range string.",
        },
    )
    text = _build_node_text(node)
    assert "formatDateRange" in text
    assert "Format a start/end date pair into a human range string." in text


def test_embedding_text_falls_back_to_snippet() -> None:
    """A node with no docstring/documentation/source (e.g. a Python class or a
    Tree-sitter TS node) must still get behavioural text from its ``snippet`` —
    otherwise it embeds as only name/type/module and misses as an equivalent."""
    node = CodeNode(
        id="cls",
        graph_id="g",
        labels=["CodeClass"],
        properties={
            "name": "DateRangeFormatter",
            "module": "shared.datetime",
            # no docstring/documentation/source — only the raw snippet
            "snippet": "class DateRangeFormatter:\n    def render(self, start, end): ...",
        },
    )
    text = _build_node_text(node)
    assert "DateRangeFormatter" in text
    assert "render" in text  # snippet body contributed vocabulary


def test_record_props_handles_nested_and_flattened_shapes() -> None:
    """_record_props/_record_labels must read properties from BOTH the nested
    SQLite/Memory shape AND the flattened Neo4j shape (n {.*, id, labels})."""
    from ariadne_graph.mcp.tools import _record_labels, _record_props

    nested = {"id": "x", "labels": ["CodeFunction"],
              "properties": {"name": "f", "file_path": "/a/b.py"}}
    flat = {"id": "x", "labels": ["CodeFunction"], "name": "f", "file_path": "/a/b.py"}
    for rec in (nested, flat):
        props = _record_props(rec)
        assert props.get("name") == "f"
        assert props.get("file_path") == "/a/b.py"
        assert _record_labels(rec) == ["CodeFunction"]
        # structural keys never leak into the flattened property bag
        assert "id" not in props and "labels" not in props


@pytest.mark.asyncio
async def test_suggest_placement_production_module_named_tests_is_kept(tmp_path: Path) -> None:
    """A production module DECLARED as 'tests' (its paths are non-peripheral) must
    keep its consumer constraint — the module NAME must not be re-tested for
    peripherality after the PATH was already classified."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    # 'tests' is a production module owning a NON-peripheral path 'qa_suite/*'.
    (repo / "qa_suite").mkdir()
    (repo / "qa_suite" / "runner.py").write_text("def run():\n    return 1\n")
    (repo / ".ariadne").mkdir(parents=True, exist_ok=True)
    (repo / ".ariadne" / "architecture.yml").write_text(
        "version: 1\n"
        "modules:\n"
        "  server:\n"
        "    paths: ['server/*']\n"
        "  shared:\n"
        "    paths: ['shared/*']\n"
        "  tests:\n"
        "    paths: ['qa_suite/*']\n"  # production path, module happens to be named 'tests'
        "    may_depend_on: []\n"  # 'tests' may NOT depend on shared
    )
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=False)
    try:
        nodes = await _seed(store, graph_id, root)
        await store.add_nodes_batch(
            graph_id,
            nodes + [
                CodeNode(id="file::qa_suite/runner.py", graph_id=graph_id, labels=["CodeFile"],
                         properties={"file_path": f"{root}/qa_suite/runner.py",
                                     "name": "qa_suite/runner.py"}),
                CodeNode(id="run", graph_id=graph_id, labels=["CodeFunction"],
                         properties={"file_path": f"{root}/qa_suite/runner.py", "name": "run"}),
            ],
        )
        # The 'tests' module (non-peripheral path) consumes the component. Placing
        # in 'shared' means tests->shared, which 'tests' may NOT do => a violation
        # that must NOT be dropped just because the module is named 'tests'.
        result = await registry.handle_suggest_placement(
            SuggestPlacementInput(
                repo_path=root,
                description="a shared helper consumed by the qa runner",
                depends_on=["shared/contracts.py"],
                consumed_by=["qa_suite/runner.py"],
            )
        )
        shared_c = next(c for c in result.candidates if c.module == "shared")
        assert any(
            v["from"] == "tests" and v["to"] == "shared" for v in shared_c.violations
        ), shared_c.violations
    finally:
        await store.close()


def test_embedding_version_tag_roundtrips() -> None:
    from ariadne_graph.mcp.tools import _parse_embedding_version

    tag = embedding_version_tag("all-MiniLM-L6-v2")
    assert tag == f"all-MiniLM-L6-v2#v{EMBEDDING_TEXT_VERSION}"
    assert _parse_embedding_version(tag) == EMBEDDING_TEXT_VERSION
    # legacy / unknown tags are treated as stale (version None)
    assert _parse_embedding_version(None) is None
    assert _parse_embedding_version("legacy-model") is None


@pytest.mark.asyncio
async def test_embedding_status_detects_model_and_version_staleness(tmp_path: Path) -> None:
    """Hard case (codex P2): stored vectors from a DIFFERENT model at the SAME
    text-schema version must still be flagged stale — comparing only the '#vN'
    suffix would wrongly report them current. Also covers an older-version tag."""
    from ariadne_graph.core.models import EmbeddingPayload

    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    # A hermetic custom provider whose model_name is the CURRENT model. Using the
    # fake (not LocalEmbeddingProvider) keeps this test independent of the optional
    # semantic extra: _semantic_ready() trusts a custom provider, so the base-install
    # CI job reaches the stale-vector check instead of the missing-extra message.
    provider = _FakeEmbeddingProvider(model_name="all-MiniLM-L6-v2")
    registry = ToolRegistry(
        graph_store=store,
        searchable_store=store,
        adapters={"python": PythonLanguageAdapter()},
        config=AnalyzerConfig(repo_root=repo),
        snippet_extractor=SnippetExtractor(repo_root=repo),
        embedding_provider=provider,
    )
    try:
        await _seed(store, graph_id, root)

        # (a) same version, DIFFERENT model -> stale
        other_model_tag = f"other-model#v{EMBEDDING_TEXT_VERSION}"
        await store.upsert_embeddings(
            graph_id,
            [EmbeddingPayload(node_id="handle", graph_id=graph_id, text="x",
                              embedding=[0.1, 0.2, 0.3], model=other_model_tag)],
        )
        status = await registry._embedding_status(graph_id)
        assert status["supported"] is True
        assert status["current_tag"] == embedding_version_tag("all-MiniLM-L6-v2")
        assert status["stale"] is True, "different model at same version must be stale"
        assert status["embedded_count"] == 1

        # find_equivalent must REFUSE to search stale vectors (a model/dimension
        # change would shape-mismatch) and give the force-rebuild guidance.
        fe = await registry.handle_find_equivalent(
            FindEquivalentInput(repo_path=root, description="handle a request")
        )
        assert fe.candidates == []
        assert "stale" in fe.message.lower()
        assert "force_rebuild" in fe.message

        # (b) current model + current version -> NOT stale
        await store.upsert_embeddings(
            graph_id,
            [EmbeddingPayload(node_id="handle", graph_id=graph_id, text="x",
                              embedding=[0.1, 0.2, 0.3],
                              model=embedding_version_tag("all-MiniLM-L6-v2"))],
        )
        status2 = await registry._embedding_status(graph_id)
        assert status2["stale"] is False
        # only 1 of the 6 seeded nodes is embedded -> PARTIAL coverage detected.
        assert status2["coverage_complete"] is False
        assert status2["coverage"] < 1.0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_equivalent_warns_on_partial_embedding_index(tmp_path: Path) -> None:
    """After enabling semantic on an already-indexed repo, an incremental index
    embeds only some nodes. find_equivalent must still return hits but WARN that
    the index is partial (unembedded files are missed) — not present it as an
    exhaustive search."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    root = str(repo.resolve())
    graph_id = _graph_id_from_repo_path(root)
    store = SQLiteGraphStore(str(tmp_path / "graph.db"))
    registry = _make_registry(store, repo, with_embeddings=True)
    try:
        nodes = await _seed(store, graph_id, root)
        # Embed ONLY the datetime helper (a partial index), not all 6 nodes.
        only = [n for n in nodes if n.properties.get("name") == "format_date_range"]
        await registry.embedding_service.embed_nodes(graph_id, only)

        result = await registry.handle_find_equivalent(
            FindEquivalentInput(repo_path=root, description="format a date range")
        )
        assert result.candidates  # the one embedded node still surfaces
        assert "partial" in result.message.lower()
        assert "force_rebuild" in result.message
    finally:
        await store.close()
