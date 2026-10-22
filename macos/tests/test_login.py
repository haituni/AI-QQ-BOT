from __future__ import annotations
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from bot.config import Config
from bot.napcat import NapCatLogin

class LoginFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.config=Config(Path(self.tmp.name));self.config.update({'bot_qq':'123456789'})
        self.session=ClientSession();self.ops=type('Operations',(),{'job':None})();self.ops.begin=lambda cmd:None
        self.login=NapCatLogin(self.config,self.session,self.ops,offline=False)
    async def asyncTearDown(self):
        await self.session.close();self.tmp.cleanup()
    async def test_wrong_host_token_recovers_actual_vm_token_and_shows_qr(self):
        actual='actual-vm-secret';hashes=[]
        async def auth(request):
            body=await request.json();hashes.append(body['hash'])
            return web.json_response({'code':0,'data':{'Credential':'session-private'}} if body['hash']==hashlib.sha256((actual+'.napcat').encode()).hexdigest() else {'code':-1,'message':'token is invalid'})
        async def status(request):
            self.assertEqual(request.headers['Authorization'],'Bearer session-private')
            return web.json_response({'code':0,'data':{'isLogin':False,'qrcodeurl':'https://txz.qq.com/p?k=TEST_ONLY'}})
        app=web.Application();app.router.add_post('/api/auth/login',auth);app.router.add_post('/api/QQLogin/CheckLoginStatus',status)
        async with TestServer(app) as server:
            self.login.endpoint=str(server.make_url('/')).rstrip('/');self.login.read_token=AsyncMock(return_value=actual)
            result=await self.login.state('open');self.assertEqual(result['state'],'qrcode');self.assertTrue(result['image'].startswith('data:image/svg+xml;base64,'));self.assertNotIn(actual,json.dumps(result));self.assertNotIn('session-private',json.dumps(result))
            await self.login.state('poll');self.assertEqual(len(hashes),2);self.login.read_token.assert_awaited_once()
    async def test_already_online_and_two_factor_states(self):
        self.login.authenticate=AsyncMock();self.login.call=AsyncMock(return_value={'isLogin':True})
        self.assertEqual((await self.login.state('open'))['state'],'online')
    async def test_qr_not_ready_is_waiting_not_an_error(self):
        self.login.authenticate=AsyncMock();self.login.call=AsyncMock(side_effect=[{},ValueError('Not ready')])
        self.assertEqual((await self.login.state('open'))['state'],'preparing')
    async def test_no_account_does_not_start_services(self):
        self.config.update({'bot_qq':''});self.login.authenticate=AsyncMock()
        self.assertEqual((await self.login.state('open'))['state'],'needs_account');self.login.authenticate.assert_not_called()
    async def test_does_not_disable_two_factor_or_expose_qr_from_other_host(self):
        self.login.post=AsyncMock(return_value={'code':0,'data':{'require2FA':True}})
        self.assertEqual((await self.login.state('open'))['state'],'two_factor')
        self.assertFalse(self.login.valid_qr('https://evil.test/login'));self.assertFalse(self.login.valid_qr('http://txz.qq.com/p?k=test'))
    async def test_container_start_grace_does_not_require_manual_retry(self):
        import aiohttp,time
        self.login.wait_until=time.monotonic()+60;self.login.authenticate=AsyncMock(side_effect=aiohttp.ClientConnectionError())
        self.assertEqual((await self.login.state('poll'))['state'],'preparing')

    async def test_restart_unauthorized_reauthenticates_without_manual_token(self):
        self.login.credential='stale-session'
        import time
        self.login.expires_at=time.time()+3000
        calls=[]
        async def endpoint(path,body=None,*,credential=''):
            calls.append((path,credential))
            if path=='/api/auth/login':return {'code':0,'data':{'Credential':'new-session'}}
            if credential=='stale-session':return {'code':-1,'message':'Unauthorized'}
            return {'code':0,'data':{'isLogin':False,'qrcodeurl':'https://txz.qq.com/p?k=TEST_ONLY'}}
        self.login.post=AsyncMock(side_effect=endpoint)
        result=await self.login.state('open')
        self.assertEqual(result['state'],'qrcode')
        self.assertEqual([p for p,c in calls].count('/api/auth/login'),1)
        self.assertEqual(self.login.credential,'new-session')
        self.assertNotIn('new-session',json.dumps(result))

    async def test_http_auth_expiry_retries_only_once(self):
        from bot.napcat import LoginCredentialExpired
        self.login.authenticate=AsyncMock()
        self.login.post=AsyncMock(side_effect=[LoginCredentialExpired('expired'),{'code':-1,'message':'Unauthorized'}])
        with self.assertRaises(ValueError):await self.login.call('/api/QQLogin/CheckLoginStatus')
        self.assertEqual(self.login.post.await_count,2)

    async def test_other_errors_do_not_reauthenticate(self):
        self.login.authenticate=AsyncMock()
        self.login.post=AsyncMock(return_value={'code':-1,'message':'Core not ready'})
        with self.assertRaises(ValueError):await self.login.call('/api/QQLogin/CheckLoginStatus')
        self.assertEqual(self.login.post.await_count,1)


class LoginStatusSync(unittest.IsolatedAsyncioTestCase):
    async def test_logged_in_dialog_waits_for_message_channel(self):
        from aiohttp.test_utils import TestClient
        from bot.server import create_app
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Path(folder),offline=True)
            client=TestClient(TestServer(app),headers={'Host':'127.0.0.1:8765','X-Bot-Request':'1'})
            await client.start_server()
            try:
                await client.post('/api/login',json={'token':app['config'].control_token})
                app['qq_login'].state=AsyncMock(return_value={'state':'online','message':'QQ已登录'})
                app['bridge'].refresh_status=AsyncMock(return_value=False)
                response=await client.post('/api/qq-login',json={'action':'poll'})
                self.assertEqual((await response.json())['state'],'connecting')
                async def ready():
                    app['bridge'].online=True;app['bridge'].connected=True;return True
                app['bridge'].refresh_status=AsyncMock(side_effect=ready)
                response=await client.post('/api/qq-login',json={'action':'poll'})
                self.assertEqual((await response.json())['state'],'online')
                status=await (await client.get('/api/status')).json()
                self.assertTrue(status['bridge']['online'])
            finally:await client.close()
