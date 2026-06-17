"""Background auto-sync polling for indexed repositories."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Protocol

from ariadne_graph.core.config import AnalyzerConfig
from ariadne_graph.mcp.schemas import IndexInput

if TYPE_CHECKING:
    from ariadne_graph.mcp.tools import ToolRegistry

logger = logging.getLogger(__name__)


class _IndexHandler(Protocol):
    """Minimal protocol for the registry operation used by auto-sync."""

    async def handle_index(self, input: IndexInput) -> Any:
        ...

    @property
    def graph_store(self) -> Any:
        ...


class AutoSyncManager:
    """Polls registered repositories and re-indexes them on a schedule.

    The manager runs an asyncio task that sleeps for ``interval`` seconds,
    lists all registered projects, and triggers an incremental index for
    each one. Concurrent syncs for the same graph are prevented.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        interval: float,
    ) -> None:
        self.registry: _IndexHandler = registry
        self.interval = max(interval, 5.0)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        """Start the background polling task if not already running."""
        if self._task is not None and not self._task.done():
            logger.warning("AutoSyncManager is already running")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="ariadne-auto-sync")
        logger.info("Auto-sync started with interval %.1fs", self.interval)

    async def stop(self) -> None:
        """Signal the polling task to stop and wait for it to finish."""
        if self._task is None or self._task.done():
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=self.interval + 5.0)
        except TimeoutError:
            logger.warning("Auto-sync task did not stop gracefully, cancelling")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("Auto-sync stopped")

    async def _loop(self) -> None:
        """Main polling loop."""
        while not self._stop_event.is_set():
            try:
                await self._sync_all_projects()
            except Exception as exc:
                logger.error("Auto-sync round failed: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval,
                )
            except TimeoutError:
                continue

    async def _sync_all_projects(self) -> None:
        """Re-index all registered projects that are not already syncing."""
        try:
            projects = await self.registry.graph_store.list_projects()
        except Exception as exc:
            logger.warning("Failed to list projects for auto-sync: %s", exc)
            return

        for project in projects:
            graph_id = project.get("graph_id", "")
            repo_path = project.get("repo_path", "")
            if not graph_id or not repo_path:
                continue
            if graph_id in self._in_flight:
                logger.debug("Skipping auto-sync for %s (already in flight)", graph_id)
                continue
            self._in_flight.add(graph_id)
            try:
                await self._sync_project(repo_path)
            finally:
                self._in_flight.discard(graph_id)

    async def _sync_project(self, repo_path: str) -> None:
        """Trigger an incremental index for a single project."""
        try:
            result = await self.registry.handle_index(
                IndexInput(repo_path=repo_path, force_rebuild=False)
            )
            logger.info(
                "Auto-synced %s: status=%s files_indexed=%d",
                repo_path,
                result.status,
                result.files_indexed,
            )
        except Exception as exc:
            logger.warning("Auto-sync failed for %s: %s", repo_path, exc)


def create_auto_sync_manager(
    registry: ToolRegistry,
    config: AnalyzerConfig,
) -> AutoSyncManager | None:
    """Create an AutoSyncManager if auto_sync is enabled in config.

    Args:
        registry: The ToolRegistry to drive indexing.
        config: AnalyzerConfig with sync settings.

    Returns:
        An initialised AutoSyncManager, or None if auto_sync is disabled.
    """
    if not config.auto_sync:
        return None
    return AutoSyncManager(registry, config.incremental_sync_interval)
