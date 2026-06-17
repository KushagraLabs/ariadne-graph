"""Runtime capability detection for optional dependencies.

The server degrades gracefully when optional extras are not installed.
This module probes the environment and reports which features are available
and which extra is required to enable each missing feature.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Availability of optional runtime features."""

    typescript_extraction: bool
    semantic_embeddings: bool
    sqlite_vector_search: bool
    neo4j_backend: bool
    scip_typescript_indexer: bool

    @classmethod
    def probe(cls) -> RuntimeCapabilities:
        """Probe the current interpreter for optional dependencies."""
        return cls(
            typescript_extraction=_module_available("tree_sitter_typescript"),
            semantic_embeddings=_module_available("sentence_transformers"),
            sqlite_vector_search=_module_available("sqlite_vec"),
            neo4j_backend=_module_available("neo4j"),
            scip_typescript_indexer=_scip_typescript_available(),
        )


def _module_available(module: str) -> bool:
    """Return True if *module* can be imported without raising."""
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


def _scip_typescript_available() -> bool:
    """Return True if scip-typescript (binary or npx) and protobuf are available."""
    if not _module_available("google.protobuf"):
        return False
    if shutil.which("scip-typescript"):
        return True
    return bool(shutil.which("npx"))


def get_capabilities() -> dict[str, Any]:
    """Return a JSON-friendly capability report.

    Each feature reports whether it is available, the pyproject extra that
    installs the required packages, and a human-readable reason when the
    feature is unavailable.
    """
    caps = RuntimeCapabilities.probe()
    features = {
        "typescript_extraction": {
            "available": caps.typescript_extraction,
            "extra": "typescript",
            "install": "pip install -e \".[typescript]\"",
        },
        "semantic_embeddings": {
            "available": caps.semantic_embeddings,
            "extra": "semantic",
            "install": "pip install -e \".[semantic]\"",
        },
        "sqlite_vector_search": {
            "available": caps.sqlite_vector_search,
            "extra": "vector",
            "install": "pip install -e \".[vector]\"",
        },
        "neo4j_backend": {
            "available": caps.neo4j_backend,
            "extra": "neo4j",
            "install": "pip install -e \".[neo4j]\"",
        },
        "scip_typescript_indexer": {
            "available": caps.scip_typescript_indexer,
            "extra": "typescript",
            "install": "pip install -e \".[typescript]\" && npm install -g @sourcegraph/scip-typescript",
        },
    }

    missing = [
        name for name, info in features.items() if not info["available"]
    ]

    return {
        "features": features,
        "degraded": bool(missing),
        "missing_features": missing,
        "message": (
            "All optional features are available."
            if not missing
            else f"Optional features unavailable: {', '.join(missing)}."
        ),
    }
