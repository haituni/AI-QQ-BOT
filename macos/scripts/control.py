#!/usr/bin/env python3
"""Start/stop only this project's server and open its local control UI."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from bootstrap import ROOT, RUNTIME, environment

sys.path.insert(0, str(ROOT))
from bot.config import Config


def running() -> bool:
    path = RUNTIME / "server.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def start() -> None:
    os.umask(0o077)
    env = environment()
    Config(ROOT)
    if not running():
        (ROOT / "logs").mkdir(exist_ok=True)
        log_path = ROOT / "logs/server.log"
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_path.with_suffix(".previous.log"))
        with log_path.open("a") as log:
            process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "bot.server"],
                                       cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError("服务未启动，请查看 logs/server.log")
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:8765/", timeout=1):
                    pid_path = RUNTIME / "server.pid"
                    if running() and pid_path.exists() and pid_path.read_text().strip() == str(process.pid):
                        break
            except OSError:
                time.sleep(.2)
        else:
            process.terminate()
            raise RuntimeError("管理服务启动超时，请查看 logs/server.log")
    token = (ROOT / "config/control_token").read_text().strip()
    subprocess.run(["/usr/bin/open", "http://127.0.0.1:8765/#token=" + token], check=True)
    print("管理界面已打开。QQ 接入尚未安装时，也可以先填写本地设置。")


def stop() -> None:
    pid_path = RUNTIME / "server.pid"
    if not running():
        print("管理服务已停止")
        return
    if not pid_path.exists():
        raise RuntimeError("服务启动中，稍后重试停止")
    pid = int(pid_path.read_text())
    # A pid file alone cannot authorize killing a process: validate its command line and cwd.
    import ctypes
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    buffer = ctypes.create_string_buffer(4096)
    size = libproc.proc_pidpath(pid, buffer, len(buffer))
    executable = buffer.value.decode() if size > 0 else ""
    if not executable or not Path(executable).resolve().is_relative_to(ROOT):
        raise RuntimeError("PID 不属于项目目录内的 Python，未停止该进程")
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not running():
            print("管理服务已停止")
            return
        time.sleep(.1)
    raise RuntimeError("服务仍在退出，请稍后重试；暂时不要删除目录")


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "stop":
            stop()
        else:
            start()
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
