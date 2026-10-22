"""Conversation authorization, serialized decisions and persistent proactive checks."""
from __future__ import annotations

import asyncio
import copy
import re
import time
from datetime import datetime
from typing import Any

from .ai import DeepSeek, ModelError, TZ, day_now
from .bridge import Bridge, BridgeError
from .config import CHAT_KEY, Config
from .store import Store


def in_window(conv: dict[str, Any], timestamp: float) -> bool:
    minute = datetime.fromtimestamp(timestamp, TZ).strftime("%H:%M")
    start, end = conv["start"], conv["end"]
    if start == end:
        return True
    return start <= minute < end if start < end else minute >= start or minute < end


def plain_text(event: dict[str, Any]) -> str:
    message = event.get("message")
    if isinstance(message, list):
        parts = []
        for segment in message:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            data = segment.get("data", {})
            if not isinstance(data, dict):
                continue
            if kind == "text":
                parts.append(str(data.get("text", "")))
            elif kind == "at":
                parts.append(f"[@{data.get('qq', '')}]")
            elif kind == "reply":
                parts.append(f"[引用消息ID:{data.get('id', '')}]" )
            elif kind == "face":
                parts.append(f"[QQ表情:{data.get('id', '')}]" )
            elif kind in {"image", "mface", "record", "video", "file", "reply", "face", "forward"}:
                parts.append(f"[未读取的{kind}消息]")
        return "".join(parts).strip()[:8000]
    # This project requests array messages; do not interpret CQ segments as tools.
    return str(message or "").strip()[:8000]


class Engine:
    def __init__(self, config: Config, store: Store, ai: DeepSeek, bridge: Bridge):
        self.config, self.store, self.ai, self.bridge = config, store, ai, bridge
        self.queues: dict[str, asyncio.Queue] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.semaphore = asyncio.Semaphore(3)
        self.incoming_versions: dict[str,int] = {}
        self.received_at: dict[str,float] = {}
        self.wake: dict[str,asyncio.Event] = {}
        self.pending_replies: dict[str,float] = {}
        if getattr(ai, "tools", None):
            ai.tools.version = lambda chat: self.incoming_versions.get(chat, 0)

    async def send(self, key: str, text: str, kind: str = "admin") -> None:
        identifier = self.store.delivery(key, kind)
        generation = self.store.state(key)["generation"]
        try:
            result = await self.bridge.send(key, text)
            life = getattr(self.ai.tools, "life", None) if getattr(self.ai, "tools", None) else None
            if life and self.store.state(key)["generation"] == generation and isinstance(result, dict) and result.get("message_id") is not None:
                life.remember_message(key, str(result["message_id"]), "bot", True, text)
        except BaseException:
            self.store.receipt(identifier, "unknown")
            raise
        else:
            self.store.receipt(identifier, "confirmed")

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def administrator(self, event: dict[str, Any]) -> bool:
        return event.get("message_type") == "private" and str(event.get("user_id", "")) in self.config.value["admin_qq"]

    async def receive(self, event: dict[str, Any]) -> None:
        if getattr(self.store, 'maintenance', False):
            return
        if event.get("post_type") == "notice" and event.get("sub_type") == "poke":
            if str(event.get("target_id")) != self.config.value['bot_qq']: return
            event = {**event, 'post_type': 'message', 'message_type': 'group' if event.get('group_id') else 'private',
                     'sub_type': 'friend', 'message_id': 'poke:'+str(event.get('time'))+':'+str(event.get('user_id')),
                     'message': [{'type':'text','data':{'text':'[对你拍了拍]'}}]}
        if event.get("post_type") != "message":
            return
        if str(event.get("user_id", "")) == str(event.get("self_id", "")):
            return
        kind = event.get("message_type")
        if kind not in {"group", "private"}:
            return
        if kind == "private" and event.get("sub_type") not in {"friend", None}:
            return  # temporary group DMs are not administrator control channels
        identifier = event.get("group_id" if kind == "group" else "user_id", "")
        key = f"{kind}:{identifier}"
        if not CHAT_KEY.fullmatch(key):
            return
        admin = self.administrator(event)
        if not admin and not self.config.allowed(key):
            return  # Do not persist content, deduplicate IDs or call the model.
        configured_bot = self.config.value["bot_qq"]
        if configured_bot and str(event.get("self_id", "")) != configured_bot:
            return
        text = plain_text(event)
        if not text:
            return
        queue = self.queues.setdefault(key, asyncio.Queue(maxsize=100))
        if queue.full():
            self.store.audit("queue_full", key)
            return
        identifier = event.get("message_id")
        if identifier is None or not self.store.once(f"{event.get('self_id')}:{key}:{identifier}"):
            return
        self.incoming_versions[key] = self.incoming_versions.get(key,0)+1
        self.received_at[key] = time.monotonic()
        self.wake.setdefault(key,asyncio.Event()).set()
        queue.put_nowait((copy.deepcopy(event), text, admin, self.store.state(key)["generation"]))
        if key not in self.workers or self.workers[key].done():
            self.workers[key] = asyncio.create_task(self.worker(key))

    async def worker(self, key: str) -> None:
        queue = self.queues[key]
        deferred = None
        while deferred is not None or not queue.empty():
            event, text, admin, generation = deferred if deferred is not None else await queue.get()
            deferred = None
            try:
                async with self.lock(key):
                    if generation != self.store.state(key)["generation"]:
                        continue
                    if admin and (self.is_management(text) or not self.config.allowed(key)):
                        await self.admin_command(key, event, text)
                    elif self.config.allowed(key):
                        life = getattr(self.ai.tools, "life", None) if getattr(self.ai, "tools", None) else None
                        if life: text += life.capture(key, event)
                        content = text[3:] if admin and text.startswith('聊天 ') else text
                        self.store.add(key, "user", str(event.get("user_id")), content)
                        if key.startswith("group:"):
                            await asyncio.sleep(2)
                        # Merge ordinary queued messages; preserve command ordering.
                        while not queue.empty():
                            extra, content, is_admin, gen = item = queue.get_nowait()
                            if is_admin and self.is_management(content):
                                deferred = item
                                break
                            try:
                                if gen == generation and self.config.allowed(key):
                                    if life: content += life.capture(key, extra)
                                    content = content[3:] if is_admin and content.startswith('聊天 ') else content
                                    self.store.add(key, "user", str(extra.get("user_id")), content)
                            finally:
                                queue.task_done()
                        if deferred is None:
                            await self.decide(key)
            except (ModelError, BridgeError, ValueError) as error:
                self.store.audit("error", f"{key}: {type(error).__name__}: {error}")
                if admin and not isinstance(error, BridgeError) and self.administrator(event):
                    try:
                        await self.send(key, f"本次未完成：{error}。可在本机管理界面查看或修改设置。")
                    except BridgeError:
                        pass
            except Exception as error:
                self.store.failure('error', error)
            finally:
                queue.task_done()

    def on_config_change(self, previous: dict[str, Any]) -> None:
        # Reset schedule when configuration changes. Revocation invalidates in-flight decisions.
        keys = set(previous["conversations"]) | set(self.config.value["conversations"])
        for key in keys:
            old = previous["conversations"].get(key)
            new = self.config.value["conversations"].get(key)
            state = self.store.state(key)
            if old != new or previous["paused"] != self.config.value["paused"]:
                state["generation"] += 1
                state["next_at"] = time.time() + self.config.conversation(key)["min_minutes"] * 60
                self.store.save_state(key, state)

    async def admin_chat(self, key: str, event: dict[str, Any], text: str) -> None:
        # Classifier explanations are internal; never use them as conversation replies.
        if not self.config.conversation(key)['allowed']:
            await self.send(key, '你的私聊还没有开启聊天授权。请在管理界面的「会话与主动聊天」中授权；管理指令仍然可用。')
            return
        if self.config.value['paused']:
            await self.send(key, '机器人目前已暂停聊天。发送「恢复机器人」，或在管理界面点击「恢复机器人」后，就可以正常聊了。')
            return
        life = getattr(self.ai.tools, "life", None) if getattr(self.ai, "tools", None) else None
        if life: text += life.capture(key, event)
        self.store.add(key, 'user', str(event['user_id']), text)
        await self.decide(key)

    @staticmethod
    def is_management(text: str) -> bool:
        if text.strip() in {'帮助','/help','管理帮助','状态','/status'}:
            return True
        return bool(re.search(
            r'(授权|取消.*授权|撤销.*授权|暂停机器人|恢复机器人|管理指令|管理设置)'
            r'|(开启|关闭|设置|配置|修改|调整|切换|更换|改成|改为|换成|使用).{0,24}'
            r'(群|私聊|会话|主动聊天|间隔|时段|每日上限|性格|人设|角色|预设|机器人)'
            r'|(群|私聊|会话|主动聊天|间隔|时段|每日上限|性格|人设|角色|预设|回复节奏|回复等待|延迟回复).{0,24}'
            r'(开启|关闭|设置|配置|修改|调整|切换|改成|改为|换成)', text))

    async def admin_command(self, key: str, event: dict[str, Any], text: str) -> None:
        # Checked again after the queue wait: removing an admin revokes queued commands.
        if not self.administrator(event):
            return
        if text.strip() in {"帮助", "/help", "管理帮助"}:
            await self.send(key, "管理指令示例：\n授权群 123456789 回复消息\n给私聊 123456789 开启主动聊天，间隔 30～120 分钟，09:00～23:00，每天最多 5 条\n暂停机器人 / 恢复机器人\n状态\n普通消息直接聊天；修改设置请明确说明管理操作\n管理员和 API 密钥只能在本机管理界面修改。")
            return
        if text.strip() in {"状态", "/status"}:
            enabled = sum(c["allowed"] for c in self.config.value["conversations"].values())
            await self.send(key, f"{'已暂停' if self.config.value['paused'] else '运行中'}；授权会话 {enabled} 个；今日模型调用 {self.store.used(day_now())} 次。")
            return
        if text.startswith("聊天 "):
            await self.admin_chat(key, event, text[3:])
            return
        # Ordinary dialogue must not enter the settings parser. Follow-ups only
        # become commands when they state an actual configuration operation.
        management = self.is_management(text)
        if not management:
            await self.admin_chat(key, event, text)
            return
        revision = self.config.value["revision"]
        async with self.semaphore:
            result = await self.ai.administration(text, usage_chat=key)
        if not self.administrator(event):
            return
        if revision != self.config.value["revision"]:
            await self.send(key, "解析期间设置发生了变化，本次未修改。请重新发送管理指令。")
            return
        patch = result.get("patch")
        if not isinstance(patch, dict):
            raise ValueError("管理指令格式无效，配置未修改")
        mode = result.get('mode')
        if mode == 'chat':
            # Even a malformed chat classification cannot carry a settings mutation.
            await self.admin_chat(key, event, text)
            return
        if mode == 'clarify':
            if patch:
                raise ValueError('管理指令仍需补充信息，配置未修改')
            question = result.get('explanation')
            await self.send(key, question[:1500] if isinstance(question, str) and question.strip()
                            else '你想修改哪个会话的哪项设置？请补充具体信息。')
            return
        if not patch:
            await self.send(key, '没有识别到可执行的设置修改。发送「帮助」可查看管理指令；普通聊天请在授权并恢复运行后使用。')
            return
        if mode not in {'update', None}:
            raise ValueError('管理指令类型无效，配置未修改')
        style_fields={'persona','roleplay_selection'}
        nested=patch.get('conversations',{})
        changes_style=bool(set(patch)&style_fields) or (isinstance(nested,dict) and any(
            isinstance(value,dict) and bool(set(value)&{'persona','character_id','user_persona_id','preset_id'}) for value in nested.values()))
        explicit_style=bool(re.search(r'(设置|配置|切换|更换|修改|改成|改为|换成|使用).*(性格|人设|角色|预设)|(性格|人设|角色|预设).*(设置|配置|切换|修改|改成|改为|换成)|(群|私聊).*(使用|换成|切换)',text))
        if changes_style and not explicit_style:
            await self.admin_chat(key,event,text)
            return
        previous = copy.deepcopy(self.config.value)
        self.config.update(patch, model=True)
        self.on_config_change(previous)
        fields = ", ".join(patch.keys())
        self.store.audit("admin_change", f"{event['user_id']}: {fields}")
        # Report exact validated change, not a model's claim of success.
        import json
        await self.send(key, "已保存以下配置：\n" + json.dumps(patch, ensure_ascii=False, indent=2)[:3000])

    async def decide(self, key: str, proactive: bool = False) -> None:
        if (getattr(self.store, 'maintenance', False) or not self.config.allowed(key) or not self.bridge.connected
                or not getattr(self.bridge, "online", True)
                or (self.config.value["bot_qq"] and getattr(self.bridge, "self_id", self.config.value["bot_qq"]) != self.config.value["bot_qq"])):
            return
        snapshot = copy.deepcopy(self.config.value)
        state = self.store.state(key)
        generation = state["generation"]
        version = self.incoming_versions.get(key,0)
        started = time.monotonic()
        self.wake.setdefault(key,asyncio.Event()).clear()
        async with self.semaphore:
            if (self.incoming_versions.get(key,0) != version or not self.config.allowed(key)
                    or snapshot != self.config.value or self.store.state(key)['generation'] != generation):
                return
            result = await self.ai.decide(key, proactive)
        if self.incoming_versions.get(key,0) != version:
            self.store.audit('reply_superseded',key+' 新消息到达，取消旧决策')
            return
        # Admin edits, deletion and memory resets take effect before any outgoing send.
        if getattr(self.store, 'maintenance', False) or not self.config.allowed(key) or snapshot != self.config.value:
            return
        current = self.store.state(key)
        if current["generation"] != generation:
            return
        emotion = getattr(self.ai, "emotion", None)
        if emotion and result.get("_emotion_context"):
            emotion.commit(result["_emotion_context"], result)
        life = getattr(self.ai.tools, "life", None) if getattr(self.ai, "tools", None) else None
        if life: life.reflect(result.get("self_state"), key)
        current["next_at"] = time.time() + result["next_minutes"] * 60
        self.store.save_state(key, current)
        if not result["reply"]:
            current["memory"] = result["memory"]
            self.store.save_state(key,current)
            self.store.audit("skip", key + (" 主动检查：模型选择保持安静" if proactive else " 模型选择不回复"))
            return
        if proactive and not self.proactive_ready(key):
            # next_at was advanced above; eligibility here deliberately excludes due-time.
            return
        parts = result.get("messages", [result.get("text", "")])
        counted = False
        for index, part in enumerate(parts[:5]):
            delays = result.get('reply_delays_seconds',[])
            delay = delays[index] if index < len(delays) else (.8 if index else 0)
            # First delay is a target since receipt; model/tool latency counts.
            origin = started if proactive else self.received_at.get(key,started)
            if not index:
                delay = max(0,origin+delay-time.monotonic())
            if not await self.wait_to_reply(key,delay,snapshot,generation,version):
                self.store.audit('reply_superseded',key+' 待发回复因新消息或状态变化取消')
                break
            if proactive and ((not index and not self.proactive_ready(key))
                    or not in_window(self.config.conversation(key),time.time())):
                break
            current = self.store.state(key)
            if (not part or not self.config.allowed(key) or snapshot != self.config.value
                    or current["generation"] != generation or not self.bridge.connected
                    or not getattr(self.bridge, "online", True)):
                break
            await self.send(key, part, "proactive" if proactive else "reply")
            current = self.store.state(key)
            if current["generation"] != generation:
                break
            # Persist each confirmed bubble; later interruption must not erase a sent message.
            self.store.add(key, "assistant", "bot", part)
            now = time.time()
            current["memory"] = result["memory"]
            current["last_sent"] = now
            if proactive and not counted:
                today = day_now()
                current["count"] = current["count"] + 1 if current["day"] == today else 1
                current["day"] = today
            counted = True
            current["next_at"] = max(current.get("next_at", 0), now + result["next_minutes"] * 60)
            self.store.save_state(key, current)
            self.store.audit("proactive_sent" if proactive else "reply_sent", key)

    async def wait_to_reply(self, key: str, delay: float, snapshot: dict,
                            generation: int, version: int) -> bool:
        deadline = time.monotonic()+delay
        event = self.wake.setdefault(key,asyncio.Event())
        if delay > 0:
            self.pending_replies[key] = time.time()+delay
        try:
            while True:
                if (getattr(self.store, 'maintenance', False) or self.incoming_versions.get(key,0)!=version or snapshot!=self.config.value
                        or not self.config.allowed(key) or self.store.state(key)['generation']!=generation
                        or not self.bridge.connected or not getattr(self.bridge,'online',True)):
                    return False
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    return True
                try:
                    await asyncio.wait_for(event.wait(),min(remaining,.25))
                except TimeoutError:
                    pass
        finally:
            self.pending_replies.pop(key,None)

    def proactive_ready(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        conv = self.config.conversation(key)
        state = self.store.state(key)
        count = state["count"] if state["day"] == datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d") else 0
        return (self.config.allowed(key) and conv["proactive"] and in_window(conv, now)
                and count < conv["daily_limit"]
                and now >= state["last_sent"] + conv["min_minutes"] * 60)

    async def tick(self, now: float | None = None) -> None:
        if getattr(self.store, 'maintenance', False):
            return
        now = time.time() if now is None else now
        if not self.bridge.connected or not self.config.value["api_key"]:
            return
        due = []
        for key, conv in list(self.config.value["conversations"].items()):
            if not conv["allowed"] or not conv["proactive"]:
                continue
            state = self.store.state(key)
            if not state["next_at"]:
                state["next_at"] = now + conv["min_minutes"] * 60
                self.store.save_state(key, state)
                continue
            if state["next_at"] <= now and self.proactive_ready(key, now) and not self.lock(key).locked():
                due.append(key)

        async def check(key: str) -> None:
            async with self.lock(key):
                # Recheck after waiting for capacity; another operation may revoke or reschedule.
                state = self.store.state(key)
                if state['next_at'] > now or not self.proactive_ready(key, now):
                    return
                state["next_at"] = time.time() + self.config.conversation(key)["min_minutes"] * 60
                self.store.save_state(key, state)
                try:
                    await self.decide(key, proactive=True)
                except (ModelError, BridgeError, ValueError) as error:
                    self.store.audit("error", f"{key}: {error}")
        # One slow conversation must not serialize all other proactive checks.
        await asyncio.gather(*(check(key) for key in due))
        life = getattr(self.ai.tools, "life", None) if getattr(self.ai, "tools", None) else None
        if life:
            async with self.semaphore:
                await life.tick(now)

    async def schedule(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.tick()
            except Exception as error:
                self.store.failure('error', error)

    async def close(self) -> None:
        for worker in self.workers.values():
            worker.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
