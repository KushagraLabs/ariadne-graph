"""Core data models for the Ariadne Graph code graph."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CodeNode(BaseModel):
    """A node in the code knowledge graph.

    Each node represents a code entity (file, module, class, function, etc.)
    with deterministic ID, typed labels, and rich properties.
    """

    id: str = Field(description="Deterministic node ID, e.g. 'mymodule.MyClass.my_method'")
    graph_id: str = Field(description="Repository graph identifier")
    labels: list[str] = Field(default_factory=list, description="Semantic labels: ['CodeFunction', ...]")
    properties: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": False, "extra": "ignore"}

    def __hash__(self) -> int:
        return hash((self.graph_id, self.id))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CodeNode):
            return NotImplemented
        return self.graph_id == other.graph_id and self.id == other.id


class CodeEdge(BaseModel):
    """A relationship between two code nodes.

    Represents structural relationships: IMPORTS, CALLS, INHERITS, etc.
    """

    source: str = Field(description="Source node id")
    target: str = Field(description="Target node id")
    graph_id: str = Field(description="Repository graph identifier")
    rel_type: str = Field(description="Relationship type: IMPORTS, CALLS, etc.")
    properties: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": False, "extra": "ignore"}

    def __hash__(self) -> int:
        return hash((self.graph_id, self.source, self.target, self.rel_type))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CodeEdge):
            return NotImplemented
        return (
            self.graph_id == other.graph_id
            and self.source == other.source
            and self.target == other.target
            and self.rel_type == other.rel_type
        )


class CodeGraphDelta(BaseModel):
    """The result of parsing a single source file.

    Contains all nodes and edges extracted from one file, plus metadata
    for incremental sync (content hash, parser version).
    """

    graph_id: str
    file_path: str
    nodes: list[CodeNode] = Field(default_factory=list)
    edges: list[CodeEdge] = Field(default_factory=list)
    content_hash: str = Field(description="XXH3 hash of file content")
    parser_version: str


class EmbeddingPayload(BaseModel):
    """Data for storing/updating a node embedding."""

    node_id: str
    graph_id: str
    text: str = Field(description="Text to embed (for regeneration)")
    embedding: list[float] | None = None


class SearchHit(BaseModel):
    """A single result from semantic or keyword search."""

    node_id: str
    score: float
    node: CodeNode | None = None


class ArchitectureSummary(BaseModel):
    """Summary of codebase architecture from community detection."""

    total_communities: int
    total_files: int
    total_entities: int
    communities: list[CommunityInfo] = Field(default_factory=list)
    hotspots: list[HotspotInfo] = Field(default_factory=list)


class CommunityInfo(BaseModel):
    """Information about a single detected community."""

    community_id: int
    member_count: int
    representative_files: list[str] = Field(default_factory=list)
    internal_edge_density: float = 0.0
    external_coupling: dict[int, int] = Field(default_factory=dict)


class HotspotInfo(BaseModel):
    """A code hotspot identified by complexity or coupling metrics."""

    node_id: str
    node_name: str = ""
    file_path: str = ""
    metric_type: str = ""  # "complexity", "coupling", "fan_in", "fan_out"
    score: float = 0.0
    community_id: int | None = None


class ImpactAnalysisResult(BaseModel):
    """Result of impact analysis for a change."""

    target_symbol: str
    total_affected: int
    direct_dependencies: list[str] = Field(default_factory=list)
    transitive_affected: list[str] = Field(default_factory=list)
    coupling_scores: dict[str, float] = Field(default_factory=dict)


class ChangeReport(BaseModel):
    """Report of detected changes between indexing runs."""

    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    since_ref: str | None = Field(default=None)
    resolved_ref: str | None = Field(default=None)
    comparison_mode: str = Field(default="stored_hash")
    message: str = Field(default="")


class IndexStatus(BaseModel):
    """Status of indexing for a repository."""

    graph_id: str
    repo_path: str
    last_indexed: str | None = None
    file_count: int = 0
    dirty_files: list[str] = Field(default_factory=list)
    sync_enabled: bool = False


class ProjectRecord(BaseModel):
    """A registered project in the graph store catalog."""

    graph_id: str
    repo_path: str
    created_at: str | None = None
    last_indexed: str | None = None
    file_count: int = 0
