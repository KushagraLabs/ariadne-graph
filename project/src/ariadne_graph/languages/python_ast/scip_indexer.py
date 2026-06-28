"""SCIP-Python indexer runner and project fingerprinting.

Detects whether ``scip-python`` (or ``npx @sourcegraph/scip-python``) can be
run, decides whether the current project fingerprint has changed, and runs the
indexer when needed. Mirrors :mod:`languages.typescript.scip_indexer` but for
Python sources.

Unlike the TypeScript indexer, this runner is synchronous: Python extraction
happens in the synchronous :meth:`PythonLanguageAdapter.extract_file` path, so
resolution is built lazily there rather than only in ``prepare_project``.

``scip-python`` emits a non-fatal ``AttributeError`` during "Gathering
environment information" on newer ``importlib.metadata``; it falls back and the
index is still written correctly, so a zero exit code with a present index file
is treated as success regardless of that warning on stderr.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import xxhash

from ariadne_graph.core.config import AnalyzerConfig

logger = logging.getLogger(__name__)

_SCIP_OUTPUT_SUBDIR = ".ariadne/scip-python"


class ScipPythonIndexer:
    """Run ``scip-python index`` for a Python project."""

    def __init__(self, repo_root: Path, config: AnalyzerConfig) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    @staticmethod
    def has_binary() -> bool:
        """Return True if ``scip-python`` is on PATH."""
        return shutil.which("scip-python") is not None

    @staticmethod
    def has_npx() -> bool:
        """Return True if ``npx`` is available."""
        return shutil.which("npx") is not None

    def is_available(self) -> bool:
        """Return True if the indexer can be invoked."""
        return self.has_binary() or self.has_npx()

    def should_run(self) -> bool:
        """Return True when SCIP-Python indexing is not explicitly disabled."""
        enabled = self.config.scip_python_enabled
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.is_available()

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    def compute_fingerprint(self, all_files: list[Path]) -> str:
        """Compute a stable fingerprint of all ``.py`` source files.

        Used to skip re-running ``scip-python`` when the Python source has not
        changed.
        """
        py_files = [p for p in all_files if p.suffix == ".py" and p.is_file()]
        pairs: list[tuple[str, str]] = []
        for file_path in sorted(py_files):
            try:
                content = file_path.read_bytes()
            except OSError as exc:
                logger.warning("Failed to read %s for SCIP fingerprint: %s", file_path, exc)
                continue
            digest = xxhash.xxh3_64_hexdigest(content)
            try:
                rel = str(file_path.resolve().relative_to(self.repo_root))
            except ValueError:
                rel = str(file_path)
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

    def _build_command(self, output_path: Path) -> list[str]:
        """Build the scip-python invocation."""
        configured = self.config.scip_python_path
        if configured and configured.lower() != "npx":
            cmd = [configured]
        elif self.has_binary():
            cmd = ["scip-python"]
        else:
            cmd = ["npx", "@sourcegraph/scip-python"]

        cmd += [
            "index",
            "--project-name",
            self.repo_root.name or "project",
            "--project-version",
            "0.0.1",
            "--output",
            str(output_path),
        ]
        if self.config.scip_python_args:
            cmd.extend(self.config.scip_python_args)
        cmd.append(".")
        return cmd

    def _run_indexer(self, output_path: Path) -> bool:
        """Run the indexer; return True if the index file was written."""
        cmd = self._build_command(output_path)
        logger.info("Running SCIP-Python indexer: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            logger.warning("Failed to launch scip-python: %s", exc)
            return False

        # scip-python may print a non-fatal AttributeError while gathering the
        # environment, then fall back and still write a correct index *and exit
        # 0*. A nonzero exit means a real failure: do NOT trust a (possibly
        # stale) leftover index file, or we would silently bless outdated
        # resolution. Remove any stale index so it cannot be reused later.
        if proc.returncode != 0:
            logger.warning(
                "scip-python exited %d\nstdout: %s\nstderr: %s",
                proc.returncode,
                proc.stdout,
                proc.stderr,
            )
            output_path.unlink(missing_ok=True)
            return False
        if not output_path.exists():
            logger.warning(
                "scip-python exited 0 but no index was written to %s", output_path
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_index(
        self,
        all_files: list[Path],
        graph_id: str,
        force: bool = False,
    ) -> Path | None:
        """Return the path to ``index.scip``, running the indexer if needed.

        Returns the index path on success, or ``None`` if indexing was skipped
        or failed.
        """
        if not self.should_run():
            logger.debug("SCIP-Python indexing is disabled or unavailable")
            return None

        output_path = self._index_path(graph_id)
        fingerprint = self.compute_fingerprint(all_files)

        if not force:
            stored = self._read_stored_fingerprint(graph_id)
            if stored == fingerprint and output_path.exists():
                logger.debug("SCIP-Python fingerprint unchanged; skipping indexer")
                return output_path

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._run_indexer(output_path):
            return None

        self._write_stored_fingerprint(graph_id, fingerprint)
        logger.info("SCIP-Python index written to %s", output_path)
        return output_path
