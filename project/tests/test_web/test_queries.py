"""Unit tests for the browser-view query layer (``web/queries.py``).

Hard case first (per verification-first workflow): two graphs share one physical
``graph.db``. ``full_graph`` MUST scope by ``graph_id`` — the node/edge set for
graph A must never leak graph B's nodes. A fuzzy implementation that forgets the
``graph_id`` predicate gets this wrong.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeEdge, CodeNode
from ariadne_graph.graphstores.sqlite import SQLiteGraphStore
from ariadne_graph.web import queries


@pytest.fixture
async def store(tmp_path: Path) -> AsyncGenerator[SQLiteGraphStore, None]:
    db_path = tmp_path / "graph.db"
    store = SQLiteGraphStore(str(db_path))
    try:
        yield store
    finally:
        await store.close()


def _file(node_id: str, graph_id: str, path: str) -> CodeNode:
    return CodeNode(
        id=node_id,
        graph_id=graph_id,
        labels=["KnowledgeNode", "CodeFile"],
        properties={"name": Path(path).name, "file_path": path},
    )


def _sym(node_id: str, graph_id: str, path: str) -> CodeNode:
    """A code symbol node carrying its file_path (source/target of REFERENCES)."""
    return CodeNode(
        id=node_id, graph_id=graph_id,
        labels=["KnowledgeNode", "CodeFunction"],
        properties={"name": node_id, "file_path": path},
    )


def _diag(node_id: str, graph_id: str, path: str, level: str) -> CodeNode:
    return CodeNode(
        id=node_id, graph_id=graph_id, labels=["KnowledgeNode", "CodeDiagnostic"],
        properties={"level": level, "rule": "x", "file_path": path},
    )



def _nodes_by_id(result: dict) -> dict:
    return {n["id"]: n for n in result["nodes"]}


def _edge_kinds(result: dict, kind: str) -> list:
    return [e for e in result["edges"] if e["kind"] == kind]


async def test_full_graph_scoped_by_graph_id(store: SQLiteGraphStore) -> None:
    """The hard case: graph A's node/edge set must exclude graph B entirely."""
    repo_a = "/Users/x/Documents/repo_a"
    repo_b = "/Users/x/Documents/repo_b"
    await store.add_nodes_batch("graphA", [
        _sym("a1", "graphA", f"{repo_a}/src/one.py"),
        _sym("a2", "graphA", f"{repo_a}/src/two.py"),
        _file("a1", "graphA", f"{repo_a}/src/one.py"),
        _file("a2", "graphA", f"{repo_a}/src/two.py"),
    ])
    await store.add_edges_batch("graphA", [
        CodeEdge(source="a1", target="a2", graph_id="graphA", rel_type="REFERENCES", properties={})])
    await store.add_nodes_batch("graphB", [
        _file("b1", "graphB", f"{repo_b}/lib/other.py"),
    ])

    g = await queries.full_graph(store, "graphA", repo_root=repo_a)
    file_ids = {n["id"] for n in g["nodes"] if n["kind"] == "file"}
    assert file_ids == {f"{repo_a}/src/one.py", f"{repo_a}/src/two.py"}
    assert all("repo_b" not in n["id"] for n in g["nodes"])  # no graphB leak
    # dep edge present between the two files (same dir -> not a violation)
    assert _edge_kinds(g, "dep") == [
        {"source": f"{repo_a}/src/one.py", "target": f"{repo_a}/src/two.py",
         "weight": 1, "kind": "dep", "violation": False}]


async def test_directory_hubs_and_tree_edges(store: SQLiteGraphStore) -> None:
    """Full nesting: every path segment becomes a ``dir`` hub, linked to its parent
    dir; each file links to its immediate parent dir. src/core/models/user.py yields
    dirs src, src/core, src/core/models plus the file, chained by tree edges.
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch("g", [
        _file("f1", "g", f"{repo}/src/core/models/user.py"),
    ])
    g = await queries.full_graph(store, "g", repo_root=repo)
    by = _nodes_by_id(g)

    # dir hubs at every level, with correct depth
    assert by["src"]["kind"] == "dir" and by["src"]["depth"] == 0
    assert by["src/core"]["kind"] == "dir" and by["src/core"]["depth"] == 1
    assert by["src/core/models"]["kind"] == "dir" and by["src/core/models"]["depth"] == 2
    file = by[f"{repo}/src/core/models/user.py"]
    assert file["kind"] == "file"
    # File carries its repo-relative parent dir (drives client-side drill-down;
    # matches the dir hub's id form so a subtree prefix filter works on both).
    assert file["dir"] == "src/core/models"

    tree = {(e["source"], e["target"]) for e in _edge_kinds(g, "tree")}
    assert ("src/core", "src") in tree                       # dir -> parent dir
    assert ("src/core/models", "src/core") in tree
    assert (f"{repo}/src/core/models/user.py", "src/core/models") in tree  # file -> parent dir


async def test_files_are_never_dropped(store: SQLiteGraphStore) -> None:
    """Behavior change from the flat view: a file with no deps and no diagnostics
    is still an 'employee' of its directory, so it is KEPT (its tree edge is its
    reason to exist). The flat view dropped such isolates; the dir view must not.
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch("g", [
        _file("iso", "g", f"{repo}/src/orphan.py"),   # no edge, no diagnostic
    ])
    g = await queries.full_graph(store, "g", repo_root=repo)
    file_ids = {n["id"] for n in g["nodes"] if n["kind"] == "file"}
    assert f"{repo}/src/orphan.py" in file_ids
    # and it is attached to its dir
    assert (f"{repo}/src/orphan.py", "src") in {
        (e["source"], e["target"]) for e in _edge_kinds(g, "tree")}


async def test_hygiene_rolls_up_the_directory_chain(store: SQLiteGraphStore) -> None:
    """A dept shows its worst employee: a warning on one deep file must roll up
    worst_level=warning to EVERY ancestor dir, while a sibling-free clean file
    leaves unrelated dirs clean.
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch("g", [
        _file("f1", "g", f"{repo}/src/core/bad.py"),
        _diag("d1", "g", f"{repo}/src/core/bad.py", "warning"),
        _file("f2", "g", f"{repo}/tests/ok.py"),   # clean, other subtree
    ])
    g = await queries.full_graph(store, "g", repo_root=repo)
    by = _nodes_by_id(g)

    assert by[f"{repo}/src/core/bad.py"]["worst_level"] == "warning"
    assert by["src/core"]["worst_level"] == "warning"    # rolled up one level
    assert by["src"]["worst_level"] == "warning"         # rolled up to the top
    assert by["tests"]["worst_level"] is None            # untouched subtree stays clean


async def test_dep_edges_from_scip_resolved_python_calls(store: SQLiteGraphStore) -> None:
    """Python graphs never emit REFERENCES — SCIP refines CALLS in place, tagging
    resolved ones ``resolved_by=scip-python``. Those become ``dep`` edges; unresolved
    bare-name CALLS must NOT (fuzzy targets = noise, the enterprise_tabular_ad bug).
    """
    repo = "/Users/x/Documents/py_repo"
    await store.add_nodes_batch("gpy", [
        _sym("caller", "gpy", f"{repo}/scripts/train.py"),
        _sym("callee", "gpy", f"{repo}/src/model.py"),
        _sym("local", "gpy", f"{repo}/scripts/train.py"),
        _file("caller", "gpy", f"{repo}/scripts/train.py"),
        _file("callee", "gpy", f"{repo}/src/model.py"),
    ])
    await store.add_edges_batch("gpy", [
        CodeEdge(source="caller", target="callee", graph_id="gpy", rel_type="CALLS",
                 properties={"resolved_by": "scip-python"}),   # resolved -> dep edge
        CodeEdge(source="caller", target="local", graph_id="gpy", rel_type="CALLS",
                 properties={}),                                # unresolved -> dropped
    ])

    g = await queries.full_graph(store, "gpy", repo_root=repo)
    # scripts/ -> src (a top-level organ front door) is NOT a violation.
    assert _edge_kinds(g, "dep") == [
        {"source": f"{repo}/scripts/train.py", "target": f"{repo}/src/model.py",
         "weight": 1, "kind": "dep", "violation": False}]


async def test_mixed_absolute_and_relative_paths(store: SQLiteGraphStore) -> None:
    """Real-world trap: one graph stores some file_paths absolute, some relative.
    Both must nest under the same dir hub (module derived from the top segment).
    """
    repo = "/Users/x/Documents/repo_a"
    await store.add_nodes_batch("g", [
        _file("a", "g", f"{repo}/server/index.ts"),   # absolute
        _file("b", "g", "server/routes.ts"),          # relative, same folder
    ])
    g = await queries.full_graph(store, "g", repo_root=repo)
    by = _nodes_by_id(g)
    assert by[f"{repo}/server/index.ts"]["module"] == "server"
    assert by["server/routes.ts"]["module"] == "server"
    # both attach to the single "server" dir hub
    tree = {(e["source"], e["target"]) for e in _edge_kinds(g, "tree")}
    assert (f"{repo}/server/index.ts", "server") in tree
    assert ("server/routes.ts", "server") in tree


def _dep(result: dict) -> dict:
    """Map (rel-source-dir hint via file name)->violation for the dep edges.

    Keyed by (source_basename, target_basename) for readable assertions.
    """
    out = {}
    for e in result["edges"]:
        if e["kind"] == "dep":
            out[(e["source"].rsplit("/", 1)[-1], e["target"].rsplit("/", 1)[-1])] = e["violation"]
    return out


async def test_layering_violations(store: SQLiteGraphStore) -> None:
    """The hard case: the layering rule must distinguish organ-internal / front-door
    imports (OK) from reaching into ANOTHER organ's internals (violation). A fuzzy
    'anything cross-dir = violation' or 'same-subtree-only' rule gets these wrong.

    Rule: A->B is a violation UNLESS same top-level organ OR B is a top-level organ
    front door. Reaching into a different organ's internals (depth>=1) = violation.
    """
    repo = "/Users/x/Documents/repo"
    # Files across organs and depths. Symbol nodes carry the REFERENCES edges.
    nodes = [
        _sym("A", "g", f"{repo}/src/core/a.py"),       # organ src, deep
        _sym("Bcousin", "g", f"{repo}/src/other/b.py"),  # organ src, deep (cousin)
        _sym("Bself", "g", f"{repo}/src/core/d.py"),     # organ src, same dir as A
        _sym("Bfront", "g", f"{repo}/tests/one.py"),     # organ tests, TOP level (dir='tests')
        _sym("Bguts", "g", f"{repo}/tests/unit/deep.py"),# organ tests, INTERNALS (dir='tests/unit')
        _file("A", "g", f"{repo}/src/core/a.py"),
        _file("Bcousin", "g", f"{repo}/src/other/b.py"),
        _file("Bself", "g", f"{repo}/src/core/d.py"),
        _file("Bfront", "g", f"{repo}/tests/one.py"),
        _file("Bguts", "g", f"{repo}/tests/unit/deep.py"),
    ]
    await store.add_nodes_batch("g", nodes)
    await store.add_edges_batch("g", [
        CodeEdge(source="A", target="Bcousin", graph_id="g", rel_type="REFERENCES", properties={}),
        CodeEdge(source="A", target="Bself", graph_id="g", rel_type="REFERENCES", properties={}),
        CodeEdge(source="A", target="Bfront", graph_id="g", rel_type="REFERENCES", properties={}),
        CodeEdge(source="A", target="Bguts", graph_id="g", rel_type="REFERENCES", properties={}),
    ])

    g = await queries.full_graph(store, "g", repo_root=repo)
    dep = _dep(g)

    assert dep[("a.py", "b.py")] is False      # same organ 'src', cousin -> OK
    assert dep[("a.py", "d.py")] is False      # same dir -> OK
    assert dep[("a.py", "one.py")] is False    # src/core -> tests/ (organ front door) -> OK
    assert dep[("a.py", "deep.py")] is True    # src/core -> tests/unit (other organ's guts) -> VIOLATION


async def test_violation_rule_pure() -> None:
    """Unit-level truth table for the predicate itself (no DB)."""
    v = queries._is_violation
    assert v("src/core", "src/other") is False   # same organ, cousin
    assert v("src/core", "src") is False         # same organ, own ancestor
    assert v("src", "tests") is False            # organ -> organ front door
    assert v("src", "tests/unit") is True        # organ -> other organ internals
    assert v("src/core", "scripts/lib") is True  # deep -> other organ internals
    assert v("src", "src") is False              # identical
    assert v("", "src") is False                 # root-level file source, no organ
    assert v("src/a", "") is False               # target is a root-level file, no organ


async def test_test_importer_exemption() -> None:
    """Tests reaching into the code they exercise are NOT violations, but the
    exemption is ONE-DIRECTIONAL: a non-test file reaching into a test organ's
    internals is still a violation. A symmetric rule would get the second wrong.
    """
    v = queries._is_violation
    # test importer -> another organ's internals: EXEMPT
    assert v("tests/integration/api", "src/api/auth") is False
    assert v("tests/unit", "scripts/lib/tool") is False
    assert v("spec/x", "src/deep/y") is False        # other test-organ names too
    # one-directional: prod file -> test organ internals is STILL a violation
    assert v("src/core", "tests/unit/y") is True
    # a non-test file is unaffected
    assert v("scripts/x", "src/deep/y") is True
