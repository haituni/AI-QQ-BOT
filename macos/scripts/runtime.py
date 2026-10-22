#!/usr/bin/env python3
"""Portable Lima VZ + NapCat inside the project. No launch agents or system QQ edits."""
from __future__ import annotations

import json
import fcntl
import os
import platform
import subprocess
import sys
from pathlib import Path

from bootstrap import ROOT, RUNTIME, download, environment, unpack

sys.path.insert(0, str(ROOT))
from bot.config import Config, atomic_json

LIMA_VERSION = "2.2.1"
LIMA_SHA256 = {
    "arm64": "9e9eacce88f37e185c346bad73aa6136f738d8cdf8c3bb23cd42b071824bc66e",
    "x86_64": "6b776e4bc41f358e6ee01fefcc78e2eb26f11635a0fcff632234af77a566c6c4",
}
IMAGE = "mlikiowa/napcat-docker:v4.18.37@sha256:f611790ecffa23c3c182017d5cf68179dc24612403608cd835ad13d79642f1bd"
INSTANCE = "qqbot"
GUEST_IMAGES = {
    "arm64": ("arm64", "aarch64", "63a93bd5a8d76e33b15ceb5daa3657bd79be804748051ab178e643b0f5da22e7"),
    "x86_64": ("amd64", "x86_64", "8800651811af9a85465ad1d552add729947bb16488dddb4a9b5305a3d97332b2"),
}


def lima(*args: str, input: str | None = None, check: bool = True, capture: bool = False):
    return subprocess.run([str(RUNTIME / "lima-bin/bin/limactl"), *args], input=input,
                          text=True, cwd=ROOT, env=environment(), check=check, timeout=1200,
                          stdout=subprocess.PIPE if capture else None)


def ensure_image() -> None:
    print("阶段：检查 QQ 接入组件", flush=True)
    result = lima("shell", INSTANCE, "sudo", "docker", "image", "inspect", IMAGE,
                  "--format", "{{.Id}}", capture=True, check=False)
    if result.returncode == 0:
        print("已找到对应版本的本地组件，无需重新下载。", flush=True)
        return
    print("阶段：下载 QQ 接入组件（首次可能需要数分钟）", flush=True)
    for attempt in range(3):
        if attempt:
            print("阶段：重试下载 QQ 接入组件（复用已完成分层）", flush=True)
        result = lima("shell", INSTANCE, "sudo", "docker", "pull", IMAGE, check=False)
        if result.returncode == 0:
            return
    raise RuntimeError("QQ 镜像下载失败；请检查代理及镜像仓库连接，然后重试安装。")


DOCKER_SETUP = r'''#!/bin/sh
set -eu
apt_direct() {
  env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    apt-get -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT \
    -o Acquire::Retries=1 -o Acquire::http::Timeout=25 -o Acquire::https::Timeout=25 "$@"
}
if ! command -v docker >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  echo '阶段：更新 Docker 软件源（直连）'
  if ! apt_direct -o APT::Update::Error-Mode=any update; then
    echo '阶段：直连软件源失败，使用当前代理重试'
    apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=1 \
      -o Acquire::http::Timeout=25 -o Acquire::https::Timeout=25 update
  fi
  echo '阶段：安装 Docker'
  if ! apt_direct install -y docker.io; then
    echo '阶段：直连安装失败，使用当前代理重试'
    apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=25 \
      -o Acquire::https::Timeout=25 install -y docker.io
  fi
fi
mkdir -p /etc/systemd/system/docker.service.d
dropin=/etc/systemd/system/docker.service.d/qq-companion-env.conf
changed=0
if ! printf '[Service]\nEnvironmentFile=-/etc/environment\n' | cmp -s - "$dropin"; then
  printf '[Service]\nEnvironmentFile=-/etc/environment\n' > "$dropin"
  systemctl daemon-reload
  changed=1
fi
systemctl enable --now docker
if [ "$changed" = 1 ]; then systemctl restart docker; fi
docker info >/dev/null
'''


def ensure_docker() -> None:
    print("阶段：检查并准备 Docker（安装失败会立即报告，可重试）", flush=True)
    try:
        lima("shell", INSTANCE, "sudo", "sh", "-s", input=DOCKER_SETUP)
    except subprocess.CalledProcessError as error:
        raise RuntimeError("Docker 安装或启动失败；请检查上方软件源和代理日志后重试安装。") from error


def install() -> None:
    if platform.system() != "Darwin" or platform.machine() not in LIMA_SHA256:
        raise RuntimeError("此安装脚本只支持 Mac")
    print("阶段：检查本地安装文件", flush=True)
    binary = RUNTIME / "lima-bin/bin/limactl"
    if not binary.exists():
        archive = RUNTIME / "downloads" / f"lima-{LIMA_VERSION}-Darwin-{platform.machine()}.tar.gz"
        download(f"https://github.com/lima-vm/lima/releases/download/v{LIMA_VERSION}/{archive.name}",
                 archive, LIMA_SHA256[platform.machine()])
        unpack(archive, RUNTIME / "lima-bin")
    guest_arch, lima_arch, guest_hash = GUEST_IMAGES[platform.machine()]
    guest_image = RUNTIME / "downloads" / f"ubuntu-26.04-server-cloudimg-{guest_arch}.img"
    download(f"https://cloud-images.ubuntu.com/releases/resolute/release-20260927/{guest_image.name}", guest_image, guest_hash)
    yaml = RUNTIME / "qqbot.yaml"
    # Lima's remote-image cache ignores XDG_CACHE_HOME on macOS. Use a local, verified image.
    prefix = "images:\n  - location: " + json.dumps(str(guest_image)) + "\n    arch: " + lima_arch + "\n    digest: sha256:" + guest_hash + "\n"
    yaml.write_text(prefix + """# All guest state lives in runtime/lima/qqbot. No host-directory mounts.
vmType: vz
cpus: 2
memory: 3GiB
disk: 20GiB
mounts: []
param:
  internal_netplanOptional: "true"
containerd:
  system: false
  user: false
ssh:
  loadDotSSHPubKeys: false
  forwardAgent: false
  forwardX11: false
portForwards:
  - guestPort: 3001
    hostIP: 127.0.0.1
    hostPort: 3001
  - guestPort: 6099
    hostIP: 127.0.0.1
    hostPort: 6099
  - guestPortRange: [1, 65535]
    guestIP: 0.0.0.0
    proto: any
    ignore: true
""")
    lima("validate", str(yaml))
    instance = RUNTIME / "lima" / INSTANCE
    if instance.exists():
        # Replace generated config, including obsolete blocking provision/probes.
        # Existing disks and QQ login data remain in place.
        (instance / "lima.yaml").write_text(yaml.read_text())
    print("阶段：启动独立环境，请稍候", flush=True)
    lima("start", "--tty=false", INSTANCE if instance.exists() else str(yaml), *([] if instance.exists() else ["--name", INSTANCE]))
    ensure_docker()
    ensure_image()
    if Config(ROOT).value["bot_qq"]:
        deploy()
        print("QQ 接入安装完成。请在管理界面打开扫码登录。", flush=True)
    else:
        print("独立环境已就绪。请在「账号与连接」填写机器人 QQ 号，再点击「打开 QQ 扫码登录」。", flush=True)


def deploy() -> None:
    print("阶段：准备 QQ 登录服务", flush=True)
    cfg = Config(ROOT).value
    if not cfg["bot_qq"]:
        raise RuntimeError("请先在本机管理界面填写并保存机器人 QQ 号，再运行 QQ 接入安装/启动")
    # Secrets go through stdin over Lima SSH, never process arguments or terminal logs.
    payload = {"account": cfg["bot_qq"], "image": IMAGE,
               "web_token": cfg["bridge_web_token"],
               "onebot": {"network": {"httpServers": [], "httpClients": [],
                   "websocketClients": [], "websocketServers": [{"name": "qq-companion", "enable": True,
                       "host": "0.0.0.0", "port": 3001, "messagePostFormat": "array",
                       "reportSelfMessage": False, "enableForcePushEvent": True,
                       "token": cfg["bridge_token"], "heartInterval": 30000, "debug": False}]},
                       "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}}
    # Root paths below are inside the dedicated Linux VM only, not the Mac filesystem.
    script = r'''
import json,os,pathlib,subprocess,sys
os.umask(0o077)
data=json.load(sys.stdin)
base=pathlib.Path('/var/lib/qq-companion')
for name in ('qq','config','plugins'):
    (base/name).mkdir(parents=True,exist_ok=True)
path=base/'config'/('onebot11_'+data['account']+'.json')
same = path.exists() and json.loads(path.read_text()) == data['onebot']
existing=subprocess.run(['docker','inspect','qq-companion'],capture_output=True,text=True)
if same and existing.returncode == 0:
    info=json.loads(existing.stdout)[0]
    if info['Config']['Image'] == data['image'] and ('ACCOUNT='+data['account']) in info['Config'].get('Env',[]):
        if not info['State']['Running']:
            subprocess.run(['docker','start','qq-companion'],check=True,stdout=subprocess.DEVNULL)
        print('QQ 接入已就绪，保留现有容器与登录状态。')
        sys.exit(0)
path.write_text(json.dumps(data['onebot']))
web=base/'config'/'webui.json'
if not web.exists():
    web.write_text(json.dumps({'host':'0.0.0.0','port':6099,'token':data['web_token'],'loginRate':10}))
# Docker's init only creates config when missing. Keep a dedicated token file for --env-file.
env=base/'container.env'
env.write_text('MODE=ws\nWEBUI_TOKEN='+data['web_token']+'\nACCOUNT='+data['account']+'\n')
subprocess.run(['docker','rm','-f','qq-companion'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
cmd=['docker','run','-d','--name','qq-companion','--restart','no','--log-opt','max-size=5m','--log-opt','max-file=2',
     '--env-file',str(env),'-p','127.0.0.1:6099:6099','-p','127.0.0.1:3001:3001']
for src,dst in [('qq','/app/.config/QQ'),('config','/app/napcat/config'),('plugins','/app/napcat/plugins')]:
    cmd += ['-v',str(base/src)+':'+dst]
cmd += [data['image']]
subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL)
print('QQ 接入容器已启动；等待扫码登录。')
'''
    # python -c uses stdin exclusively for JSON; not a pipe of executable remote content.
    lima("shell", INSTANCE, "sudo", "python3", "-c", script, input=json.dumps(payload))


def main() -> None:
    os.umask(0o077)
    environment()
    os.chdir(ROOT)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    command = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if command == 'web-token':
        script = "import json,pathlib; p=pathlib.Path('/var/lib/qq-companion/config/webui.json'); d=json.loads(p.read_text()); print(json.dumps({'token':d.get('token','')}))"
        result = lima('shell', INSTANCE, 'sudo', 'python3', '-c', script, capture=True)
        print(result.stdout.strip())
        return
    lock = (RUNTIME / 'bridge-operation.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('另一个 QQ 环境操作正在执行，请稍后再试')
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command in ("install", "start"):
        install()
    elif command == "stop":
        if (RUNTIME / "lima" / INSTANCE).exists():
            lima("stop", INSTANCE)
    elif command == "status":
        if (RUNTIME / "lima-bin/bin/limactl").exists():
            lima("list", INSTANCE)
        else:
            print("QQ 独立环境尚未安装")
    else:
        raise SystemExit("用法：runtime.py install|start|stop|status")


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        print("未完成：QQ 环境操作超时，请检查网络或代理后重试安装。", file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print("未完成：" + str(error), file=sys.stderr)
        raise SystemExit(1)
