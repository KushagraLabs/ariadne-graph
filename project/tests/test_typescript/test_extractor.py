"""Tests for the TypeScript/TSX Tree-sitter fact extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne_graph.core.models import CodeEdge, CodeGraphDelta, CodeNode
from ariadne_graph.languages.base import ExtractionContext
from ariadne_graph.languages.typescript.adapter import TypeScriptLanguageAdapter
from ariadne_graph.languages.typescript.extractor import (
    HAS_TREE_SITTER,
    TypeScriptFactExtractor,
)

if not HAS_TREE_SITTER:
    pytest.skip("tree-sitter-typescript is not installed", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract(
    source: str,
    repo_root: Path | None = None,
    file_name: str = "mymodule.ts",
) -> CodeGraphDelta:
    """Run the extractor on a source string and return the delta."""
    if repo_root is None:
        repo_root = Path("/tmp/repo")
    file_path = repo_root / file_name
    extractor = TypeScriptFactExtractor(
        source=source,
        file_path=file_path,
        repo_root=repo_root,
        graph_id="test-graph",
        parser_version="tree-sitter-typescript_test",
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


def _edges_from(
    delta: CodeGraphDelta, source: str, rel_type: str | None = None
) -> list[CodeEdge]:
    return [
        e for e in delta.edges if e.source == source and (rel_type is None or e.rel_type == rel_type)
    ]


def _edges_to(
    delta: CodeGraphDelta, target: str, rel_type: str | None = None
) -> list[CodeEdge]:
    return [
        e for e in delta.edges if e.target == target and (rel_type is None or e.rel_type == rel_type)
    ]


def _labels_for(delta: CodeGraphDelta, node_id: str) -> list[str]:
    node = _node_by_id(delta, node_id)
    return node.labels if node is not None else []


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

SAMPLE_TS = '''
import { helper } from "./helpers";
import * as utils from "./utils";
import React from "react";

export interface Config {
  port: number;
  debug?: boolean;
}

export type ID = string;

export class BaseService {
  constructor(public endpoint: string) {}

  fetch(id: ID): Promise<string> {
    return helper(endpoint, id);
  }
}

export class UserService extends BaseService implements Config {
  fetch(id: ID): Promise<string> {
    return utils.getUser(id);
  }
}

export function createUser(name: string): UserService {
  return new UserService("/users");
}

export const useCounter = (initial: number) => {
  const [count, setCount] = React.useState(initial);
  return { count, setCount };
};

const unused = require("fs");
'''


class TestBasicExtraction:
    """Smoke tests for TypeScriptFactExtractor."""

    def test_file_and_module_nodes(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)
        file_id = str(Path("/tmp/repo/mymodule.ts").resolve())
        assert file_id in ids
        assert "mymodule" in ids

    def test_imports_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert any("import:./helpers:helper:helper" in nid for nid in ids)
        assert any("import:./utils:*:utils" in nid for nid in ids)
        assert any("import:react:React:React" in nid for nid in ids)

    def test_interfaces_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert "mymodule.Config" in ids

    def test_type_aliases_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert "mymodule:type:ID" in ids

    def test_classes_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert "mymodule.BaseService" in ids
        assert "mymodule.UserService" in ids

    def test_methods_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert "mymodule.BaseService.fetch" in ids
        assert "mymodule.UserService.fetch" in ids

    def test_functions_detected(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        assert "mymodule.createUser" in ids

    def test_contains_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule", "CONTAINS", "mymodule.BaseService") for e in edges)
        assert any(
            e == ("mymodule.BaseService", "CONTAINS", "mymodule.BaseService.fetch")
            for e in edges
        )

    def test_defines_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule", "DEFINES", "mymodule.createUser") for e in edges)

    def test_inherits_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule.UserService", "INHERITS", "BaseService") for e in edges)

    def test_implements_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule.UserService", "IMPLEMENTS", "Config") for e in edges)

    def test_overrides_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(
            e[0] == "mymodule.UserService.fetch" and e[1] == "OVERRIDES" for e in edges
        )

    def test_calls_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule.createUser", "CALLS", "UserService") for e in edges)

    def test_exports_detected(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(
            e == ("mymodule", "EXPORTS", "mymodule.UserService") for e in edges
        )
        assert any(
            e == ("mymodule", "EXPORTS", "mymodule.createUser") for e in edges
        )

    def test_export_star_from_detected(self):
        source = """
export * from "./api";
export * from "./helpers";
"""
        delta = _extract(source, file_name="reexports.ts")
        export_nodes = [n for n in delta.nodes if "CodeExport" in n.labels]
        assert len(export_nodes) == 2, f"expected 2 reexport_all nodes, got {len(export_nodes)}"
        ids = {n.id for n in export_nodes}
        assert len(ids) == len(export_nodes), "export * from nodes collide"

    def test_export_star_and_named_reexport_are_distinct(self):
        """export * from "x" and export { foo } from "x" must not share a node."""
        source = """
export * from "./api";
export { foo } from "./api";
"""
        delta = _extract(source, file_name="reexports.ts")
        ids = [n.id for n in delta.nodes]
        assert len(ids) == len(set(ids)), "duplicate node ids emitted for mixed re-exports"
        export_nodes = [n for n in delta.nodes if "CodeExport" in n.labels]
        assert len(export_nodes) == 2, f"expected 2 export nodes, got {len(export_nodes)}"
        types = {n.properties.get("export_type") for n in export_nodes}
        assert types == {"reexport_all", "reexport_named"}

    def test_type_edges(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(
            e[0] == "mymodule.BaseService.fetch" and e[1] == "RETURNS_TYPE"
            for e in edges
        )

    def test_unused_import_diagnostic(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        # "fs" require import is not referenced anywhere
        assert any("diagnostic:unused" in nid for nid in ids)


# ---------------------------------------------------------------------------
# React / TSX extraction
# ---------------------------------------------------------------------------

SAMPLE_TSX = '''
import React, { useState } from "react";

export const Counter: React.FC = () => {
  const [count, setCount] = useState(0);
  return <div onClick={() => setCount(c => c + 1)}>{count}</div>;
};

export function useToggle(initial: boolean) {
  const [value, setValue] = useState(initial);
  return [value, setValue];
}
'''


class TestTsxExtraction:
    """Smoke tests for TSX-specific extraction."""

    def test_react_component_label(self):
        delta = _extract(SAMPLE_TSX, file_name="Counter.tsx")
        labels = _labels_for(delta, "Counter.Counter")
        assert "CodeReactComponent" in labels

    def test_hook_label(self):
        delta = _extract(SAMPLE_TSX, file_name="hooks.tsx")
        labels = _labels_for(delta, "hooks.useToggle")
        assert "CodeHook" in labels


# ---------------------------------------------------------------------------
# Unique node IDs
# ---------------------------------------------------------------------------


class TestUniqueNodeIds:
    """Ensure the extractor emits one distinct ID per node in a file."""

    SOURCE_WITH_COLLISIONS = '''
const outer = () => {
  const p = 1;
  const cb = () => {};
  return p;
};

const inner = () => {
  const p = 2;
  const cb = () => {};
  return p;
};

export const handlers = [
  () => {},
  () => {},
  () => {},
];

export class Box {
  p: number;
  p: string;
}

export interface IBox {
  p: number;
  p: string;
}
'''

    def test_all_node_ids_are_unique(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS, file_name="collisions.ts")
        ids = [n.id for n in delta.nodes]
        assert len(ids) == len(set(ids)), (
            f"duplicate node ids found: {len(ids) - len(set(ids))} collisions"
        )

    def test_anonymous_functions_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS, file_name="collisions.ts")
        anon_nodes = [n for n in delta.nodes if n.properties.get("name") == "<anonymous>"]
        # Three arrow functions inside the `handlers` array are anonymous.
        assert len(anon_nodes) >= 3, (
            f"expected at least 3 anonymous functions, got {len(anon_nodes)}"
        )
        ids = {n.id for n in anon_nodes}
        assert len(ids) == len(anon_nodes), "anonymous function IDs collide"

    def test_repeated_variable_names_are_not_collapsed(self):
        delta = _extract(self.SOURCE_WITH_COLLISIONS, file_name="collisions.ts")
        p_vars = [n for n in delta.nodes if "CodeVariable" in n.labels and n.properties.get("name") == "p"]
        assert len(p_vars) >= 2, f"expected at least 2 'p' variables, got {len(p_vars)}"
        ids = {n.id for n in p_vars}
        assert len(ids) == len(p_vars), "variable IDs collide"


NESTED_SOURCE = """
function a() {
  function b() {
    function c() {
      function d() {
        console.log("deep");
      }
    }
  }
}
"""


class TestNoExponentialRetraversal:
    """Regression test for the 2^n duplicate-node bug in expression traversal.

    When _visit_expression both dispatched to a handler and then recursed over
    all children, nested functions/calls were visited twice per level. A file
    with just a few levels of nesting would emit the same node thousands of
    times, bloating node_fts and indexing time.
    """

    def test_nested_functions_emit_one_node_per_id(self):
        delta = _extract(NESTED_SOURCE, file_name="nested.ts")
        ids = [n.id for n in delta.nodes]
        distinct = set(ids)
        assert len(ids) == len(distinct), (
            f"duplicate node ids emitted: {len(ids)} total, {len(distinct)} distinct"
        )

    def test_nested_calls_emit_one_node_per_id(self):
        source = """
function run() {
  a(b(c(d(e()))));
}
"""
        delta = _extract(source, file_name="nested_calls.ts")
        ids = [n.id for n in delta.nodes]
        distinct = set(ids)
        assert len(ids) == len(distinct), (
            f"duplicate node ids emitted: {len(ids)} total, {len(distinct)} distinct"
        )

    def test_nested_function_calls_are_tracked(self):
        """Ensure the traversal fix does not drop nested call edges."""
        source = """
function outer() {
  inner(helper(nested()));
}
"""
        delta = _extract(source, file_name="nested_call_edges.ts")
        edges = _edge_labels(delta)
        assert any(e == ("nested_call_edges.outer", "CALLS", "inner") for e in edges)
        assert any(e == ("nested_call_edges.outer", "CALLS", "helper") for e in edges)
        assert any(e == ("nested_call_edges.outer", "CALLS", "nested") for e in edges)

    def test_realistic_nested_fixture_has_no_duplicate_ids(self):
        """Belt-and-suspenders: a realistic, nested file must emit each id once.

        The original shallow unique-id fixture did not trigger the 2^n
        re-traversal bug; this fixture mixes nested functions, classes, methods,
        arrow callbacks, and chained calls so duplicate emissions would be caught.
        """
        source = """
import { fetchUser } from "./api";
import { log } from "./logger";

export class UserStore {
  private cache: Map<string, any> = new Map();

  async load(id: string) {
    const wrapper = () => {
      const inner = () => fetchUser(id);
      return inner();
    };
    const user = await wrapper();
    this.cache.set(id, user);
    log(user);
    return user;
  }

  clear() {
    this.cache.clear();
  }
}

export function buildStore() {
  return new UserStore();
}
"""
        delta = _extract(source, file_name="UserStore.ts")
        ids = [n.id for n in delta.nodes]
        distinct = set(ids)
        assert len(ids) == len(distinct), (
            f"duplicate node ids emitted: {len(ids)} total, {len(distinct)} distinct"
        )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class TestTypeScriptLanguageAdapter:
    """Smoke tests for TypeScriptLanguageAdapter.extract_file."""

    def test_extract_file_sets_content_hash(self, tmp_path: Path):
        source = "export function foo(): number { return 1; }\n"
        file_path = tmp_path / "src" / "foo.ts"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(source)

        adapter = TypeScriptLanguageAdapter()
        context = ExtractionContext(
            graph_id="test-graph",
            repo_root=tmp_path,
            source_commit=None,
        )
        delta = adapter.extract_file(file_path, context)

        assert delta.content_hash != ""
        assert delta.parser_version.startswith("tree-sitter-typescript_")
        assert any("CodeFunction" in n.labels for n in delta.nodes)


    def test_extract_file_resolves_tsconfig_alias(self, tmp_path: Path):
        """Imports using a tsconfig path alias record a resolved_source."""
        import json

        repo = tmp_path
        helper_file = repo / "src" / "utils" / "helper.ts"
        helper_file.parent.mkdir(parents=True)
        helper_file.write_text("export const helper = () => {};")

        (repo / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "baseUrl": ".",
                        "paths": {"@/*": ["src/*"]},
                    }
                }
            )
        )

        consumer_file = repo / "src" / "consumer.ts"
        consumer_file.write_text('import { helper } from "@/utils/helper";\n')

        adapter = TypeScriptLanguageAdapter()
        context = ExtractionContext(
            graph_id="test-graph",
            repo_root=repo,
            source_commit=None,
        )
        delta = adapter.extract_file(consumer_file, context)

        import_nodes = [n for n in delta.nodes if "CodeImport" in n.labels]
        assert import_nodes
        assert import_nodes[0].properties.get("resolved_source") == str(helper_file)
        assert import_nodes[0].properties.get("resolved_module") == "utils.helper"
