"""Launch the local service; stop through its authenticated shutdown workflow."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from bootstrap import ROOT, RUNTIME, environment
from native_process import hidden
sys.path.insert(0, str(ROOT))
from bot.config import Config
from bot import filelock


def running() -> bool:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / 'server.lock').open('a+b') as handle:
        try:
            filelock.flock(handle, filelock.LOCK_EX | filelock.LOCK_NB)
            filelock.flock(handle, filelock.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def start() -> None:
    env = environment()
    Config(ROOT)
    if not running():
        (ROOT / 'logs').mkdir(exist_ok=True)
        path = ROOT / 'logs/server.log'
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix('.previous.log'))
        with path.open('a', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'bot.server'], cwd=ROOT,
                env=env, stdout=log, stderr=log,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0)
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('服务未启动，请查看 logs/server.log；请先停止其他版本。')
            pid = RUNTIME / 'server.pid'
            if running() and pid.exists() and pid.read_text().strip() == str(process.pid):
                break
            time.sleep(.2)
        else:
            process.terminate()
            raise RuntimeError('管理服务启动超时，请查看 logs/server.log。')
    token = (ROOT / 'config/control_token').read_text().strip()
    webbrowser.open('http://127.0.0.1:8765/#token=' + token)
    print('管理界面已打开；关闭浏览器不会停止服务。')


def stop() -> None:
    if not running():
        # A stopped web server does not imply its QQ container has stopped.
        if (RUNTIME / 'napcat/installed').is_dir():
            subprocess.run([sys.executable, str(ROOT / 'scripts/runtime.py'), 'stop'],
                           cwd=ROOT, env=environment(), check=True, timeout=120, **hidden())
        print('管理服务已停止。')
        return
    token = (ROOT / 'config/control_token').read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://127.0.0.1:8765/api/'
    def request(path: str, body: dict | None = None, cookie: str = '') -> tuple[dict, str]:
        req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'X-Bot-Request': '1', 'Cookie': cookie})
        with opener.open(req, timeout=10) as response:
            return json.load(response), response.headers.get('Set-Cookie', '').split(';', 1)[0]
    _, cookie = request('login', {'token': token})
    try:
        request('runtime', {'command': 'stop'}, cookie)
        for _ in range(150):
            state, _ = request('status?light=1', cookie=cookie)
            job = state['operations']['job']
            if job['status'] == 'succeeded':
                request('shutdown', {}, cookie)
                break
            if job['status'] != 'running':
                raise RuntimeError('QQ 接入未能停止，请在控制台查看环境任务。')
            time.sleep(1)
        else:
            raise RuntimeError('QQ 接入停止超时，请在控制台重试。')
    finally:
        try:
            request('logout', {}, cookie)
        except OSError:
            pass
    for _ in range(100):
        if not running():
            print('机器人已停止。')
            return
        time.sleep(.2)
    raise RuntimeError('服务仍在退出，请稍后重试。')


if __name__ == '__main__':
    try:
        stop() if len(sys.argv) > 1 and sys.argv[1] == 'stop' else start()
    except Exception as error:
        print('操作未完成：'+str(error), file=sys.stderr)
        raise SystemExit(1)
