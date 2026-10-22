"""DeepSeek structured decisions; chat content never grants configuration access."""
from __future__ import annotations

import json
import asyncio
import copy
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .config import Config, conversation_defaults
from .store import Store
from .memory import Memory
from contextvars import ContextVar
from .roleplay import compile_prompt
from .toolkit import ToolService, ToolContext, definitions
from .model_queue import ModelQueue, QueueFull

TZ = ZoneInfo("Asia/Shanghai")


def day_now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


class ModelError(Exception):
    pass


class ModelDeferred(ModelError):
    """A local cancellation or capacity condition, not a provider failure."""


def json_object(content: Any) -> dict[str, Any]:
    """Accept one JSON object, optionally enclosed in a complete Markdown fence."""
    if not isinstance(content, str):
        raise ValueError('Expected JSON text')
    text = content.strip().removeprefix('\ufeff').strip()
    fence = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text, flags=re.IGNORECASE)
    if fence:
        text = fence[1]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object')
    return value


class DeepSeek:
    def __init__(self, config: Config, store: Store, session: aiohttp.ClientSession):
        self.config, self.store, self.session = config, store, session
        self.endpoint = "https://api.deepseek.com/chat/completions"
        self.failures = 0
        self.retry_at = 0.0
        self.active = 0
        self.queue = ModelQueue()
        self.last_success = 0.0
        self.last_error = ''
        self.tools = ToolService(config, store, session)
        self.memory = Memory(config, store, self)
        from .emotion import Emotion
        self.emotion = Emotion(config, store)
        self.usage_chat = ContextVar("usage_chat", default="")
        self.usage_purpose = ContextVar("usage_purpose", default="chat")
        self.request_guard = ContextVar("request_guard", default=None)
        self.background_work = ContextVar('background_work', default=False)

    async def json(self, system: str, data: dict[str, Any], *, purpose: str = "chat", usage_chat: str = "", **options) -> dict[str, Any]:
        if time.time() < self.retry_at:
            raise ModelError(f"模型连接暂时保护中，{max(1, int(self.retry_at-time.time()))} 秒后可重试；请检查连接设置")
        if getattr(self.store, 'maintenance', False):
            raise ModelDeferred('数据库正在维护，请稍后重试')
        purpose_token = self.usage_purpose.set(purpose)
        context = options.get("tool_context")
        chat_token = self.usage_chat.set(usage_chat or (context.chat if context and not context.sandbox else ""))
        revision = self.config.value['revision']
        chat = self.usage_chat.get()
        generation = self.store.state(chat)['generation'] if chat else None
        parent_guard = self.request_guard.get()
        def guard():
            if parent_guard:
                parent_guard()
            if (getattr(self.store, 'maintenance', False) or revision != self.config.value['revision']
                    or (chat and generation != self.store.state(chat)['generation'])):
                raise ModelDeferred('排队期间设置或记忆已变化，本次请求已取消')
            if context:
                try:
                    self.tools.check(context)
                except ValueError as error:
                    raise ModelDeferred(str(error)) from error
        guard_token = self.request_guard.set(guard)
        background_token = self.background_work.set(self.background_work.get() or purpose in {'memory', 'autonomy'})
        self.active += 1
        started = time.monotonic()
        try:
            async with asyncio.timeout(180):
                result = await self._json(system, data, **options)
            self.failures, self.retry_at, self.last_error = 0, 0, ''
            self.last_success = time.time()
            return result
        except TimeoutError as error:
            self.last_error = "模型与工具处理超过本轮总时限"
            raise ModelError(self.last_error) from error
        except ModelDeferred:
            raise
        except ModelError as error:
            self.failures += 1
            self.last_error = str(error)
            if self.failures >= 3:
                self.retry_at = time.time() + min(300, 60 * (self.failures-2))
            raise
        finally:
            self.active -= 1
            self.usage_purpose.reset(purpose_token)
            self.usage_chat.reset(chat_token)
            self.request_guard.reset(guard_token)
            self.background_work.reset(background_token)

    async def _completion(self, messages: list[dict], parameters: dict, schemas: list[dict]) -> dict:
        # Only HTTP requests hold slots. A tool may make a nested model call.
        try:
            async with self.queue.slot(background=self.background_work.get()):
                guard = self.request_guard.get()
                if guard:
                    guard()
                if time.time() < self.retry_at:
                    raise ModelDeferred('模型连接暂时保护中，请稍后重试')
                return await self._request_completion(messages, parameters, schemas)
        except QueueFull as error:
            raise ModelDeferred(str(error)) from error

    async def _request_completion(self, messages: list[dict], parameters: dict, schemas: list[dict]) -> dict:
        cfg = self.config.value
        if not cfg["api_key"]:
            raise ModelError("尚未配置 DeepSeek API Key")
        if not self.store.reserve_call(day_now(), cfg["daily_model_limit"]):
            raise ModelError("今天的模型调用已达到管理员设置的上限")
        body = {"model": cfg["model"], "messages": messages,
                "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
                "max_tokens": parameters.get("max_tokens", 2000), "stream": False,
                "temperature": parameters.get("temperature", 1.0), "top_p": parameters.get("top_p", 1.0)}
        if schemas:
            body.update(tools=schemas, tool_choice='auto')
        started = time.monotonic()
        usage, success = {}, False
        try:
            async with self.session.post(self.endpoint, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json=body, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=90)) as response:
                if response.status != 200:
                    raise ModelError(f"DeepSeek HTTP {response.status}，请检查密钥、余额和模型名称")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ModelError('模型响应超过大小限制')
                payload = json.loads(raw)
            if isinstance(payload.get('usage'), dict): usage = payload['usage']
            choice = payload['choices'][0]
            if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
                raise ModelError('模型响应格式无效')
            if choice.get('finish_reason') not in ('stop', 'tool_calls'):
                raise ModelError('模型输出不完整，本次不发送')
            success = True
            return choice
        except (aiohttp.ClientError, TimeoutError) as error:
            raise ModelError("DeepSeek 网络连接失败或超时") from error
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise ModelError("DeepSeek 返回格式无效，本次不发送") from error
        finally:
            self.store.model_metric(success, time.monotonic()-started, usage, model=cfg["model"], purpose=self.usage_purpose.get(), chat=self.usage_chat.get())

    async def _json(self, system: str, data: dict[str, Any], *, messages: list[dict] | None = None,
                    generation: dict[str, Any] | None = None, tool_context: ToolContext | None = None) -> dict[str, Any]:
        request_messages = copy.deepcopy(messages) if messages is not None else [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]
        if messages is not None:
            # Stored replies contain text, while this endpoint must produce JSON.
            # Preserve content without teaching following turns to emit plain text.
            for item in request_messages:
                if item.get('role') == 'assistant' and isinstance(item.get('content'), str):
                    item['content'] = json.dumps({'reply': True, 'messages': [item['content']], 'memory': '', 'next_minutes': 60}, ensure_ascii=False)
        settings = copy.deepcopy(self.config.value['tools'])
        schemas = definitions(tool_context.tools) if tool_context else []
        call_ids: set[str] = set()
        result_chars = 0
        final_instruction_added = False
        for round_index in range(settings['max_rounds'] + 1):
            if tool_context:
                try: self.tools.check(tool_context)
                except ValueError as error: raise ModelError(str(error)) from error
            current_schemas = schemas if round_index < settings['max_rounds'] else []
            if call_ids and not current_schemas and not final_instruction_added:
                request_messages.append({'role': 'system', 'content':
                    '工具阶段已经结束。不要再调用工具，也不要输出工具名、调用参数或执行计划。'
                    '根据已经返回的工具结果，按本轮运行协议要求的JSON结构给出最终决定。'
                    '只能输出一个完整JSON对象，不加Markdown代码围栏或对象外文字；未成功的操作不能声称成功。'})
                final_instruction_added = True
            choice = await self._completion(request_messages, generation or {}, current_schemas)
            message = choice['message']
            calls = message.get('tool_calls')
            if choice.get('finish_reason') == 'tool_calls' or calls:
                if not tool_context or not schemas:
                    raise ModelError('模型请求了未授权的工具，本次不执行')
                if round_index >= settings['max_rounds']:
                    raise ModelError('工具调用已达最大轮数，本次未生成最终回复')
                if not isinstance(calls, list) or not 1 <= len(calls) <= settings['max_calls']-len(call_ids):
                    raise ModelError('模型工具调用数量超过本轮上限')
                cleaned = []
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        raise ModelError('模型工具调用格式无效')
                    identifier, function = call.get('id'), call['function']
                    if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 128 or identifier in call_ids
                            or call.get('type') != 'function' or not isinstance(function.get('name'), str)
                            or len(function['name']) > 64 or not isinstance(function.get('arguments'), str)
                            or len(function['arguments']) > 16384):
                        raise ModelError('模型工具调用标识或参数格式无效')
                    call_ids.add(identifier)
                    cleaned.append({'id': identifier, 'type': 'function', 'function': {'name': function['name'], 'arguments': function['arguments']}})
                request_messages.append({'role': 'assistant', 'content': None, 'tool_calls': cleaned})
                for call in cleaned:
                    try: arguments = json.loads(call['function']['arguments'])
                    except (ValueError, RecursionError): arguments = {}
                    result = await self.tools.execute(call['function']['name'], arguments, tool_context)
                    text = json.dumps(result, ensure_ascii=False)
                    remaining = max(500, 32000-result_chars)
                    if len(text) > remaining:
                        text = json.dumps({'ok': result['ok'], 'tool': result['tool'], 'truncated': True, 'excerpt': text[:remaining-200]}, ensure_ascii=False)
                    result_chars += len(text)
                    request_messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': text})
                if round_index + 1 >= settings['max_rounds'] or len(call_ids) >= settings['max_calls'] or result_chars >= 32000:
                    schemas = []
                continue
            try:
                return json_object(message.get('content'))
            except (ValueError, TypeError, RecursionError):
                return await self._repair_json(request_messages, message, generation or {}, tool_context, len(call_ids))
        raise ModelError('工具处理已达本轮上限')

    async def _repair_json(self, messages: list[dict], invalid: dict, generation: dict,
                           context: ToolContext | None, tool_calls: int) -> dict[str, Any]:
        # Only reformat the final answer once. This path never re-enters the tool loop.
        content = invalid.get('content')
        self.store.audit('model_format_error', json.dumps({
            'purpose': self.usage_purpose.get(), 'tool_calls': tool_calls,
            'content_type': type(content).__name__, 'chars': len(content) if isinstance(content, str) else 0}))
        def check() -> None:
            if context:
                try:
                    self.tools.check(context)
                except ValueError as error:
                    raise ModelError(str(error)) from error
        check()
        request = copy.deepcopy(messages)
        if isinstance(content, str) and content and len(content) <= 32000:
            request.append({'role': 'assistant', 'content': content})
        request.append({'role': 'system', 'content':
            '上一条最终输出无法解析为JSON。只修正输出格式，不执行、重复或补做任何工具操作。'
            '保留本轮原本要求的字段与类型，根据已有结果返回一个完整JSON对象；'
            '不写代码围栏、解释或JSON对象之外的文字。只有工具回执成功的事情才能说已完成。'})
        choice = await self._completion(request, generation, [])
        check()
        message = choice.get('message', {})
        if choice.get('finish_reason') == 'tool_calls' or message.get('tool_calls'):
            raise ModelError('模型格式修正请求了额外工具，已阻止，本轮回复未生成')
        try:
            result = json_object(message.get('content'))
        except (ValueError, TypeError, RecursionError) as error:
            raise ModelError('DeepSeek 最终回复格式异常，修正后仍无法解析，本轮回复未生成') from error
        self.store.audit('model_format_recovered', '最终JSON格式已修正，未重复执行工具')
        return result

    async def decide(self, chat: str, proactive: bool = False, *, force: bool = False) -> dict[str, Any]:
        conv = self.config.conversation(chat)
        state = self.store.state(chat)
        from .roleplay import resolve
        history = self.store.history(chat, self.config.value['history_messages'])
        query = next((m['text'] for m in reversed(history) if m['role']=='user'),'')
        resolved = resolve(self.config.value, chat)
        character = resolved[0]['id']
        from .context import layers, bounded_history, memory_context
        policy = layers(resolved[0])
        _, history_dropped = bounded_history(history, policy)
        recalled = self.memory.retrieve(chat, query, character, max_chars=policy.get('memory_chars',max(500,resolved[2]['context_chars']//4)), personal=not proactive or chat.startswith('private:'))
        cfg = copy.deepcopy(self.config.value)
        cfg['_memory_options'] = self.memory.settings()
        compiled = compile_prompt(cfg, chat, history,
            memory_context(policy, state.get('manual_memory', ''), state.get('memory',''), recalled['text'], trimmed=bool(history_dropped)) if self.memory.settings()['enabled'] else state.get('memory',''), proactive=proactive)
        emotion_context = self.emotion.context(chat, proactive)
        if self.tools.life and self.config.value['life']['enabled']:
            compiled['messages'].append({'role':'system','content':self.tools.life.prompt(chat)})
        if emotion_context['enabled']:
            compiled['messages'].append({'role': 'system', 'content': self.emotion.prompt(emotion_context)})
        compiled['fallback_memory'] = state.get('memory', '')
        result = await self.generate(compiled, conv, tool_context=self.tools.context(chat, proactive))
        if not next(block["enabled"] for block in compiled["blocks"] if block["id"] == "memory"):
            # Excluding memory from this prompt must not erase durable stored memory.
            result["memory"] = state.get("memory", "")
        result["_emotion_context"] = emotion_context
        self.memory.consider(chat)
        return result

    async def generate(self, compiled: dict[str, Any], conv: dict[str, Any], *, tool_context: ToolContext | None = None, purpose: str = "chat") -> dict[str, Any]:
        result = await self.json("", {}, messages=compiled["messages"], generation=compiled["generation"], tool_context=tool_context, purpose=purpose)
        if type(result.get("reply")) is not bool:
            raise ModelError("模型没有返回有效的回复决策")
        # Some otherwise valid JSON replies omit optional bookkeeping fields. No new
        # summary means preserve the old one; never clear durable memory or retry a
        # paid call merely to obtain these fields. Keep malformed values rejected.
        if result.get("memory") is None:
            result["memory"] = compiled.get('fallback_memory', '')
        if not isinstance(result.get("memory"), str) or len(result["memory"]) > 2000:
            raise ModelError("模型记忆格式无效")
        minutes = result.get("next_minutes")
        if minutes is None:
            minutes = conv["max_minutes"]
        if type(minutes) is not int:
            raise ModelError("模型没有返回有效的下次检查时间")
        result["next_minutes"] = max(conv["min_minutes"], min(conv["max_minutes"], minutes))
        parts = result.get("messages", [result["text"]] if isinstance(result.get("text"), str) else None)
        if not isinstance(parts, list) or len(parts) > 5 or any(not isinstance(part, str) for part in parts):
            raise ModelError("模型分条回复格式无效")
        remaining = compiled["generation"]["max_reply_chars"]
        messages = []
        for part in parts:
            part = part.strip()[:remaining]
            if part and (not tool_context or part not in tool_context.sent_texts):
                messages.append(part)
                remaining -= len(part)
        result["messages"] = messages if result["reply"] else []
        raw_delays = result.get('reply_delays_seconds')
        valid_delays = isinstance(raw_delays,list) and len(raw_delays)==len(parts) and all(
            type(n) in (int,float) and 0 <= n <= 3600 for n in raw_delays)
        timing = conv.get('reply_timing',True)
        delays = []
        for index,part in enumerate(parts):
            if not part.strip():
                continue
            low = 2 if not delays else 1
            high = conv.get('max_reply_delay',45) if not delays else min(12,conv.get('max_reply_delay',45))
            seconds = raw_delays[index] if valid_delays else (6 if not delays else 2)
            delays.append(max(low,min(high,seconds)) if timing else (0 if not delays else .8))
            if len(delays)==len(result['messages']):break
        result['reply_delays_seconds'] = delays if result['reply'] else []
        result['reply_timing_source'] = ('model' if valid_delays else 'fallback') if timing else 'disabled'
        result["text"] = "\n".join(result["messages"])
        result["reply"] = result["reply"] and bool(messages)
        return result

    async def administration(self, text: str, *, usage_chat: str = "") -> dict[str, Any]:
        cfg = self.config.public()
        cfg.pop("tools", None)
        cfg.pop("roleplay", None)  # Keep character libraries out of command-classification requests.
        for conv in cfg['conversations'].values():
            conv.pop('tools_enabled', None)
            conv.pop('tool_allowlist', None)
        pack = self.config.value["roleplay"]
        cfg["roleplay_choices"] = {collection: [{"id": item["id"], "name": item["name"]} for item in pack[collection]]
                                   for collection in ("characters", "personas", "presets")}
        cfg.pop("admin_qq", None)
        cfg.pop("bot_qq", None)
        return await self.json(
            "你是管理账号私聊文字指令的解析器，只输出 JSON。"
            "输出 {\"mode\":\"chat|clarify|update\",\"patch\":{},\"explanation\":\"...\"}。"
            "日常聊天、提问和问候返回 mode=chat、patch={}、explanation=空字符串。"
            "只在消息明确要求修改设置时返回 mode=update 并填 patch。"
            "不得猜测群号、QQ号、时段或间隔；管理指令有歧义时返回 mode=clarify、patch 为空，"
            "explanation 直接向管理员询问缺少的参数，不写第三人称分析或分类理由。"
            "patch 仅可包含 paused:boolean,persona:string,history_messages:4..100,"
            "daily_model_limit:1..10000,conversations:object。"
            "也允许 roleplay_selection:{active_character:id,active_persona:id,active_preset:id}，"
            "只在明确要求切换全局默认角色、人设或预设时使用，从 roleplay_choices 中按名称找到已有 id，不得编造 id。"
            "conversations 的键只能是 group:实际群号 或 private:实际QQ号，值是部分会话配置。"
            "会话字段有 name,allowed,proactive,min_minutes,max_minutes,start,end,daily_limit,persona，reply_timing:boolean控制模型回复节奏，max_reply_delay:2..120是首条目标等待秒数上限；"
            "也可设置 character_id,user_persona_id,preset_id，值来自现有角色库 id，空字符串表示跟随默认；"
            "时段为24小时HH:MM，范围相同代表全天，跨午夜允许。"
            "proactive=true 必须同时 allowed=true。取消授权用 allowed=false。"
            "不能修改管理员、密钥、模型名称、接入设置、路径；不能删除记忆或文件；"
            "不能执行程序、转发消息或访问其他会话历史。"
            "不要将普通问候解释为修改配置。只返回拟实施的修改，不声称已经保存。",
            {"request": text[:4000], "current": cfg,
             "new_conversation_defaults": {k: v for k, v in conversation_defaults().items() if k not in {"tools_enabled", "tool_allowlist"}}}, purpose="admin", usage_chat=usage_chat)
