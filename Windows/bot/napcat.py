"""Authenticated NapCat login gateway; no WebUI secrets ever reach the browser."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import qrcode
from qrcode.image.svg import SvgPathFillImage

from .config import ROOT


class LoginCredentialExpired(ValueError):
    pass


class NeedsTwoFactor(ValueError):
    pass


class NapCatLogin:
    def __init__(self, config, session: aiohttp.ClientSession, operations, *, offline: bool):
        self.config, self.session, self.operations, self.offline = config, session, operations, offline
        self.endpoint = 'http://127.0.0.1:6099'
        self.credential = ''
        self.expires_at = 0.0
        self.effective_token = ''
        self.lock = asyncio.Lock()
        self.qr_value, self.qr_image = '', ''
        self.wait_until = 0.0

    async def read_token(self) -> str:
        # Invoke this project's Python and script by absolute path; no shell or PATH dependency.
        if self.offline or self.config.root.resolve() != ROOT.resolve():
            raise ValueError('离线环境不能读取真实 QQ 登录凭据')
        process = await asyncio.create_subprocess_exec(__import__('sys').executable, str(ROOT/'scripts/runtime.py'), 'web-token',
            cwd=ROOT, env=dict(os.environ, PYTHONPYCACHEPREFIX=str(ROOT/'runtime/cache/pycache')),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({'creationflags': __import__('subprocess').CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
            if process.returncode != 0 or len(stdout) > 8192:
                raise ValueError('QQ 环境尚未就绪，请稍后重试扫码')
            token = json.loads(stdout).get('token')
            if not isinstance(token, str) or not 1 <= len(token) <= 512:
                raise ValueError('QQ 接入凭据无法读取，请重新启动 QQ 接入')
            return token
        except (ValueError, TimeoutError) as error:
            if process.returncode is None:
                process.kill(); await process.wait()
            raise ValueError('QQ 接入凭据暂不可用，请稍后重试或重新启动 QQ 接入') from error
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill(); await process.wait()
            raise

    async def post(self, path: str, body: dict | None = None, *, credential: str = '') -> dict:
        headers = {'Authorization': 'Bearer '+credential} if credential else {}
        async with self.session.post(self.endpoint + path, json=body or {}, headers=headers,
                                     allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as response:
            if response.status in {401, 403}:
                raise LoginCredentialExpired('QQ 登录凭据已失效')
            if response.status != 200:
                raise ValueError('QQ 登录服务正在启动或暂时不可用')
            raw = await response.content.read(200000)
            data = json.loads(raw)
            if not isinstance(data, dict): raise ValueError('QQ 登录服务响应格式异常')
            return data

    async def authenticate(self, totp: str = '') -> None:
        if self.credential and self.expires_at > time.time() and not totp:
            return
        candidates = [self.effective_token or self.config.value['bridge_web_token']]
        for attempt in range(2):
            token = candidates[-1]
            body = {'hash': hashlib.sha256((token+'.napcat').encode()).hexdigest()}
            if totp: body['totpCode'] = totp
            result = await self.post('/api/auth/login', body)
            if result.get('code') == 0:
                data = result.get('data') or {}
                if data.get('require2FA'):
                    raise NeedsTwoFactor('接入服务已启用动态验证，请输入验证器中的六位动态码')
                credential = data.get('Credential')
                if not isinstance(credential, str) or not credential:
                    raise ValueError('QQ 接入认证暂未完成，请重试')
                self.effective_token = token
                self.credential, self.expires_at = credential, time.time()+3000
                return
            # Old volumes may contain a random token different from settings.json.
            if attempt == 0:
                if totp and 'code' in str(result.get('message', '')).lower():
                    raise NeedsTwoFactor('动态码不正确或已过期，请重新输入验证器中的六位动态码')
                actual = await self.read_token()
                if actual != token:
                    candidates.append(actual)
                    continue
            raise ValueError('QQ 接入认证暂未完成。请重新启动接入后重试，无需查找或输入 Token')

    async def call(self, path: str) -> dict:
        await self.authenticate()
        try:
            result = await self.post(path, credential=self.credential)
        except LoginCredentialExpired:
            result = {'code': 401, 'message': 'Unauthorized'}
        message = str(result.get('message', '')).casefold()
        expired = result.get('code') in {401, 403} or (result.get('code') != 0 and any(
            word in message for word in ('unauthorized', 'authorization', 'token', 'credential', 'authentication', 'auth failed')))
        if expired:
            # NapCat rotates its session key on restart. The host-side cached
            # credential may be younger than its TTL and still be invalid.
            self.credential = ''
            self.expires_at = 0
            await self.authenticate()
            result = await self.post(path, credential=self.credential)
        if result.get('code') != 0:
            raise ValueError('QQ 登录服务暂未准备好，请稍后重试')
        data = result.get('data')
        return data if isinstance(data, dict) else {}

    @staticmethod
    def valid_qr(value: str) -> bool:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096: return False
        try:
            parsed = urlsplit(value)
            return parsed.scheme == 'https' and (parsed.hostname or '').lower() in {'txz.qq.com', 'ssl.ptlogin2.qq.com', 'xui.ptlogin2.qq.com'} and not parsed.username and not parsed.password
        except ValueError:
            return False

    def image(self, value: str) -> str:
        if value != self.qr_value:
            image = qrcode.make(value, image_factory=SvgPathFillImage, border=4)
            self.qr_value = value
            self.qr_image = 'data:image/svg+xml;base64,'+base64.b64encode(image.to_string()).decode()
        return self.qr_image

    async def state(self, action: str, totp: str = '') -> dict:
        if action not in {'open', 'poll', 'refresh'}:
            raise ValueError('不支持的登录操作')
        if self.offline:
            raise ValueError('离线验证环境不连接真实 QQ，请在实际控制台扫码')
        if not self.config.value['bot_qq']:
            return {'state': 'needs_account', 'message': '先填写并保存机器人 QQ 号，再打开扫码登录。', 'page': 'settings'}
        if self.lock.locked():
            return {'state': 'preparing', 'message': '正在准备登录，请稍候…'}
        async with self.lock:
            job = self.operations.job
            if job and job['status'] == 'running':
                self.wait_until = max(self.wait_until, time.monotonic()+30)
                return {'state': 'starting', 'message': '正在准备 QQ 接入，完成后自动显示二维码。', 'job': {'status': job['status'], 'command': job['command'], 'lines': job['lines'][-8:]}}
            try:
                await self.authenticate(totp)
                if action == 'refresh': await self.call('/api/QQLogin/RefreshQRcode')
                data = await self.call('/api/QQLogin/CheckLoginStatus')
                if data.get('isLogin') is True:
                    self.qr_value, self.qr_image = '', ''
                    return {'state': 'online', 'message': 'QQ 已登录。可以关闭此窗口并授权会话。'}
                phase = data.get('loginPhase', '')
                if data.get('qrLoginAccepted') or phase in {'qrcode_scanned', 'initializing'}:
                    return {'state': 'scanned', 'message': '已扫码，请在手机 QQ 确认；正在等待登录完成。'}
                qr = data.get('qrcodeurl') or ''
                if not qr:
                    try:
                        qr = (await self.call('/api/QQLogin/GetQQLoginQrcode')).get('qrcode', '')
                    except ValueError:
                        return {'state': 'preparing', 'message': 'QQ 正在连接服务器，二维码准备好后会自动显示。'}
                if self.valid_qr(qr):
                    return {'state': 'qrcode', 'message': '使用机器人账号的手机 QQ 扫码，并在手机上确认登录。', 'image': self.image(qr),
                            'hint': '二维码过期或无法识别时，点击“刷新二维码”。'}
                return {'state': 'preparing', 'message': 'QQ 正在连接服务器，二维码准备好后会自动显示。'}
            except NeedsTwoFactor as error:
                return {'state': 'two_factor', 'message': str(error)}
            except (aiohttp.ClientError, TimeoutError):
                self.credential = ''
                if action == 'open':
                    installed = (self.config.root/'runtime/napcat/installed').is_dir()
                    self.operations.begin('start' if installed else 'install')
                    self.wait_until = time.monotonic()+120
                    return {'state': 'starting', 'message': '正在自动启动 QQ 接入，二维码准备好后会显示在这里。'}
                if time.monotonic() < self.wait_until and not (job and job.get('status') in {'failed', 'interrupted'}):
                    return {'state': 'preparing', 'message': '环境已启动，正在等待 QQ 登录服务就绪…'}
                return {'state': 'unavailable', 'message': 'QQ 接入未就绪。点击“重试并启动”恢复连接。'}
            except ValueError as error:
                if time.monotonic() < self.wait_until and not (job and job.get('status') in {'failed', 'interrupted'}):
                    return {'state': 'preparing', 'message': 'QQ 登录服务正在初始化，准备好后自动显示二维码。'}
                if job and job.get('status') in {'failed', 'interrupted'}:
                    return {'state': 'failed', 'message': 'QQ 接入启动未完成。可重试，或在运行与数据查看详情。'}
                return {'state': 'error', 'message': str(error)}
