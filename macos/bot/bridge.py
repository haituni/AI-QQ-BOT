"""OneBot v11 forward WebSocket, reconnecting without automatic send retries."""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
import random
from typing import Any, Awaitable, Callable

import aiohttp

from .config import Config


class BridgeError(Exception):
    pass


class Bridge:
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self.config, self.session = config, session
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.connected = False
        self.self_id = ""
        self.online = False
        self.error = "等待 QQ 接入服务启动"
        self.endpoint = "ws://127.0.0.1:3001"
        self.last_event = 0.0
        self.connected_at = 0.0
        self.reconnects = 0
        self.status_lock = asyncio.Lock()
        self.status_failures = 0
        self.report_exception = None

    async def run(self, receive: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        while True:
            probe = None
            try:
                async with self.session.ws_connect(self.endpoint, headers={
                    "Authorization": f"Bearer {self.config.value['bridge_token']}"},
                    heartbeat=20, max_msg_size=2**20, timeout=aiohttp.ClientWSTimeout(ws_receive=75, ws_close=5)) as ws:
                    self.ws, self.connected, self.error = ws, True, ""
                    self.connected_at = time.time()
                    self.reconnects = 0
                    probe = asyncio.create_task(self.watch_status())
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            event = json.loads(message.data)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        self.last_event = time.time()
                        echo = event.get("echo")
                        if isinstance(echo, str) and echo in self.pending:
                            future = self.pending[echo]
                            if not future.done():
                                future.set_result(event)
                            continue
                        if event.get("self_id"):
                            self.self_id = str(event["self_id"])
                        if event.get("meta_event_type") == "heartbeat":
                            status = event.get("status")
                            self.online = isinstance(status, dict) and status.get("online") is True
                        if event.get("post_type") in ("message", "notice"):
                            self.online = True
                            await receive(event)
            except (aiohttp.ClientError, TimeoutError, OSError):
                self.error = "QQ 接入未连接；请启动接入并扫码登录"
            finally:
                if probe:
                    probe.cancel()
                    await asyncio.gather(probe, return_exceptions=True)
                self.connected, self.online, self.ws = False, False, None
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(BridgeError("QQ 接入连接中断"))
            self.reconnects += 1
            await asyncio.sleep(min(30, 2 ** min(self.reconnects, 5)) + random.uniform(0, 1))

    async def refresh_status(self) -> bool:
        async with self.status_lock:
            ws = self.ws
            if not self.connected or not ws or ws.closed:
                return False
            data = await self.call('get_status')
            if self.ws is not ws:
                return False
            self.online = isinstance(data, dict) and data.get('online') is True
            if self.online and not self.self_id:
                info = await self.call('get_login_info')
                if self.ws is ws and isinstance(info, dict) and info.get('user_id'):
                    self.self_id = str(info['user_id'])
            return self.online

    async def watch_status(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.refresh_status(), 5)
                self.status_failures = 0
                self.error = '' if self.online else 'QQ 账号离线，请检查扫码登录状态'
            except (BridgeError, TimeoutError) as error:
                self.status_failures += 1
                if self.status_failures == 3:
                    self.online = False
                    self.error = 'QQ 状态连续检查失败，请查看接入与账号状态'
                    if self.report_exception:
                        with contextlib.suppress(Exception):
                            self.report_exception(error)
            except Exception as error:
                self.online = False
                self.error = 'QQ 状态检查异常，将继续检查'
                if self.report_exception:
                    with contextlib.suppress(Exception):
                        self.report_exception(error)
            await asyncio.sleep(10)

    async def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        if not self.ws or self.ws.closed:
            raise BridgeError("QQ 接入未连接")
        echo = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send_json({"action": action, "params": params or {}, "echo": echo})
            response = await asyncio.wait_for(future, 20)
            if response.get("retcode") != 0 or response.get("status") == "failed":
                raise BridgeError("QQ 接口拒绝该操作；请检查登录和会话权限")
            return response.get("data")
        except (TimeoutError, aiohttp.ClientError, ConnectionError) as error:
            raise BridgeError("QQ 接口超时或断开；为避免重复发送，本次不自动重试") from error
        finally:
            self.pending.pop(echo, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                if future.done():
                    future.exception()

    async def send(self, key: str, text: str) -> Any:
        return await self.send_segments(key, [{"type": "text", "data": {"text": text}}])

    async def send_segments(self, key: str, segments: list[dict]) -> Any:
        kind, identifier = key.split(":", 1)
        data: dict[str, Any] = {"message": segments}
        data["group_id" if kind == "group" else "user_id"] = int(identifier)
        return await self.call("send_group_msg" if kind == "group" else "send_private_msg", data)
