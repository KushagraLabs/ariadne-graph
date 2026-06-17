"""Source code snippet extraction."""

from __future__ import annotations

from pathlib import Path

from ariadne_graph.core.models import CodeNode


class SnippetExtractor:
    """Extracts source code snippets with context and line numbers."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()

    def _resolve_file(self, file_path: str) -> Path:
        """Resolve a file path relative to repo_root."""
        path = Path(file_path)
        if not path.is_absolute():
            path = self.repo_root / path
        return path.resolve()

    def get_snippet(
        self,
        file_path: str,
        line_start: int,
        line_end: int,
        context_lines: int = 3,
    ) -> str:
        """Read a file and extract lines with context.

        Extracts from (line_start - context_lines) to (line_end + context_lines),
        clamped to valid line range. Returns formatted string with line numbers.

        Args:
            file_path: Path to the source file (relative to repo_root or absolute).
            line_start: 1-based starting line number of the target region.
            line_end: 1-based ending line number of the target region.
            context_lines: Number of extra context lines to include before and after.

        Returns:
            Formatted snippet with line numbers, or an error message if the file
            cannot be read.
        """
        path = self._resolve_file(file_path)

        if not path.exists():
            return f"# File not found: {file_path}"

        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            return f"# Error reading {file_path}: {exc}"

        if not lines:
            return "# (empty file)\n"

        # Clamp to valid range (1-based → 0-based indexing)
        total_lines = len(lines)
        start_idx = max(0, line_start - context_lines - 1)
        end_idx = min(total_lines, line_end + context_lines)

        # Highlight range (0-based, inclusive)
        highlight_start = line_start - 1
        highlight_end = line_end - 1

        result_lines: list[str] = []
        for idx in range(start_idx, end_idx):
            line_num = idx + 1
            line_text = lines[idx].rstrip("\n").rstrip("\r")
            prefix = ">>>" if highlight_start <= idx <= highlight_end else "   "
            result_lines.append(f"{prefix} {line_num:4d} | {line_text}")

        return "\n".join(result_lines) + "\n"

    def get_node_snippet(self, node: CodeNode, context_lines: int = 3) -> str:
        """Extract snippet for a CodeNode using its line range.

        Args:
            node: The code node with line_start and line_end properties.
            context_lines: Number of extra context lines to include.

        Returns:
            Formatted snippet with line numbers.
        """
        file_path = node.properties.get("file_path", "")
        line_start = node.properties.get("line_start", 1)
        line_end = node.properties.get("line_end", line_start)

        # Handle case where line info might be missing or None
        if line_start is None or not isinstance(line_start, int):
            line_start = 1
        if line_end is None or not isinstance(line_end, int):
            line_end = line_start

        return self.get_snippet(
            file_path=file_path,
            line_start=line_start,
            line_end=line_end,
            context_lines=context_lines,
        )
