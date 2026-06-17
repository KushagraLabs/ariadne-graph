"""Tests for the Python AST fact extractor."""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.python_ast.adapter import PythonLanguageAdapter
from ariadne_graph.languages.python_ast.extractor import PythonFactExtractor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(source: str, repo_root: Path | None = None) -> CodeGraphDelta:
    """Run the extractor on a source string and return the delta."""
    if repo_root is None:
        repo_root = Path("/tmp/repo")
    file_path = repo_root / "mymodule.py"
    extractor = PythonFactExtractor(
        source=source,
        file_path=file_path,
        repo_root=repo_root,
        graph_id="test-graph",
        parser_version="ast_test",
        source_commit=None,
    )
    return extractor.extract()


def _node_ids(delta: CodeGraphDelta) -> set[str]:
    return {n.id for n in delta.nodes}


def _edge_labels(delta: CodeGraphDelta) -> list[tuple[str, str, str]]:
    return [(e.source, e.rel_type, e.target) for e in delta.edges]


def _node_by_id(delta: CodeGraphDelta, node_id: str) -> CodeNode | None:
    for n in delta.nodes:
        if n.id == node_id:
            return n
    return None


def _edges_from(delta: CodeGraphDelta, source: str, rel_type: str | None = None) -> list[CodeEdge]:
    return [
        e for e in delta.edges
        if e.source == source and (rel_type is None or e.rel_type == rel_type)
    ]


def _edges_to(delta: CodeGraphDelta, target: str, rel_type: str | None = None) -> list[CodeEdge]:
    return [
        e for e in delta.edges
        if e.target == target and (rel_type is None or e.rel_type == rel_type)
    ]


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

SAMPLE_MODULE = '''
"""A sample module for testing."""

import os
import sys
from pathlib import Path
from typing import Optional, List

import requests

MY_CONSTANT = 42

class MyClass:
    """A sample class."""

    class_attr: int = 10

    def __init__(self, value: int) -> None:
        self.value = value

    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

    @property
    def doubled(self) -> int:
        return self.value * 2

    @staticmethod
    def static_method() -> None:
        pass

    @classmethod
    def class_method(cls) -> "MyClass":
        return cls()

class ChildClass(MyClass):
    def greet(self, name: str) -> str:
        return super().greet(name).upper()

@dataclass
class Person:
    name: str
    age: int

class Status(Enum):
    ACTIVE = 1
    INACTIVE = 2

class User(BaseModel):
    username: str
    email: str

def top_level_function(items: List[int]) -> int:
    total = 0
    for item in items:
        total += item
    return total

async def async_fetch(url: str) -> dict:
    import aiohttp
    return {}

def caller() -> None:
    result = top_level_function([1, 2, 3])
    obj = MyClass(42)
    msg = obj.greet("world")
    print(msg)

@app.get("/users/")
def list_users() -> List[User]:
    return []

@app.post("/users/")
def create_user(user: User) -> User:
    return user

__all__ = ["MyClass", "top_level_function", "Person"]
'''


class TestBasicExtraction:
    """Smoke tests for the PythonFactExtractor."""

    def test_module_and_file_nodes(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert "mymodule" in ids
        assert str(Path("/tmp/repo/mymodule.py").resolve()) in ids

    def test_classes_detected(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert "mymodule.MyClass" in ids
        assert "mymodule.ChildClass" in ids
        assert "mymodule.Person" in ids
        assert "mymodule.Status" in ids
        assert "mymodule.User" in ids

    def test_methods_detected(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert "mymodule.MyClass.__init__" in ids
        assert "mymodule.MyClass.greet" in ids
        assert "mymodule.MyClass.doubled" in ids
        assert "mymodule.MyClass.static_method" in ids
        assert "mymodule.MyClass.class_method" in ids

    def test_functions_detected(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert "mymodule.top_level_function" in ids
        assert "mymodule.async_fetch" in ids
        assert "mymodule.caller" in ids
        assert "mymodule.list_users" in ids
        assert "mymodule.create_user" in ids

    def test_imports_detected(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert any("import:os" in nid for nid in ids)
        assert any("import:sys" in nid for nid in ids)
        assert any("import:pathlib.Path" in nid for nid in ids)

    def test_contains_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule", "CONTAINS", "mymodule.MyClass") for e in edges
        )
        assert any(
            e == ("mymodule.MyClass", "CONTAINS", "mymodule.MyClass.greet")
            for e in edges
        )

    def test_defines_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule", "DEFINES", "mymodule.MyClass") for e in edges
        )
        assert any(
            e == ("mymodule", "DEFINES", "mymodule.top_level_function")
            for e in edges
        )

    def test_inherits_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule.ChildClass", "INHERITS", "MyClass") for e in edges
        )

    def test_overrides_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e[0] == "mymodule.ChildClass.greet" and e[1] == "OVERRIDES"
            for e in edges
        )

    def test_calls_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule.caller", "CALLS", "top_level_function")
            for e in edges
        )
        assert any(
            e == ("mymodule.caller", "CALLS", "MyClass") for e in edges
        )

    def test_decorated_by_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule.Person", "DECORATED_BY", "dataclass")
            for e in edges
        )
        assert any(
            e == ("mymodule.MyClass.doubled", "DECORATED_BY", "property")
            for e in edges
        )

    def test_returns_type_edges(self):
        delta = _extract(SAMPLE_MODULE)
        edges = _edge_labels(delta)

        assert any(
            e[0] == "mymodule.top_level_function" and e[1] == "RETURNS_TYPE"
            for e in edges
        )

    def test_async_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.async_fetch")
        assert node is not None
        assert node.properties.get("is_async") is True

    def test_property_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.MyClass.doubled")
        assert node is not None
        assert node.properties.get("is_property") is True

    def test_staticmethod_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.MyClass.static_method")
        assert node is not None
        assert node.properties.get("is_staticmethod") is True

    def test_classmethod_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.MyClass.class_method")
        assert node is not None
        assert node.properties.get("is_classmethod") is True

    def test_dataclass_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.Person")
        assert node is not None
        assert node.properties.get("is_dataclass") is True

    def test_enum_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.Status")
        assert node is not None
        assert node.properties.get("is_enum") is True

    def test_pydantic_marked(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.User")
        assert node is not None
        assert node.properties.get("is_pydantic_model") is True

    def test_route_nodes_created(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)

        assert any("list_users:route" in nid for nid in ids)
        assert any("create_user:route" in nid for nid in ids)

    def test_route_properties(self):
        delta = _extract(SAMPLE_MODULE)
        for nid in [nid for nid in _node_ids(delta) if ":route:" in nid]:
            node = _node_by_id(delta, nid)
            assert "route_path" in node.properties
            assert "route_method" in node.properties

    def test_module_level_variable(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)
        assert any("MY_CONSTANT" in nid for nid in ids)

    def test_class_attribute(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)
        assert any("class_attr" in nid for nid in ids)

    def test_line_numbers_present(self):
        delta = _extract(SAMPLE_MODULE)
        for node in delta.nodes:
            if node.id == "mymodule.MyClass":
                assert "line_start" in node.properties
                assert "line_end" in node.properties
                break

    def test_snippet_present(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.top_level_function")
        assert node is not None
        assert "snippet" in node.properties
        assert "def top_level_function" in node.properties["snippet"]

    def test_complexity_computed(self):
        delta = _extract(SAMPLE_MODULE)
        node = _node_by_id(delta, "mymodule.top_level_function")
        assert node is not None
        assert node.properties.get("complexity", 0) >= 2  # base + for loop

    def test_unused_import_diagnostic(self):
        delta = _extract(SAMPLE_MODULE)
        ids = _node_ids(delta)
        # 'requests' is imported but never used -> should have diagnostic
        assert any("requests" in nid and "diagnostic" in nid for nid in ids)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Tests for unusual or malformed inputs."""

    def test_empty_file(self):
        delta = _extract("")
        assert len(delta.nodes) == 2  # CodeFile + CodeModule
        assert len(delta.edges) == 1  # file CONTAINS module

    def test_syntax_error(self):
        delta = _extract("def broken(\n")
        assert delta.parser_version.startswith("error:syntax:")
        assert len(delta.nodes) == 0
        assert len(delta.edges) == 0

    def test_import_all_used(self):
        source = """
import os
print(os.getcwd())
"""
        delta = _extract(source)
        ids = _node_ids(delta)
        # os is used -> no diagnostic for it
        assert not any("diagnostic" in nid and "os" in nid for nid in ids)

    def test_nested_class(self):
        source = """
class Outer:
    class Inner:
        def method(self) -> int:
            return 1
"""
        delta = _extract(source)
        ids = _node_ids(delta)
        assert "mymodule.Outer" in ids
        assert "mymodule.Outer.Inner" in ids
        assert "mymodule.Outer.Inner.method" in ids

    def test_type_annotations_on_methods(self):
        source = """
class Calculator:
    def add(self, a: int, b: int) -> int:
        return a + b
"""
        delta = _extract(source)
        edges = _edge_labels(delta)
        assert any(
            e[0] == "mymodule.Calculator.add" and e[1] == "USES_TYPE"
            for e in edges
        )
        assert any(
            e[0] == "mymodule.Calculator.add" and e[1] == "RETURNS_TYPE"
            for e in edges
        )

    def test_deep_import_path(self):
        source = """
from a.b.c.d import something
something()
"""
        delta = _extract(source)
        ids = _node_ids(delta)
        assert any("import" in nid for nid in ids)
        # Import should be marked as used
        assert not any(
            "diagnostic" in nid and "something" in nid for nid in ids
        )

    def test_annotations_on_variables(self):
        source = """
x: int = 10
y: str = "hello"
"""
        delta = _extract(source)
        ids = _node_ids(delta)
        assert any("var:x" in nid for nid in ids)
        assert any("var:y" in nid for nid in ids)


# ---------------------------------------------------------------------------
# PythonLanguageAdapter tests
# ---------------------------------------------------------------------------

class TestPythonLanguageAdapter:
    """Tests for the LanguageAdapter implementation."""

    def test_adapter_attributes(self):
        adapter = PythonLanguageAdapter()
        assert adapter.language == "python"
        assert adapter.extensions == (".py",)
        assert adapter.parser_version.startswith("ast_")

    def test_discover_files(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cached.cpython-312.pyc").write_text("")

        from ariadne_graph.core.config import AnalyzerConfig
        config = AnalyzerConfig(repo_root=tmp_path)
        adapter = PythonLanguageAdapter()
        paths = adapter.discover_files(tmp_path, config)
        names = sorted([p.name for p in paths])
        assert names == ["a.py", "b.py"]

    def test_extract_file(self, tmp_path: Path):
        src = tmp_path / "testmod.py"
        src.write_text("\nclass Foo:\n    def bar(self) -> int:\n        return 1\n")

        context = ExtractionContext(graph_id="g1", repo_root=tmp_path)
        adapter = PythonLanguageAdapter()
        delta = adapter.extract_file(src, context)

        assert delta.graph_id == "g1"
        assert delta.file_path == str(src)
        assert delta.content_hash != ""
        assert delta.parser_version.startswith("ast_")

        ids = _node_ids(delta)
        assert "testmod.Foo" in ids
        assert "testmod.Foo.bar" in ids

    def test_extract_file_source_commit(self, tmp_path: Path):
        src = tmp_path / "mod.py"
        src.write_text("x = 1")

        context = ExtractionContext(
            graph_id="g1", repo_root=tmp_path, source_commit="abc123"
        )
        adapter = PythonLanguageAdapter()
        delta = adapter.extract_file(src, context)

        for node in delta.nodes:
            assert node.properties.get("source_commit") == "abc123"

    def test_extract_file_edge_owner(self, tmp_path: Path):
        src = tmp_path / "mod.py"
        src.write_text("class C:\n    pass\n")

        context = ExtractionContext(graph_id="g1", repo_root=tmp_path)
        adapter = PythonLanguageAdapter()
        delta = adapter.extract_file(src, context)

        for edge in delta.edges:
            assert edge.properties.get("owner_file_path") == str(src)


# ---------------------------------------------------------------------------
# Unique node IDs
# ---------------------------------------------------------------------------


class TestUniqueNodeIds:
    """Ensure the extractor emits one distinct ID per node in a file."""

    SOURCE_WITH_COLLISIONS = '''
import json
import json

ontology_mappings = {}
ontology_mappings = []

class Box:
    value: int
    value: str

    def size(self) -> int:
        return 1

    @property
    def size(self):
        return 2

    @size.setter
    def size(self, val):
        pass

def helper():
    p = 1
    p = 2
    return p
'''

    def test_all_node_ids_are_unique(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS)
        ids = [n.id for n in delta.nodes]
        assert len(ids) == len(set(ids)), (
            f"duplicate node ids found: {len(ids) - len(set(ids))} collisions"
        )

    def test_repeated_imports_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS)
        imports = [n for n in delta.nodes if "CodeImport" in n.labels]
        json_imports = [n for n in imports if n.properties.get("name") == "json"]
        assert len(json_imports) == 2, f"expected 2 json imports, got {len(json_imports)}"
        assert len({n.id for n in json_imports}) == 2, "import IDs collide"

    def test_reassigned_variables_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS)
        vars = [n for n in delta.nodes if "CodeVariable" in n.labels]
        om_vars = [n for n in vars if n.properties.get("name") == "ontology_mappings"]
        assert len(om_vars) == 2, f"expected 2 ontology_mappings variables, got {len(om_vars)}"
        assert len({n.id for n in om_vars}) == 2, "variable IDs collide"

    def test_overloaded_methods_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS)
        methods = [n for n in delta.nodes if "CodeMethod" in n.labels]
        size_methods = [n for n in methods if n.properties.get("name") == "size"]
        assert len(size_methods) == 3, f"expected 3 size methods, got {len(size_methods)}"
        assert len({n.id for n in size_methods}) == 3, "method IDs collide"

    def test_repeated_class_attributes_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS)
        attrs = [n for n in delta.nodes if "CodeAttribute" in n.labels]
        value_attrs = [n for n in attrs if n.properties.get("name") == "value"]
        assert len(value_attrs) == 2, f"expected 2 value attributes, got {len(value_attrs)}"
        assert len({n.id for n in value_attrs}) == 2, "attribute IDs collide"


# ---------------------------------------------------------------------------
# Determinism test
# ---------------------------------------------------------------------------

class TestDeterminism:
    """Same input must produce identical output (stable IDs)."""

    def test_same_file_same_nodes(self):
        source = "class A:\n    def b(self): pass\n"
        delta1 = _extract(source)
        delta2 = _extract(source)
        ids1 = sorted(n.id for n in delta1.nodes)
        ids2 = sorted(n.id for n in delta2.nodes)
        assert ids1 == ids2

    def test_same_file_same_edges(self):
        source = "class A:\n    def b(self): pass\n"
        delta1 = _extract(source)
        delta2 = _extract(source)
        edges1 = sorted((e.source, e.rel_type, e.target) for e in delta1.edges)
        edges2 = sorted((e.source, e.rel_type, e.target) for e in delta2.edges)
        assert edges1 == edges2
