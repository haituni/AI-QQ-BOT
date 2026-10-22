"""Loopback-only control plane with secret bootstrap and same-origin session auth."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import base64
import binascii
import json
import os
import secrets
import signal
import time
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .ai import DeepSeek, ModelError, day_now
from .bridge import Bridge, BridgeError
from .config import CHAT_KEY, ROOT, Config
from .engine import Engine
from .store import Store
from . import roleplay
from .operations import Operations
from .toolkit import ToolService
from .napcat import NapCatLogin


def create_app(root: Path = ROOT, port: int = 8765, *, offline: bool = False) -> web.Application:
    config = Config(root)
    store = Store(root)
    sessions: dict[str, float] = {}
    login_attempts: list[float] = []
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @web.middleware
    async def security(request: web.Request, handler):
        request_id = secrets.token_hex(6)
        try:
            if request.host not in allowed_hosts:
                raise web.HTTPForbidden(text="Invalid host")
            origin = request.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
                raise web.HTTPForbidden(text="Invalid origin")
            if request.path.startswith("/api/") and request.path != "/api/login":
                token = request.cookies.get("qqbot_session", "")
                if sessions.get(token, 0) < time.time():
                    raise web.HTTPUnauthorized(text="请先登录管理界面")
                if request.method != "GET" and request.headers.get("X-Bot-Request") != "1":
                    raise web.HTTPForbidden(text="Missing request protection")
            if request.method == 'POST' and request.path.startswith('/api/'):
                try:
                    body = await request.json()
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError('请求不是有效的 JSON 对象')
                if not isinstance(body, dict):
                    raise ValueError('请求内容必须为 JSON 对象')
            # Recheck after reading the body: maintenance may have begun while
            # a slow client was still uploading its request.
            if (request.method != 'GET' and request.path not in {'/api/login', '/api/logout'}
                    and (store.maintenance or (app.get('operations') and app['operations'].data_busy))):
                raise web.HTTPServiceUnavailable(text='数据维护正在进行，请稍后重试；如恢复中断，请重启服务')
            response = await handler(request)
        except web.HTTPException as error:
            response = web.json_response({'error': error.text, 'request_id': request_id}, status=error.status)
        except (ValueError, ModelError, BridgeError) as error:
            response = web.json_response({"error": str(error), 'request_id': request_id}, status=400)
        except Exception as error:
            with contextlib.suppress(Exception):
                store.failure('server_error', error, identifier=request_id)
            response = web.json_response({'error': '服务暂时无法完成操作，请重试或查看运行记录', 'request_id': request_id}, status=500)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", 'X-Request-ID': request_id,
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    app = web.Application(middlewares=[security], client_max_size=16 * 1024 * 1024)
    app["config"], app["store"] = config, store
    app["shutdown_event"] = asyncio.Event()

    async def login(request: web.Request) -> web.Response:
        now = time.time()
        login_attempts[:] = [item for item in login_attempts if now - item < 60]
        if len(login_attempts) >= 10:
            raise web.HTTPTooManyRequests(text="请一分钟后重试")
        body = await request.json()
        token = body.get("token", "") if isinstance(body, dict) else ""
        if not isinstance(token, str) or not secrets.compare_digest(token.encode(), config.control_token.encode()):
            login_attempts.append(now)
            raise web.HTTPUnauthorized(text="管理密钥不正确")
        for expired in [key for key, expiry in sessions.items() if expiry < now]:
            sessions.pop(expired, None)
        if len(sessions) >= 20:
            sessions.pop(next(iter(sessions)))
        session = secrets.token_urlsafe(40)
        sessions[session] = now + 12 * 3600
        response = web.json_response({"ok": True})
        response.set_cookie("qqbot_session", session, httponly=True, samesite="Strict", path="/")
        return response

    async def status(request: web.Request) -> web.Response:
        bridge = app["bridge"]
        states = {key: store.state(key) for key in config.value["conversations"]}
        for key,state in states.items():
            state["reply_at"] = app["engine"].pending_replies.get(key)
            state.pop("memory", None)
            state.pop("manual_memory", None)
        public = config.public()
        if request.query.get('light') == '1':
            public.pop('roleplay', None)
        return web.json_response({"config": public, "states": states,
            "bridge": {"connected": bridge.connected, "online": bridge.online,
                       "self_id": bridge.self_id, "error": bridge.error},
            "today_calls": store.used(day_now()), "audit": store.audits(),
            "offline": offline, "operations": app["operations"].status(bridge, app["ai"], app["engine"])})

    async def save(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict) or body.get("revision") != config.value["revision"]:
            return web.json_response({"error": "设置已被其他操作修改，请刷新后重试"}, status=409)
        previous = copy.deepcopy(config.value)
        result = config.update(body.get("patch"))
        app["engine"].on_config_change(previous)
        if any(previous[key] != config.value[key] for key in ('api_key', 'model')):
            app['ai'].failures, app['ai'].retry_at = 0, 0
            app['ai'].last_error, app['ai'].last_success = '', 0
        store.audit("ui_change", ", ".join(body["patch"].keys()))
        return web.json_response(result)

    async def history(request: web.Request) -> web.Response:
        chat = request.query.get("chat", "")
        if not isinstance(chat, str) or not CHAT_KEY.fullmatch(chat):
            raise ValueError("会话编号无效")
        if request.query.get('page') == '1':
            try:
                before = int(request.query.get('before', '0'))
            except ValueError:
                raise ValueError('分页编号无效')
            query = request.query.get('q', '')
            if before < 0 or len(query) > 200:
                raise ValueError('搜索或分页参数超出限制')
            return web.json_response({**store.message_page(chat, before, query), 'memory': store.state(chat)['memory']})
        return web.json_response({"messages": store.history(chat, 100), "memory": store.state(chat)["memory"]})

    async def clear(request: web.Request) -> web.Response:
        body = await request.json()
        chat = body.get("chat", "")
        if not isinstance(chat, str) or not CHAT_KEY.fullmatch(chat):
            raise ValueError("会话编号无效")
        store.clear(chat)
        store.audit("memory_cleared", chat)
        return web.json_response({"ok": True})

    async def contacts(request: web.Request) -> web.Response:
        # The administrator explicitly asks for roster metadata; never fetch message history.
        bridge = app["bridge"]
        groups = await bridge.call("get_group_list")
        friends = await bridge.call("get_friend_list")
        return web.json_response({"groups": [{"id": str(g["group_id"]), "name": str(g.get("group_name", ""))} for g in groups or []],
                                  "friends": [{"id": str(g["user_id"]), "name": str(g.get("remark") or g.get("nickname", ""))} for g in friends or []]})

    async def test_model(request: web.Request) -> web.Response:
        result = await app["ai"].json("只输出 JSON 对象 {\"ok\":true}。", {"test": True})
        if result.get("ok") is not True:
            raise ModelError("模型连接成功，但结构化响应检查未通过")
        store.audit("model_test", "DeepSeek 连接与 JSON 响应检查通过")
        return web.json_response({"ok": True})

    async def bridge_access(request: web.Request) -> web.Response:
        # Legacy callers receive only the local login surface, never a WebUI token.
        return web.json_response({'url': '/', 'inline_login': True})

    async def qq_login(request: web.Request) -> web.Response:
        body = await request.json()
        action, totp = body.get('action', 'poll'), body.get('totp', '')
        if not isinstance(action, str) or not isinstance(totp, str) or (totp and (not totp.isdigit() or len(totp) != 6)):
            raise ValueError('登录请求格式无效')
        result = await app['qq_login'].state(action, totp)
        if result.get('state') == 'online':
            bridge = app['bridge']
            ready = False
            try:
                ready = await asyncio.wait_for(bridge.refresh_status(), 5)
            except (BridgeError, TimeoutError):
                pass
            if not ready:
                result = {'state':'connecting', 'message':'QQ 账号已登录，正在连接消息通道，连接完成后即可聊天。'}
        return web.json_response(result)

    def prompt_request(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(body, dict): raise ValueError("试聊请求格式无效")
        cfg = copy.deepcopy(config.value)
        if "draft" in body:
            cfg["roleplay"] = roleplay.validate(copy.deepcopy(body["draft"]))
        chat = body.get("chat", "")
        if not isinstance(chat, str) or (chat and chat not in cfg["conversations"]):
            raise ValueError("请选择已配置的会话，或使用独立试聊")
        mode = body.get("mode", "sandbox")
        if mode not in {"sandbox", "live"}: raise ValueError("未知预览模式")
        memory = ""
        if mode == "live":
            if not chat: raise ValueError("查看真实上下文前请先选择会话")
            history = store.history(chat, cfg["history_messages"])
            memory = store.state(chat)["memory"]
        else:
            history = body.get("history", [])
            if not isinstance(history, list) or len(history) > 100:
                raise ValueError("试聊历史最多 100 条")
            for item in history:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                    raise ValueError("试聊只接受 user / assistant 消息")
                if not isinstance(item.get("content"), str) or len(item["content"]) > 8000:
                    raise ValueError("每条试聊消息不能超过 8000 字")
            if body.get("use_memory") is True:
                if not chat: raise ValueError("使用记忆前请先选择会话")
                memory = store.state(chat)["memory"]
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > 8000: raise ValueError("试聊输入最多 8000 字")
        if text.strip(): history = [*history, {"role": "user", "content": text.strip()}]
        if not history: history = [{"role": "user", "content": "你好。"}]
        selection = body.get("selection", {})
        if not isinstance(selection, dict) or set(selection) - {"character_id", "user_persona_id", "preset_id"}:
            raise ValueError("试聊角色选择无效")
        if not all(isinstance(v, str) for v in selection.values()): raise ValueError("试聊角色标识无效")
        proactive = body.get("proactive", False)
        if type(proactive) is not bool: raise ValueError("主动检查开关格式无效")
        cfg['_memory_options'] = app['ai'].memory.settings()
        if chat and (mode == 'live' or body.get('use_memory') is True):
            query = next((str(m.get('text',m.get('content',''))) for m in reversed(history) if m['role']=='user'),'')
            from .context import layers, bounded_history, memory_context
            card = roleplay.resolve(cfg,chat,selection)[0]
            policy = layers(card)
            _, dropped = bounded_history(history,policy)
            current = store.state(chat)
            recalled = app['ai'].memory.retrieve(chat,query,card['id'],max_chars=policy.get('memory_chars'),personal=not proactive or chat.startswith('private:'))['text']
            memory = memory_context(policy,current.get('manual_memory',''),current.get('memory',''),recalled,trimmed=bool(dropped)) if cfg['_memory_options']['enabled'] else current.get('memory','')
        compiled = roleplay.compile_prompt(cfg, chat, history, memory, proactive=proactive, selection=selection)
        emotion = app['ai'].emotion
        context = emotion.context(chat, proactive) if mode == 'live' else emotion.sandbox_context(body.get('emotion_state'))
        if context['enabled'] and emotion.enabled():
            compiled['messages'].append({'role': 'system', 'content': emotion.prompt(context)})
            compiled['characters'] = sum(len(m.get('content') or '') for m in compiled['messages'])
            compiled['emotion_state'] = context['state'] if mode == 'sandbox' else None
        return compiled, config.conversation(chat)

    async def long_memory(request: web.Request) -> web.Response:
        chat=request.query.get('chat','')
        if chat and chat not in config.value['conversations']:raise ValueError('请选择已配置的会话')
        query=request.query.get('query','')
        if len(query)>2000:raise ValueError('检索文本过长')
        data = app['ai'].memory.inspect(chat,query)
        data['emotion'] = app['ai'].emotion.inspect(chat)
        data['emotion']['reply_at'] = app['engine'].pending_replies.get(chat)
        data['emotion']['next_at'] = store.state(chat).get('next_at', 0)
        return web.json_response(data)

    async def memory_action(request: web.Request) -> web.Response:
        body=await request.json();action=body.get('action');memory=app['ai'].memory
        if action=='settings':
            if body.get('previous') is not None and body['previous']!=memory.settings():raise ValueError('记忆设置已变化，请重新加载后保存')
            return web.json_response(memory.configure(body.get('patch')))
        chat=body.get('chat','')
        if chat not in config.value['conversations']:raise ValueError('请选择已配置的会话')
        if action in {'emotion-enabled', 'emotion-reset'}:
            enabled = body.get('enabled') if action == 'emotion-enabled' else None
            if action == 'emotion-enabled' and type(enabled) is not bool:
                raise ValueError('情绪开关必须是布尔值')
            app['ai'].emotion.control(chat, enabled)
            return web.json_response({'ok': True})
        if action=='consolidate':
            if memory.tasks.get(chat) and not memory.tasks[chat].done():raise ValueError('正在整理记忆，请等待完成')
            memory.tasks[chat]=asyncio.create_task(memory.consolidate(chat,manual=True))
            return web.json_response(await memory.tasks[chat])
        if action=='forget':
            identifier=body.get('id')
            if type(identifier)is not int:raise ValueError('记忆编号无效')
            # Tombstone prevents the same evidence from being silently re-extracted.
            store.db.execute("UPDATE memory_notes SET status='forgotten' WHERE id=? AND chat=?",(identifier,chat))
            store.db.commit();return web.json_response({'ok':True})
        if action=='reset-adaptation':
            current=store.state(chat);current['generation']+=1;store.save_state(chat,current)
            memory.reset_adaptation(chat)
            return web.json_response({'ok':True})
        raise ValueError('未知的记忆操作')

    async def token_usage(request: web.Request) -> web.Response:
        from .usage import report
        try:days=int(request.query.get('days','7'))
        except ValueError:raise ValueError('用量日期无效')
        chat=request.query.get('chat')
        if chat not in (None, '') and not CHAT_KEY.fullmatch(chat):raise ValueError('会话编号无效')
        data=report(store.db,days,chat)
        for row in data['conversations']:
            row['name']=config.value['conversations'].get(row['chat'],{}).get('name') or row['chat'] or '历史未归属 / 系统调用'
        return web.json_response(data)

    async def preview_prompt(request: web.Request) -> web.Response:
        compiled, _ = prompt_request(await request.json())
        return web.json_response(compiled)

    async def studio_test(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict): raise ValueError("试聊请求应为对象")
        if body.get("mode", "sandbox") != "sandbox":
            raise ValueError("试聊只允许独立沙盒模式，不会写入真实聊天")
        compiled, conv = prompt_request(body)
        result = await app["ai"].generate(compiled, conv, purpose="sandbox")
        previous = compiled.get('emotion_state')
        if previous and app['ai'].emotion.valid(result.get('emotion')):
            updated = app['ai'].emotion.advance(previous, result['emotion'])
            result['emotion_state'] = {k: updated[k] for k in ('pleasure', 'energy', 'tension', 'warmth', 'flutter', 'mood', 'labels')}
        store.audit("studio_test", "本地角色试聊；未发送 QQ，未写入会话记忆")
        return web.json_response({"result": result, "prompt": compiled})

    async def studio_import(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict): raise ValueError("导入请求应为对象")
        kind = body.get("kind", "card")
        maximum = roleplay.FILE_LIMIT if kind == "card" else roleplay.LIBRARY_FILE_LIMIT
        if not isinstance(body.get("content"), str) or len(body["content"]) > 4 * maximum // 3 + 8:
            raise ValueError("导入文件格式无效或超过大小限制")
        try:
            raw = base64.b64decode(body["content"], validate=True)
        except binascii.Error as error:
            raise ValueError("导入数据编码无效") from error
        if len(raw) > maximum: raise ValueError("导入文件超过大小限制")
        if kind == "card":
            payload, portrait = roleplay.extract_card(raw)
            result = roleplay.import_card(payload, portrait)
            candidate = copy.deepcopy(config.value["roleplay"])
            candidate["characters"].append(result["character"])
            candidate["worldbooks"] += result["worldbooks"]
            roleplay.validate(candidate)
            return web.json_response(result)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ValueError("不是有效的 JSON 文件") from error
        if not isinstance(payload, dict): raise ValueError("导入 JSON 根节点应为对象")
        candidate = copy.deepcopy(config.value["roleplay"])
        if kind == "book":
            book, warnings = roleplay.import_book(payload)
            candidate["worldbooks"].append(book)
            roleplay.validate(candidate)
            return web.json_response({"worldbook": book, "warnings": warnings})
        if kind == "preset":
            p, warnings = roleplay.import_preset(payload)
            candidate["presets"].append(p)
            roleplay.validate(candidate)
            return web.json_response({"preset": p, "warnings": warnings})
        if kind == "library" and payload.get("format") == "qq-companion-studio":
            return web.json_response({"library": roleplay.validate(payload.get("data")), "warnings": ["将载入整个角色库草稿；保存后才会替换现有角色库。账号和真实会话记忆不在角色库文件中。"]})
        raise ValueError("未知导入类型")

    async def studio_export(request: web.Request) -> web.Response:
        pack = config.value["roleplay"]
        identifier, kind = request.query.get("id", ""), request.query.get("kind", "card")
        suffix, content_type = "json", "application/json"
        if kind == "library":
            payload = {"format": "qq-companion-studio", "version": 1, "data": pack}
            identifier = "character-library"
        else:
            if kind not in {"card", "book", "preset"}: raise ValueError("未知导出类型")
            collection = {"card": "characters", "book": "worldbooks", "preset": "presets"}[kind]
            item = next((x for x in pack[collection] if x["id"] == identifier), None)
            if item is None: raise ValueError("导出项目不存在")
            if kind == "card" and request.query.get("format") == "png":
                raw = roleplay.export_png(item, pack)
                if len(raw) > roleplay.FILE_LIMIT: raise ValueError("PNG 总大小超过 3 MB，请导出 JSON")
                return web.Response(body=raw, content_type="image/png", headers={"Content-Disposition": f'attachment; filename="{identifier}.png"'})
            payload = roleplay.export_card(item, pack) if kind == "card" else roleplay.export_book(item) if kind == "book" else {"format": "qq-companion-preset", "data": item}
        return web.Response(body=json.dumps(payload, ensure_ascii=False, indent=2).encode(), content_type=content_type,
                            headers={"Content-Disposition": f'attachment; filename="{identifier}.{suffix}"'})

    async def life_menu(request: web.Request) -> web.Response:
        life = app['ai'].tools.life
        return web.json_response({'settings': config.value['life'], 'revision': config.value['revision'],
            'state': life.state(), 'stickers': life.stickers(''), 'offline': offline, 'retrieval': life.retrieval.status(),
            'actions': [dict(r) for r in store.db.execute('SELECT id,time,chat,name,status FROM life_actions ORDER BY id DESC LIMIT 40')]})

    async def life_image(request: web.Request) -> web.Response:
        row = store.db.execute('SELECT blob,mime FROM life_media WHERE id=? AND saved=1', (request.match_info['id'],)).fetchone()
        if not row or not row['blob']: raise web.HTTPNotFound()
        return web.Response(body=row['blob'], content_type=row['mime'])

    async def life_action(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get('revision') != config.value['revision']:
            return web.json_response({'error':'设置已变化，请重新载入后操作'},status=409)
        if body.get('action') == 'upload':
            encoded=body.get('base64'); description=body.get('description','')
            if not isinstance(encoded,str) or len(encoded)>9*1024*1024 or not isinstance(description,str) or not description.strip() or len(description)>400:
                raise ValueError('图片或描述格式无效，单图上限6 MiB，描述需填写1～400字')
            try: blob=base64.b64decode(encoded, validate=True)
            except (ValueError,binascii.Error): raise ValueError('图片编码无效') from None
            identifier=app['ai'].tools.life.register('upload','upload',blob=blob,description=description,saved=True)
            return web.json_response({'id':identifier,'saved':True})
        if body.get('action') == 'delete-sticker':
            identifier=body.get('id')
            if not isinstance(identifier,str): raise ValueError('表情ID无效')
            store.db.execute('DELETE FROM life_media WHERE id=? AND saved=1',(identifier,));store.db.commit()
            app['ai'].tools.life.retrieval.invalidate(identifier)
            return web.json_response({'ok':True})
        raise ValueError('未知的自主生活操作')

    async def tools_menu(request: web.Request) -> web.Response:
        return web.json_response({'settings': config.value['tools'], 'revision': config.value['revision'], **app['ai'].tools.public()})

    async def tools_test(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get('revision') != config.value['revision']:
            return web.json_response({'error': '设置已变化，请刷新工具菜单后再试'}, status=409)
        name, arguments = body.get('name'), body.get('arguments')
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError('请选择工具并填写正确参数')
        context = app['ai'].tools.context('tool-test', sandbox=True, text=arguments.get('url', '') if isinstance(arguments.get('url', ''), str) else '')
        result = await app['ai'].tools.execute(name, arguments, context)
        return web.json_response(result)

    async def activity(request: web.Request) -> web.Response:
        try:
            before = int(request.query.get('before', '0'))
        except ValueError:
            raise ValueError('分页编号无效')
        if before < 0 or len(request.query.get('kind', '')) > 50:
            raise ValueError('运行记录参数无效')
        return web.json_response(store.activity(request.query.get('kind', ''), before))

    async def runtime_job(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(app['operations'].begin(body.get('command')), status=202)

    async def shutdown(request: web.Request) -> web.Response:
        operations = app['operations']
        if operations.offline or root.resolve() != ROOT.resolve():
            raise ValueError('离线验证环境禁止关闭真实服务')
        job = operations.job or {}
        if (job.get('command') != 'stop' or job.get('status') != 'succeeded'
                or (operations.task and not operations.task.done())):
            raise ValueError('请先等待 QQ 环境确认停止，管理界面仍保持运行')
        asyncio.get_running_loop().call_later(0.5, app['shutdown_event'].set)
        return web.json_response({'ok': True})

    async def backup(request: web.Request) -> web.Response:
        return web.json_response(await app['operations'].snapshot_async())

    async def restore(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get('revision') != config.value['revision']:
            return web.json_response({'error': '设置已变化，请刷新后再恢复'}, status=409)
        if not config.value['paused']:
            raise ValueError('请先暂停机器人，再恢复备份')
        engine = app['engine']
        if (any(not task.done() for task in engine.workers.values()) or app['ai'].active
                or any(not t.done() for t in app['ai'].memory.tasks.values())
                or any(lock.locked() for lock in engine.locks.values()) or app['bridge'].pending or app['ai'].tools.active):
            raise ValueError('仍有请求正在结束，请稍后再恢复')
        if app['operations'].task and not app['operations'].task.done():
            raise ValueError('请等待 QQ 环境任务完成后再恢复')
        previous = copy.deepcopy(config.value)
        await app['operations'].restore_async(body.get('id'))
        engine.on_config_change(previous)
        return web.json_response({'ok': True})

    async def diagnostics(request: web.Request) -> web.Response:
        op = app['operations'].status(app['bridge'], app['ai'], app['engine'])
        # No account IDs, content, paths, keys, role definitions or subprocess output.
        payload = {'format': 'qq-companion-diagnostics', 'version': 1, 'time': time.time(),
                   'ready': op['ready'], 'uptime': op['uptime'], 'integrity': op['integrity'],
                   'free_bytes': op['free_bytes'], 'queue': op['queue'], 'workers': op['workers'],
                   'metrics_24h': op['metrics'], 'checks': [{'id': c['id'], 'ok': c['ok']} for c in op['checks']],
                   'delivery_counts': {r['status']: r['n'] for r in store.db.execute('SELECT status,count(*) n FROM deliveries GROUP BY status')}}
        return web.Response(body=json.dumps(payload, ensure_ascii=False, indent=2).encode(), content_type='application/json',
                            headers={'Content-Disposition': 'attachment; filename="diagnostics.json"'})

    async def memory_export(request: web.Request) -> web.Response:
        chat = request.query.get('chat', '')
        if not isinstance(chat, str) or not CHAT_KEY.fullmatch(chat):
            raise ValueError('会话编号无效')
        payload = {'format': 'qq-companion-memory', 'version': 1, 'chat': chat,
                   'memory': store.state(chat)['memory'], 'messages': store.history(chat, 2000),
                   'long_term': app['ai'].memory.inspect(chat),
                   'archive': [dict(r) for r in store.db.execute('SELECT * FROM archive WHERE chat=? ORDER BY id',(chat,))]}
        return web.Response(body=json.dumps(payload, ensure_ascii=False, indent=2).encode(), content_type='application/json',
                            headers={'Content-Disposition': 'attachment; filename="conversation-memory.json"'})

    async def memory_update(request: web.Request) -> web.Response:
        body = await request.json()
        chat, memory = body.get('chat', ''), body.get('memory')
        if not isinstance(chat, str) or chat not in config.value['conversations']:
            raise ValueError('请选择已配置的会话')
        if not isinstance(memory, str) or len(memory) > 2000:
            raise ValueError('长期记忆最多 2000 字')
        current = store.state(chat)
        if current['memory'] != body.get('previous'):
            return web.json_response({'error': '记忆已被更新，请重新加载后编辑'}, status=409)
        current.update(memory=memory, manual_memory=memory, generation=current['generation']+1)
        store.save_state(chat, current)
        store.audit('memory_edited', chat)
        return web.json_response({'ok': True})

    async def logout(request: web.Request) -> web.Response:
        sessions.pop(request.cookies.get("qqbot_session", ""), None)
        response = web.json_response({"ok": True})
        response.del_cookie("qqbot_session")
        return response

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=(ROOT / "static/index.html").read_text(), content_type="text/html")

    async def static(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in {"app.js", "style.css", "studio.js", "studio.css", "operations.js", "operations.css", "tools.js", "tools.css", "login.js", "login.css", "memory.js", "memory.css", "life.js"}:
            raise web.HTTPNotFound()
        return web.Response(text=(ROOT / "static" / name).read_text(),
                            content_type="text/javascript" if name.endswith("js") else "text/css")

    async def lifecycle(application):
        session = aiohttp.ClientSession(trust_env=False)
        bridge, ai = Bridge(config, session), DeepSeek(config, store, session)
        bridge.report_exception = lambda error: store.failure('bridge_error', error) if not store.maintenance else None
        ai.tools = ToolService(config, store, session, offline=offline)
        from .life import Life
        ai.tools.life = Life(config, store, ai, bridge, offline=offline)
        ai.memory.offline = offline
        engine = Engine(config, store, ai, bridge)
        operations = Operations(config, store, offline=offline)
        operations.services = (engine, ai, bridge)
        application['operations'] = operations
        application['qq_login'] = NapCatLogin(config, session, operations, offline=offline)
        application["bridge"], application["ai"], application["engine"] = bridge, ai, engine
        # One verified snapshot per day when starting the actual application.
        snapshots = operations.backups()
        if not offline and (not snapshots or time.time()-snapshots[0]['time'] > 86400):
            try:
                await operations.snapshot_async('daily_startup')
            except Exception as error:
                store.failure('backup_error', error)
        from .health import supervise
        loops = {'bridge': lambda: bridge.run(engine.receive), 'scheduler': engine.schedule,
                 'maintenance': operations.maintain, 'memory': ai.memory.maintain}
        tasks = [] if offline else [asyncio.create_task(supervise(name, factory, store, operations.tasks), name=name)
                                   for name, factory in loops.items()]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await operations.close()
        await ai.memory.close()
        await engine.close()
        await session.close()
        store.db.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/life", life_menu)
    app.router.add_get("/api/life/image/{id}", life_image)
    app.router.add_post("/api/life", life_action)
    app.router.add_get("/api/tools", tools_menu)
    app.router.add_post("/api/tools/test", tools_test)
    app.router.add_get('/api/activity', activity)
    app.router.add_get('/api/long-memory', long_memory)
    app.router.add_post('/api/long-memory', memory_action)
    app.router.add_get('/api/token-usage', token_usage)
    app.router.add_post('/api/runtime', runtime_job)
    app.router.add_post('/api/shutdown', shutdown)
    app.router.add_post('/api/backups', backup)
    app.router.add_post('/api/backups/restore', restore)
    app.router.add_get('/api/diagnostics', diagnostics)
    app.router.add_get('/api/memory/export', memory_export)
    app.router.add_post('/api/memory', memory_update)
    app.router.add_post("/api/settings", save)
    app.router.add_get("/api/history", history)
    app.router.add_post("/api/clear-memory", clear)
    app.router.add_get("/api/contacts", contacts)
    app.router.add_post("/api/test-model", test_model)
    app.router.add_get("/api/bridge-access", bridge_access)
    app.router.add_post("/api/qq-login", qq_login)
    app.router.add_post("/api/roleplay/preview", preview_prompt)
    app.router.add_post("/api/roleplay/test", studio_test)
    app.router.add_post("/api/roleplay/import", studio_import)
    app.router.add_get("/api/roleplay/export", studio_export)
    return app


async def main() -> None:
    import fcntl
    os.umask(0o077)
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    # Lock stays held for the lifetime of the service; no duplicate scheduler processes.
    lock = (runtime / "server.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("机器人服务已在运行")
    port = int(os.environ.get("QQBOT_PORT", "8765"))
    app = create_app(port=port, offline="--offline" in __import__("sys").argv)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise SystemExit(f"本机端口 {port} 已占用；未启动机器人")
    pid = runtime / "server.pid"
    pid.write_text(str(os.getpid()))
    stopped = app['shutdown_event']
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    print(f"管理界面：http://127.0.0.1:{port}/", flush=True)
    try:
        await stopped.wait()
    finally:
        await runner.cleanup()
        pid.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    asyncio.run(main())
