from __future__ import annotations

import asyncio
import base64
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from bot.ai import DeepSeek, day_now
from bot.config import Config, defaults, migrate
from bot.emotion import Emotion
from bot.engine import Engine
from bot.life import Life, FeedParser, media_url, image_mime
from bot.roleplay import resolve
from bot.server import create_app
from bot.store import Store

CHAT='private:123456789'; OTHER='private:987654321'; GROUP='group:111112222'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')


class LifeRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=Config(self.root)
        cfg=copy.deepcopy(self.config.value['life']);cfg.update(start='00:00',end='00:00',cooldown_seconds=0)
        self.config.update({'api_key':'fake-key','bot_qq':'555556666','life':cfg,'conversations':{CHAT:{'allowed':True},OTHER:{'allowed':True},GROUP:{'allowed':True}}})
        self.store=Store(self.root);self.session=ClientSession();self.ai=DeepSeek(self.config,self.store,self.session)
        self.bridge=type('Bridge',(),{'connected':True,'online':True,'self_id':'555556666','call':AsyncMock(return_value={}), 'send_segments':AsyncMock(return_value={'message_id':42}), 'send':AsyncMock(return_value={'message_id':43})})()
        self.life=Life(self.config,self.store,self.ai,self.bridge)
        self.ai.tools.life=self.life;self.engine=Engine(self.config,self.store,self.ai,self.bridge)

    async def asyncTearDown(self):
        await self.engine.close();await self.ai.memory.close();await self.session.close();self.store.db.close();self.tmp.cleanup()

    def context(self,chat=CHAT):return self.ai.tools.context(chat,chat=='life')

    async def run_tool(self,name,args,chat=CHAT):return await self.ai.tools.execute(name,args,self.context(chat))

    async def test_visual_contract_same_model_multimodal_and_usage_scope(self):
        identifier=self.life.register(CHAT,'chat',blob=PNG)
        bodies=[]
        async def handler(request):
            bodies.append(await request.json())
            return web.json_response({'choices':[{'finish_reason':'stop','message':{'content':'{"description":"小猫","text":"你好","meaning":"打招呼"}'}}]})
        app=web.Application();app.router.add_post('/',handler)
        async with TestServer(app) as server:
            self.ai.endpoint=str(server.make_url('/'))
            result=await self.run_tool('inspect_images',{'image_ids':identifier,'question':'这张表情什么意思'})
        self.assertTrue(result['ok']);self.assertEqual(bodies[0]['model'],'deepseek-flash')
        self.assertEqual(bodies[0]['messages'][-1]['content'][1]['type'],'image_url')
        self.assertTrue(bodies[0]['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(self.store.used(day_now()),1)
        self.assertNotIn(base64.b64encode(PNG).decode(),json.dumps(self.store.tool_runs()))

    async def test_prompt_uses_actual_image_limit_and_tool_budget(self):
        text=self.life.prompt(CHAT)
        self.assertIn('最多4张图片',text)
        self.assertIn('最多6次工具调用、3批',text)
        self.assertIn('及时sticker_save收藏',text)

    async def test_unsaved_images_cannot_cross_conversations(self):
        identifier=self.life.register(CHAT,'chat',blob=PNG)
        self.assertFalse((await self.run_tool('inspect_images',{'image_ids':identifier,'question':''},OTHER))['ok'])
        self.ai.json=AsyncMock()
        self.assertFalse((await self.run_tool('inspect_images',{'image_ids':','.join([identifier]*7),'question':''}))['ok'])
        self.ai.json.assert_not_awaited()

    async def test_sticker_saved_locally_when_qq_sync_fails_and_persists(self):
        identifier=self.life.register(CHAT,'chat',blob=PNG)
        self.bridge.call.side_effect=ValueError('unsupported')
        result=await self.run_tool('sticker_save',{'image_id':identifier,'description':'喜欢猫猫挥手'})
        self.assertTrue(result['ok']);self.assertTrue(result['data']['saved']);self.assertFalse(result['data']['qq_synced'])
        self.assertEqual(self.life.media(identifier,self.context(OTHER))['saved'],1)
        other=Life(self.config,self.store,self.ai,self.bridge)
        self.assertEqual(other.stickers('猫猫')[0]['id'],identifier)

    async def test_sticker_send_quote_and_own_receipt(self):
        identifier=self.life.register('upload','upload',blob=PNG,saved=True)
        self.life.remember_message(CHAT,'123','123456789',False,'hi')
        args={'text':'嘿','reply_id':'123','sticker_id':identifier,'face_id':'14','target':''}
        context=self.context()
        result=await self.ai.tools.execute('qq_send',args,context)
        self.assertTrue(result['ok'])
        parts=self.bridge.send_segments.await_args.args[1]
        self.assertEqual([p['type'] for p in parts],['reply','text','face','image'])
        self.assertTrue(self.life.message('42',self.context(),own=True)['own'])
        self.assertEqual(self.store.history(CHAT)[-1]['role'],'assistant');self.assertEqual(context.sent_texts,['嘿'])
        self.assertEqual(self.store.db.execute('SELECT status FROM life_actions').fetchone()[0],'confirmed')

    async def test_collect_then_send_immediately_with_normal_cooldown(self):
        cfg=copy.deepcopy(self.config.value['life']);cfg['cooldown_seconds']=30
        self.config.update({'life':cfg})
        identifier=self.life.register(CHAT,'chat',blob=PNG)
        self.bridge.call.side_effect=[{'file':'/tmp/qq-fixture.png'},{}]
        saved=await self.run_tool('sticker_save',{'image_id':identifier,'description':'喜欢的表情'})
        self.assertTrue(saved['ok']);self.assertTrue(saved['data']['qq_synced'])
        args={'text':'','reply_id':'','sticker_id':identifier,'face_id':'','target':''}
        sent=await self.run_tool('qq_send',args)
        self.assertTrue(sent['ok'],sent)
        self.assertEqual(self.bridge.send_segments.await_count,1)
        blocked=await self.run_tool('qq_send',{**args,'text':'再发一个'})
        self.assertFalse(blocked['ok']);self.assertIn('冷却',blocked['error'])
        self.assertEqual(self.bridge.send_segments.await_count,1)

    async def test_favourite_sync_does_not_block_more_collection(self):
        cfg=copy.deepcopy(self.config.value['life']);cfg['cooldown_seconds']=30
        self.config.update({'life':cfg})
        self.life.fetch=AsyncMock(return_value=PNG)
        self.bridge.call.side_effect=[{'file':'/tmp/qq-fixture1.png'},{},{'file':'/tmp/qq-fixture2.png'},{}]
        for index in range(2):
            identifier=self.life.register(CHAT,'chat',url=f'https://example.com/{index}.png')
            result=await self.run_tool('sticker_save',{'image_id':identifier,'description':'喜欢'})
            self.assertTrue(result['ok']);self.assertTrue(result['data']['qq_synced'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM life_actions WHERE name='sticker_save' AND status='confirmed'").fetchone()[0],2)

    async def test_semantic_retrieval_then_native_send_tool(self):
        from bot.sticker_rag import DIMENSIONS
        import numpy as np
        encoder=type('Encoder',(),{'ready':lambda self:True,'encode':lambda self,texts,query=False:np.tile(np.eye(1,DIMENSIONS,dtype=np.float32),(len(texts),1))})()
        self.life.retrieval.encoder=encoder
        identifier=self.life.register('upload','upload',blob=PNG,saved=True,description='嘴硬但开心的小猫')
        choices=[]
        def call(name,args,index):
            return {'finish_reason':'tool_calls','message':{'tool_calls':[{'id':str(index),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}
        async def completion(messages,parameters,schemas):
            choices.append(messages)
            if len(choices)==1:return call('sticker_find',{'query':'口是心非但内心喜悦'},1)
            result=json.loads(messages[-1]['content'])
            self.assertTrue(result['ok'],result)
            if len(choices)==2:
                candidate=result['data']['stickers'][0]
                self.assertEqual(candidate['id'],identifier);self.assertIn('score',candidate)
                return call('qq_send',{'sticker_id':candidate['id']},2)
            return {'finish_reason':'stop','message':{'content':'{"reply":false,"messages":[]}'}}
        self.ai._completion=completion
        result=await self.ai.json(self.life.prompt(CHAT),{},tool_context=self.context())
        self.assertFalse(result['reply']);self.assertEqual(len(choices),3)
        self.bridge.send_segments.assert_awaited_once()
        self.assertEqual([p['type'] for p in self.bridge.send_segments.await_args.args[1]],['image'])
        self.assertNotIn(identifier,self.life.prompt(CHAT))
        self.assertNotIn('嘴硬但开心的小猫',self.life.prompt(CHAT))

    async def test_legacy_final_id_cannot_send_image(self):
        identifier=self.life.register('upload','upload',blob=PNG,saved=True)
        self.ai.json=AsyncMock(return_value={'reply':True,'messages':[],'memory':'','next_minutes':60,'sticker_id':identifier})
        self.ai.memory.consider=lambda chat:None
        self.engine.wait_to_reply=AsyncMock(return_value=True)
        await self.engine.decide(CHAT)
        self.bridge.send_segments.assert_not_awaited();self.bridge.send.assert_not_awaited()

    async def test_partial_send_arguments_still_validate(self):
        for args in ({'sticker_id':5},{'unknown':'value'},{}):
            self.assertFalse((await self.run_tool('qq_send',args))['ok'])
        self.bridge.send_segments.assert_not_awaited()

    async def test_model_can_choose_not_to_send_sticker(self):
        self.life.register('upload','upload',blob=PNG,saved=True)
        self.ai.json=AsyncMock(return_value={'reply':True,'messages':['这次先聊聊'],'memory':'','next_minutes':60})
        self.ai.memory.consider=lambda chat:None
        self.engine.wait_to_reply=AsyncMock(return_value=True)
        await self.engine.decide(CHAT)
        self.bridge.send_segments.assert_not_awaited()
        self.bridge.send.assert_awaited_once_with(CHAT,'这次先聊聊')

    async def test_recall_and_reaction_scope(self):
        self.life.remember_message(CHAT,'1','123456789',False,'hi')
        self.life.remember_message(CHAT,'2','bot',True,'own')
        self.life.remember_message(OTHER,'3','bot',True,'other')
        for mid in ('1','3','999'):
            self.assertFalse((await self.run_tool('qq_recall',{'message_id':mid}))['ok'])
        self.bridge.call.assert_not_awaited()
        self.assertTrue((await self.run_tool('qq_recall',{'message_id':'2'}))['ok'])
        self.bridge.call.assert_awaited_once_with('delete_msg',{'message_id':'2'})
        self.assertTrue((await self.run_tool('qq_react',{'message_id':'1','emoji_id':'76'}))['ok'])

    async def test_poke_allowed_person_and_wrong_account_blocked(self):
        self.assertFalse((await self.run_tool('qq_poke',{'user_id':'987654321'}))['ok'])
        self.assertTrue((await self.run_tool('qq_poke',{'user_id':''}))['ok'])
        self.bridge.call.assert_awaited_once_with('send_poke',{'user_id':'123456789'})
        self.bridge.self_id='000000000'
        self.assertFalse((await self.run_tool('qq_like',{'user_id':''}))['ok'])
        self.assertEqual(self.bridge.call.await_count,1)

    async def test_new_message_and_revoke_cancel_effect(self):
        context=self.context();self.engine.incoming_versions[CHAT]=1
        self.assertFalse((await self.ai.tools.execute('qq_poke',{'user_id':''},context))['ok'])
        context=self.context();self.config.update({'conversations':{CHAT:{'allowed':False}}})
        self.assertFalse((await self.ai.tools.execute('qq_poke',{'user_id':''},context))['ok'])
        self.bridge.call.assert_not_awaited()

    async def test_clear_during_qq_send_does_not_restore_chat_or_message_map(self):
        async def accepted(*args):
            self.store.clear(CHAT)
            return {'message_id':88}
        self.bridge.send_segments.side_effect=accepted
        result=await self.run_tool('qq_send',{'text':'待发内容','reply_id':'','sticker_id':'','face_id':'','target':''})
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['history_preserved'])
        self.assertEqual(self.store.history(CHAT),[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM life_messages').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT status FROM life_actions').fetchone()[0],'confirmed')

    async def test_unknown_effect_is_durable_and_not_retried(self):
        self.bridge.call.side_effect=TimeoutError()
        self.assertFalse((await self.run_tool('qq_poke',{'user_id':''}))['ok'])
        self.assertFalse((await self.run_tool('qq_poke',{'user_id':''}))['ok'])
        self.assertEqual(self.bridge.call.await_count,1)
        self.assertEqual(self.store.db.execute('SELECT status FROM life_actions').fetchone()[0],'unknown')

    async def test_autonomous_send_scope_and_observed_new_message(self):
        args={'text':'我想来看看你','reply_id':'','sticker_id':'','face_id':'','target':OTHER}
        self.assertFalse((await self.run_tool('qq_send',args))['ok'])
        context=self.context('life')
        read=await self.ai.tools.execute('qq_context',{'target':OTHER},context)
        self.assertTrue(read['ok'])
        self.engine.incoming_versions[OTHER]=2
        self.assertFalse((await self.ai.tools.execute('qq_send',args,context))['ok'])
        self.bridge.send_segments.assert_not_awaited()
        self.assertTrue((await self.run_tool('qq_send',args,'life'))['ok'])

    async def test_autonomous_target_tools_off_and_pause_block(self):
        self.config.update({'conversations':{OTHER:{'tools_enabled':False}}})
        args={'text':'嘿','reply_id':'','sticker_id':'','face_id':'','target':OTHER}
        self.assertFalse((await self.run_tool('qq_send',args,'life'))['ok'])
        context=self.context('life');self.config.update({'paused':True})
        self.assertFalse((await self.ai.tools.execute('qq_contacts',{},context))['ok'])
        self.bridge.call.assert_not_awaited()

    async def test_post_visibility_limits_originals_and_chat_images(self):
        self.ai.json=AsyncMock(return_value={'safe':True})
        args={'content':'今天的云很漂亮，想安静地走一走。','visibility':'include','targets':'123456789','image_ids':''}
        result=await self.run_tool('qzone_publish',args)
        self.assertTrue(result['ok'])
        params=self.bridge.call.await_args.args[1]
        self.assertEqual(params['ugc_right'],16);self.assertEqual(params['target_uins'],['123456789'])
        self.store.add(CHAT,'user','123456789','这是不想让别人知道的原文秘密。')
        self.assertFalse((await self.run_tool('qzone_publish',{**args,'content':'这是不想让别人知道的原文秘密。'}))['ok'])
        self.assertFalse((await self.run_tool('qzone_publish',{**args,'content':'联系123456789'}))['ok'])
        self.assertFalse((await self.run_tool('qzone_publish',{**args,'visibility':'include','targets':''}))['ok'])
        image=self.life.register(CHAT,'chat',blob=PNG,saved=True)
        self.assertFalse((await self.run_tool('qzone_publish',{**args,'image_ids':image}))['ok'])
        self.assertEqual(self.bridge.call.await_count,1)

    async def test_space_daily_zero_and_failed_privacy_cannot_publish(self):
        args={'content':'今天想休息。','visibility':'self','targets':'','image_ids':''}
        self.ai.json=AsyncMock(return_value={'safe':False})
        self.assertFalse((await self.run_tool('qzone_publish',args))['ok'])
        self.ai.json.return_value={'safe':True}
        cfg=copy.deepcopy(self.config.value['life']);cfg['daily_posts']=0;self.config.update({'life':cfg})
        self.assertFalse((await self.run_tool('qzone_publish',args))['ok'])
        self.bridge.call.assert_not_awaited()

    async def test_comment_requires_actual_cached_post_and_authorized_owner(self):
        post={'id':'123456789:abcd','owner':'123456789','tid':'abcd','abstime':'1','unikey':'u','curkey':'c'}
        self.store.db.execute('INSERT INTO life_posts VALUES(?,?,?,?)',(post['id'],post['owner'],json.dumps(post),time.time()));self.store.db.commit()
        self.life.qzone_request=AsyncMock(return_value={'code':0})
        self.assertTrue((await self.run_tool('qzone_comment',{'post_id':post['id'],'content':'今天的天空很好看'}))['ok'])
        self.config.update({'conversations':{CHAT:{'allowed':False}}})
        self.assertFalse((await self.run_tool('qzone_comment',{'post_id':post['id'],'content':'另一句'},OTHER))['ok'])
        self.assertFalse((await self.run_tool('qzone_like',{'post_id':'madeup'},OTHER))['ok'])
        self.assertEqual(self.life.qzone_request.await_count,1)

    async def test_tick_persists_schedule_and_reflects_without_new_messages(self):
        self.ai.json=AsyncMock(return_value={'reply':False,'next_minutes':70,'self_state':{'mood':'期待','energy':.5,'interests':['猫咪表情']}})
        await self.life.tick()
        self.assertEqual(self.ai.json.await_count,1)
        self.assertEqual(self.ai.json.await_args.kwargs['purpose'],'autonomy')
        self.assertEqual(self.life.state()['mood'],'期待')
        self.assertGreater(self.life.state()['next_at'],time.time()+69*60)
        await self.life.tick();self.assertEqual(self.ai.json.await_count,1)
        self.bridge.send_segments.assert_not_awaited()

    async def test_tick_failure_does_not_loop_or_run_while_offline(self):
        self.ai.json=AsyncMock(side_effect=ValueError('failed'))
        with self.assertRaises(ValueError):await self.life.tick()
        await self.life.tick();self.assertEqual(self.ai.json.await_count,1)
        self.life.offline=True;state=self.life.state();state['next_at']=0;self.life.set_state(state)
        await self.life.tick();self.assertEqual(self.ai.json.await_count,1)

    async def test_self_thought_scope_and_relationship_continuity(self):
        self.life.reflect({'mood':'开心','thought':'私聊内的原因'},CHAT)
        self.assertIn('私聊内的原因',self.life.prompt(CHAT));self.assertNotIn('私聊内的原因',self.life.prompt(GROUP))
        self.store.add(CHAT,'user','123456789','谢谢你认真解释')
        self.store.add(GROUP,'user','123456789','晚上好')
        emotion=Emotion(self.config,self.store);context=emotion.context(CHAT)
        raw={'labels':['释然'],'pleasure':.4,'energy':.1,'tension':.1,'warmth':.1,'trust':.8,'attachment':.5,'security':.9,'stance':'私聊秘密的缘由'}
        self.assertTrue(emotion.commit(context,{'emotion':raw}))
        state=emotion.context(GROUP)['state'];self.assertGreater(state['trust'],0)
        self.assertEqual(emotion.context(CHAT)['character'],emotion.context(GROUP)['character'])
        self.assertNotIn('私聊秘密的缘由',emotion.prompt(emotion.context(GROUP)))
        self.assertNotIn('私聊秘密的缘由',json.dumps(emotion.inspect(GROUP),ensure_ascii=False))


class LifeValidation(unittest.TestCase):
    def test_single_identity_and_explicit_preview(self):
        cfg=defaults();cfg['conversations'][CHAT]={'character_id':'traveler'}
        self.assertEqual(resolve(cfg,CHAT)[0]['id'],cfg['roleplay']['active_character'])
        self.assertEqual(resolve(cfg,CHAT,{'character_id':'traveler'})[0]['id'],'traveler')

    def test_media_address_and_magic(self):
        for url in ('http://127.0.0.1/a','http://[::1]/a','file:///tmp/a','http://localhost/a','http://a.local/a','http://user:pw@example.com/a','https://example.com:8080/a'):
            with self.subTest(url=url),self.assertRaises(ValueError):media_url(url)
        self.assertEqual(image_mime(PNG),'image/png')
        with self.assertRaises(ValueError):image_mime(b'<svg/>')

    def test_old_settings_enable_new_capabilities_without_changing_credentials(self):
        cfg=defaults();cfg.pop('life');cfg['tools']['enabled_tools']=['calculator'];cfg['api_key']='keep'
        upgraded=migrate(cfg)
        self.assertEqual(upgraded['api_key'],'keep');self.assertIn('inspect_images',upgraded['tools']['enabled_tools'])
        self.assertEqual(migrate(upgraded),upgraded)

    def test_feed_parser_ignores_comments(self):
        parser=FeedParser();parser.feed('<div id="feed_123456789_311_0_1_0_1" data-key="abcd"><div class="f-info">今日<span>晴</span></div><div>秘密评论</div><a data-unikey="http://example.com/k" data-curkey="k"></a></div>')
        self.assertEqual(parser.owner,'123456789');self.assertEqual(parser.tid,'abcd')
        self.assertEqual(parser.content,'今日晴');self.assertNotIn('秘密',parser.content)


class LifeAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(Path(self.tmp.name),offline=True)
        self.client=TestClient(TestServer(self.app),headers={'Host':'127.0.0.1:8765','X-Bot-Request':'1'})
        await self.client.start_server()

    async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()

    async def test_auth_upload_preview_delete_and_no_qq(self):
        self.assertEqual((await self.client.get('/api/life')).status,401)
        await self.client.post('/api/login',json={'token':self.app['config'].control_token})
        revision=self.app['config'].value['revision']
        body={'revision':revision,'action':'upload','base64':base64.b64encode(PNG).decode(),'description':'探头'}
        response=await self.client.post('/api/life',json=body);self.assertEqual(response.status,200)
        identifier=(await response.json())['id']
        image=await self.client.get('/api/life/image/'+identifier);self.assertEqual(await image.read(),PNG)
        data=await (await self.client.get('/api/life')).json();self.assertEqual(len(data['stickers']),1)
        self.assertEqual((await self.client.post('/api/life',json={**body,'revision':revision+1})).status,409)
        self.assertEqual((await self.client.post('/api/life',json={**body,'base64':'eHh4'})).status,400)
        self.assertEqual((await self.client.post('/api/life',json={'revision':revision,'action':'delete-sticker','id':identifier})).status,200)
        self.assertEqual((await self.client.get('/api/life/image/'+identifier)).status,404)
        self.assertEqual(self.app['store'].db.execute('SELECT count(*) FROM life_actions').fetchone()[0],0)

    async def test_model_cannot_change_life_permissions_and_sandbox_has_no_qq(self):
        await self.client.post('/api/login',json={'token':self.app['config'].control_token})
        with self.assertRaises(ValueError):self.app['config'].update({'life':self.app['config'].value['life']},model=True)
        context=self.app['ai'].tools.context('sandbox',sandbox=True)
        self.assertNotIn('qq_send',context.tools)
        response=await self.client.post('/api/tools/test',json={'revision':0,'name':'qq_poke','arguments':{'user_id':'123456789'}})
        self.assertFalse((await response.json())['ok'])
