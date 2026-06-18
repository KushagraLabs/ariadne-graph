"""Event-driven auto-sync using a filesystem watcher.

When ``watchdog`` is installed and ``ARIADNE_WATCH_MODE`` is not ``poll``,
this manager watches indexed repositories and triggers targeted incremental
syncs as soon as files change. It falls back to the polling-based
``AutoSyncManager`` if ``watchdog`` is unavailable or watcher mode is
explicitly disabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ariadne_graph.core.config import AnalyzerConfig

if TYPE_CHECKING:
    from ariadne_graph.mcp.tools import ToolRegistry

logger = logging.getLogger(__name__)


class WatchSyncManager:
    """Watches registered repositories and syncs changed files on the fly.

    Creates one filesystem watcher per registered project. Detected changes
    are debounced and then passed to ``ToolRegistry.handle_targeted_sync``,
    which only re-parses the files that actually changed.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.config: AnalyzerConfig = registry.config
        self._watchers: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()
        self._in_flight: set[str] = set()

    @staticmethod
    def is_available() -> bool:
        """Return True if watchdog is installed."""
        try:
            from ariadne_graph.core.file_watcher import FileWatcher

            return FileWatcher.is_available()
        except Exception:
            return False

    async def start(self) -> None:
        """Start watchers for all registered projects."""
        if not self.is_available():
            logger.warning(
                "WatchSyncManager requested but watchdog is not installed. "
                'Install with: pip install -e ".[watch]"'
            )
            return

        try:
            projects = await self.registry.graph_store.list_projects()
        except Exception as exc:
            logger.warning("Failed to list projects for watch sync: %s", exc)
            return

        for project in projects:
            graph_id = project.get("graph_id", "")
            repo_path = project.get("repo_path", "")
            if not graph_id or not repo_path:
                continue
            if graph_id in self._tasks and not self._tasks[graph_id].done():
                continue
            self._tasks[graph_id] = asyncio.create_task(
                self._watch_project(repo_path, graph_id),
                name=f"ariadne-watch-{graph_id}",
            )

        logger.info("WatchSyncManager started for %d project(s)", len(self._tasks))

    def is_running(self) -> bool:
        """Return True if at least one watcher task is currently active."""
        return any(not task.done() for task in self._tasks.values())

    async def stop(self) -> None:
        """Stop all watchers and wait for tasks to finish."""
        self._stop_event.set()
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._watchers.clear()
        logger.info("WatchSyncManager stopped")

    async def _watch_project(self, repo_path: str, graph_id: str) -> None:
        """Watch a single project and trigger targeted syncs on changes."""
        from ariadne_graph.core.file_watcher import FileWatcher

        watcher = FileWatcher(self.config, extensions=self._adapter_extensions())
        try:
            await watcher.start(Path(repo_path))
        except Exception as exc:
            logger.warning("Failed to start watcher for %s: %s", repo_path, exc)
            return

        self._watchers[graph_id] = watcher
        try:
            while not self._stop_event.is_set():
                try:
                    changed = await asyncio.wait_for(
                        watcher.wait_for_changes(), timeout=1.0
                    )
                except TimeoutError:
                    continue
                if changed:
                    await self._sync_project(repo_path, graph_id, changed)
        except asyncio.CancelledError:
            pass
        finally:
            await watcher.stop()
            self._watchers.pop(graph_id, None)

    async def _sync_project(
        self, repo_path: str, graph_id: str, changed_files: set[Path]
    ) -> None:
        """Trigger a targeted sync for a single project."""
        if graph_id in self._in_flight:
            logger.debug("Skipping watch sync for %s (already in flight)", graph_id)
            return
        self._in_flight.add(graph_id)
        try:
            result = await self.registry.handle_targeted_sync(
                repo_path, list(changed_files)
            )
            logger.info(
                "Watch-synced %s: status=%s files_indexed=%d",
                repo_path,
                result.status,
                result.files_indexed,
            )
        except Exception as exc:
            logger.warning("Watch sync failed for %s: %s", repo_path, exc)
        finally:
            self._in_flight.discard(graph_id)

    def _adapter_extensions(self) -> set[str]:
        """Collect file extensions from registered language adapters."""
        extensions: set[str] = set()
        for adapter in self.registry.adapters.values():
            extensions.update(getattr(adapter, "extensions", ()))
        if extensions:
            return extensions
        # Safe default if adapters do not advertise extensions.
        return {
            ".py",
            ".pyi",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
        }
