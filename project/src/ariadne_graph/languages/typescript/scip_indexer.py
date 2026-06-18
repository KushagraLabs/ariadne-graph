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

    def _build_command(self) -> list[str]:
        """Build the scip-typescript invocation."""
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

        infer = self.config.scip_typescript_infer_tsconfig
        if infer is True or infer is None and not (self.repo_root / "tsconfig.json").exists():
            cmd.append("--infer-tsconfig")

        return cmd

    async def _run_indexer(self, output_path: Path) -> tuple[int, str, str]:
        """Run the indexer and return (returncode, stdout, stderr)."""
        cmd = self._build_command()
        cmd.extend(["--output", str(output_path)])
        logger.info("Running SCIP indexer: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.repo_root),
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
