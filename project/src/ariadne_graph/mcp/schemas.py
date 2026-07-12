"""Pydantic input/output schemas for all 14 MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ariadne_graph.core.models import ProjectRecord

# Bumped whenever a response shape changes in a way callers should notice.
SCHEMA_VERSION = "1"

# ============================================================================
# Input schemas
# ============================================================================

class IndexInput(BaseModel):
    """Input for code_graph_index tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    force_rebuild: bool = Field(default=False, description="Delete existing graph and re-index")


class IndexStatusInput(BaseModel):
    """Input for code_graph_index_status tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")


class CapabilitiesInput(BaseModel):
    """Input for code_graph_capabilities tool."""


class DeleteProjectInput(BaseModel):
    """Input for code_graph_delete_project tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")


class RetrieveInput(BaseModel):
    """Input for code_graph_retrieve tool."""

    query: str = Field(description="Node ID or name to retrieve")
    graph_id: str | None = Field(default=None, description="Graph ID; derived from repo_path if omitted")
    repo_path: str | None = Field(
        default=None,
        description="Optional repository path used to derive graph_id",
    )


class SearchSemanticInput(BaseModel):
    """Input for code_graph_search_semantic tool."""

    query_text: str = Field(description="Natural language query text")
    repo_path: str | None = Field(
        default=None,
        description="Optional repository path to restrict search to a single graph",
    )
    limit: int = Field(default=10, ge=1, le=100, description="Maximum number of results")
    types: list[str] = Field(default_factory=list, description="Filter by node type labels")


class SearchCodeInput(BaseModel):
    """Input for code_graph_search_code tool."""

    pattern: str = Field(description="Code pattern or keyword to search")
    repo_path: str | None = Field(
        default=None,
        description="Optional repository path to restrict search to a single graph",
    )
    language: str | None = Field(default=None, description="Filter by language (e.g. 'python')")
    limit: int = Field(default=10, ge=1, le=100, description="Maximum number of results")


class TraceDependenciesInput(BaseModel):
    """Input for code_graph_trace_dependencies tool."""

    symbol: str = Field(description="Symbol/node ID to trace dependencies from")
    direction: str = Field(default="both", description='Direction: "both", "upstream", or "downstream"')
    max_depth: int = Field(default=3, ge=1, le=10, description="Maximum traversal depth")
    graph_id: str | None = Field(default=None, description="Graph ID; derived from registered projects if omitted")


class ImpactAnalysisInput(BaseModel):
    """Input for code_graph_impact_analysis tool."""

    symbol: str = Field(description="Symbol/node ID to analyze impact for")
    graph_id: str | None = Field(default=None, description="Graph ID; derived from registered projects if omitted")


class DetectChangesInput(BaseModel):
    """Input for code_graph_detect_changes tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    since_ref: str | None = Field(default=None, description="Git ref (commit SHA, branch, or tag) for comparison")


class FindHotspotsInput(BaseModel):
    """Input for code_graph_find_hotspots tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    top_n: int = Field(default=10, ge=1, le=100, description="Number of top hotspots to return")
    metric: str = Field(
        default="complexity",
        description='Metric to rank by: "complexity", "coupling", "fan_in", "fan_out"',
    )
    offset: int = Field(default=0, ge=0, description="Number of ranked hotspots to skip before returning top_n")
    summary_only: bool = Field(
        default=False, description="Return only counts (total/has_more), omitting the hotspot entries"
    )
    include_edges: bool = Field(
        default=True, description="Reserved for parity with other paginated tools; hotspots carry no edges"
    )


class GetArchitectureInput(BaseModel):
    """Input for code_graph_get_architecture tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    granularity: str = Field(
        default="symbol",
        description='Community granularity: "symbol" (default) or "file"',
    )
    limit: int = Field(default=50, ge=1, le=500, description="Maximum number of communities to return")
    offset: int = Field(default=0, ge=0, description="Number of communities to skip before returning limit")
    summary_only: bool = Field(
        default=False, description="Return only aggregate counts, omitting the communities list"
    )
    include_edges: bool = Field(
        default=True, description="Reserved for parity with other paginated tools; architecture carries no edges"
    )


class ExplainEdgeInput(BaseModel):
    """Input for code_graph_explain_edge tool."""

    src_path: str = Field(description="Repo-relative path of the importing file")
    dst_path: str = Field(description="Repo-relative path of the imported file")


class GetDependencyMatrixInput(BaseModel):
    """Input for code_graph_get_dependency_matrix tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    group_by: Literal["file", "directory", "module"] = Field(
        default="file", description="Granularity to collapse dependency-matrix nodes/edges to"
    )


class AuditPublicSurfacesInput(BaseModel):
    """Input for code_graph_audit_public_surfaces tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")


class ListCommunitiesInput(BaseModel):
    """Input for code_graph_list_communities tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    community_id: int | None = Field(default=None, description="Filter to a specific community")
    granularity: str = Field(
        default="symbol",
        description='Community granularity: "symbol" (default) or "file"',
    )


class InspectFileInput(BaseModel):
    """Input for code_graph_inspect_file tool."""

    file_path: str = Field(description="Path of the file to inspect")
    graph_id: str | None = Field(default=None, description="Graph ID; derived from file_path if omitted")
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum number of nodes (and, independently, edges) to return")
    offset: int = Field(default=0, ge=0, description="Number of nodes/edges to skip before returning limit")
    summary_only: bool = Field(
        default=False, description="Return only node/edge counts, omitting the nodes and edges lists"
    )
    include_edges: bool = Field(default=True, description="Whether to include the edges list in the response")


class LumenRetrieveInput(BaseModel):
    """Input for the Lumen-compatible ``lumen_code_graph_retrieve`` alias."""

    query: str = Field(description="Node ID or symbol name to retrieve")
    graph_id: str | None = Field(default=None, description="Graph ID; derived from repo_path if omitted")
    repo_path: str | None = Field(
        default=None,
        description="Optional repository path used to derive graph_id",
    )


# ============================================================================
# Output schemas
# ============================================================================


class FreshnessMixin(BaseModel):
    """Shared in-band index-freshness envelope for analysis/query responses.

    Optional and additive: a consumer that ignores it sees no change. When
    present, ``freshness`` carries ``{last_indexed, dirty_file_count, stale,
    sync_enabled}`` so a caller can tell a moved-since-index graph apart from a
    fresh one without a separate ``code_graph_index_status`` round-trip. Left
    ``None`` when the graph is unknown/unindexed (nothing meaningful to report).
    """

    freshness: dict[str, Any] | None = Field(
        default=None,
        description=(
            "In-band index-freshness envelope: {last_indexed (ISO str|None), "
            "dirty_file_count (int), stale (bool), sync_enabled (bool)}. "
            "None when the graph is not indexed."
        ),
    )


class ResolutionMixin(BaseModel):
    """Shared in-band resolution-provenance envelope for dep-graph responses.

    Rides the same optional-field mechanism as :class:`FreshnessMixin` (bead
    hh0): additive, ignorable, no parallel channel. Tells a consumer the
    confidence of the file->file dep edges behind an analysis response — SCIP
    definition-resolution vs the tree-sitter IMPORTS fallback (bead 1u5) — so an
    agent knows whether it is reading precise edges or import-granularity ones.
    ``None`` when the graph is unknown/unindexed (nothing to report).
    """

    resolution: dict[str, Any] | None = Field(
        default=None,
        description=(
            "In-band resolution-provenance envelope: {references_files (int), "
            "imports_only_files (int), resolution ('scip'|'tree-sitter-imports'|"
            "'none')}. imports_only_files>0 means some file->file edges are "
            "import-granularity fallback, not SCIP. None when not indexed."
        ),
    )


class IndexOutput(BaseModel):
    """Output for code_graph_index tool."""

    status: str = Field(description="Indexing status: 'success', 'partial', 'error'")
    files_indexed: int = Field(description="Number of files indexed")
    graph_id: str = Field(description="Graph ID of the indexed repository")
    message: str = Field(default="", description="Human-readable status message")


class IndexStatusOutput(BaseModel):
    """Output for code_graph_index_status tool."""

    graph_id: str = Field(description="Graph ID of the repository")
    repo_path: str = Field(description="Repository path")
    last_indexed: str | None = Field(default=None, description="ISO timestamp of last index run")
    file_count: int = Field(default=0, description="Number of indexed files")
    dirty_files: list[str] = Field(default_factory=list, description="Files changed since last index")
    sync_enabled: bool = Field(default=False, description="Whether auto-sync is enabled")
    capabilities: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime capability report for optional features",
    )
    message: str = Field(default="", description="Human-readable status message")


class CapabilitiesOutput(BaseModel):
    """Output for code_graph_capabilities tool."""

    capabilities: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime capability report for optional features",
    )
    message: str = Field(default="", description="Human-readable status message")


class ProjectListOutput(BaseModel):
    """Output for code_graph_list_projects tool."""

    projects: list[ProjectRecord] = Field(
        default_factory=list, description="List of registered project records"
    )


class DeleteProjectOutput(BaseModel):
    """Output for code_graph_delete_project tool."""

    deleted: bool = Field(description="Whether the project was deleted")
    graph_id: str = Field(description="Graph ID of the deleted project")
    message: str = Field(default="", description="Human-readable status message")


class RetrieveOutput(FreshnessMixin):
    """Output for code_graph_retrieve tool."""

    results: list[dict[str, Any]] = Field(default_factory=list, description="Retrieved nodes and edges")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Response schema version")


class LumenRetrieveOutput(RetrieveOutput):
    """Output for the Lumen-compatible ``lumen_code_graph_retrieve`` alias.

    Extends the canonical retrieve output with a Lumen-style context field.
    """

    lumen_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Lumen-style prompt context metadata",
    )


class SearchSemanticOutput(FreshnessMixin):
    """Output for code_graph_search_semantic tool."""

    hits: list[dict[str, Any]] = Field(default_factory=list, description="Ranked search hits")
    message: str = Field(default="", description="Human-readable status message")


class SearchCodeOutput(FreshnessMixin):
    """Output for code_graph_search_code tool."""

    matches: list[dict[str, Any]] = Field(default_factory=list, description="Matching code snippets")
    message: str = Field(default="", description="Human-readable status message")


class TraceDependenciesOutput(FreshnessMixin):
    """Output for code_graph_trace_dependencies tool."""

    paths: list[list[str]] = Field(default_factory=list, description="Dependency paths as node ID chains")


class ImpactAnalysisOutput(FreshnessMixin):
    """Output for code_graph_impact_analysis tool."""

    target_symbol: str = Field(description="The analyzed symbol")
    total_affected: int = Field(description="Total number of affected symbols")
    direct_dependencies: list[str] = Field(default_factory=list, description="Direct dependency symbols")
    transitive_affected: list[str] = Field(default_factory=list, description="Transitively affected symbols")
    coupling_scores: dict[str, float] = Field(default_factory=dict, description="Coupling strength per symbol")
    message: str = Field(default="", description="Human-readable status message")


class DetectChangesOutput(BaseModel):
    """Output for code_graph_detect_changes tool."""

    added: list[str] = Field(default_factory=list, description="Newly added symbols")
    modified: list[str] = Field(default_factory=list, description="Modified symbols")
    deleted: list[str] = Field(default_factory=list, description="Deleted symbols")
    message: str = Field(default="", description="Human-readable status message")
    since_ref: str | None = Field(default=None, description="Git ref requested for comparison")
    resolved_ref: str | None = Field(default=None, description="Resolved git commit SHA")
    comparison_mode: str = Field(
        default="stored_hash",
        description='Comparison mode: "git_ref" or "stored_hash"',
    )


class FindHotspotsOutput(FreshnessMixin):
    """Output for code_graph_find_hotspots tool."""

    hotspots: list[dict[str, Any]] = Field(default_factory=list, description="Ranked hotspot entries")
    total: int = Field(default=0, description="Total number of hotspots available before pagination")
    has_more: bool = Field(default=False, description="Whether more hotspots exist beyond this page")
    message: str = Field(default="", description="Human-readable status message")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Response schema version")


class ArchitectureOutput(FreshnessMixin, ResolutionMixin):
    """Output for code_graph_get_architecture tool."""

    summary: dict[str, Any] = Field(default_factory=dict, description="Architecture summary")
    total: int = Field(default=0, description="Total number of communities available before pagination")
    has_more: bool = Field(default=False, description="Whether more communities exist beyond this page")
    message: str = Field(default="", description="Human-readable status message")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Response schema version")


class ExplainEdgeOutput(FreshnessMixin):
    """Output for code_graph_explain_edge tool."""

    src: str = Field(default="", description="Repo-relative path of the importing file")
    dst: str = Field(default="", description="Repo-relative path of the imported file")
    src_organ: str = Field(default="", description="Top-level organ of the source file")
    dst_organ: str = Field(default="", description="Top-level organ of the target file")
    allowed: bool = Field(default=True, description="Whether the edge is a layering violation")
    reason: str = Field(default="", description="Classification: top_level, peripheral_source, same_organ, front_door, cross_organ_internal")
    rule: str | None = Field(default=None, description="Violated rule name, or None when allowed")
    front_door_would_fix: bool = Field(
        default=False, description="Whether routing through the target organ's front door would make the edge valid"
    )
    message: str = Field(default="", description="Human-readable status message")


class DependencyMatrixOutput(FreshnessMixin, ResolutionMixin):
    """Output for code_graph_get_dependency_matrix tool."""

    nodes: list[dict[str, Any]] = Field(default_factory=list, description="Matrix nodes: id, module, production")
    edges: list[dict[str, Any]] = Field(
        default_factory=list, description="Matrix edges: source, target, import_count, violation_count"
    )
    message: str = Field(default="", description="Human-readable status message")


class AuditPublicSurfacesOutput(FreshnessMixin):
    """Output for code_graph_audit_public_surfaces tool.

    Per-module facade/encapsulation report. Requires a declared
    ``public_surfaces`` list in `.ariadne/architecture.yml` -- with no config
    present, ``modules`` is empty and ``message`` explains why.
    """

    modules: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "One report per declared module: 'module', 'public_exports', "
            "'via_surface_consumers' (list of {consumer, surface}), "
            "'deep_import_consumers' (list of {consumer, internal}), "
            "'unused_public_exports', 'high_fan_in_internals' "
            "(list of {internal, external_fan_in}), 'is_all_exporting_barrel'"
        ),
    )
    message: str = Field(default="", description="Human-readable status message")


class CommunitiesOutput(FreshnessMixin):
    """Output for code_graph_list_communities tool."""

    communities: list[dict[str, Any]] = Field(default_factory=list, description="Community entries")
    message: str = Field(default="", description="Human-readable status message")


class InspectFileOutput(FreshnessMixin):
    """Output for code_graph_inspect_file tool."""

    nodes: list[dict[str, Any]] = Field(default_factory=list, description="Nodes in the file")
    edges: list[dict[str, Any]] = Field(default_factory=list, description="Edges in the file")
    total_nodes: int = Field(default=0, description="Total number of nodes available before pagination")
    total_edges: int = Field(default=0, description="Total number of edges available before pagination")
    has_more: bool = Field(
        default=False, description="Whether more nodes or edges exist beyond this page"
    )
    message: str = Field(default="", description="Human-readable status message")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Response schema version")


class ListDiagnosticsInput(BaseModel):
    """Input for code_graph_list_diagnostics tool."""

    repo_path: str = Field(description="Absolute or relative path to repository root")
    level: str | None = Field(default=None, description="Filter by severity level")
    rule: str | None = Field(
        default=None,
        description=(
            "Filter by rule identifier, e.g. architecture/layering rules "
            "'deep_import', 'dependency_cycle', 'orphan_module', 'upward_import' "
            "or per-file lint rules 'unused_import', 'missing_type_annotation'"
        ),
    )
    file_path: str | None = Field(default=None, description="Filter by source file path")
    production_only: bool = Field(
        default=False,
        description="Exclude diagnostics owned by peripheral organs (tests/scripts)",
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum diagnostics to return")


class ListDiagnosticsOutput(FreshnessMixin):
    """Output for code_graph_list_diagnostics tool."""

    diagnostics: list[dict[str, Any]] = Field(
        default_factory=list, description="Diagnostic entries (truncated to limit)"
    )
    counts: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Aggregate rollup over ALL matching diagnostics (before the limit "
            "truncation): 'by_rule' (per-rule totals) and 'by_production' "
            "(production vs test split)"
        ),
    )
    message: str = Field(default="", description="Human-readable status message")
