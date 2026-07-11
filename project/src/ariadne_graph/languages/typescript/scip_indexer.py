"""SCIP-TypeScript indexer runner and project fingerprinting.

Detects whether ``scip-typescript`` (or ``npx @sourcegraph/scip-typescript``)
can be run, decides whether the current project fingerprint has changed, and
runs the indexer when needed.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import xxhash

from ariadne_graph.core.config import AnalyzerConfig

logger = logging.getLogger(__name__)

_SCIP_OUTPUT_SUBDIR = ".ariadne/scip"


class ScipTypeScriptIndexer:
    """Run ``scip-typescript index`` for a TypeScript project."""

    def __init__(self, repo_root: Path, config: AnalyzerConfig) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    @staticmethod
    def has_binary() -> bool:
        """Return True if ``scip-typescript`` is on PATH."""
        return shutil.which("scip-typescript") is not None

    @staticmethod
    def has_npx() -> bool:
        """Return True if ``npx`` is available."""
        return shutil.which("npx") is not None

    def is_available(self) -> bool:
        """Return True if the indexer can be invoked."""
        return self.has_binary() or self.has_npx()

    def has_project(self) -> bool:
        """Return True if the repo looks like a runnable TS/JS project."""
        return (
            (self.repo_root / "tsconfig.json").exists()
            or (self.repo_root / "package.json").exists()
        )

    # Directories whose tsconfigs are never real project roots: vendored deps,
    # build output, and archived/legacy code.
    _NON_PROJECT_DIRS = frozenset({"node_modules", "dist", "build", "archive", ".ariadne"})

    def discover_projects(self) -> list[Path]:
        """Return every directory that is its own scip-typescript project root.

        A TypeScript project root is a directory containing a ``tsconfig.json``.
        A single repo can hold several (e.g. a root app plus a ``mobile/``
        subproject); scip-typescript reads only the tsconfig it is pointed at, so
        each must be indexed separately or the others are silently uncovered.

        The repo root is always included (even with no tsconfig — it is then
        indexed via ``--infer-tsconfig``). Vendored/build/archived directories
        are skipped. Results are sorted for determinism, repo root first.
        """
        projects: set[Path] = {self.repo_root}
        for tsconfig in self.repo_root.rglob("tsconfig.json"):
            rel_parts = tsconfig.relative_to(self.repo_root).parts
            if any(part in self._NON_PROJECT_DIRS for part in rel_parts):
                continue
            projects.add(tsconfig.parent)
        ordered = sorted(projects, key=lambda p: str(p))
        # Keep the repo root first — it is the primary project.
        ordered.remove(self.repo_root)
        return [self.repo_root, *ordered]

    def should_run(self) -> bool:
        """Return True when SCIP indexing is not explicitly disabled."""
        enabled = self.config.scip_typescript_enabled
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.is_available() and self.has_project()

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    def compute_fingerprint(self, all_files: list[Path]) -> str:
        """Compute a stable fingerprint of all TS/TSX source files.

        The fingerprint is the xxhash of sorted ``(relative_path, hash)``
        tuples. It is used to skip re-running ``scip-typescript`` when the
        TypeScript source has not changed.
        """
        ts_files = [
            p for p in all_files if p.suffix in {".ts", ".tsx"} and p.is_file()
        ]
        pairs: list[tuple[str, str]] = []
        for file_path in sorted(ts_files):
            try:
                content = file_path.read_bytes()
            except OSError as exc:
                logger.warning("Failed to read %s for SCIP fingerprint: %s", file_path, exc)
                continue
            digest = xxhash.xxh3_64_hexdigest(content)
            rel = str(file_path.relative_to(self.repo_root))
            pairs.append((rel, digest))

        if not pairs:
            return xxhash.xxh3_64_hexdigest("")

        serialized = "\n".join(f"{rel}\t{digest}" for rel, digest in pairs)
        return xxhash.xxh3_64_hexdigest(serialized.encode("utf-8"))

    def _fingerprint_path(self, graph_id: str) -> Path:
        return self._output_dir(graph_id) / "fingerprint.txt"

    def _read_stored_fingerprint(self, graph_id: str) -> str | None:
        path = self._fingerprint_path(graph_id)
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _write_stored_fingerprint(self, graph_id: str, fingerprint: str) -> None:
        path = self._fingerprint_path(graph_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fingerprint, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write SCIP fingerprint to %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Index execution
    # ------------------------------------------------------------------

    def _output_dir(self, graph_id: str) -> Path:
        return self.repo_root / _SCIP_OUTPUT_SUBDIR / graph_id

    def _index_path(self, graph_id: str) -> Path:
        return self._output_dir(graph_id) / "index.scip"

    def _build_command(self, project_dir: Path | None = None) -> list[str]:
        """Build the scip-typescript invocation for ``project_dir`` (default: repo root)."""
        project_dir = project_dir or self.repo_root
        configured = self.config.scip_typescript_path
        if configured and configured.lower() != "npx":
            cmd = [configured]
        elif self.has_binary():
            cmd = ["scip-typescript"]
        else:
            cmd = ["npx", "@sourcegraph/scip-typescript"]

        cmd.append("index")

        if self.config.scip_typescript_args:
            cmd.extend(self.config.scip_typescript_args)

        # --infer-tsconfig only when the project has no tsconfig of its own
        # (scip-typescript needs *some* project graph to walk).
        infer = self.config.scip_typescript_infer_tsconfig
        if infer is True or infer is None and not (project_dir / "tsconfig.json").exists():
            cmd.append("--infer-tsconfig")

        return cmd

    async def _run_indexer(
        self, output_path: Path, cwd: Path | None = None
    ) -> tuple[int, str, str]:
        """Run the indexer in ``cwd`` (default: repo root) and return
        (returncode, stdout, stderr)."""
        cwd = cwd or self.repo_root
        cmd = self._build_command(cwd)
        cmd.extend(["--output", str(output_path)])
        logger.info("Running SCIP indexer in %s: %s", cwd, " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_index(
        self,
        all_files: list[Path],
        graph_id: str,
        force: bool = False,
    ) -> Path | None:
        """Return the path to ``index.scip``, running the indexer if needed.

        Args:
            all_files: All discovered source files (used for fingerprinting).
            graph_id: Graph identifier used for the output subdirectory.
            force: If True, re-run even when the fingerprint is unchanged.

        Returns:
            Path to ``index.scip`` on success, or ``None`` if indexing was
            skipped or failed.
        """
        if not self.should_run():
            logger.debug("SCIP-TypeScript indexing is disabled or unavailable")
            return None

        output_path = self._index_path(graph_id)
        fingerprint = self.compute_fingerprint(all_files)

        if not force:
            stored = self._read_stored_fingerprint(graph_id)
            if stored == fingerprint and output_path.exists():
                logger.debug("SCIP fingerprint unchanged; skipping indexer")
                return output_path

        output_path.parent.mkdir(parents=True, exist_ok=True)
        returncode, stdout, stderr = await self._run_indexer(output_path)
        if returncode != 0:
            logger.warning(
                "scip-typescript exited %d\nstdout: %s\nstderr: %s",
                returncode,
                stdout,
                stderr,
            )
            return None

        self._write_stored_fingerprint(graph_id, fingerprint)
        logger.info("SCIP index written to %s", output_path)
        return output_path

    def _project_index_path(self, graph_id: str, project_dir: Path) -> Path:
        """Per-project index.scip path; the repo root keeps the canonical name."""
        if project_dir == self.repo_root:
            return self._index_path(graph_id)
        rel = project_dir.relative_to(self.repo_root)
        slug = str(rel).replace("/", "__")
        return self._output_dir(graph_id) / f"index__{slug}.scip"

    async def ensure_project_indexes(
        self,
        all_files: list[Path],
        graph_id: str,
        force: bool = False,
    ) -> list[tuple[Path, Path]]:
        """Index every tsconfig project in the repo, not just the root.

        scip-typescript only walks the single tsconfig it is pointed at, so a
        repo with a root app plus a ``mobile/`` subproject would otherwise leave
        the subproject entirely uncovered. This runs the indexer once per project
        discovered by :meth:`discover_projects` and returns one
        ``(project_dir, scip_path)`` per project that indexed successfully.

        Fingerprinting is repo-wide (as for :meth:`ensure_index`): when unchanged
        and every project's index already exists, the run is skipped.

        Returns:
            ``(project_dir, scip_path)`` pairs. Empty if disabled/unavailable.
            A project whose indexer fails is dropped; other projects still count.
        """
        if not self.should_run():
            logger.debug("SCIP-TypeScript indexing is disabled or unavailable")
            return []

        projects = self.discover_projects()
        fingerprint = self.compute_fingerprint(all_files)
        expected = [(p, self._project_index_path(graph_id, p)) for p in projects]

        if not force:
            stored = self._read_stored_fingerprint(graph_id)
            if stored == fingerprint and all(sp.exists() for _, sp in expected):
                logger.debug("SCIP fingerprint unchanged; skipping all projects")
                return expected

        results: list[tuple[Path, Path]] = []
        for project_dir, scip_path in expected:
            scip_path.parent.mkdir(parents=True, exist_ok=True)
            returncode, stdout, stderr = await self._run_indexer(scip_path, cwd=project_dir)
            if returncode != 0:
                logger.warning(
                    "scip-typescript exited %d for %s\nstdout: %s\nstderr: %s",
                    returncode, project_dir, stdout, stderr,
                )
                continue
            results.append((project_dir, scip_path))

        # Only cache the fingerprint if at least one project succeeded, so a
        # total failure re-runs next time rather than being skipped as "fresh".
        if results:
            self._write_stored_fingerprint(graph_id, fingerprint)
        logger.info("SCIP indexed %d/%d projects", len(results), len(expected))
        return results
