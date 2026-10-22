from __future__ import annotations
import copy
import unittest
from bot.config import defaults
from bot import roleplay
from bot.context import bounded_history, layers, memory_context


def policy() -> dict:
    return {'enabled':True,'core':{'description':'稳定身份蓝鲸','personality':'聪明嘴硬但有自主性','scenario':'慢热，不给选项','mes_example':''},
            'detail_chars':300,'history_messages':4,'history_chars':500,'memory_chars':1000,'lore_chars':1000}


class LayeredContext(unittest.TestCase):
    def setUp(self):
        self.cfg=defaults();self.card=self.cfg['roleplay']['characters'][0]
        self.card['extensions']={'qq_companion':{'prompt_layers':policy()}}
        self.card['description']='【外貌】\n尾巴深蓝色，末端分两瓣。\n\n【饮食】\n喜欢热白饭，不需要配菜。'
        self.card['personality']='不可每次全部发送的作者完整原卡。'*200
        self.card['mes_example']='白饭示例：我先吃一口。\n\n打游戏示例：这关真难。'
    def compile(self,text='在吗',history=None):
        return roleplay.compile_prompt(self.cfg,'',history or [{'role':'user','text':text}])
    def test_full_card_preserved_and_only_related_detail_retrieved(self):
        original=copy.deepcopy(self.cfg)
        result=self.compile('你的尾巴是什么样的')
        text='\n'.join(m['content'] for m in result['messages'])
        self.assertIn('稳定身份蓝鲸',text);self.assertIn('尾巴深蓝色',text)
        self.assertNotIn('喜欢热白饭',text);self.assertNotIn('作者完整原卡',text)
        self.assertEqual(self.cfg,original)
        self.assertLessEqual(sum(s['detail_chars'] for s in result['profile_retrieval'].values()),300)
    def test_greeting_does_not_force_first_paragraph(self):
        text='\n'.join(m['content'] for m in self.compile()['messages'])
        self.assertNotIn('尾巴深蓝色',text)
    def test_selected_macros_cannot_reintroduce_full_card(self):
        e=roleplay.entry('macro');e.update(constant=True,content='{{personality}}')
        self.cfg['roleplay']['worldbooks']=[{'id':'test_book','name':'macro','entries':[e]}]
        self.card['worldbook_ids']=['test_book']
        text='\n'.join(m['content'] for m in self.compile()['messages'])
        self.assertNotIn('作者完整原卡',text)
    def test_history_is_bounded_but_latest_long_turn_is_whole(self):
        history=[{'role':'user','text':str(i)*200} for i in range(8)]
        result=self.compile(history=history)
        self.assertGreater(result['dropped_messages'],0)
        self.assertEqual(result['messages'][-1]['content'],history[-1]['text'])
        long=[{'role':'user','text':'长'*1500}]
        kept,dropped=bounded_history(history+long,policy())
        self.assertEqual(kept,long);self.assertEqual(dropped,len(history))
    def test_disabled_policy_retains_existing_full_profile_behavior(self):
        self.card['extensions']['qq_companion']['prompt_layers']['enabled']=False
        result=self.compile();self.assertFalse(result['context_layers']['enabled'])
        self.assertIn(self.card['personality'],'\n'.join(m['content'] for m in result['messages']))
    def test_invalid_policy_is_rejected_on_save(self):
        self.card['extensions']['qq_companion']['prompt_layers']['history_chars']=-10
        with self.assertRaises(ValueError):roleplay.validate(self.cfg['roleplay'])
    def test_summary_only_bridges_trimmed_context_and_is_bounded(self):
        p=policy()
        text=memory_context(p,'人工事实','旧剧情摘要','检索原话',trimmed=False)
        self.assertNotIn('旧剧情摘要',text);self.assertIn('检索原话',text)
        text=memory_context(p,'人工事实','旧剧情摘要','检索原话'*1000,trimmed=True)
        self.assertIn('旧剧情摘要',text);self.assertIn('原话与当前纠正优先',text)
        self.assertLessEqual(len(text),p['memory_chars'])
        p['memory_chars']=500
        self.assertLessEqual(len(memory_context(p,'事实'*500,'摘要'*500,'原话'*500,trimmed=True)),500)
