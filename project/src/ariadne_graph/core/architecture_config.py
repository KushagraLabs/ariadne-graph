"""Optional `.ariadne/architecture.yml` override for architecture layering.

By default, :mod:`ariadne_graph.core.architecture` derives layering purely from
directory structure (:func:`~ariadne_graph.core.architecture.is_deep_import`).
A repo can opt into an explicit, declared module map instead: named modules
with path globs and a ``may_depend_on`` allow-list, checked by
:func:`ArchitectureConfig.allows`. Absence of the config file is the common
case and must leave analysis behavior unchanged — see
:func:`load_architecture_config`.
"""

from __future__ import annotations

import fnmatch
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_CONFIG_REL_PATH = ".ariadne/architecture.yml"


class ModuleSpec(BaseModel):
    """A declared module: the paths it owns and what it may depend on."""

    paths: list[str] = Field(default_factory=list, description="Glob patterns owned by this module")
    public_surfaces: list[str] = Field(default_factory=list, description="Paths other modules may import")
    exclude: list[str] = Field(default_factory=list, description="Glob patterns excluded from this module")
    may_depend_on: list[str] = Field(default_factory=list, description="Module names this module may depend on")


class ArchitectureException(BaseModel):
    """A time-boxed, documented exception to the layering rule."""

    from_: str = Field(alias="from", description="Source module name")
    to: str = Field(description="Target module name")
    reason: str = Field(default="", description="Why the exception exists")
    expires: date | None = Field(default=None, description="Exception no longer applies after this date")

    model_config = {"populate_by_name": True}


class ArchitectureConfig(BaseModel):
    """Declared module map for `.ariadne/architecture.yml`."""

    version: int = 1
    modules: dict[str, ModuleSpec] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    exceptions: list[ArchitectureException] = Field(default_factory=list)

    def module_of(self, rel_path: str) -> str | None:
        """Name of the module owning ``rel_path`` (first glob match), or None."""
        for name, spec in self.modules.items():
            if any(fnmatch.fnmatch(rel_path, pat) for pat in spec.exclude):
                continue
            if any(fnmatch.fnmatch(rel_path, pat) for pat in spec.paths):
                return name
        return None

    def allows(self, src_mod: str, dst_mod: str) -> bool:
        """Whether ``src_mod`` may depend on ``dst_mod``.

        Allowed when: same module, declared in ``may_depend_on``, or a
        non-expired exception covers the pair. Exceptions with an ``expires``
        date in the past no longer apply.
        """
        if src_mod == dst_mod:
            return True
        spec = self.modules.get(src_mod)
        if spec is not None and dst_mod in spec.may_depend_on:
            return True
        today = datetime.now().date()
        for exc in self.exceptions:
            if exc.from_ == src_mod and exc.to == dst_mod:
                if exc.expires is None or exc.expires >= today:
                    return True
        return False


def load_architecture_config(repo_root: Path) -> ArchitectureConfig | None:
    """Load `.ariadne/architecture.yml` under ``repo_root``, or None if absent."""
    config_path = Path(repo_root) / _CONFIG_REL_PATH
    if not config_path.is_file():
        return None
    raw = yaml.safe_load(config_path.read_text()) or {}
    return ArchitectureConfig.model_validate(raw)
