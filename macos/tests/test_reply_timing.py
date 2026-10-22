import asyncio,copy,time,unittest
from unittest.mock import AsyncMock
from bot.ai import DeepSeek
from bot.config import Config
import test_bot as baseline

CHAT=baseline.PRIVATE

def reply(text='新回复',delays=None):
    return {'reply':True,'messages':[text],'memory':text,'next_minutes':90,'reply_delays_seconds':delays or [0]}

class ReplyTiming(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=baseline.Fixtures.asyncSetUp
    asyncTearDown=baseline.Fixtures.asyncTearDown
    drain=baseline.Fixtures.drain

    async def pending(self):
        async with asyncio.timeout(2):
            while CHAT not in self.engine.pending_replies:await asyncio.sleep(.005)

    async def test_new_message_cancels_unsent_reply_and_reconsiders(self):
        self.ai.decide.side_effect=[reply('过时回复',[20]),reply('新回复')]
        await self.engine.receive(baseline.event(private=True,text='第一句'))
        await self.pending()
        self.assertFalse(self.bridge.send.called)
        await self.engine.receive(baseline.event(private=True,text='补充一句',identifier=2))
        await asyncio.wait_for(self.drain(),2)
        self.assertEqual([c.args[1] for c in self.bridge.send.await_args_list],['新回复'])
        self.assertEqual([m['text'] for m in self.store.history(CHAT) if m['role']=='user'],['第一句','补充一句'])
        self.assertEqual(self.store.state(CHAT)['memory'],'新回复')
        self.assertNotIn(CHAT,self.engine.pending_replies)

    async def test_wait_does_not_hold_model_semaphore_and_pause_cancels(self):
        self.ai.decide.return_value=reply('不能发',[20])
        task=asyncio.create_task(self.engine.decide(CHAT))
        await self.pending()
        for _ in range(3):await asyncio.wait_for(self.engine.semaphore.acquire(),.2)
        for _ in range(3):self.engine.semaphore.release()
        previous=copy.deepcopy(self.config.value);self.config.update({'paused':True});self.engine.on_config_change(previous)
        await asyncio.wait_for(task,1)
        self.bridge.send.assert_not_called()

    async def test_admin_status_interrupts_wait_without_chat_classification(self):
        self.ai.decide.return_value=reply('过时回复',[20])
        await self.engine.receive(baseline.event(private=True,text='第一句'))
        await self.pending()
        await self.engine.receive(baseline.event(private=True,text='状态',identifier=2))
        await asyncio.wait_for(self.drain(),2)
        self.ai.administration.assert_not_called()
        self.assertEqual(self.bridge.send.await_count,1)
        self.assertIn('授权会话',self.bridge.send.await_args.args[1])

    async def test_queued_burst_is_one_model_decision(self):
        self.ai.decide.return_value=reply()
        await self.engine.receive(baseline.event(private=True,text='第一句'))
        await self.engine.receive(baseline.event(private=True,text='第二句',identifier=2))
        await self.drain()
        self.ai.decide.assert_awaited_once()
        self.assertEqual(len([m for m in self.store.history(CHAT) if m['role']=='user']),2)

    async def test_model_latency_counts_toward_first_wait(self):
        async def slow(*args):
            await asyncio.sleep(.03);return reply(delays=[.01])
        self.ai.decide.side_effect=slow
        self.engine.wait_to_reply=AsyncMock(return_value=True)
        await self.engine.decide(CHAT)
        self.assertEqual(self.engine.wait_to_reply.await_args.args[1],0)

    async def test_distinct_bubble_gaps_are_used(self):
        self.ai.decide.return_value={'reply':True,'messages':['一','二','三'],'memory':'','next_minutes':90,'reply_delays_seconds':[0,3,7]}
        self.engine.wait_to_reply=AsyncMock(return_value=True)
        await self.engine.decide(CHAT)
        self.assertEqual([c.args[1] for c in self.engine.wait_to_reply.await_args_list],[0,3,7])
        self.assertEqual(self.bridge.send.await_count,3)

    async def test_model_delays_are_clamped_and_missing_values_fallback(self):
        ai=DeepSeek(self.config,self.store,None)
        compiled={'messages':[],'generation':{'max_reply_chars':1000}}
        conv=self.config.conversation(CHAT)
        ai.json=AsyncMock(return_value={'reply':True,'messages':['一','二'],'memory':'','next_minutes':90,'reply_delays_seconds':[0,999]})
        result=await ai.generate(compiled,conv)
        self.assertEqual(result['reply_delays_seconds'],[2,12])
        ai.json=AsyncMock(return_value={'reply':True,'messages':['一'],'memory':'','next_minutes':90,'reply_delays_seconds':[float('nan')]})
        self.assertEqual((await ai.generate(compiled,conv))['reply_delays_seconds'],[6])
        conv['reply_timing']=False
        self.assertEqual((await ai.generate(compiled,conv))['reply_delays_seconds'],[0])

    async def test_config_migration_preserves_existing_conversation_and_enables_pacing(self):
        import json
        from bot.config import atomic_json
        old=copy.deepcopy(self.config.value)
        for conv in old['conversations'].values():
            conv.pop('reply_timing');conv.pop('max_reply_delay')
        atomic_json(self.config.path,old)
        migrated=Config(self.root)
        self.assertEqual(migrated.value['api_key'],old['api_key'])
        self.assertTrue(migrated.conversation(CHAT)['reply_timing'])
        self.assertEqual(migrated.conversation(CHAT)['max_reply_delay'],45)
        self.assertEqual(migrated.conversation(CHAT)['allowed'],old['conversations'][CHAT]['allowed'])

    async def test_revoke_while_waiting_for_model_slot_never_sends_history_to_model(self):
        self.engine.semaphore=asyncio.Semaphore(0)
        task=asyncio.create_task(self.engine.decide(CHAT))
        await asyncio.sleep(.01)
        self.config.update({'conversations':{CHAT:{'allowed':False}}})
        self.engine.semaphore.release()
        await asyncio.wait_for(task,1)
        self.ai.decide.assert_not_called()

    async def test_prompt_supplies_actual_tempo_without_making_it_dialogue(self):
        from bot.roleplay import compile_prompt
        now=time.time()
        history=[{'role':'user','text':'一','time':now-40},
                 {'role':'user','text':'二','time':now-38},
                 {'role':'user','text':'三','time':now-8}]
        p=compile_prompt(self.config.value,CHAT,history)
        self.assertIn('最近用户发言间隔（秒）：2.0、30.0',p['messages'][-2]['content'])
        self.assertEqual(p['messages'][-1]['content'],'三')
