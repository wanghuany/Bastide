"""Tie desktop process lifetime to open pages without requiring a vault password."""

import asyncio
import time
from collections.abc import Callable


class BrowserLifetime:
    """Allow refresh/reconnect, and stop only after the final page has disconnected."""

    def __init__(
        self,
        on_exit: Callable[[], None],
        *,
        grace_seconds: float = 5,
        startup_seconds: float = 120,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.on_exit = on_exit
        self.clock = clock
        self.grace_seconds = grace_seconds
        self.startup_deadline = clock() + startup_seconds
        self.empty_since: float | None = None
        self.connections: set[object] = set()
        self.stopping = False
        self.keep_running: Callable[[], bool] = lambda: False

    def connect(self) -> object:
        connection = object()
        self.connections.add(connection)
        self.empty_since = None
        return connection

    def disconnect(self, connection: object) -> None:
        if connection in self.connections:
            self.connections.remove(connection)
            if not self.connections:
                self.empty_since = self.clock()

    def shutdown_due(self) -> bool:
        if self.connections:
            return False
        if self.empty_since is not None:
            return self.clock() >= self.empty_since + self.grace_seconds
        return self.clock() >= self.startup_deadline

    async def watch(self) -> None:
        """Ask Uvicorn to drain requests and run normal encrypted-vault cleanup."""
        while self.keep_running() or not self.shutdown_due():
            await asyncio.sleep(0.25)
        self.stopping = True
        self.on_exit()
