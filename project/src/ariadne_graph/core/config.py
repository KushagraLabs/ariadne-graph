"""Configuration for the Ariadne Graph analyzer."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AnalyzerConfig(BaseModel):
    """Configuration for a code graph analysis run.

    Controls file discovery, parsing, indexing, and retrieval behavior.
    All fields have sensible defaults for local development.
    """

    repo_root: Path = Field(description="Root path of the repository to analyze")
    graph_id: str | None = Field(default=None, description="Graph identifier; derived from repo_root if not set")
    ignore_patterns: list[str] = Field(default_factory=lambda: [
        ".git", "__pycache__", "*.pyc", "node_modules", ".venv", "venv",
        ".tox", ".pytest_cache", ".mypy_cache", "*.egg-info", "dist", "build",
        ".idea", ".vscode", ".DS_Store", ".claude",
    ])
    python_paths: list[str] = Field(default_factory=list)
    max_file_size: int = Field(default=1_000_000, description="Maximum file size in bytes to parse")
    embedding_provider: str = Field(
        default_factory=lambda: os.environ.get("ARIADNE_EMBEDDING_PROVIDER", "local"),
        description="Embedding provider: local, openai, ollama",
    )
    embedding_model: str = Field(default="all-MiniLM-L6-v2")
    embedding_dimensions: int = Field(default=384)
    embedding_batch_size: int = Field(default=32)
    incremental_sync_interval: float = Field(default=30.0, description="Seconds between sync polls")
    auto_sync: bool = Field(default=False, description="Enable automatic background sync")

    # Storage configuration (can be set via environment variables or explicitly)
    db_path: str | None = Field(
        default_factory=lambda: os.environ.get("ARIADNE_DB"),
        description="SQLite database path; defaults to .ariadne/graph.db",
    )
    neo4j_uri: str | None = Field(
        default_factory=lambda: os.environ.get("ARIADNE_NEO4J_URI"),
        description="Neo4j bolt URI",
    )
    neo4j_user: str | None = Field(
        default_factory=lambda: os.environ.get("ARIADNE_NEO4J_USER", "neo4j"),
        description="Neo4j username",
    )
    neo4j_password: str | None = Field(
        default_factory=lambda: os.environ.get("ARIADNE_NEO4J_PASSWORD", "password"),
        description="Neo4j password",
    )

    # SCIP-TypeScript indexer (optional)
    scip_typescript_enabled: bool | None = Field(
        default_factory=lambda: _env_bool(os.environ.get("ARIADNE_SCIP_TYPESCRIPT_ENABLED")),
        description="Enable scip-typescript. None = auto-detect.",
    )
    scip_typescript_path: str | None = Field(
        default_factory=lambda: os.environ.get("ARIADNE_SCIP_TYPESCRIPT_PATH"),
        description="Path to scip-typescript binary, 'npx', or None to search PATH.",
    )
    scip_typescript_args: list[str] = Field(
        default_factory=lambda: _env_list(os.environ.get("ARIADNE_SCIP_TYPESCRIPT_ARGS", "")),
        description="Extra CLI args passed to scip-typescript.",
    )
    scip_typescript_infer_tsconfig: bool | None = Field(
        default_factory=lambda: _env_bool(
            os.environ.get("ARIADNE_SCIP_TYPESCRIPT_INFER_TSCONFIG")
        ),
        description="Use --infer-tsconfig for JS-only projects. None = auto.",
    )

    # Lumen compatibility (optional)
    lumen_enabled: bool = Field(
        default_factory=lambda: os.environ.get("LUMEN_CODE_GRAPH_PROVIDER", "")
        .lower()
        .startswith("standalone"),
        description="Enable Lumen compatibility layer",
    )
    lumen_workspace_root: Path | None = Field(
        default=None,
        description="Restrict Lumen adapter to projects under this root",
    )
    lumen_workspace_id: str | None = Field(
        default_factory=lambda: os.environ.get("LUMEN_WORKSPACE_ID"),
        description="Lumen workspace identifier stored in project metadata",
    )
    lumen_compat_aliases: bool = Field(
        default=True,
        description="Expose Lumen-style tool aliases in the MCP server",
    )

    model_config = {"arbitrary_types_allowed": True}

    def model_post_init(self, __context: Any) -> None:
        if self.graph_id is None:
            path_str = str(self.repo_root.resolve())
            self.graph_id = hashlib.sha256(path_str.encode()).hexdigest()[:16]

    @property
    def resolved_repo_root(self) -> Path:
        return self.repo_root.resolve()


def _env_bool(value: str | None) -> bool | None:
    """Parse an environment variable string into bool or None."""
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _env_list(value: str) -> list[str]:
    """Parse a comma-separated environment variable string into a list."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
