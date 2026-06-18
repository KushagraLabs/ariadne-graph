"""Filesystem watcher for event-driven incremental graph sync.

Uses ``watchdog`` when available and bridges its thread-based callbacks into
asyncio. Falls back to a clear import-time error if ``watchdog`` is missing
and the caller requested watcher mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from pathlib import Path
from typing import Any

from ariadne_graph.core.config import AnalyzerConfig

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
except ImportError:
    FileSystemEvent = Any  # type: ignore[misc,assignment]
    FileSystemEventHandler = object  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


# Default source extensions to watch when no explicit extension set is given.
DEFAULT_WATCHED_EXTENSIONS: set[str] = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
}


def _has_watchdog() -> bool:
    """Return True if the optional watchdog dependency is installed."""
    try:
        import watchdog  # noqa: F401

        return True
    except ImportError:
        return False


class _WatchdogEventHandler(FileSystemEventHandler):
    """Thread-safe handler that enqueues file-system events."""

    def __init__(
        self,
        queue: asyncio.Queue[set[Path]],
        repo_root: Path,
        ignore_patterns: list[str],
        extensions: set[str],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        super().__init__()
        self.queue = queue
        self.repo_root = repo_root.resolve()
        self.ignore_patterns = ignore_patterns
        self.extensions = extensions
        self.loop = loop

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Forward all relevant events into the async queue."""
        if not self._should_handle(event):
            return
        path = Path(str(event.src_path)).resolve()
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(self.queue.put({path}), self.loop)

    def _is_ignored(self, path: Path) -> bool:
        """Check whether a path matches any ignore pattern."""
        try:
            rel = path.relative_to(self.repo_root)
        except ValueError:
            rel = path

        rel_str = str(rel)
        for part in rel.parts:
            for pattern in self.ignore_patterns:
                if fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(rel_str, pattern):
                    return True
        return False

    def _should_handle(self, event: FileSystemEvent) -> bool:
        """Return True if the event is worth forwarding to the async consumer."""
        if event.is_directory:
            return False
        path = Path(str(event.src_path)).resolve()
        if self._is_ignored(path):
            return False
        return bool(
            not self.extensions or path.suffix.lower() in self.extensions
        )


class FileWatcher:
    """Async filesystem watcher for a repository root.

    Example::

        watcher = FileWatcher(config, extensions={".py", ".ts"})
        await watcher.start(Path("/path/to/repo"))
        changed = await watcher.wait_for_changes()
        await watcher.stop()
    """

    def __init__(
        self,
        config: AnalyzerConfig,
        extensions: set[str] | None = None,
    ) -> None:
        self.config = config
        self.extensions = extensions or DEFAULT_WATCHED_EXTENSIONS
        self.repo_root: Path | None = None
        self._raw_queue: asyncio.Queue[set[Path]] = asyncio.Queue()
        self._batch_queue: asyncio.Queue[set[Path]] = asyncio.Queue()
        self._observer: Any | None = None
        self._handler: _WatchdogEventHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @staticmethod
    def is_available() -> bool:
        """Return whether the optional watchdog dependency is installed."""
        return _has_watchdog()

    async def start(self, repo_root: Path) -> None:
        """Start watching ``repo_root`` recursively.

        Raises:
            ImportError: If ``watchdog`` is not installed.
        """
        if not _has_watchdog():
            raise ImportError(
                "watchdog is required for file-system watching. "
                'Install with: pip install -e ".[watch]"'
            )

        from watchdog.observers import Observer

        self.repo_root = repo_root.resolve()
        loop = asyncio.get_running_loop()
        self._handler = _WatchdogEventHandler(
            queue=self._raw_queue,
            repo_root=self.repo_root,
            ignore_patterns=self.config.ignore_patterns,
            extensions=self.extensions,
            loop=loop,
        )
        self._observer = Observer()
        self._observer.schedule(
            self._handler, str(self.repo_root), recursive=True
        )
        self._observer.start()
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._consume(), name="ariadne-file-watcher"
        )
        logger.info("Started file watcher for %s", self.repo_root)

    async def stop(self) -> None:
        """Stop the observer and consumer task."""
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            except Exception as exc:
                logger.warning("Error stopping file watcher consumer: %s", exc)
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5.0)
            except Exception as exc:
                logger.warning("Error stopping file observer: %s", exc)
        logger.info("Stopped file watcher for %s", self.repo_root)

    async def wait_for_changes(self) -> set[Path]:
        """Wait until at least one change has been detected and debounced.

        Returns a set of changed file paths.
        """
        return await self._batch_queue.get()

    async def _consume(self) -> None:
        """Consume raw events, debounce, and emit batched changed paths."""
        debounce = max(self.config.watch_debounce, 0.1)
        pending: set[Path] = set()

        while not self._stop_event.is_set():
            try:
                if not pending:
                    batch = await self._raw_queue.get()
                    pending.update(batch)

                # Wait for debounce quiet period, collecting more events.
                while True:
                    try:
                        extra = await asyncio.wait_for(
                            self._raw_queue.get(), timeout=debounce
                        )
                        pending.update(extra)
                    except TimeoutError:
                        break

                if pending:
                    logger.debug(
                        "File watcher emitting %d changed paths", len(pending)
                    )
                    await self._batch_queue.put(pending)
                    pending = set()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("File watcher consumer error: %s", exc)


