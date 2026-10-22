"""Concurrency, interrupted restore and safe diagnostics regression checks."""
from __future__ import annotations
import asyncio
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.ai import DeepSeek, ModelError, day_now
from bot.config import Config
from bot.health import supervise
from bot.model_queue import ModelQueue
from bot.operations import Operations
from bot.store import Store

CHAT = 'private:123456789'


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_and_capacity(self):
        queue = ModelQueue(capacity=1)
        order = []
        async def run(label, background=False):
            async with queue.slot(background=background):
                order.append(label)
        async with queue.slot():
            memory = asyncio.create_task(run('memory', True))
            await asyncio.sleep(0)
            chat = asyncio.create_task(run('chat'))
            await asyncio.sleep(0)
            self.assertEqual(queue.waiting, 2)
        await asyncio.gather(memory, chat)
        self.assertEqual(order, ['chat', 'memory'])
        self.assertEqual(queue.active, 0)

    async def test_background_leaves_two_slots_for_chat(self):
        queue = ModelQueue()
        started = asyncio.Event()
        async def background():
            async with queue.slot(background=True):
                started.set()
        async with queue.slot(background=True):
            task = asyncio.create_task(background())
            await asyncio.sleep(0)
            self.assertFalse(started.is_set())
            async with queue.slot(), queue.slot():
                self.assertEqual(queue.active, 3)
        await task
        self.assertTrue(started.is_set())

    async def test_cancel_after_grant_returns_capacity(self):
        queue = ModelQueue(capacity=1)
        async with AsyncExitStack() as holder:
            await holder.enter_async_context(queue.slot())
            async def wait():
                async with queue.slot():
                    pass
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            await holder.aclose()  # dispatches task, which has not resumed yet
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(queue.active, 0)
        self.assertEqual(queue.waiting, 0)
        async with asyncio.timeout(.5):
            async with queue.slot():
                pass


class AsyncStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root)
        self.store = Store(self.root)
        self.ops = Operations(self.config, self.store, offline=True)
        self.config.update({'paused': True, 'conversations': {CHAT: {'allowed': True, 'name': 'before'}}})

    async def asyncTearDown(self):
        await self.ops.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_snapshot_runs_off_loop_and_cancellation_keeps_gate(self):
        entered, release = threading.Event(), threading.Event()
        original = Operations.snapshot
        def slow_snapshot(worker, reason):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release worker')
            return original(worker, reason)
        with patch.object(Operations, 'snapshot', slow_snapshot):
            task = asyncio.create_task(self.ops.snapshot_async())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertTrue(self.config.frozen)
                task.cancel()
                await asyncio.sleep(0)
                self.assertEqual(self.ops.data_busy, 'snapshot')
                with self.assertRaises(ValueError):
                    await self.ops.snapshot_async()
                # A normal message write and read remain possible during a backup.
                self.store.add(CHAT, 'user', '123456789', 'while backing up')
                self.assertEqual(len(self.store.history(CHAT)), 1)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertFalse(self.config.frozen)
        self.assertFalse(self.store.maintenance)
        self.assertEqual(len(self.ops.backups()), 1)

    async def test_async_restore_uses_own_connection_and_updates_live_config(self):
        self.store.add(CHAT, 'user', '123456789', 'original')
        snap = await self.ops.snapshot_async()
        self.config.update({'conversations': {CHAT: {'name': 'after'}}})
        self.store.clear(CHAT)
        await self.ops.restore_async(snap['id'])
        self.assertEqual(self.config.conversation(CHAT)['name'], 'before')
        self.assertEqual(self.store.history(CHAT)[0]['text'], 'original')
        self.assertTrue(self.config.value['paused'])
        self.assertFalse(self.config.frozen)
        self.assertFalse(self.store.db.execute('SELECT * FROM restore_journal').fetchall())

    async def test_committed_restore_config_failure_replays_before_startup(self):
        from bot import operations
        self.store.add(CHAT, 'user', '123456789', 'original')
        snap = await self.ops.snapshot_async()
        self.config.update({'conversations': {CHAT: {'name': 'after'}}})
        self.store.clear(CHAT)
        original = operations.atomic_json
        def fail_config(path, data):
            if path == self.config.path:
                raise OSError('simulated power interruption')
            return original(path, data)
        with patch.object(operations, 'atomic_json', fail_config):
            with self.assertRaises(OSError):
                await self.ops.restore_async(snap['id'])
        self.assertTrue(self.store.maintenance)
        self.assertTrue(self.config.frozen)
        self.assertEqual(self.store.history(CHAT)[0]['text'], 'original')
        recovered = Config(self.root)
        self.assertEqual(recovered.conversation(CHAT)['name'], 'before')
        self.assertTrue(recovered.value['paused'])
        self.assertFalse(self.store.db.execute('SELECT * FROM restore_journal').fetchall())

    async def test_restore_failure_before_commit_rolls_back_everything(self):
        snap = await self.ops.snapshot_async()
        self.store.add(CHAT, 'user', '123456789', 'must survive')
        previous = copy.deepcopy(self.config.value)
        with patch('bot.memory.rebuild_indexes', side_effect=RuntimeError('interruption before commit')):
            with self.assertRaises(RuntimeError):
                await self.ops.restore_async(snap['id'])
        self.assertEqual(self.config.value, previous)
        self.assertEqual(self.store.history(CHAT)[0]['text'], 'must survive')
        self.assertFalse(self.store.db.execute('SELECT * FROM restore_journal').fetchall())
        self.assertFalse(self.store.maintenance)

    async def test_clear_defers_vacuum_until_idle_maintenance(self):
        self.store.add(CHAT, 'user', '123456789', 'remove me')
        statements = []
        self.store.db.set_trace_callback(statements.append)
        self.store.clear(CHAT)
        self.assertTrue(self.store.vacuum_pending)
        self.assertNotIn('VACUUM', statements)
        await self.ops._data_operation('vacuum')
        self.assertFalse(self.store.vacuum_pending)
        self.assertFalse(self.store.history(CHAT))

    async def test_queued_revoked_request_never_spends_budget(self):
        ai = DeepSeek(self.config, self.store, None)
        ai._request_completion = AsyncMock()
        async with AsyncExitStack() as slots:
            for _ in range(3):
                await slots.enter_async_context(ai.queue.slot())
            task = asyncio.create_task(ai.json('', {}, usage_chat=CHAT))
            await asyncio.sleep(0)
            self.config.update({'conversations': {CHAT: {'allowed': False}}})
        with self.assertRaisesRegex(ModelError, '取消'):
            await task
        ai._request_completion.assert_not_called()
        self.assertEqual(self.store.used(day_now()), 0)
        self.assertEqual(ai.failures, 0)

    async def test_tool_round_releases_http_slot_before_nested_model_call(self):
        ai = DeepSeek(self.config, self.store, None)
        ai.queue = ModelQueue(capacity=1)
        ai._request_completion = AsyncMock(return_value={'ok': True})
        async def json_with_nested(*args, **kwargs):
            await ai._completion([], {}, [])
            self.assertEqual(ai.queue.active, 0)
            return await ai.json('nested', {}, purpose='vision')
        async def dispatch(system, data, **kwargs):
            if system == 'nested':
                return await ai._completion([], {}, [])
            return await json_with_nested()
        ai._json = dispatch
        self.assertEqual(await asyncio.wait_for(ai.json('outer', {}), .5), {'ok': True})
        self.assertEqual(ai.queue.active, 0)

    async def test_safe_trace_does_not_capture_exception_text_or_locals(self):
        private_local = 'SECRET-NOT-LOGGED'
        try:
            raise RuntimeError(private_local)
        except RuntimeError as error:
            identifier = self.store.failure('test_error', error)
        trace = self.store.db.execute('SELECT trace FROM error_traces WHERE id=?', (identifier,)).fetchone()[0]
        self.assertIn('test_stability.py:', trace)
        self.assertIn('RuntimeError', trace)
        self.assertNotIn('SECRET', trace)
        self.assertNotIn(str(self.root), trace)

    async def test_background_task_failure_is_recorded_and_restarted(self):
        states, recovered = {}, asyncio.Event()
        calls = 0
        async def loop():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('SECRET')
            recovered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(supervise('test', loop, self.store, states))
        try:
            await asyncio.wait_for(recovered.wait(), 2)
            self.assertEqual(states['test']['restarts'], 1)
            self.assertEqual(states['test']['state'], 'running')
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(states['test']['state'], 'stopped')


class VersionProtection(unittest.TestCase):
    def test_newer_database_is_not_migrated_by_older_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = Config(root)
            store = Store(root)
            store.db.execute('PRAGMA user_version=999')
            store.db.close()
            for factory in (Config, Store):
                with self.assertRaisesRegex(ValueError, '版本'):
                    factory(root)
            with sqlite3.connect(root / 'data/memory.sqlite3') as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 999)


class IntegrationBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_six_model_rounds_share_three_http_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(Path(folder))
            store = Store(Path(folder))
            ai = DeepSeek(config, store, None)
            entered, release = asyncio.Event(), asyncio.Event()
            active = maximum = 0
            async def request(*args):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if active == 3:
                    entered.set()
                try:
                    await release.wait()
                    return {'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}
                finally:
                    active -= 1
            ai._request_completion = request
            tasks = [asyncio.create_task(ai.json('', {})) for _ in range(6)]
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertEqual(ai.queue.waiting, 3)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
                self.assertTrue(all(result['ok'] for result in results))
                self.assertEqual(maximum, 3)
                self.assertEqual(ai.queue.waiting, 0)
                self.assertEqual(ai.active, 0)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                store.db.close()

    async def test_status_probe_survives_unexpected_error_and_logging_failure(self):
        from bot.bridge import Bridge
        bridge = Bridge(None, None)
        bridge.refresh_status = AsyncMock(side_effect=[RuntimeError('SECRET'), False])
        bridge.report_exception = lambda error: (_ for _ in ()).throw(OSError('disk full'))
        with patch('bot.bridge.asyncio.sleep', AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await bridge.watch_status()
        self.assertEqual(bridge.refresh_status.await_count, 2)
        self.assertFalse(bridge.online)

    async def test_maintenance_blocks_writes_but_allows_status(self):
        from test_operations import OperationsAPI
        fixture = OperationsAPI()
        await fixture.asyncSetUp()
        try:
            ops = fixture.app['operations']
            ops.data_busy = 'restore'
            fixture.store.maintenance = True
            response = await fixture.client.post('/api/settings', json={
                'revision': fixture.config.value['revision'], 'patch': {'paused': True}})
            self.assertEqual(response.status, 503)
            response = await fixture.client.get('/api/status')
            self.assertEqual(response.status, 200)
            data = await response.json()
            self.assertTrue(data['operations']['blockers'])
        finally:
            await fixture.asyncTearDown()
