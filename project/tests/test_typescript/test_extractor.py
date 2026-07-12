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


def _edges_from(delta: CodeGraphDelta, source: str, rel_type: str | None = None) -> list[CodeEdge]:
    return [
        e
        for e in delta.edges
        if e.source == source and (rel_type is None or e.rel_type == rel_type)
    ]


def _edges_to(delta: CodeGraphDelta, target: str, rel_type: str | None = None) -> list[CodeEdge]:
    return [
        e
        for e in delta.edges
        if e.target == target and (rel_type is None or e.rel_type == rel_type)
    ]


def _labels_for(delta: CodeGraphDelta, node_id: str) -> list[str]:
    node = _node_by_id(delta, node_id)
    return node.labels if node is not None else []


def test_scipless_relative_import_resolves_source(tmp_path: Path) -> None:
    """SCIP-less hard case (bead 1u5): a relative import ``./b`` must record its
    ``resolved_source`` from the BASE extractor alone — no SCIP, no enricher.

    Relative-import resolution used to live only in TreeSitterEnricher (SCIP
    path), so a genuinely SCIP-less project produced no file->file dep edge for
    the common ``./x`` import and the fallback recovered nothing. The extractor
    now shares the same resolver. Real files on disk are required because
    resolution checks existence.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.ts").write_text("export const x = 1;\n")
    a = tmp_path / "src" / "a.ts"
    a.write_text("import { x } from './b';\n")

    delta = _extract("import { x } from './b';\n", repo_root=tmp_path, file_name="src/a.ts")

    imports = [n for n in delta.nodes if "CodeImport" in n.labels]
    assert imports, "no CodeImport node emitted"
    target = str(tmp_path / "src" / "b.ts")
    assert any(n.properties.get("resolved_source") == target for n in imports), (
        f"relative import did not resolve to {target}; "
        f"got {[n.properties.get('resolved_source') for n in imports]}"
    )


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

SAMPLE_TS = """
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
"""


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
            e == ("mymodule.BaseService", "CONTAINS", "mymodule.BaseService.fetch") for e in edges
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

        assert any(e[0] == "mymodule.UserService.fetch" and e[1] == "OVERRIDES" for e in edges)

    def test_no_ast_calls_edges(self):
        # Bead code_hygiene_mcp-0b9 (+ codex): the AST path emits NO CALLS edge.
        # A raw-string target dangles; an import-site target is semantically
        # wrong (calls the import declaration, not the target symbol). This
        # single-file extractor cannot know the target symbol's node id in
        # another file -- SCIP supplies resolved CALLS. Better no edge than a
        # misleading one for dependency tracing.
        delta = _extract(SAMPLE_TS)
        assert not [e for e in delta.edges if e.rel_type == "CALLS"]

    def test_call_and_new_still_mark_imports_used(self):
        # The reason the extractor still walks callees despite emitting no CALLS
        # edge: value-position uses (helper(), new UserService via createUser)
        # must mark their imports used so unused-import detection stays correct.
        source = """
import { helper } from "./helpers";
import { widget } from "./widget";

export function run(): void {
  helper();
  new widget();
}
"""
        delta = _extract(source, file_name="uses.ts")
        unused = [
            n
            for n in delta.nodes
            if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
        ]
        unused_names = {n.properties.get("message", "") for n in unused}
        assert not any("helper" in m for m in unused_names), "helper() call should mark import used"
        assert not any("widget" in m for m in unused_names), "new widget() should mark import used"

    def test_export_default_call_marks_import_used(self):
        # Bead code_hygiene_mcp-bhe: `export default <call>(...)` puts the call
        # under the export_statement's `value` field (not `declaration`), so the
        # callee was never walked and its import was falsely flagged unused.
        source = """
import { defineConfig } from "drizzle-kit";
import { unusedThing } from "./other";

export default defineConfig({ out: "./x" });
"""
        delta = _extract(source, file_name="drizzle.config.ts")
        unused = [
            n
            for n in delta.nodes
            if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
        ]
        unused_names = {n.properties.get("message", "") for n in unused}
        assert not any("defineConfig" in m for m in unused_names), (
            "export default defineConfig(...) should mark defineConfig used"
        )
        # Guard: a genuinely unused import in the same file is still reported.
        assert any("unusedThing" in m for m in unused_names), (
            "genuinely unused import must still be flagged"
        )

    def test_export_default_identifier_marks_import_used(self):
        # Bead code_hygiene_mcp-bhe (codex follow-ups): a default export names an
        # imported binding through arbitrary wrapping syntax -- bare identifier,
        # parens, `as` cast, object shorthand. Each is a value-position use, but
        # _visit_expression only marks call/new callees, so each must be handled.
        # Every import below is referenced somewhere in the `export default`
        # expression: bare shorthand (alpha), an `as` cast (gamma), a plain
        # value (delta), and a parenthesized identifier (eps).
        source = """
import alpha from "./alpha";
import gamma from "./gamma";
import delta from "./delta";
import eps from "./eps";
import { unusedThing } from "./other";

export default { alpha, wrapped: (gamma as Config), d: delta, e: (eps) };
"""
        delta = _extract(source, file_name="index.ts")
        unused = [
            n
            for n in delta.nodes
            if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
        ]
        unused_names = {n.properties.get("message", "") for n in unused}
        for used in ("alpha", "gamma", "delta", "eps"):
            assert not any(f"'{used}'" in m for m in unused_names), (
                f"{used} is referenced in the export default expression; not unused. "
                f"unused={unused_names}"
            )
        # Guard: a genuinely unused import is still flagged.
        assert any("unusedThing" in m for m in unused_names), (
            "genuinely unused import must still be flagged"
        )

    def test_export_default_does_not_mark_shadowing_binding_used(self):
        # codex follow-up: an import shadowed by a parameter inside a default
        # export function is NOT a use of the import -- the walker must stop at
        # scope boundaries so it doesn't suppress a real unused-import warning.
        source = """
import foo from "./foo";

export default (foo) => foo;
"""
        delta = _extract(source, file_name="index.ts")
        unused_names = {
            n.properties.get("message", "")
            for n in delta.nodes
            if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
        }
        assert any("'foo'" in m for m in unused_names), (
            "foo is only a shadowing parameter here; the import IS unused and must be flagged"
        )

    def test_export_default_computed_member_marks_index_import_used(self):
        # codex follow-up: `local[key]` is a subscript_expression; the index
        # identifier `key` is a value-position use of its import.
        source = """
import key from "./key";
import { unusedThing } from "./other";

const local = {};
export default local[key];
"""
        delta = _extract(source, file_name="index.ts")
        unused_names = {
            n.properties.get("message", "")
            for n in delta.nodes
            if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
        }
        assert not any("'key'" in m for m in unused_names), (
            f"key is used as a computed index; not unused. unused={unused_names}"
        )
        assert any("unusedThing" in m for m in unused_names), "guard: real unused still flagged"

    def test_exports_detected(self):
        delta = _extract(SAMPLE_TS)
        edges = _edge_labels(delta)

        assert any(e == ("mymodule", "EXPORTS", "mymodule.UserService") for e in edges)
        assert any(e == ("mymodule", "EXPORTS", "mymodule.createUser") for e in edges)

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

    def test_no_ast_type_edges(self):
        # Bead code_hygiene_mcp-0b9: USES_TYPE/RETURNS_TYPE AST edges removed
        # (unresolved string targets). The return_type node property remains.
        delta = _extract(SAMPLE_TS)
        assert not [e for e in delta.edges if e.rel_type in ("USES_TYPE", "RETURNS_TYPE")]
        fetch = _node_by_id(delta, "mymodule.BaseService.fetch")
        assert fetch is not None and fetch.properties.get("return_type") == "Promise<string>"

    def test_unused_import_diagnostic(self):
        delta = _extract(SAMPLE_TS)
        ids = _node_ids(delta)

        # "fs" require import is not referenced anywhere
        assert any("diagnostic:unused" in nid for nid in ids)


# ---------------------------------------------------------------------------
# React / TSX extraction
# ---------------------------------------------------------------------------

SAMPLE_TSX = """
import React, { useState } from "react";

export const Counter: React.FC = () => {
  const [count, setCount] = useState(0);
  return <div onClick={() => setCount(c => c + 1)}>{count}</div>;
};

export function useToggle(initial: boolean) {
  const [value, setValue] = useState(initial);
  return [value, setValue];
}
"""


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

    SOURCE_WITH_COLLISIONS = """
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
"""

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
        p_vars = [
            n for n in delta.nodes if "CodeVariable" in n.labels and n.properties.get("name") == "p"
        ]
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

    def test_nested_function_calls_are_visited(self):
        """The traversal fix must reach every nested callee.

        AST CALLS edges were removed (bead code_hygiene_mcp-0b9), so we assert
        traversal completeness via unused-import marking instead: each nested
        callee is an import, and all three must be marked used (none reported
        unused), which only happens if the visitor descends into every nesting
        level.
        """
        source = """
import { inner, helper, nested } from "./calls";

function outer() {
  inner(helper(nested()));
}
"""
        delta = _extract(source, file_name="nested_call_edges.ts")
        unused = _unused_import_names(delta)
        assert "inner" not in unused
        assert "helper" not in unused
        assert "nested" not in unused

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
# Bead code_hygiene_mcp-0b9: AST CALLS/USES_TYPE/RETURNS_TYPE are dangling noise
# ---------------------------------------------------------------------------


class TestNoDanglingRelationEdges:
    """CALLS/USES_TYPE/RETURNS_TYPE must never target an unresolved raw string.

    The tree-sitter AST path used to emit these edges with the callee/type
    literal source text as the target (e.g. 'console.error'), which resolves to
    no node -- 100% dangling. The SCIP translator emits the resolved forms, so
    the AST forms are removed. Every emitted edge target must resolve to a node
    the extractor also emits, or the relation must be absent.
    """

    _FIXTURE = """
import { UserService } from "./services";
import type { Config } from "./config";

export class Store {
  cache: Map<string, number> = new Map();

  load(cfg: Config): Promise<UserService> {
    console.error("boom");
    process.exit(1);
    return new UserService(cfg).fetch();
  }
}

export function make(): Store {
  return new Store();
}
"""

    def test_no_calls_uses_returns_edges_with_string_targets(self):
        delta = _extract(self._FIXTURE, file_name="store.ts")
        node_ids = _node_ids(delta)
        offenders = [
            (e.source, e.rel_type, e.target)
            for e in delta.edges
            if e.rel_type in ("CALLS", "USES_TYPE", "RETURNS_TYPE") and e.target not in node_ids
        ]
        assert not offenders, f"dangling relation edges: {offenders}"

    def test_extraction_does_not_error(self):
        # Fixture with member-expression callees exercised the deleted path.
        delta = _extract(self._FIXTURE, file_name="store.ts")
        assert "store.Store" in _node_ids(delta)


# ---------------------------------------------------------------------------
# Bead code_hygiene_mcp-df7: type-position usage counts against unused-import
# ---------------------------------------------------------------------------


def _unused_import_names(delta: CodeGraphDelta) -> set[str]:
    return {
        n.properties.get("name")
        for n in delta.nodes
        if "CodeDiagnostic" in n.labels and n.properties.get("rule") == "unused_import"
    }


class TestTypePositionImportUsage:
    """Imports used only in type positions must not be flagged unused."""

    def test_import_type_used_as_annotation(self):
        source = """
import type { LifeDomain } from "./domains";

export function classify(d: LifeDomain): string {
  return String(d);
}
"""
        delta = _extract(source, file_name="classify.ts")
        assert "LifeDomain" not in _unused_import_names(delta)

    def test_inline_type_specifier_used_as_indexed_access(self):
        source = """
import { type SecondaryProgressionsResult } from "./astro";

export type Sun = SecondaryProgressionsResult["sun"];
"""
        delta = _extract(source, file_name="astro.ts")
        assert "SecondaryProgressionsResult" not in _unused_import_names(delta)

    def test_import_used_only_as_cast(self):
        source = """
import { Foo } from "./foo";

export function coerce(x: unknown) {
  return x as Foo;
}
"""
        delta = _extract(source, file_name="coerce.ts")
        assert "Foo" not in _unused_import_names(delta)

    def test_import_used_only_as_class_extends(self):
        source = """
import { BaseWidget } from "./base";

export class Widget extends BaseWidget {}
"""
        delta = _extract(source, file_name="widget.ts")
        assert "BaseWidget" not in _unused_import_names(delta)

    def test_import_used_only_as_class_implements(self):
        source = """
import { Renderable } from "./iface";

export class View implements Renderable {}
"""
        delta = _extract(source, file_name="view.ts")
        assert "Renderable" not in _unused_import_names(delta)

    def test_import_used_only_as_generic_argument(self):
        source = """
import { Widget } from "./widget";

export function first(items: Array<Widget>): Widget {
  return items[0];
}
"""
        delta = _extract(source, file_name="generic.ts")
        assert "Widget" not in _unused_import_names(delta)

    def test_genuinely_unused_import_still_flagged(self):
        source = """
import type { Unused } from "./unused";
import { AlsoUnused } from "./also";

export const x = 1;
"""
        delta = _extract(source, file_name="dead.ts")
        flagged = _unused_import_names(delta)
        assert "Unused" in flagged
        assert "AlsoUnused" in flagged


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


class TestScopeAwareImportUsage:
    """Scope walker: mark an import used only when a name resolves to it rather
    than to a closer binding (bead code_hygiene_mcp-ukr).
    """

    def test_export_default_closure_marks_import_used(self):
        # RED case 1: a genuine closure use inside an arrow body is a real use of
        # the import. The scope-flat walker stopped at the arrow boundary and left
        # `foo` looking unused (a false positive).
        source = """
import foo from "./foo";

export default () => foo;
"""
        delta = _extract(source, file_name="index.ts")
        assert "foo" not in _unused_import_names(delta), (
            "foo is closed over by the arrow body; it IS used and must not be flagged"
        )

    def test_export_default_shadowing_param_leaves_import_unused(self):
        # RED case 2: the arrow parameter `foo` shadows the import `foo`; the body
        # reference resolves to the param, NOT the import. The import is unused and
        # must still be flagged (guard against a scope-blind false negative).
        source = """
import foo from "./foo";

export default (foo) => foo;
"""
        delta = _extract(source, file_name="index.ts")
        assert "foo" in _unused_import_names(delta), (
            "foo the import is shadowed by the param; the import IS unused and must be flagged"
        )

    def test_function_body_closure_marks_import_used(self):
        # A closure use inside a named function body is a real use.
        source = """
import foo from "./foo";

export function wrap() {
  return foo;
}
"""
        delta = _extract(source, file_name="wrap.ts")
        assert "foo" not in _unused_import_names(delta)

    def test_function_param_shadow_leaves_import_unused(self):
        # A parameter shadowing the import makes the import unused.
        source = """
import foo from "./foo";

export function wrap(foo) {
  return foo();
}
"""
        delta = _extract(source, file_name="wrap.ts")
        assert "foo" in _unused_import_names(delta), (
            "foo the import is shadowed by the param; the import IS unused"
        )

    def test_local_const_shadow_leaves_import_unused(self):
        # A const declarator shadowing the import inside a body makes it unused.
        source = """
import foo from "./foo";

export function wrap() {
  const foo = 1;
  return foo;
}
"""
        delta = _extract(source, file_name="wrap.ts")
        assert "foo" in _unused_import_names(delta), (
            "foo is shadowed by a local const; the import IS unused"
        )

    def test_destructured_param_shadow_leaves_import_unused(self):
        # Destructuring patterns bind names too: { foo } shadows the import.
        source = """
import foo from "./foo";

export function wrap({ foo }) {
  return foo;
}
"""
        delta = _extract(source, file_name="wrap.ts")
        assert "foo" in _unused_import_names(delta), (
            "foo is shadowed by a destructured param; the import IS unused"
        )

    def test_member_prefix_match_survives(self):
        # Member-expression prefix matching must still work post-rewrite: a
        # namespace import used as `ns.thing()` marks `ns` used.
        source = """
import * as ns from "./ns";

export function go() {
  return ns.thing();
}
"""
        delta = _extract(source, file_name="go.ts")
        assert "ns" not in _unused_import_names(delta), (
            "ns.thing() is a member-prefix use of the namespace import"
        )

    def test_value_binding_does_not_shadow_type_position_use(self):
        # Design decision (bead code_hygiene_mcp-ukr #2): TS has separate type and
        # value namespaces. A value binding `Foo` (the const) does NOT shadow a
        # type-position use of imported `Foo`; the import is used in the type
        # position and must not be flagged. _mark_type_used is intentionally
        # scope-blind for this reason.
        source = """
import { Foo } from "./foo";

export function make(): Foo {
  const Foo = 1;
  return Foo as any;
}
"""
        delta = _extract(source, file_name="make.ts")
        assert "Foo" not in _unused_import_names(delta), (
            "imported Foo is used in the return-type position; the value-namespace "
            "local const Foo does not shadow it"
        )

    def test_import_use_before_shadowing_block_still_used(self):
        # Codex finding 1: a block-scoped `const foo` shadows only inside its
        # block; a reference in the enclosing block still resolves to the import.
        source = """
import foo from "./foo";

export function f() {
  foo();
  {
    const foo = 1;
    return foo;
  }
}
"""
        delta = _extract(source, file_name="f.ts")
        assert "foo" not in _unused_import_names(delta), (
            "foo() before the shadowing block is a genuine import use"
        )

    def test_switch_case_local_shadows_import(self):
        # Codex finding 3: a `const` declared in a switch case shadows the import
        # within the switch body; the import is then unused.
        source = """
import foo from "./foo";

export function f(x) {
  switch (x) {
    case 1: {
      const foo = () => 1;
      return foo();
    }
  }
}
"""
        delta = _extract(source, file_name="f.ts")
        assert "foo" in _unused_import_names(delta), (
            "foo is shadowed by a switch-case local; the import IS unused"
        )

    def test_type_position_use_survives_value_shadow_in_body(self):
        # Codex finding 2: a value local `Foo` must NOT hide a type-position use
        # of imported `Foo` inside the same body (`as Foo`). Type and value
        # namespaces are separate.
        source = """
import { Foo } from "./foo";

export function make(x: unknown) {
  const Foo = 1;
  return x as Foo;
}
"""
        delta = _extract(source, file_name="make.ts")
        assert "Foo" not in _unused_import_names(delta), (
            "the `as Foo` type use references the imported type, not the value local"
        )

    def test_var_in_nested_block_hoists_to_function_scope(self):
        # Codex round-2 finding: `var` hoists to the function scope, so a `var foo`
        # in a nested block shadows the import for the WHOLE function, including a
        # reference after the block. (`let`/`const` are block-scoped and do not.)
        source = """
import foo from "./foo";

export function f() {
  {
    var foo = 1;
  }
  return foo();
}
"""
        delta = _extract(source, file_name="f.ts")
        assert "foo" in _unused_import_names(delta), (
            "var foo hoists to the function; the import IS shadowed and unused"
        )

    def test_local_type_shadow_is_out_of_scope(self):
        # Design decision (bead code_hygiene_mcp-ukr #2): type-NAMESPACE bindings
        # (local `type`/`interface`/type params) are NOT tracked. A local
        # `type Foo` therefore does NOT suppress marking imported `Foo` used from
        # a type-position reference (the return-type annotation below, which
        # reaches _mark_type_used). This pins the documented current behaviour;
        # tracking the type namespace is deliberately out of scope for this bead.
        source = """
import { Foo } from "./foo";

export function f(): Foo {
  type Foo = number;
  return 1 as any;
}
"""
        delta = _extract(source, file_name="f.ts")
        # Current (documented) behaviour: imported Foo is treated as used even
        # though a local type alias `Foo` also exists in the body.
        assert "Foo" not in _unused_import_names(delta), (
            "local type-namespace bindings are out of scope (bead decision #2); "
            "the imported Foo is conservatively treated as used"
        )

    def test_function_expression_param_shadows_import(self):
        # Codex round-3 finding: an anonymous `function (foo) {}` expression (a
        # callback) opens a parameter scope just like a declaration/arrow. Its
        # param `foo` shadows the import; the import is unused.
        source = """
import foo from "./foo";

export const x = consume(function (foo) {
  return foo();
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" in _unused_import_names(delta), (
            "the function-expression param foo shadows the import; the import IS unused"
        )

    def test_function_expression_closure_marks_import_used(self):
        # Dual guard: a closure use inside a function expression with no shadowing
        # param is a genuine use of the import.
        source = """
import foo from "./foo";

export const x = consume(function () {
  return foo();
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" not in _unused_import_names(delta), (
            "foo is closed over by the function-expression body; it IS used"
        )

    def test_named_function_expression_self_name_shadows_import(self):
        # Codex round-4 finding: a NAMED function expression binds its own name
        # within its body (the classic self-reference binding). `function foo()`
        # makes `foo()` inside refer to the function itself, not the import.
        source = """
import foo from "./foo";

export const x = consume(function foo() {
  return foo();
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" in _unused_import_names(delta), (
            "the named function expression's own name foo shadows the import; "
            "the import IS unused"
        )

    def test_class_expression_method_param_shadows_import(self):
        # Codex round-5 finding: an anonymous `class {}` expression's method still
        # opens a parameter scope. The method param `foo` shadows the import.
        source = """
import foo from "./foo";

export const x = consume(class {
  method(foo) {
    return foo();
  }
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" in _unused_import_names(delta), (
            "the class-expression method param foo shadows the import; unused"
        )

    def test_class_expression_method_closure_marks_import_used(self):
        # Dual guard: a closure use inside a class-expression method with no
        # shadowing param is a genuine use of the import.
        source = """
import foo from "./foo";

export const x = consume(class {
  method() {
    return foo();
  }
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" not in _unused_import_names(delta), (
            "foo is closed over by the class-expression method body; it IS used"
        )

    def test_named_class_expression_self_name_shadows_import(self):
        # Codex round-6 finding: a NAMED class expression binds its own name
        # within the class body (self-reference), shadowing an import of the same
        # name. `class foo { method() { return foo; } }` -> foo is the class.
        source = """
import foo from "./foo";

export const x = consume(class foo {
  method() {
    return foo;
  }
});
"""
        delta = _extract(source, file_name="x.ts")
        assert "foo" in _unused_import_names(delta), (
            "the named class expression's own name foo shadows the import; unused"
        )
