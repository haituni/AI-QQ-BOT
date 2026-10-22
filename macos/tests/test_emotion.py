from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.config import Config
from bot.store import Store
from bot.emotion import Emotion
from bot.ai import DeepSeek
from bot.engine import Engine

PRIVATE = 'private:987654321'
GROUP = 'group:123456789'


def decision():
    return {'reply': True, 'messages': ['嗯，听你说'], 'memory': '', 'next_minutes': 90,
            'reply_delays_seconds': [4], 'timing_reason': '有些担心，先及时接话，之后留一点空间',
            'emotion': {'labels': ['担心', '委屈'], 'pleasure': -.8, 'energy': -.3,
                        'tension': .8, 'warmth': .8, 'reason': '私聊中的具体原因', 'event': '私聊秘密'}}


class EmotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root)
        self.config.update({'conversations': {PRIVATE: {'allowed': True}, GROUP: {'allowed': True}}})
        self.store = Store(self.root)
        self.emotion = Emotion(self.config, self.store)
        self.store.add(PRIVATE, 'user', '987654321', '刚刚有点误会')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_person_character_isolation_persistence_decay_and_bounds(self):
        c = self.emotion.context(PRIVATE)
        self.assertTrue(self.emotion.commit(c, decision()))
        s = self.emotion.read(c['character'], c['subject'])
        self.assertAlmostEqual(s['pleasure'], -.25, places=4)
        self.assertAlmostEqual(s['warmth'], .08, places=4)
        self.assertEqual(self.emotion.read(c['character'], '111112222')['labels'], ['平静'])
        self.assertEqual(self.emotion.read('different-character', c['subject'])['labels'], ['平静'])
        self.assertEqual(Emotion(self.config, self.store).read(c['character'], c['subject'])['labels'], ['担心', '委屈'])
        later = self.emotion.read(c['character'], c['subject'], time.time() + 86400)
        self.assertLess(abs(later['pleasure']), .002)
        self.assertEqual(later['labels'], ['平静'])
        self.assertGreaterEqual(later['tension'], 0)

    def test_flutter_is_independent_bounded_persistent_and_decays(self):
        c = self.emotion.context(PRIVATE)
        result = decision(); result['emotion']['flutter'] = 90
        self.assertTrue(self.emotion.commit(c, result))
        state = self.emotion.read(c['character'], c['subject'])
        self.assertAlmostEqual(state['flutter'], 5, places=4)
        self.assertAlmostEqual(state['warmth'], .08, places=4)
        self.assertEqual(self.emotion.read(c['character'], '111112222')['flutter'], 0)
        self.assertEqual(self.emotion.read('another', c['subject'])['flutter'], 0)
        self.assertAlmostEqual(self.emotion.read(c['character'], c['subject'], state['updated'] + 10800)['flutter'], 2.5)
        self.emotion.commit(self.emotion.context(PRIVATE), result)
        self.assertLessEqual(self.emotion.context(PRIVATE)['state']['flutter'], 5)
        self.assertAlmostEqual(Emotion(self.config, self.store).context(PRIVATE)['state']['flutter'], 5, places=3)
        self.emotion.control(PRIVATE)
        self.assertEqual(self.emotion.context(PRIVATE)['state']['flutter'], 0)

    def test_flutter_missing_preserves_old_value_and_invalid_rejects(self):
        raw = decision()['emotion']
        self.assertTrue(self.emotion.valid(raw))
        old = self.emotion.context(PRIVATE)['state']; old['flutter'] = 42
        self.assertEqual(self.emotion.advance(old, raw)['flutter'], 42)
        for value in (-1, 101, True, float('nan'), float('inf'), '50'):
            with self.subTest(value=value):
                self.assertFalse(self.emotion.valid({**raw, 'flutter': value}))
        legacy = {**raw, 'mood': 0}
        self.assertEqual(self.emotion.sandbox_context(legacy)['state']['flutter'], 0)
        sandbox = self.emotion.sandbox_context({**legacy, 'flutter': 42})
        self.assertEqual(sandbox['state']['flutter'], 42)
        self.assertIn('"flutter": 42', self.emotion.prompt(sandbox))
        decreased = self.emotion.advance(old, {**raw, 'flutter': 0})
        self.assertEqual(decreased['flutter'], 37)

    def test_same_person_group_reuses_affect_without_private_event_or_plan(self):
        c = self.emotion.context(PRIVATE)
        self.emotion.commit(c, decision())
        self.store.add(GROUP, 'user', '987654321', '你好')
        gc = self.emotion.context(GROUP)
        self.assertEqual(gc['state']['labels'], ['担心', '委屈'])
        self.assertNotIn('私聊秘密', self.emotion.prompt(gc))
        self.assertNotIn('私聊中的具体原因', json.dumps(self.emotion.inspect(GROUP), ensure_ascii=False))
        self.assertEqual(self.emotion.inspect(GROUP)['state']['plan'], {})
        self.assertFalse(self.emotion.context(GROUP, proactive=True)['enabled'])
        self.store.add(GROUP, 'user', '111112222', '另一个人')
        self.assertEqual(self.emotion.context(GROUP)['state']['labels'], ['平静'])

    def test_duplicate_source_and_proactive_do_not_inflate_emotion(self):
        self.emotion.commit(self.emotion.context(PRIVATE), decision())
        before = self.emotion.context(PRIVATE)
        self.emotion.commit(before, decision())
        after = self.emotion.context(PRIVATE)['state']
        self.assertLessEqual(after['warmth'], .08)
        self.assertEqual(len(after['events']), 1)
        self.assertEqual(after['plan']['next_minutes'], 90)

    def test_stale_revision_disabled_and_malformed_cannot_write(self):
        stale = self.emotion.context(PRIVATE)
        self.emotion.commit(stale, decision())
        self.assertFalse(self.emotion.commit(stale, decision()))
        self.emotion.control(PRIVATE, False)
        self.assertFalse(self.emotion.commit(self.emotion.context(PRIVATE), decision()))
        self.emotion.control(PRIVATE, True)
        for value in (float('nan'), float('inf'), True, 2):
            bad = decision(); bad['emotion']['energy'] = value
            self.assertFalse(self.emotion.commit(self.emotion.context(PRIVATE), bad))
        bad = decision(); bad['emotion']['labels'] = ['任意注入文本']
        self.assertFalse(self.emotion.commit(self.emotion.context(PRIVATE), bad))

    def test_reset_and_clear_remove_derived_affect_and_invalidate_other_chat(self):
        self.emotion.commit(self.emotion.context(PRIVATE), decision())
        self.store.save_state(GROUP, self.store.state(GROUP))
        self.emotion.control(PRIVATE)
        self.assertEqual(self.emotion.context(PRIVATE)['state']['revision'], 0)
        self.assertEqual(self.store.state(GROUP)['generation'], 1)
        self.emotion.commit(self.emotion.context(PRIVATE), decision())
        self.store.clear(PRIVATE)
        self.assertEqual(self.emotion.context(PRIVATE)['state']['labels'], ['平静'])
        self.assertEqual(self.store.state(GROUP)['generation'], 2)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.config = Config(self.root)
        self.config.update({'conversations': {PRIVATE: {'allowed': True}}})
        self.store = Store(self.root)
        self.store.add(PRIVATE, 'user', '987654321', '有些误会')
        self.ai = DeepSeek(self.config, self.store, None)
        self.ai.json = AsyncMock(return_value=decision())
        self.ai.memory.consider = lambda chat: None
        self.bridge = type('Bridge', (), {'connected': True, 'online': True, 'send': AsyncMock()})()
        self.engine = Engine(self.config, self.store, self.ai, self.bridge)
        self.engine.wait_to_reply = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        await self.engine.close(); self.store.db.close(); self.temp.cleanup()

    async def test_same_call_affect_and_timing_commit_only_by_valid_engine(self):
        result = await self.ai.decide(PRIVATE)
        self.assertEqual(self.ai.emotion.context(PRIVATE)['state']['revision'], 0)
        self.assertIn('情绪状态', self.ai.json.await_args.kwargs['messages'][-1]['content'])
        self.assertEqual(result['reply_delays_seconds'], [4])
        self.ai.json.reset_mock()
        await self.engine.decide(PRIVATE)
        self.ai.json.assert_awaited_once()
        self.assertEqual(self.ai.emotion.context(PRIVATE)['state']['revision'], 1)
        self.assertEqual(self.bridge.send.await_count, 1)
        self.assertGreater(self.store.state(PRIVATE)['next_at'], time.time() + 89 * 60)

    async def test_superseded_generation_never_commits_affect(self):
        async def superseded(*args, **kwargs):
            self.engine.incoming_versions[PRIVATE] = 1
            return decision()
        self.ai.json.side_effect = superseded
        await self.engine.decide(PRIVATE)
        self.assertEqual(self.ai.emotion.context(PRIVATE)['state']['revision'], 0)
        self.bridge.send.assert_not_awaited()

    async def test_sandbox_generate_does_not_read_or_commit_real_affect(self):
        from bot.roleplay import compile_prompt
        compiled = compile_prompt(copy.deepcopy(self.config.value), '', [], '')
        await self.ai.generate(compiled, self.config.conversation(PRIVATE), purpose='sandbox')
        self.assertNotIn('私聊秘密', json.dumps(self.ai.json.await_args.kwargs['messages'], ensure_ascii=False))
        self.assertEqual(self.ai.emotion.context(PRIVATE)['state']['revision'], 0)

    async def test_silent_decision_also_schedules_next_model_interval(self):
        result = decision(); result.update(reply=False, messages=[], next_minutes=75)
        self.ai.json.return_value = result
        await self.engine.decide(PRIVATE)
        self.bridge.send.assert_not_awaited()
        self.assertGreater(self.store.state(PRIVATE)['next_at'], time.time() + 74 * 60)
        self.assertEqual(self.ai.emotion.context(PRIVATE)['state']['labels'], ['担心', '委屈'])


class EmotionAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        from bot.server import create_app
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.app = create_app(self.root, offline=True)
        self.client = TestClient(TestServer(self.app), headers={'Host': '127.0.0.1:8765', 'X-Bot-Request': '1'})
        await self.client.start_server()
        self.config = self.app['config']
        self.config.update({'conversations': {PRIVATE: {'allowed': True}}})
        await self.client.post('/api/login', json={'token': self.config.control_token})
        self.store = self.app['store']; self.affect = self.app['ai'].emotion
        self.store.add(PRIVATE, 'user', '987654321', '发生了误会')

    async def asyncTearDown(self):
        await self.client.close(); self.temp.cleanup()

    async def test_view_control_and_reset_preserve_memory(self):
        self.affect.commit(self.affect.context(PRIVATE), decision())
        data = await (await self.client.get('/api/long-memory?chat=' + PRIVATE)).json()
        self.assertEqual(data['emotion']['state']['labels'], ['担心', '委屈'])
        response = await self.client.post('/api/long-memory', json={'action': 'emotion-enabled', 'chat': PRIVATE, 'enabled': 'true'})
        self.assertEqual(response.status, 400)
        response = await self.client.post('/api/long-memory', json={'action': 'emotion-enabled', 'chat': PRIVATE, 'enabled': False})
        self.assertEqual(response.status, 200); self.assertFalse(self.affect.enabled())
        response = await self.client.post('/api/long-memory', json={'action': 'emotion-reset', 'chat': PRIVATE})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.affect.context(PRIVATE)['state']['labels'], ['平静'])
        self.assertEqual(len(self.store.history(PRIVATE)), 1)

    async def test_sandbox_continuity_and_live_preview_are_isolated(self):
        self.affect.commit(self.affect.context(PRIVATE), decision())
        revision = self.affect.context(PRIVATE)['state']['revision']
        model_result = decision(); model_result['emotion']['flutter'] = 50
        self.app['ai'].json = AsyncMock(return_value=model_result)
        first = await self.client.post('/api/roleplay/test', json={'mode': 'sandbox', 'text': '有点委屈'})
        self.assertEqual(first.status, 200)
        data = await first.json()
        self.assertAlmostEqual(data['result']['emotion_state']['warmth'], .08)
        self.assertEqual(data['result']['emotion_state']['flutter'], 5)
        second = await self.client.post('/api/roleplay/test', json={'mode': 'sandbox', 'text': '慢慢解释', 'emotion_state': data['result']['emotion_state']})
        self.assertEqual(second.status, 200)
        second_data = await second.json()
        self.assertAlmostEqual(second_data['result']['emotion_state']['warmth'], .16)
        self.assertEqual(second_data['result']['emotion_state']['flutter'], 10)
        self.assertEqual(self.affect.context(PRIVATE)['state']['revision'], revision)
        response = await self.client.post('/api/roleplay/preview', json={'mode': 'live', 'chat': PRIVATE})
        prompt = (await response.json())['messages'][-1]['content']
        self.assertIn('情绪状态', prompt)
        self.assertIn('私聊秘密', prompt)

    async def test_backups_restore_emotion_and_switch(self):
        self.affect.commit(self.affect.context(PRIVATE), decision())
        self.affect.control(PRIVATE, False)
        snap = self.app['operations'].snapshot('emotion-test')
        self.affect.control(PRIVATE, True); self.affect.control(PRIVATE)
        self.app['operations'].restore(snap['id'])
        self.assertEqual(self.affect.context(PRIVATE)['state']['labels'], ['担心', '委屈'])
        self.assertFalse(self.affect.enabled())
