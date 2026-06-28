"""HARD-CASE TEST: cross-file call resolution must point to the RIGHT definition.

This is the test described in VERIFICATION.md. It encodes the exact case a
*fuzzy* (cheap) resolver gets wrong but a *real* resolver gets right:

  - file_a.py defines  save()  and calls it.
  - file_b.py ALSO defines a different save().

A real resolver: the CALLS edge from file_a's caller resolves to file_a.save,
NOT to file_b.save (and definitely not to the bare string "save").

A fuzzy resolver: the edge target is the string "save", which is ambiguous —
it matches both definitions. This test asserts the target is an actual node
ID of the correct definition, which the current bare-name implementation
CANNOT satisfy. So this test is EXPECTED TO FAIL today. That failure is the
proof that the resolver is fuzzy, not real.

When someone builds real resolution, this test goes red -> green. The owner
watches that transition without reading the implementation.
"""

from pathlib import Path

from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter


def _extract(path: Path):
    adapter = PythonLanguageAdapter()
    ctx = ExtractionContext(graph_id="g1", repo_root=path.parent)
    return adapter.extract_file(path, ctx)


def test_call_resolves_to_local_definition_not_bare_name(tmp_path: Path) -> None:
    file_a = tmp_path / "file_a.py"
    file_a.write_text(
        "def save():\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return save()\n"
    )
    file_b = tmp_path / "file_b.py"
    file_b.write_text("def save():\n    return 2\n")

    delta_a = _extract(file_a)

    # Find the save() definition node in file_a and the CALLS edge.
    save_defs = [
        n for n in delta_a.nodes
        if n.properties.get("name") == "save"
    ]
    assert save_defs, "expected a save() definition node in file_a"
    local_save_id = save_defs[0].id

    calls = [e for e in delta_a.edges if e.rel_type == "CALLS"]
    assert calls, "expected a CALLS edge from caller()"
    call = calls[0]

    # THE HARD ASSERTION:
    # The edge target must be the ID of the real local definition,
    # not the ambiguous bare string "save".
    assert call.target == local_save_id, (
        f"CALLS edge target is {call.target!r}; expected the resolved node id "
        f"{local_save_id!r}. A bare name like 'save' is ambiguous because "
        f"file_b also defines save() — this is the fuzzy-resolver bug."
    )
