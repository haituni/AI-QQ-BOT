"""Supervise long-lived loops and retain safe failure locations."""
from __future__ import annotations
import asyncio
import time
from collections.abc import Callable, Awaitable


async def supervise(name: str, factory: Callable[[], Awaitable], store, states: dict) -> None:
    state = states.setdefault(name, {'state': 'starting', 'restarts': 0, 'last_failure': 0, 'error_id': ''})
    delay = 1
    while True:
        started = time.monotonic()
        state['state'] = 'running'
        try:
            await factory()
            raise RuntimeError('Background loop returned')
        except asyncio.CancelledError:
            state['state'] = 'stopped'
            raise
        except Exception as error:
            state.update(state='retrying', last_failure=time.time())
            try:
                state['error_id'] = store.failure('background_error', error)
            except Exception:
                # A full or broken disk must not kill the supervisor itself.
                state['error_id'] = 'diagnostics_unavailable'
            state['restarts'] += 1
            if time.monotonic() - started > 60:
                delay = 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                state['state'] = 'stopped'
                raise
            delay = min(delay * 2, 60)
