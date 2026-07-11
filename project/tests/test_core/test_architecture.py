"""Graph-level architecture hygiene analysis.

``analyze`` is a pure function over repo-relative file paths + file->file dep
edges. Each rule is tested on its hard case: the input a naive implementation
gets wrong.
"""

from __future__ import annotations

from ariadne_graph.core.architecture import analyze


def _rules(diags: list, rule: str) -> list:
    return [d for d in diags if d.rule == rule]


# --------------------------------------------------------------------------
# dependency_cycle — hard case: a linear DAG must NOT be flagged.
# --------------------------------------------------------------------------

def test_linear_dependency_chain_is_not_a_cycle():
    files = ["pkg/a.py", "pkg/b.py", "pkg/c.py"]
    edges = [("pkg/a.py", "pkg/b.py"), ("pkg/b.py", "pkg/c.py")]

    diags = analyze(files, edges)

    assert _rules(diags, "dependency_cycle") == []


def test_import_ring_is_a_cycle():
    files = ["pkg/a.py", "pkg/b.py", "pkg/c.py"]
    edges = [
        ("pkg/a.py", "pkg/b.py"),
        ("pkg/b.py", "pkg/c.py"),
        ("pkg/c.py", "pkg/a.py"),
    ]

    diags = analyze(files, edges)

    cyc = _rules(diags, "dependency_cycle")
    assert {d.node_id for d in cyc} == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    assert all(d.level == "error" for d in cyc)


# --------------------------------------------------------------------------
# deep_import — hard case: reaching another organ's *front door* is allowed;
# reaching into its *internals* is a violation.
# --------------------------------------------------------------------------

def test_front_door_import_is_not_a_deep_import():
    files = ["src/x.py", "tests/conftest.py"]
    edges = [("src/x.py", "tests/conftest.py")]  # target dir "tests" IS the organ root

    diags = analyze(files, edges)

    assert _rules(diags, "deep_import") == []


def test_reaching_into_another_organ_internals_is_a_deep_import():
    files = ["src/x.py", "lib/deep/nested/y.py"]
    edges = [("src/x.py", "lib/deep/nested/y.py")]  # target dir is below "lib" root

    diags = analyze(files, edges)

    deep = _rules(diags, "deep_import")
    assert [d.node_id for d in deep] == ["src/x.py"]
    assert deep[0].level == "warning"


def test_same_organ_import_is_never_a_deep_import():
    files = ["src/a/x.py", "src/b/deep/y.py"]
    edges = [("src/a/x.py", "src/b/deep/y.py")]  # both under "src" — same organ

    diags = analyze(files, edges)

    assert _rules(diags, "deep_import") == []


# --------------------------------------------------------------------------
# orphan_module — hard case: an unreferenced entry point (cli.py) is NOT an
# orphan, but an unreferenced helper IS.
# --------------------------------------------------------------------------

def test_unreferenced_entry_point_is_not_an_orphan():
    files = ["src/cli.py", "src/util.py"]
    edges = [("src/cli.py", "src/util.py")]  # cli has fan-in 0 but is an entry point

    diags = analyze(files, edges)

    assert _rules(diags, "orphan_module") == []


def test_unreferenced_helper_is_an_orphan():
    files = ["src/helper.py", "src/util.py"]
    edges = [("src/util.py", "src/helper.py")]  # util has fan-in 0 and no suppression

    diags = analyze(files, edges)

    orphans = _rules(diags, "orphan_module")
    assert [d.node_id for d in orphans] == ["src/util.py"]
    assert orphans[0].level == "info"


def test_file_in_peripheral_organ_is_not_an_orphan():
    files = ["tests/test_foo.py", "src/foo.py"]
    edges = [("tests/test_foo.py", "src/foo.py")]  # test file has fan-in 0

    diags = analyze(files, edges)

    assert _rules(diags, "orphan_module") == []


def test_referenced_file_is_not_an_orphan():
    files = ["src/a.py", "src/b.py"]
    edges = [("src/a.py", "src/b.py")]  # b has fan-in 1

    diags = analyze(files, edges)

    assert not any(d.node_id == "src/b.py" for d in _rules(diags, "orphan_module"))


def test_no_orphans_when_graph_has_no_dep_edges():
    # SCIP unavailable => zero resolved edges. Fan-in is meaningless: flagging
    # EVERY file as an orphan is noise, so orphan detection must stay silent.
    files = ["src/a.py", "src/b.py", "src/c.py"]
    edges: list[tuple[str, str]] = []

    diags = analyze(files, edges)

    assert _rules(diags, "orphan_module") == []


# --------------------------------------------------------------------------
# upward_import — hard case: a parent importing a child (down) is fine; a child
# reaching back up to a parent module (up) is the inversion.
# --------------------------------------------------------------------------

def test_downward_import_is_not_an_upward_import():
    files = ["pkg/core.py", "pkg/sub/leaf.py"]
    edges = [("pkg/core.py", "pkg/sub/leaf.py")]  # parent -> child (down)

    diags = analyze(files, edges)

    assert _rules(diags, "upward_import") == []


def test_child_reaching_up_to_parent_is_an_upward_import():
    files = ["pkg/core.py", "pkg/sub/leaf.py"]
    edges = [("pkg/sub/leaf.py", "pkg/core.py")]  # child -> parent module (up)

    diags = analyze(files, edges)

    up = _rules(diags, "upward_import")
    assert [d.node_id for d in up] == ["pkg/sub/leaf.py"]
    assert up[0].level == "warning"


def test_sibling_import_is_not_an_upward_import():
    files = ["pkg/sub/a.py", "pkg/sub/b.py"]
    edges = [("pkg/sub/a.py", "pkg/sub/b.py")]  # same dir — no level change

    diags = analyze(files, edges)

    assert _rules(diags, "upward_import") == []


def test_cross_organ_edge_is_not_an_upward_import():
    files = ["src/sub/leaf.py", "lib/core.py"]
    edges = [("src/sub/leaf.py", "lib/core.py")]  # different organ — deep_import's job

    diags = analyze(files, edges)

    assert _rules(diags, "upward_import") == []
