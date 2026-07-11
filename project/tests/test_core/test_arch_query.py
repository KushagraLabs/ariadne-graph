"""Query-layer wrappers over the architecture dependency graph.

These pin the *intended* read-only API that MCP tools will expose:

  * ``explain_edge(src_rel, dst_rel)`` — classify a single file->file edge using
    the existing :func:`is_deep_import` + ``_organ``/``_dir_of`` helpers, and say
    WHY (same-organ / front-door / cross-organ internals) plus whether routing
    through the target organ's front door would make it valid.
  * ``dependency_matrix(files, dep_edges)`` — the file-level node/edge graph that
    ``_DEP_EDGE_SQL`` already materializes, with per-edge ``violation_count``
    driven by :func:`is_deep_import` (same rule the web view paints with).

Both are RED until the wrappers exist — they reuse architecture.py's SSOT
helpers rather than reimplementing the layering rule.
"""

from __future__ import annotations

import pytest

from ariadne_graph.core import architecture


# --------------------------------------------------------------------------
# Item 1: explain_edge — hard case is a cross-organ DEEP import that a fuzzy
# "any cross-organ import is bad" check would misclassify vs. a front-door
# import (which is allowed). The wrapper must distinguish them and name the
# front-door remedy.
# --------------------------------------------------------------------------

def test_explain_edge_cross_organ_deep_import_is_a_violation():
    result = architecture.explain_edge("src/x.py", "lib/deep/nested/y.py")

    assert result.allowed is False
    assert result.rule == "deep_import"
    assert result.src_organ == "src"
    assert result.dst_organ == "lib"
    # It is a violation *because* it reaches below the target organ's root.
    assert result.reason == "cross_organ_internal"
    # Routing through the organ front door ("lib") would make it valid.
    assert result.front_door_would_fix is True


def test_explain_edge_front_door_import_is_allowed():
    # Target dir IS the organ root ("lib") — a front-door import, not deep.
    result = architecture.explain_edge("src/x.py", "lib/api.py")

    assert result.allowed is True
    assert result.reason == "front_door"
    assert result.front_door_would_fix is False


# --------------------------------------------------------------------------
# Item 2: dependency_matrix — hard case is that violation_count must be driven
# by is_deep_import, NOT by a raw cross-organ count: a front-door edge is
# cross-organ but contributes ZERO violations, while a deep edge contributes one.
# --------------------------------------------------------------------------

@pytest.mark.xfail(reason="bead cq7", strict=True)
def test_dependency_matrix_file_level_nodes_and_violation_counts():
    files = ["src/x.py", "lib/deep/nested/y.py", "lib/api.py"]
    dep_edges = [
        ("src/x.py", "lib/deep/nested/y.py"),  # deep -> violation
        ("src/x.py", "lib/api.py"),            # front door -> not a violation
    ]

    matrix = architecture.dependency_matrix(files, dep_edges)

    node_ids = {n.id for n in matrix.nodes}
    assert node_ids == set(files)
    # Nodes carry their top-level module (organ) for grouping/color.
    modules = {n.id: n.module for n in matrix.nodes}
    assert modules["src/x.py"] == "src"
    assert modules["lib/deep/nested/y.py"] == "lib"

    by_pair = {(e.source, e.target): e for e in matrix.edges}
    deep = by_pair[("src/x.py", "lib/deep/nested/y.py")]
    front = by_pair[("src/x.py", "lib/api.py")]

    assert deep.import_count == 1
    assert deep.violation_count == 1  # is_deep_import == True
    assert front.import_count == 1
    assert front.violation_count == 0  # front door is allowed


@pytest.mark.xfail(reason="bead cq7", strict=True)
def test_dependency_matrix_matches_is_deep_import_for_every_edge():
    files = ["src/a/x.py", "src/b/deep/y.py", "app/deep/z.py"]
    dep_edges = [
        ("src/a/x.py", "src/b/deep/y.py"),  # same organ -> not a violation
        ("src/a/x.py", "app/deep/z.py"),    # cross-organ deep -> violation
    ]

    matrix = architecture.dependency_matrix(files, dep_edges)

    for edge in matrix.edges:
        src_dir = architecture._dir_of(edge.source)
        dst_dir = architecture._dir_of(edge.target)
        expected = 1 if architecture.is_deep_import(src_dir, dst_dir) else 0
        assert edge.violation_count == expected
