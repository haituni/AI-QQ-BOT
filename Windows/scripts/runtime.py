#!/usr/bin/env python3
"""Project-owned NapCat container on Docker Desktop (Linux containers)."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from bootstrap import ROOT, RUNTIME, environment
from native_process import hidden
sys.path.insert(0, str(ROOT))
from bot.config import Config, atomic_json
from bot import filelock

IMAGE = 'mlikiowa/napcat-docker:v4.18.37@sha256:f611790ecffa23c3c182017d5cf68179dc24612403608cd835ad13d79642f1bd'
OWNER = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()[:16]
CONTAINER = 'qq-companion-' + OWNER
BASE = RUNTIME / 'napcat'


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    binary = shutil.which('docker')
    if not binary:
        raise RuntimeError('请先安装并启动 Docker Desktop，启用 WSL 2 与 Linux 容器模式。')
    return subprocess.run([binary, *args], check=check, text=True, encoding='utf-8',
                          errors='replace', capture_output=True, cwd=ROOT, **hidden())


def inspect() -> dict | None:
    result = docker('container', 'inspect', CONTAINER, check=False)
    if result.returncode:
        return None
    info = json.loads(result.stdout)[0]
    if info['Config'].get('Labels', {}).get('local.qqcompanion.owner') != OWNER:
        raise RuntimeError('同名容器不属于此目录，未修改该容器。')
    return info


def deploy() -> None:
    cfg = Config(ROOT).value
    if not cfg['bot_qq']:
        raise RuntimeError('请先在控制台填写并保存机器人 QQ 号。')
    for name in ('qq', 'config', 'plugins'):
        (BASE / name).mkdir(parents=True, exist_ok=True)
    onebot = {'network': {'httpServers': [], 'httpClients': [], 'websocketClients': [],
        'websocketServers': [{'name': 'qq-companion', 'enable': True, 'host': '0.0.0.0',
        'port': 3001, 'messagePostFormat': 'array', 'reportSelfMessage': False,
        'enableForcePushEvent': True, 'token': cfg['bridge_token'], 'heartInterval': 30000, 'debug': False}]},
        'musicSignUrl': '', 'enableLocalFile2Url': False, 'parseMultMsg': False}
    path = BASE / 'config' / ('onebot11_' + cfg['bot_qq'] + '.json')
    web = {'host': '0.0.0.0', 'port': 6099, 'token': cfg['bridge_web_token'], 'loginRate': 10}
    web_path = BASE / 'config/webui.json'
    same = (path.exists() and json.loads(path.read_text(encoding='utf-8')) == onebot
            and web_path.exists() and json.loads(web_path.read_text(encoding='utf-8')) == web)
    info = inspect()
    if info and same and info['Config']['Image'] == IMAGE and ('ACCOUNT='+cfg['bot_qq']) in info['Config'].get('Env', []):
        if not info['State']['Running']:
            docker('start', CONTAINER)
        print('QQ 接入已就绪，保留登录状态。', flush=True)
        return
    if info:
        docker('rm', '-f', CONTAINER)
    atomic_json(path, onebot)
    atomic_json(web_path, web)
    envfile = BASE / 'container.env'
    envfile.write_text('MODE=ws\nWEBUI_TOKEN='+cfg['bridge_web_token']+'\nACCOUNT='+cfg['bot_qq']+'\n', encoding='utf-8')
    envfile.chmod(0o600)
    args = ['run', '-d', '--name', CONTAINER, '--label', 'local.qqcompanion.owner='+OWNER,
            '--restart', 'no', '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
            '--env-file', str(envfile), '-p', '127.0.0.1:6099:6099', '-p', '127.0.0.1:3001:3001']
    for src, dst in (('qq', '/app/.config/QQ'), ('config', '/app/napcat/config'), ('plugins', '/app/napcat/plugins')):
        args += ['--mount', f'type=bind,source={BASE / src},target={dst}']
    docker(*args, IMAGE)
    print('QQ 接入容器已启动，等待扫码登录。', flush=True)


def main() -> None:
    environment()
    BASE.mkdir(parents=True, exist_ok=True)
    command = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if command == 'web-token':
        value = json.loads((BASE / 'config/webui.json').read_text(encoding='utf-8'))
        print(json.dumps({'token': value.get('token', '')}))
        return
    with (RUNTIME / 'bridge-operation.lock').open('a+b') as lock:
        filelock.flock(lock, filelock.LOCK_EX | filelock.LOCK_NB)
        if command in ('install', 'start'):
            result = docker('info', '--format', '{{.OSType}}')
            if result.stdout.strip() != 'linux':
                raise RuntimeError('请将 Docker Desktop 切换为 Linux 容器模式。')
            if docker('image', 'inspect', IMAGE, check=False).returncode:
                print('阶段：下载 QQ 接入镜像，首次可能需要数分钟。', flush=True)
                docker('pull', IMAGE)
            (BASE / 'installed').mkdir(exist_ok=True)
            if Config(ROOT).value['bot_qq']:
                deploy()
            else:
                print('环境已就绪，请填写机器人 QQ 后打开扫码登录。')
        elif command == 'stop':
            if (BASE / 'installed').is_dir() and inspect():
                docker('stop', '--time', '20', CONTAINER)
            print('此目录的 QQ 接入已停止。')
        elif command == 'status':
            info = inspect()
            print('QQ 接入运行中' if info and info['State']['Running'] else 'QQ 接入已停止或尚未安装')
        else:
            raise ValueError('用法：runtime.py install|start|stop|status')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Docker errors may contain environment values. Do not echo raw output.
        print('Docker 操作失败，请检查 Docker Desktop、网络和本机 3001/6099 端口。', file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, OSError, ValueError) as error:
        print('未完成：' + str(error), file=sys.stderr)
        raise SystemExit(1)
