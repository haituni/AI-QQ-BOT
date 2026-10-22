"""Opt-in prompt layers; source cards remain intact and inspectable.

Local paragraph/keyword retrieval, not an embedding model. No I/O or user memory.
"""
from __future__ import annotations
import re
from typing import Any

FIELDS = ('description', 'personality', 'scenario', 'mes_example')
LIMITS = {'detail_chars': (0, 4000), 'history_messages': (2, 40),
          'history_chars': (500, 12000), 'memory_chars': (500, 4000),
          'lore_chars': (500, 6000)}
STOP = {'什么','怎么','这个','那个','一下','一个','我们','你们','他们','就是','的是','可以','没有','不是','自己','时候','这样','这样子','因为','所以','觉得','还是','已经','真的','不要','知道','现在','今天','刚刚'}


def layers(card: dict[str, Any]) -> dict[str, Any]:
    ext = card.get('extensions', {}).get('qq_companion', {})
    policy = ext.get('prompt_layers', {}) if isinstance(ext, dict) else {}
    if not isinstance(policy, dict) or policy.get('enabled') is not True:
        return {}
    core = policy.get('core')
    if not isinstance(core, dict) or set(core) != set(FIELDS):
        raise ValueError('分层人设必须包含四个核心字段')
    if any(not isinstance(core[k], str) or len(core[k]) > 3000 for k in FIELDS):
        raise ValueError('分层人设核心字段过长或格式无效')
    for key, (low, high) in LIMITS.items():
        if type(policy.get(key)) is not int or not low <= policy[key] <= high:
            raise ValueError('分层上下文预算无效：' + key)
    return policy


def bounded_history(history: list[dict], policy: dict) -> tuple[list[dict], int]:
    if not policy:
        return history, 0
    kept = list(history[-policy['history_messages']:])
    def size(item: dict) -> int:
        return len(str(item.get('text', item.get('content', ''))))
    # Never truncate the latest message. A long newest message still uses the
    # compiler's global safety limit; older messages leave only as whole turns.
    while len(kept) > 1 and sum(map(size, kept)) > policy['history_chars']:
        kept.pop(0)
    return kept, len(history) - len(kept)


def retrieve_profile(card: dict, history: list[dict], policy: dict) -> tuple[dict, dict]:
    from .memory import terms
    query = '\n'.join(str(m.get('text', m.get('content', ''))) for m in [m for m in history if m.get('role') == 'user'][-2:])
    query = query[-1600:]
    tokens = set(terms(query)) - STOP
    candidates = []
    originals = {field: card[field] for field in FIELDS}
    for field, text in originals.items():
        heading = ''
        for order, paragraph in enumerate(re.split(r'\n\s*\n|\n(?=\d+[.、]|[-*] )', text)):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if paragraph.startswith('#'):
                first, _, rest = paragraph.partition('\n')
                heading = first
                paragraph = rest.strip()
                if not paragraph:
                    continue
            # Long sections split at line boundaries, rather than fixed offsets.
            parts = paragraph.splitlines() if len(paragraph) > 600 else [paragraph]
            for sub, part in enumerate(parts):
                part = part.strip()
                if not part or len(part) > policy['detail_chars']:
                    continue
                hits = tokens & (set(terms(part + ' ' + heading)) - STOP)
                if not hits:
                    continue
                score = len(hits) / max(1, len(set(terms(part)))) ** .5
                if score < .16:
                    continue
                candidates.append((-score, field, order, sub, heading, part))
    selected = []; used = 0; seen = set()
    for candidate in sorted(candidates):
        _, field, order, sub, heading, part = candidate
        if part in seen or part in policy['core'][field]:
            continue
        rendered = (heading + '\n' if heading else '') + part
        if used + len(rendered) + 2 > policy['detail_chars']:
            continue
        seen.add(part); used += len(rendered) + 2
        selected.append((field, order, sub, rendered))
    result = dict(card); stats = {}
    for field in FIELDS:
        details = '\n\n'.join(row[3] for row in sorted(selected) if row[0] == field)
        core = policy['core'][field]
        result[field] = core + ('\n本轮相关原卡片段（语气示例不是共同经历）：\n' + details if details else '')
        stats['character.' + field] = {'original_chars': len(originals[field]),
            'selected_chars': len(result[field]), 'core_chars': len(core),
            'detail_chars': len(details), 'mode': 'layered_local_retrieval'}
    return result, stats


def memory_context(policy: dict, manual: str, summary: str, recalled: str, *, trimmed: bool) -> str:
    if not policy:
        return manual[:800] + '\n' + recalled
    budget = policy['memory_chars']
    parts = []
    def append(label: str, text: str, limit: int) -> None:
        room = budget - sum(len(p) + 1 for p in parts) - len(label) - 1
        if text and room > 0:
            parts.append(label + text[:min(room,limit)])
    append('管理员记录的会话事实：\n', manual, 300)
    if trimmed:
        append('本会话先前的模型摘要（可能出错；只作连续性参考，原话与当前纠正优先；虚构剧情不作真实经历）：\n', summary, 650)
    prefix = '\n'.join(parts)
    remaining = max(0, budget - len(prefix) - 1)
    # Keep retrieved evidence as whole lines; never turn a clipped fact into a
    # different assertion just to fill the remaining budget.
    selected = []
    for line in recalled.splitlines():
        if line and len(line) + 1 <= remaining:
            selected.append(line); remaining -= len(line) + 1
    return prefix + ('\n' + '\n'.join(selected) if selected else '')
