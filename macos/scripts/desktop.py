#!/usr/bin/env python3
"""Commands used by the native menu-bar launcher; never print credentials."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from bootstrap import ROOT, environment
from control import running, start, stop


def status() -> str:
    if not running():
        return '服务已停止'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'http://127.0.0.1:8765/api/'
    token = (ROOT / 'config/control_token').read_text().strip()
    request = urllib.request.Request(base + 'login', data=json.dumps({'token': token}).encode(),
                                     headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=3) as response:
        cookie = response.headers.get('Set-Cookie', '').split(';', 1)[0]
    try:
        request = urllib.request.Request(base + 'status?light=1', headers={'Cookie': cookie})
        with opener.open(request, timeout=3) as response:
            bridge = json.load(response)['bridge']
    finally:
        request = urllib.request.Request(base + 'logout', data=b'{}', headers={
            'Cookie': cookie, 'Content-Type': 'application/json', 'X-Bot-Request': '1'})
        with opener.open(request, timeout=3):
            pass
    return 'QQ 在线' if bridge['online'] else ('QQ 账号离线' if bridge['connected'] else 'QQ 接入未连接')


def main() -> None:
    action = sys.argv[1]
    if action == 'start':
        start()
    elif action == 'status':
        print(status())
    elif action == 'stop':
        stop()
        subprocess.run([sys.executable, str(ROOT / 'scripts/runtime.py'), 'stop'],
                       cwd=ROOT, env=environment(), check=True, timeout=120)
    else:
        raise ValueError('不支持的应用操作')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('操作未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
