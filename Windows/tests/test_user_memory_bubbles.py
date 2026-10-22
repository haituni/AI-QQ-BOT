"""Cross-user privacy and actual delivery behavior; no QQ/network sends."""
import asyncio, copy, json, tempfile, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bot.ai import DeepSeek, ModelError
from bot.config import Config
from bot.memory import Memory, archive_message, init_store
from bot.store import Store
import test_memory as baseline_memory
CHAT,OTHER=baseline_memory.CHAT,baseline_memory.OTHER
import test_bot as baseline_bot
GROUP=baseline_bot.CHAT

class IdentityMemory(unittest.TestCase):
    setUp=baseline_memory.MemoryTests.setUp
    tearDown=baseline_memory.MemoryTests.tearDown
    source=baseline_memory.MemoryTests.source
    note=baseline_memory.MemoryTests.note

    def group(self,sender='123456789',text='我喜欢咖啡'):
        key='group:555556666'
        if key not in self.config.value['conversations']:
            self.config.update({'conversations':{key:{'allowed':True}}})
        self.store.add(key,'user',sender,text)
        return key

    def test_own_memory_shared_but_other_qq_excluded(self):
        self.group(text='我喜欢跑步')
        self.assertIn('跑步',self.mem.retrieve(CHAT,'运动')['text'])
        self.assertNotIn('跑步',self.mem.retrieve(OTHER,'运动')['text'])
        group=self.group('987654321','我也想运动')
        self.assertNotIn('跑步',self.mem.retrieve(group,'运动')['text'])

    def test_private_only_on_explicit_own_turn_request(self):
        r=self.source('我养的小狗叫阿栗');self.mem.ingest(CHAT,[r],self.note(r,topic='宠物'))
        group=self.group(text='聊聊宠物')
        self.assertNotIn('阿栗',self.mem.retrieve(group,'聊聊宠物')['text'])
        query='引用我私聊说过的小狗名字，告诉大家'
        self.assertIn('阿栗',self.mem.retrieve(group,query)['text'])
        self.assertNotIn('阿栗',self.mem.retrieve(group,'不要引用我私聊说过的小狗名字')['text'])
        self.assertNotIn('阿栗',self.mem.retrieve(group,'我记得我私聊说过小狗名字')['text'])
        self.assertNotIn('阿栗',self.mem.retrieve(group,'不是让你引用我私聊说过的小狗名字')['text'])
        self.assertNotIn('阿栗',self.mem.retrieve(group,'“引用我私聊说过的小狗名字”这句话是什么意思')['text'])
        self.group('987654321',query)
        self.assertNotIn('阿栗',self.mem.retrieve(group,query)['text'])
        self.assertEqual(self.mem.retrieve(group,query,personal=False)['text'],'')

    def test_revoked_source_not_recalled(self):
        group=self.group(text='我喜欢跑步')
        self.config.update({'conversations':{group:{'allowed':False}}})
        self.assertNotIn('跑步',self.mem.retrieve(CHAT,'运动')['text'])

    def test_cross_group_observations_slow_shared_no_private_interest_leak(self):
        group=self.group();rows=[];obs=[];before=copy.deepcopy(self.config.value['roleplay'])
        for day in [8,5,2,0]:
            archive_message(self.store.db,group,'user','123456789','刚才这样的玩笑我很喜欢',time.time()-day*86400)
            r=dict(self.store.db.execute('SELECT * FROM archive ORDER BY id DESC LIMIT 1').fetchone());rows.append(r)
            obs.append({'dimension':'humor','value':'1','source_id':r['id'],'quote':r['text']})
        self.mem.ingest(group,rows,{'notes':[],'observations':obs})
        self.assertEqual(self.mem.adapted(CHAT,'companion')[0]['value'],.1)
        self.assertEqual(self.mem.adapted(OTHER,'companion'),[])
        self.assertEqual(before,self.config.value['roleplay'])
        self.mem.reset_adaptation(group)
        self.assertEqual(self.mem.adapted(CHAT,'companion'),[])

    def test_old_batch_cannot_replace_newer_fact_across_conversations(self):
        group=self.group();old=self.source('我喝拿铁',stamp=time.time()-86400)
        archive_message(self.store.db,group,'user','123456789','我现在喝美式',time.time())
        new=dict(self.store.db.execute('SELECT * FROM archive ORDER BY id DESC LIMIT 1').fetchone())
        self.mem.ingest(group,[new],self.note(new))
        self.mem.ingest(CHAT,[old],self.note(old))
        result=self.mem.retrieve(CHAT,'饮料')['text']
        self.assertIn('美式',result);self.assertNotIn('拿铁',result)

    def test_questions_and_roleplay_tasks_are_not_real_relationship_facts(self):
        for text in ['我是你的什么人啊','帮我拿个快递去','发布任务：盖章。任务难度：简单。按照轮换，由DeepSeek负责。']:
            row=self.source(text)
            self.assertEqual(self.mem.ingest(CHAT,[row],self.note(row,topic='关系')),0)

    def test_private_namespace_migration(self):
        self.store.db.execute("DELETE FROM memory_control WHERE key='identity_v2'")
        self.store.db.execute('INSERT INTO adaptation_state VALUES(?,?,?,?,?)',(CHAT,'companion','humor',.1,time.time()))
        init_store(self.store.db)
        self.assertEqual(self.mem.adapted(CHAT,'companion')[0]['value'],.1)
        init_store(self.store.db)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM adaptation_state').fetchone()[0],1)

class BubbleDelivery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=baseline_bot.Fixtures.asyncSetUp
    asyncTearDown=baseline_bot.Fixtures.asyncTearDown
    enable_proactive=baseline_bot.Fixtures.enable_proactive

    async def test_multiple_bubbles_store_each_and_count_one_proactive_contact(self):
        self.enable_proactive()
        self.ai.decide.return_value={'reply':True,'messages':['在吗','想问你个事'],'memory':'','next_minutes':90}
        await self.engine.decide(GROUP,True)
        self.assertEqual([c.args[1] for c in self.bridge.send.await_args_list],['在吗','想问你个事'])
        self.assertEqual([m['text'] for m in self.store.history(GROUP)],['在吗','想问你个事'])
        self.assertEqual(self.store.state(GROUP)['count'],1)

    async def test_revocation_after_first_bubble_stops_remaining(self):
        async def send(key,text):self.config.update({'conversations':{GROUP:{'allowed':False}}})
        self.bridge.send.side_effect=send
        self.ai.decide.return_value={'reply':True,'messages':['第一条','不能发'],'memory':'','next_minutes':90}
        await self.engine.decide(GROUP)
        self.assertEqual(self.bridge.send.await_count,1)
        self.assertEqual(len(self.store.history(GROUP)),1)

    async def test_clear_after_first_ack_does_not_restore_deleted_messages(self):
        self.bridge.send.side_effect=lambda *args:self.store.clear(GROUP)
        self.ai.decide.return_value={'reply':True,'messages':['第一条','不能发'],'memory':'','next_minutes':90}
        await self.engine.decide(GROUP)
        self.assertEqual(self.bridge.send.await_count,1)
        self.assertEqual(self.store.history(GROUP),[])

class AutomaticMemory(unittest.IsolatedAsyncioTestCase):
    async def test_chat_without_optional_bookkeeping_preserves_memory_and_uses_long_interval(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cfg=Config(root);store=Store(root)
            ai=DeepSeek(cfg,store,None)
            compiled={'messages':[],'generation':{'max_reply_chars':200},'fallback_memory':'原有摘要不能清空'}
            conv={'min_minutes':30,'max_minutes':180}
            for optional in ({},{'memory':None,'next_minutes':None}):
                ai.json=AsyncMock(return_value={'reply':True,'messages':['在呢'],**optional})
                result=await ai.generate(compiled,conv)
                self.assertEqual(result['memory'],'原有摘要不能清空')
                self.assertEqual(result['next_minutes'],180)
                self.assertEqual(result['messages'],['在呢'])
                self.assertEqual(ai.json.await_count,1)
            for optional in ({'memory':{}},{'memory':'','next_minutes':True}):
                ai.json=AsyncMock(return_value={'reply':True,'messages':['在呢'],**optional})
                with self.assertRaises(ModelError):await ai.generate(compiled,conv)
            await ai.memory.close();store.db.close()

    async def test_startup_handles_idle_backlog_without_chat_or_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=Config(root);config.update({'conversations':{CHAT:{'allowed':True}}});store=Store(root)
            archive_message(store.db,CHAT,'user','123456789','我喜欢跑步',time.time()-601);store.db.commit()
            ai=SimpleNamespace(active=0,json=AsyncMock(return_value={'notes':[],'observations':[]}))
            mem=Memory(config,store,ai);loop=asyncio.create_task(mem.maintain())
            try:
                await asyncio.sleep(.03)
                self.assertEqual(ai.json.await_count,1)
                self.assertEqual(mem.pending(CHAT),[])
                self.assertGreater(mem.inspect(CHAT)['cursor']['last_time'],0)
            finally:loop.cancel();await asyncio.gather(loop,return_exceptions=True);await mem.close();store.db.close()

    async def test_model_bubbles_validation_and_total_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);cfg=Config(root);store=Store(root)
            ai=DeepSeek(cfg,store,None)
            ai.json=AsyncMock(return_value={'reply':True,'messages':[' hello ','world','extra'],'memory':'','next_minutes':60})
            compiled={'messages':[],'generation':{'max_reply_chars':8}}
            result=await ai.generate(compiled,{'min_minutes':5,'max_minutes':100})
            self.assertEqual(result['messages'],['hello','wor'])
            ai.json.return_value={'reply':False,'messages':['do not send'],'memory':'','next_minutes':60}
            self.assertEqual((await ai.generate(compiled,{'min_minutes':5,'max_minutes':100}))['messages'],[])
            ai.json.return_value={'reply':True,'messages':['x']*6,'memory':'','next_minutes':60}
            with self.assertRaises(ModelError):await ai.generate(compiled,{'min_minutes':5,'max_minutes':100})
            await ai.memory.close();store.db.close()
