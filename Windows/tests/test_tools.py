from __future__ import annotations
import asyncio, copy, json, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer, TestClient
from bot.config import Config
from bot.store import Store
from bot.ai import DeepSeek, ModelError, day_now
from bot.server import create_app
from bot.toolkit import ToolService, calculate, public_url, defaults

CHAT='private:123456789'

class ToolSafety(unittest.TestCase):
    def test_calculator_is_bounded_and_never_evaluates_code(self):
        self.assertEqual(calculate('(128*3+56)/4')['value'],110)
        for value in ('__import__("os").system("pwd")','2**999999','1/0','[1,2]','True','2**(2**12)'):
            with self.assertRaises(ValueError):calculate(value)
    def test_links_and_domain_rules(self):
        cfg=defaults()
        for url in ('file:///etc/passwd','http://127.0.0.1/','http://localhost/','http://192.168.1.1/','http://a.local/','https://user:pw@example.com/','https://example.com:8080/','https://example.com/?token=x'):
            with self.assertRaises(ValueError):public_url(url,cfg)
        cfg['allowed_domains']=['example.com'];cfg['blocked_domains']=['bad.example.com']
        self.assertEqual(public_url('https://docs.example.com/a#section',cfg),'https://docs.example.com/a')
        for url in ('https://bad.example.com','https://example.com.attacker.net/'):
            with self.assertRaises(ValueError):public_url(url,cfg)
    def test_old_config_migration_and_model_cannot_grant_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg=Config(Path(folder));old=copy.deepcopy(cfg.value);old.pop('tools');(Path(folder)/'config/settings.json').write_text(json.dumps(old))
            cfg=Config(Path(folder));self.assertTrue(cfg.value['tools']['enabled']);self.assertTrue((Path(folder)/'config/settings.before-tools.json').exists())
            for patch in ({'tools':defaults()},{'conversations':{CHAT:{'tools_enabled':True}}}):
                with self.assertRaises(ValueError):cfg.update(patch,model=True)

class ToolRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.config=Config(Path(self.tmp.name));self.config.update({'api_key':'dummy-key','conversations':{CHAT:{'allowed':True}}});self.store=Store(Path(self.tmp.name));self.session=ClientSession();self.tools=ToolService(self.config,self.store,self.session)
    async def asyncTearDown(self):
        await self.session.close();self.store.db.close();self.tmp.cleanup()
    async def test_local_execution_budget_and_log_redaction(self):
        settings=copy.deepcopy(self.config.value['tools']);settings['daily_limit']=1;self.config.update({'tools':settings})
        context=self.tools.context(CHAT)
        first=await self.tools.execute('calculator',{'expression':'777+1'},context);second=await self.tools.execute('current_time',{'timezone':'Asia/Shanghai'},context)
        self.assertEqual(first['data']['value'],778);self.assertFalse(second['ok']);self.assertEqual(self.store.tool_used(day_now()),1)
        self.assertNotIn('777+1',json.dumps(self.store.tool_runs()))
    async def test_disabled_and_revoked_and_proactive_never_execute(self):
        self.assertEqual(self.tools.context(CHAT,True).tools,[])
        context=self.tools.context(CHAT);self.config.update({'conversations':{CHAT:{'allowed':False}}})
        self.tools._execute=AsyncMock()
        result=await self.tools.execute('calculator',{'expression':'1+1'},context)
        self.assertFalse(result['ok']);self.tools._execute.assert_not_called()
    async def test_only_given_or_search_returned_links_are_read(self):
        context=self.tools.context(CHAT);self.tools._post=AsyncMock(return_value={'title':'test','content':'hello'})
        denied=await self.tools.execute('web_read',{'url':'https://example.com'},context);self.assertFalse(denied['ok']);self.tools._post.assert_not_called()
        context.urls.add('https://example.com');allowed=await self.tools.execute('web_read',{'url':'https://example.com'},context)
        self.assertTrue(allowed['ok'])
    async def test_search_filters_domains_and_grants_only_returned_urls(self):
        settings=copy.deepcopy(self.config.value['tools']);settings['allowed_domains']=['example.com'];self.config.update({'tools':settings})
        self.tools._post=AsyncMock(return_value={'results':[{'title':'ok','url':'https://example.com/a','summary':'public'},{'title':'bad','url':'https://evil.test','summary':'bad'}]})
        context=self.tools.context(CHAT);result=await self.tools.execute('web_search',{'query':'public topic'},context)
        self.assertEqual(len(result['data']['results']),1);self.assertIn('https://example.com/a',context.urls);self.assertNotIn('https://evil.test',context.urls)
    async def test_offline_and_secret_args_fail_before_network(self):
        self.tools.offline=True;self.tools._post=AsyncMock();context=self.tools.context(CHAT)
        self.assertFalse((await self.tools.execute('web_search',{'query':'topic'},context))['ok']);self.tools._post.assert_not_called()
        self.tools.offline=False
        self.assertFalse((await self.tools.execute('web_search',{'query':'dummy-key'},context))['ok']);self.tools._post.assert_not_called()
    async def test_real_function_call_loop_and_final_decision(self):
        bodies=[]
        async def completion(request):
            body=await request.json();bodies.append(body)
            if len(bodies)==1:return web.json_response({'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':[{'id':'call_1','type':'function','function':{'name':'calculator','arguments':'{"expression":"12*7"}'}}]}}]})
            self.assertEqual(body['messages'][-1]['role'],'tool');self.assertEqual(json.loads(body['messages'][-1]['content'])['data']['value'],84)
            return web.json_response({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'reply':True,'text':'84','memory':'','next_minutes':60})}}]})
        app=web.Application();app.router.add_post('/',completion)
        async with TestServer(app) as server:
            ai=DeepSeek(self.config,self.store,self.session);ai.endpoint=str(server.make_url('/'));result=await ai.decide(CHAT)
        self.assertEqual(result['text'],'84');self.assertEqual(self.store.used(day_now()),2);self.assertEqual(len(bodies),2)
        from bot.usage import report
        self.assertEqual(report(self.store.db,1,CHAT)['total']['calls'],2)
        self.assertEqual(report(self.store.db,1,'')['total']['calls'],0)
    async def test_concurrent_usage_context_is_isolated_and_reset(self):
        import asyncio
        ai=DeepSeek(self.config,self.store,self.session)
        observed=[]
        async def complete(*args):
            await asyncio.sleep(.01)
            observed.append((ai.usage_chat.get(),ai.usage_purpose.get()))
            return {'finish_reason':'stop','message':{'content':'{}'}}
        ai._completion=AsyncMock(side_effect=complete)
        await asyncio.gather(ai.json('',{},purpose='memory',usage_chat=CHAT),
                             ai.json('',{},purpose='admin',usage_chat='group:123456789'))
        self.assertCountEqual(observed,[(CHAT,'memory'),('group:123456789','admin')])
        self.assertEqual(ai.usage_chat.get(),'')
        self.assertEqual(ai.usage_purpose.get(),'chat')

    async def test_admin_requests_have_no_tools(self):
        ai=DeepSeek(self.config,self.store,self.session);ai._completion=AsyncMock(return_value={'finish_reason':'stop','message':{'content':'{}'}})
        await ai.administration('授权群 123456789')
        self.assertEqual(ai._completion.call_args.args[2],[])

class ToolAPI(unittest.IsolatedAsyncioTestCase):
    async def test_auth_local_test_and_old_settings_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Path(folder),offline=True);client=TestClient(TestServer(app),headers={'Host':'127.0.0.1:8765','X-Bot-Request':'1'})
            await client.start_server()
            try:
                self.assertEqual((await client.get('/api/tools')).status,401)
                await client.post('/api/login',json={'token':app['config'].control_token})
                data=await (await client.get('/api/tools')).json();self.assertEqual({t['name'] for t in data['catalog']},set(defaults()['enabled_tools']))
                result=await (await client.post('/api/tools/test',json={'revision':app['config'].value['revision'],'name':'calculator','arguments':{'expression':'8*9'}})).json()
                self.assertEqual(result['data']['value'],72)
            finally:await client.close()

class ToolLoopLimits(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ToolRuntime.asyncSetUp
    asyncTearDown=ToolRuntime.asyncTearDown
    async def test_revoke_during_model_call_blocks_tool_and_next_request(self):
        ai=DeepSeek(self.config,self.store,self.session)
        async def completion(*args):
            self.config.update({'conversations':{CHAT:{'allowed':False}}})
            return {'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'x','type':'function','function':{'name':'calculator','arguments':'{"expression":"1+1"}'}}]}}
        ai._completion=AsyncMock(side_effect=completion);ai.tools._execute=AsyncMock()
        with self.assertRaises(ModelError):await ai.decide(CHAT)
        ai.tools._execute.assert_not_called();self.assertEqual(ai._completion.await_count,1)
    async def test_repeat_call_ids_never_reexecute(self):
        ai=DeepSeek(self.config,self.store,self.session)
        ai._completion=AsyncMock(return_value={'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'same','type':'function','function':{'name':'calculator','arguments':'{"expression":"1+1"}'}}]}})
        with self.assertRaises(ModelError):await ai.decide(CHAT)
        self.assertEqual(self.store.tool_used(day_now()),1)
    async def test_disabled_tool_is_reported_to_model_without_execution(self):
        settings=copy.deepcopy(self.config.value['tools']);settings['enabled_tools']=['current_time'];self.config.update({'tools':settings})
        ai=DeepSeek(self.config,self.store,self.session)
        ai._completion=AsyncMock(side_effect=[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'x','type':'function','function':{'name':'calculator','arguments':'{"expression":"1+1"}'}}]}},{'finish_reason':'stop','message':{'content':'{"reply":false,"text":"","memory":"","next_minutes":60}'}}])
        await ai.decide(CHAT);self.assertEqual(self.store.tool_used(day_now()),0)

class FinalJSONRecovery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=ToolRuntime.asyncSetUp
    asyncTearDown=ToolRuntime.asyncTearDown

    def final(self, content):
        return {'finish_reason':'stop','message':{'content':content}}

    def call(self):
        return {'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'once','type':'function','function':{'name':'calculator','arguments':'{"expression":"1+1"}'}}]}}

    async def test_complete_fenced_json_is_accepted_without_another_paid_call(self):
        ai=DeepSeek(self.config,self.store,self.session)
        ai._completion=AsyncMock(return_value=self.final('```json\n{"reply":false,"messages":[],"memory":"","next_minutes":60}\n```'))
        result=await ai.decide(CHAT)
        self.assertFalse(result['reply']);self.assertEqual(ai._completion.await_count,1)

    async def test_repair_after_tool_success_never_repeats_tool_or_logs_content(self):
        ai=DeepSeek(self.config,self.store,self.session)
        ai.tools._execute=AsyncMock(return_value={'value':2})
        ai._completion=AsyncMock(side_effect=[self.call(),self.final('原始隐私内容，不是JSON'),self.final('{"reply":true,"messages":["2"],"memory":"","next_minutes":60}')])
        result=await ai.decide(CHAT)
        self.assertEqual(result['messages'],['2'])
        self.assertEqual(ai.tools._execute.await_count,1)
        self.assertEqual(ai._completion.await_args_list[-1].args[2],[])
        self.assertNotIn('原始隐私内容',json.dumps(self.store.audits(),ensure_ascii=False))
        self.assertTrue(any(r['kind']=='model_format_recovered' for r in self.store.audits()))

    async def test_exhausted_tool_rounds_get_explicit_final_json_instruction(self):
        settings=copy.deepcopy(self.config.value['tools']);settings['max_rounds']=1;self.config.update({'tools':settings})
        ai=DeepSeek(self.config,self.store,self.session)
        requests=[]
        async def completion(messages,parameters,schemas):
            requests.append(copy.deepcopy((messages,schemas)))
            return self.call() if len(requests)==1 else self.final('{"reply":false,"messages":[],"memory":"","next_minutes":60}')
        ai._completion=AsyncMock(side_effect=completion)
        await ai.decide(CHAT)
        self.assertEqual(requests[-1][1],[])
        self.assertIn('工具阶段已经结束',requests[-1][0][-1]['content'])

    async def test_repair_is_limited_to_one_request_and_refuses_tools(self):
        ai=DeepSeek(self.config,self.store,self.session)
        ai._completion=AsyncMock(side_effect=[self.final('not json'),self.call()])
        ai.tools.execute=AsyncMock()
        with self.assertRaises(ModelError):await ai.decide(CHAT)
        self.assertEqual(ai._completion.await_count,2);ai.tools.execute.assert_not_awaited()
        ai._completion=AsyncMock(return_value=self.final('still invalid'))
        with self.assertRaises(ModelError):await ai.decide(CHAT)
        self.assertEqual(ai._completion.await_count,2)

    async def test_revocation_during_repair_cancels_final_output(self):
        ai=DeepSeek(self.config,self.store,self.session)
        calls=0
        async def completion(*args):
            nonlocal calls
            calls+=1
            if calls==1:return self.final('bad json')
            self.config.update({'conversations':{CHAT:{'allowed':False}}})
            return self.final('{"reply":true,"messages":["不能发送"],"memory":"","next_minutes":60}')
        ai._completion=AsyncMock(side_effect=completion)
        with self.assertRaises(ModelError):await ai.decide(CHAT)
        self.assertFalse(any(r['kind']=='model_format_recovered' for r in self.store.audits()))

    async def test_prose_or_array_is_not_silently_turned_into_a_decision(self):
        from bot.ai import json_object
        for value in ('说明 {"reply":true}', '[]', '```json\n{}\n```额外文字', None):
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):
                json_object(value)
