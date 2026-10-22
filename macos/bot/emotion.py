"""Continuous person-level affect, relationships and evidence-aware changes."""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any

LABELS = {'平静', '开心', '好奇', '害羞', '委屈', '难过', '烦躁', '生气', '担心', '疲倦', '期待', '满足', '孤独', '失落', '放松', '无聊', '兴奋', '嫉妒', '怀念', '警惕', '矛盾', '释然', '失望'}
AXES = ('pleasure', 'energy', 'tension', 'warmth')
RELATION = ('trust', 'attachment', 'security')


def init_store(db) -> None:
    db.executescript('''
    CREATE TABLE IF NOT EXISTS emotion_state(
      character TEXT, subject TEXT, value TEXT, PRIMARY KEY(character,subject));
    CREATE TABLE IF NOT EXISTS emotion_control(key TEXT PRIMARY KEY,value TEXT);
    ''')


class Emotion:
    def __init__(self, config, store):
        self.config, self.store, self.db = config, store, store.db

    def enabled(self) -> bool:
        row = self.db.execute("SELECT value FROM emotion_control WHERE key='enabled'").fetchone()
        return not row or row[0] == 'true'

    def target(self, chat: str, proactive: bool = False) -> tuple[str, str]:
        from .roleplay import resolve
        character = resolve(self.config.value, chat)[0]['id']
        if chat.startswith('private:'):
            return character, chat.split(':', 1)[1]
        if not proactive:
            row = self.db.execute("SELECT sender FROM archive WHERE chat=? AND role='user' ORDER BY id DESC LIMIT 1", (chat,)).fetchone()
            if row and re.fullmatch(r'\d{5,20}', row[0]):
                return character, row[0]
        return character, ''

    def read(self, character: str, subject: str, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self.db.execute('SELECT value FROM emotion_state WHERE character=? AND subject=?', (character, subject)).fetchone()
        state = json.loads(row[0]) if row else {}
        elapsed = max(0, now - state.get('updated', now))
        # Affect recovers toward neutral, never toward resentment for absence.
        fast, slow = .5 ** (elapsed / (3 * 3600)), .5 ** (elapsed / (24 * 3600))
        result = {axis: state.get(axis, 0) * (slow if axis == 'warmth' else fast) for axis in AXES}
        result.update({axis: state.get(axis, 0) * .5 ** (elapsed/(90*86400)) for axis in RELATION})
        result['stance'] = state.get('stance', '')
        result['stance_chat'] = state.get('stance_chat','')
        result.update(flutter=state.get('flutter', 0) * fast,
                      mood=state.get('mood', 0) * slow,
                      labels=state.get('labels', ['平静']) if fast > .2 else ['平静'],
                      updated=state.get('updated', 0), revision=state.get('revision', 0),
                      events=state.get('events', []), sources=state.get('sources', {}),
                      plan=state.get('plan', {}))
        return result

    def context(self, chat: str, proactive: bool = False) -> dict[str, Any]:
        character, subject = self.target(chat, proactive)
        state = self.read(character, subject)
        row = self.db.execute("SELECT max(id) FROM archive WHERE chat=? AND role='user' AND sender=?", (chat, subject)).fetchone()
        return {'character': character, 'subject': subject, 'chat': chat,
                'source': row[0] or 0, 'revision': state['revision'],
                'enabled': self.enabled() and bool(subject), 'state': state}

    def prompt(self, context: dict) -> str:
        state = context['state']
        visible = {key: state[key] for key in (*AXES, *RELATION, 'flutter', 'mood', 'labels')}
        if state.get('stance_chat') == context['chat']:
            visible['stance'] = state.get('stance','')
        # Event text from a private conversation must never enter a group prompt.
        events = [e for e in state['events'] if e['chat'] == context['chat']]
        return ('情绪状态（仅此角色对此QQ，其他人的状态不参与）：' + json.dumps(
            {'QQ': context['subject'], 'state': visible, 'recent_events': events[-3:]}, ensure_ascii=False) +
            '\n根据角色卡、当前性格、长期记忆、相处经历和既有情绪自主决定态度、reply_delays_seconds与next_minutes。'
            '可以有混合情绪、自己的偏好和边界，也可以拒绝、冷淡、沉默、释然；不强制开心、顺从或亲密。'
            '同一个QQ在群聊和私聊中的关系与情绪连续，场景只影响表达方式，不重置感受。'
            '本轮回复表现更新后的情绪，不必提及内部状态。情绪可以混合，不能机械按关键词变化。'
            'recent_events只是历史原因，不代表事情仍未解决；以最新互动和当前状态为准，不重复翻旧账。'
            '关系变化需要互动依据；时间可以让情绪自然恢复。不要把无消息当作对方做过某件事。'
            '额外可在emotion中返回trust（信任）、attachment（依恋）、security（关系安全感），各-1到1，代表目标状态；'
            'stance可写最多200字的当前关系理解，属于内部记录，不能把私聊缘由透露到群聊。'
            '额外返回emotion对象：{"labels":["开心","害羞"],"pleasure":0.2,"energy":0.1,'
            '"tension":0,"warmth":0.1,"flutter":5,"reason":"本轮具体原因","event":"仍在意的事情，没有则空字符串"}；'
            '四个数是目标状态，范围-1到1，tension范围0到1。普通情绪单轮变化最多0.25，warmth最多0.08，按此幅度表现回复。labels可选平静、开心、好奇、害羞、委屈、难过、烦躁、生气、担心、疲倦、期待、满足、孤独、失落、放松、无聊、兴奋、嫉妒、怀念、警惕、矛盾、释然、失望，最多3项。'
            'flutter是0～100的当下心动值，表示对此人的浪漫触动，区别于warmth的放松亲近。单轮最多变化5；无新触动时可保持或回落。'
            '结合有依据的相处细节、人设和既有关系判断，普通关心、赞美或对方要求加分不必引起心动；不能用数值自动确立恋爱、同意亲密互动或覆盖对方意愿。'
            '心动可影响害羞、措辞和三个回复节奏，也可能犹豫；不固定加快、不刻意拖延，正常聊天不报数。'
            '另返回timing_reason字符串，简短解释这轮三个节奏如何考虑情绪和上下文。reason、event与timing_reason都不放进聊天气泡。')

    @staticmethod
    def valid(raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        for axis in AXES:
            value = raw.get(axis)
            if type(value) not in (int, float) or not math.isfinite(value) or not (-1 <= value <= 1):
                return False
        for axis in RELATION:
            if axis in raw and (type(raw[axis]) not in (int,float) or not math.isfinite(raw[axis]) or not -1 <= raw[axis] <= 1):
                return False
        if not isinstance(raw.get('stance',''),str) or len(raw.get('stance',''))>200: return False
        flutter = raw.get('flutter', 0)
        if type(flutter) not in (int, float) or not math.isfinite(flutter) or not 0 <= flutter <= 100:
            return False
        labels = raw.get('labels')
        if not isinstance(labels, list) or not 1 <= len(labels) <= 3 or any(not isinstance(v, str) or v not in LABELS for v in labels):
            return False
        for key in ('reason', 'event'):
            if not isinstance(raw.get(key, ''), str) or len(raw.get(key, '')) > 400:
                return False
        return True

    def sandbox_context(self, previous: object = None) -> dict:
        state = self.read('', '')
        if isinstance(previous, dict):
            # This is browser-local fictional state, never an official QQ profile.
            candidate = {**previous, 'reason': '', 'event': ''}
            if self.valid(candidate):
                for key in (*AXES, 'labels'):
                    state[key] = previous[key]
                state['flutter'] = previous.get('flutter', 0)
                mood = previous.get('mood', 0)
                if type(mood) in (int, float) and math.isfinite(mood) and -1 <= mood <= 1:
                    state['mood'] = mood
        return {'character': 'sandbox', 'subject': 'sandbox', 'chat': 'sandbox',
                'source': 0, 'revision': 0, 'enabled': True, 'state': state}

    @staticmethod
    def advance(old: dict, raw: dict) -> dict:
        state = dict(old)
        for axis in AXES:
            step = .08 if axis == 'warmth' else .25
            state[axis] = max(0 if axis == 'tension' else -1, min(1, old[axis] + max(-step, min(step, raw[axis] - old[axis]))))
        for axis in RELATION:
            previous = old.get(axis,0)
            state[axis] = max(-1,min(1,previous+max(-.08,min(.08,raw.get(axis,previous)-previous))))
        state['stance'] = raw.get('stance',old.get('stance',''))
        old_flutter = old.get('flutter', 0)
        target_flutter = raw.get('flutter', old_flutter)
        state['flutter'] = max(0, min(100, old_flutter + max(-5, min(5, target_flutter - old_flutter))))
        state['mood'] = old['mood'] + .08 * (state['pleasure'] - old['mood'])
        state['labels'] = list(dict.fromkeys(raw['labels']))
        return state

    def commit(self, context: dict, result: dict) -> bool:
        if not context['enabled'] or not self.enabled():
            return False
        old = self.read(context['character'], context['subject'])
        if old['revision'] != context['revision']:
            return False  # Another conversation for this person changed it meanwhile.
        raw = result.get('emotion')
        if not isinstance(raw, dict):
            return False  # Older models remain compatible; do not invent affect.
        if not self.valid(raw):
            return False
        now = time.time()
        source = context['source']
        fresh = source > old['sources'].get(context['chat'], 0)
        state = dict(old)
        if fresh:
            state = self.advance(old, raw)
            if 'stance' in raw: state['stance_chat'] = context['chat']
            state['sources'] = {**old['sources'], context['chat']: source}
            state['events'] = (old['events'] + [{'chat': context['chat'], 'source': source, 'time': now,
                'reason': raw.get('reason', '')[:240], 'event': raw.get('event', '')[:240]}])[-12:]
        reason = result.get('timing_reason', '')
        state['plan'] = {'chat': context['chat'], 'time': now,
                         'delays': result.get('reply_delays_seconds', []),
                         'next_minutes': result.get('next_minutes'),
                         'reason': reason[:240] if isinstance(reason, str) else ''}
        state.update(updated=now, revision=old['revision'] + 1)
        self.db.execute('INSERT OR REPLACE INTO emotion_state VALUES(?,?,?)',
            (context['character'], context['subject'], json.dumps(state, ensure_ascii=False)))
        self.db.commit()
        return True

    def inspect(self, chat: str) -> dict:
        context = self.context(chat)
        state = context['state']
        state['events'] = [e for e in state['events'] if e['chat'] == chat]
        if state.get('stance_chat') != chat:
            state['stance'] = ''
        if state['plan'].get('chat') != chat:
            state['plan'] = {}
        return {**context, 'global_enabled': self.enabled(), 'state': state}

    def control(self, chat: str, enabled: bool | None = None) -> None:
        if enabled is not None:
            self.db.execute("INSERT OR REPLACE INTO emotion_control VALUES('enabled',?)", (json.dumps(enabled),))
        else:
            character, subject = self.target(chat)
            self.db.execute('DELETE FROM emotion_state WHERE character=? AND subject=?', (character, subject))
        # All chats may have a pending reply for this QQ: invalidate all in-flight work.
        for key in self.config.value['conversations']:
            current = self.store.state(key)
            current['generation'] += 1
            self.store.save_state(key, current)
        self.db.commit()
