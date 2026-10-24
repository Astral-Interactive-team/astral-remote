"""Explicit Discord Host runtime layered on an already-running Agent."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from astral_remote.agent.connection import is_fatal_auth_error, login_with_backoff
from astral_remote.agent.runtime import AgentRuntime


class HostRuntime:
    """Own one optional Discord Host connection for an Agent process.

    Future election code activates this runtime only after choosing a host.
    Calling :meth:`start` twice shares the same start task and never creates
    a second Discord client.  Stopping this runtime does not stop its Agent.
    """

    def __init__(self, agent: AgentRuntime, token: str | None = None, config: dict | None = None, *,
                 credential_store=None, start_host: Callable[[dict, str], Awaitable[Any]] | None = None,
                 lifecycle=None):
        if not isinstance(agent, AgentRuntime):
            raise TypeError("agent must be an AgentRuntime.")
        self._agent = agent
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._credential_store = credential_store
        self._config = dict(config or {})
        self._start_host = start_host or self._default_start
        self._lifecycle = lifecycle
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._lock: asyncio.Lock | None = None

    @property
    def lifecycle(self):
        return self._lifecycle

    @lifecycle.setter
    def lifecycle(self, value):
        self._lifecycle = value

    def is_authoritative(self) -> bool:
        if self._lifecycle is not None:
            return self._lifecycle.is_authoritative()
        return self.started

    @property
    def started(self) -> bool:
        return self._client is not None or (self._task is not None and not self._task.done())

    @property
    def client(self) -> Any:
        return self._client

    async def start(self) -> Any:
        """Explicitly activate Host Discord work, once per process."""
        if not self._agent.started:
            await self._agent.start()
        lock = self._runtime_lock()
        async with lock:
            if self._client is not None:
                return self._client
            if self._task is None or self._task.done():
                if self._token is None:
                    if self._credential_store is None:
                        from astral_remote.shared.security import HostCredentialStore
                        self._credential_store = HostCredentialStore()
                    self._token = self._credential_store.load_token()
                self._task = asyncio.create_task(self._connect())
            self._client = await self._task
            return self._client

    async def wait(self) -> Any:
        """Wait until Host connection exits, returning its client object."""
        await self.start()
        if self._task is None:
            return self._client
        self._client = await self._task
        return self._client

    async def close(self) -> None:
        """Stop only Host work. Local Agent state remains available."""
        client = self._client
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if asyncio.iscoroutine(result):
                    await result
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._client = None
        self._task = None

    def _runtime_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _connect(self) -> Any:
        try:
            return await login_with_backoff(
                lambda: self._start_host(self._config, self._token),
                is_fatal=is_fatal_auth_error,
            )
        except Exception as err:
            if is_fatal_auth_error(err):
                raise RuntimeError(
                    "Discord rejected the stored Host credential. Reset it with `astral-remote credentials set`."
                ) from err
            raise

    async def _default_start(self, config: dict, token: str) -> Any:
        from astral_remote.agent.discord_bot import start_bot

        return await start_bot(config, token, authority_checker=self.is_authoritative)
