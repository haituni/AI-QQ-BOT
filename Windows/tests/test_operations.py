from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web, ClientSession
from aiohttp.test_utils import TestClient, TestServer

from bot.ai import DeepSeek, ModelError, day_now
from bot.bridge import BridgeError
from bot.config import Config
from bot.engine import Engine
from bot.operations import Operations
from bot.server import create_app
from bot.store import Store

CHAT='private:123456789'


class LocalSnapshots(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=Config(self.root);self.store=Store(self.root)
        self.ops=Operations(self.config,self.store,offline=True)
        self.config.update({'conversations':{CHAT:{'allowed':True,'name':'原始备注'}}})
    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def test_verified_restore_preserves_current_secrets_budgets_and_dedup(self):
        self.store.add(CHAT,'user','123456789','原始消息')
        state=self.store.state(CHAT);state['memory']='原始摘要';self.store.save_state(CHAT,state)
        snap=self.ops.snapshot()
        self.config.update({'api_key':'new-secret','admin_qq':['888889999'],'conversations':{CHAT:{'name':'新备注'}}})
        self.store.clear(CHAT);self.store.add(CHAT,'user','123456789','新消息')
        self.store.reserve_call(day_now(),10);self.store.once('already-received')
        generation=self.store.state(CHAT)['generation']
        self.ops.restore(snap['id'])
        self.assertTrue(self.config.value['paused']);self.assertEqual(self.config.value['api_key'],'new-secret')
        self.assertEqual(self.config.value['admin_qq'],['888889999'])
        self.assertEqual(self.config.conversation(CHAT)['name'],'原始备注')
        self.assertEqual(self.store.history(CHAT)[0]['text'],'原始消息')
        self.assertEqual(self.store.state(CHAT)['memory'],'原始摘要')
        self.assertGreater(self.store.state(CHAT)['generation'],generation)
        self.assertEqual(self.store.used(day_now()),1);self.assertFalse(self.store.once('already-received'))
        self.assertTrue(any(b['reason']=='before_restore' for b in self.ops.backups()))
        self.assertEqual(os.stat(self.root/'backups'/snap['id']/'settings.json').st_mode&0o777,0o600)

    def test_tampering_rejected_without_changes(self):
        snap=self.ops.snapshot();before=copy.deepcopy(self.config.value)
        (self.root/'backups'/snap['id']/'settings.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'完整性'):self.ops.restore(snap['id'])
        self.assertEqual(self.config.value,before)

    def test_backup_retention_and_traversal(self):
        for _ in range(10):self.ops.snapshot()
        self.assertEqual(len(self.ops.backups()),8)
        for identifier in ('../config',None,'snapshot-foo'):
            with self.assertRaises(ValueError):self.ops.restore(identifier)

    def test_corrupt_job_metadata_does_not_prevent_startup(self):
        path=self.root/'runtime/operation.json';path.parent.mkdir(exist_ok=True);path.write_text('{broken')
        recovered=Operations(self.config,self.store,offline=True)
        self.assertIsNone(recovered.job)
        self.assertEqual(self.store.audits()[0]['kind'],'runtime_job')

    def test_offline_cannot_start_real_environment(self):
        with self.assertRaisesRegex(ValueError,'离线'):self.ops.begin('start')
        self.assertIsNone(self.ops.task)

    def test_crashed_delivery_reopens_as_unknown_without_retry(self):
        identifier=self.store.delivery(CHAT,'reply')
        reopened=Store(self.root)
        try:self.assertEqual(reopened.db.execute('SELECT status FROM deliveries WHERE id=?',(identifier,)).fetchone()[0],'unknown')
        finally:reopened.db.close()

    def test_message_search_pagination_is_literal_and_isolated(self):
        for n in range(65):self.store.add(CHAT,'user','123',f'消息 {n} %_')
        self.store.add('private:999998888','user','456','另一会话秘密')
        first=self.store.message_page(CHAT,query='%_');second=self.store.message_page(CHAT,first['before'],query='%_')
        self.assertEqual(len(first['messages']),50);self.assertTrue(first['more'])
        self.assertEqual(len(second['messages']),15);self.assertFalse(second['more'])
        self.assertFalse({x['id'] for x in first['messages']}&{x['id'] for x in second['messages']})
        self.assertEqual(self.store.message_page(CHAT,query='秘密')['messages'],[])


class ModelProtection(unittest.IsolatedAsyncioTestCase):
    async def test_failures_open_circuit_without_spending_more_calls_then_recover(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder));config.update({'api_key':'test-secret'})
            store=Store(Path(folder));mode={'fail':True};calls=[]
            async def complete(request):
                calls.append(1)
                if mode['fail']:return web.json_response({'error':'sensitive-error-body'},status=503)
                return web.json_response({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'prompt_tokens':12,'completion_tokens':4}})
            app=web.Application();app.router.add_post('/',complete)
            async with TestServer(app) as server,ClientSession() as session:
                ai=DeepSeek(config,store,session);ai.endpoint=str(server.make_url('/'))
                for _ in range(3):
                    with self.assertRaises(ModelError):await ai.json('',{})
                self.assertGreater(ai.retry_at,time.time());self.assertEqual(store.used(day_now()),3)
                with self.assertRaisesRegex(ModelError,'保护'):await ai.json('',{})
                self.assertEqual(len(calls),3);self.assertNotIn('sensitive',ai.last_error)
                ai.retry_at=0;mode['fail']=False;self.assertTrue((await ai.json('',{}))['ok'])
                self.assertEqual(ai.failures,0);self.assertEqual(ai.active,0)
                metrics=store.metrics(0);self.assertEqual(metrics['total'],4);self.assertEqual(metrics['success'],1);self.assertEqual(metrics['input_tokens'],12)
            store.db.close()

    async def test_concurrency_wait_does_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder));store=Store(Path(folder))
            async with ClientSession() as session:
                ai=DeepSeek(config,store,session)
                from contextlib import AsyncExitStack
                async with AsyncExitStack() as slots:
                    for _ in range(3):await slots.enter_async_context(ai.queue.slot())
                    task=asyncio.create_task(ai.json('',{}))
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertEqual(ai.queue.waiting,1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(store.used(day_now()),0)
            store.db.close()

    async def test_send_failure_and_cancel_remain_uncertain(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder));store=Store(Path(folder))
            bridge=type('Fake',(),{})();bridge.send=AsyncMock(side_effect=BridgeError('unknown'))
            engine=Engine(config,store,None,bridge)
            with self.assertRaises(BridgeError):await engine.send(CHAT,'secret reply')
            bridge.send.side_effect=asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):await engine.send(CHAT,'another reply')
            rows=[dict(r) for r in store.db.execute('SELECT * FROM deliveries')]
            self.assertEqual([r['status'] for r in rows],['unknown','unknown'])
            self.assertNotIn('secret',json.dumps(rows));store.db.close()


class OperationsAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(self.root,offline=True)
        self.client=TestClient(TestServer(self.app),headers={'Host':'127.0.0.1:8765','X-Bot-Request':'1'})
        await self.client.start_server()
        self.config=self.app['config'];self.store=self.app['store']
        await self.client.post('/api/login',json={'token':self.config.control_token})
    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    async def test_shutdown_requires_confirmed_vm_stop_and_isolated_mode_blocks_it(self):
        from unittest.mock import patch
        response=await self.client.post('/api/shutdown',json={})
        self.assertEqual(response.status,400)
        self.assertFalse(self.app['shutdown_event'].is_set())
        operations=self.app['operations']
        operations.offline=False
        with patch('bot.server.ROOT',self.root):
            for job in (None,{'command':'start','status':'succeeded'}, {'command':'stop','status':'failed'}, {'command':'stop','status':'running'}):
                operations.job=job
                response=await self.client.post('/api/shutdown',json={})
                self.assertEqual(response.status,400)
                self.assertFalse(self.app['shutdown_event'].is_set())
            operations.job={'command':'stop','status':'succeeded'}
            response=await self.client.post('/api/shutdown',json={})
            self.assertEqual(response.status,200)
            await asyncio.wait_for(self.app['shutdown_event'].wait(),2)

    async def test_error_envelope_and_headers_even_for_auth_and_bad_json(self):
        for payload in ('[1,2]','{invalid','null'):
            response=await self.client.post('/api/settings',data=payload,headers={'Content-Type':'application/json'})
            self.assertEqual(response.status,400);self.assertIn('error',await response.json())
            self.assertEqual(response.headers['X-Frame-Options'],'DENY');self.assertIn('X-Request-ID',response.headers)
        response=await self.client.post('/api/login',json={'token':'错误的中文密钥'})
        self.assertEqual(response.status,401)

    async def test_restore_requires_pause_and_matching_revision(self):
        snap=await self.client.post('/api/backups',json={});identifier=(await snap.json())['id']
        response=await self.client.post('/api/backups/restore',json={'id':identifier,'revision':self.config.value['revision']})
        self.assertEqual(response.status,400)
        self.config.update({'paused':True})
        response=await self.client.post('/api/backups/restore',json={'id':identifier,'revision':0})
        self.assertEqual(response.status,409)
        response=await self.client.post('/api/backups/restore',json={'id':identifier,'revision':self.config.value['revision']})
        self.assertEqual(response.status,200);self.assertTrue(self.config.value['paused'])

    async def test_memory_optimistic_edit_invalidates_inflight_generation(self):
        self.config.update({'conversations':{CHAT:{}}})
        response=await self.client.post('/api/memory',json={'chat':CHAT,'previous':'','memory':'修改后的摘要'})
        self.assertEqual(response.status,200);self.assertEqual(self.store.state(CHAT)['generation'],1)
        response=await self.client.post('/api/memory',json={'chat':CHAT,'previous':'','memory':'覆盖'})
        self.assertEqual(response.status,409)

    async def test_diagnostics_and_light_status_exclude_content_and_secrets(self):
        self.config.update({'api_key':'SECRET-API','conversations':{CHAT:{}}})
        self.store.add(CHAT,'user','123','SECRET-CONTENT')
        response=await self.client.get('/api/diagnostics');body=await response.text()
        for value in ('SECRET-API','SECRET-CONTENT',CHAT,self.config.value['bridge_token']):self.assertNotIn(value,body)
        response=await self.client.get('/api/status?light=1');data=await response.json()
        self.assertNotIn('roleplay',data['config']);self.assertFalse(data['operations']['ready'])

    async def test_second_login_does_not_revoke_first_session(self):
        old=[c.value for c in self.client.session.cookie_jar if c.key=='qqbot_session'][0]
        await self.client.post('/api/login',json={'token':self.config.control_token})
        response=await self.client.get('/api/status',headers={'Cookie':'qqbot_session='+old})
        self.assertEqual(response.status,200)

    async def test_export_contains_only_selected_memory(self):
        self.store.add(CHAT,'user','123','选中会话')
        self.store.add('private:777778888','user','456','其他会话秘密')
        response=await self.client.get('/api/memory/export?chat='+CHAT);body=await response.text()
        self.assertIn('选中会话',body);self.assertNotIn('其他会话秘密',body)

class SchedulerFairness(unittest.IsolatedAsyncioTestCase):
    async def test_slow_conversation_does_not_block_another_due_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder));store=Store(Path(folder))
            second='private:777778888'
            conv={'allowed':True,'proactive':True,'start':'00:00','end':'00:00'}
            config.update({'api_key':'fake','conversations':{CHAT:conv,second:conv}})
            for chat in (CHAT,second):
                state=store.state(chat);state['next_at']=time.time()-1;store.save_state(chat,state)
            gate=asyncio.Event();fast=asyncio.Event()
            async def decide(key,proactive):
                if key==CHAT:await gate.wait()
                else:fast.set()
                return {'reply':False,'text':'','memory':'','next_minutes':60}
            ai=type('FakeAI',(),{})();ai.decide=decide
            bridge=type('FakeBridge',(),{'connected':True})()
            engine=Engine(config,store,ai,bridge)
            task=asyncio.create_task(engine.tick())
            try:
                await asyncio.wait_for(fast.wait(),1)
                self.assertFalse(task.done())
            finally:
                gate.set();await task;await engine.close();store.db.close()

class RecoveryBoundaries(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OperationsAPI.asyncSetUp
    asyncTearDown = OperationsAPI.asyncTearDown
    async def test_unexpected_error_response_never_exposes_exception_details(self):
        from unittest.mock import patch
        with patch.object(self.app['operations'],'snapshot',side_effect=RuntimeError('SECRET-EXCEPTION')):
            response=await self.client.post('/api/backups',json={})
        data=await response.json();self.assertEqual(response.status,500)
        self.assertNotIn('SECRET',json.dumps(data));self.assertIn('request_id',data)
        audit=self.store.audits()[0];self.assertIn(data['request_id'],audit['detail']);self.assertNotIn('SECRET',audit['detail'])

    async def test_restore_rejects_pending_send(self):
        snap=self.app['operations'].snapshot();self.config.update({'paused':True})
        self.app['bridge'].pending['test']=asyncio.get_running_loop().create_future()
        response=await self.client.post('/api/backups/restore',json={'id':snap['id'],'revision':self.config.value['revision']})
        self.assertEqual(response.status,400)
        self.app['bridge'].pending.pop('test').cancel()

    async def test_correcting_connection_settings_resets_model_protection(self):
        ai=self.app['ai'];ai.failures=3;ai.retry_at=time.time()+60
        response=await self.client.post('/api/settings',json={'revision':0,'patch':{'api_key':'changed-key'}})
        self.assertEqual(response.status,200);self.assertEqual(ai.failures,0);self.assertEqual(ai.retry_at,0)

    async def test_malformed_runtime_commands_are_validation_errors(self):
        for command in ([],{},None,'delete'):
            response=await self.client.post('/api/runtime',json={'command':command})
            self.assertEqual(response.status,400)


class RuntimeJobSafety(unittest.IsolatedAsyncioTestCase):
    async def test_carriage_return_progress_is_available_before_process_exits(self):
        from bot.operations import runtime_lines
        stream = asyncio.StreamReader()
        stream.feed_data(b'10%\r20%\r')
        lines = runtime_lines(stream)
        self.assertEqual(await asyncio.wait_for(anext(lines), 1), '10%')
        self.assertEqual(await asyncio.wait_for(anext(lines), 1), '20%')
        stream.feed_data('完成'.encode('utf-8')[:2])
        stream.feed_data('完成'.encode('utf-8')[2:])
        stream.feed_eof()
        self.assertEqual(await anext(lines), '完成')
        with self.assertRaises(StopAsyncIteration):
            await anext(lines)

    async def test_runtime_output_is_redacted_and_result_is_durable(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=Config(root);config.update({'api_key':'TEST-SECRET-KEY'})
            store=Store(root);ops=Operations(config,store,offline=True)
            ops.job={'command':'install','status':'running','lines':[],'started':time.time()}
            stream=asyncio.StreamReader();stream.feed_data(b'progress TEST-SECRET-KEY bearer PRIVATE-TOKEN\rproxy http://user:password@localhost:7897\r');stream.feed_eof()
            process=type('Process',(),{'stdout':stream,'returncode':0})()
            process.wait=AsyncMock(return_value=0)
            with patch('bot.operations.asyncio.create_subprocess_exec',new=AsyncMock(return_value=process)):
                await ops.run('install')
            self.assertEqual(ops.job['status'],'succeeded')
            saved=(root/'runtime/operation.json').read_text()
            self.assertNotIn('TEST-SECRET-KEY',saved);self.assertNotIn('PRIVATE-TOKEN',saved);self.assertNotIn('user:password',saved)
            self.assertIn('progress',saved);store.db.close()
