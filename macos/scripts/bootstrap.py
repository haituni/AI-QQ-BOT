#!/usr/bin/env python3
"""Install Python and libraries inside the project, without a global installer."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from install_state import complete, installation, ready

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"


def environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"UV_CACHE_DIR": str(RUNTIME / "cache/uv"),
                "UV_PYTHON_INSTALL_DIR": str(RUNTIME / "python"),
                "UV_PYTHON_BIN_DIR": str(RUNTIME / "bin"),
                "UV_NO_CONFIG": "1", "PYTHONPYCACHEPREFIX": str(RUNTIME / "cache/pycache"),
                "XDG_CACHE_HOME": str(RUNTIME / "cache"),
                "XDG_CONFIG_HOME": str(RUNTIME / "xdg-config"),
                "XDG_DATA_HOME": str(RUNTIME / "xdg-data"),
                "TMPDIR": str(RUNTIME / "tmp"),
                "LIMA_HOME": str(RUNTIME / "lima"),
                "LIMA_WORKDIR": "/tmp", "LIMA_SSH_PORT_FORWARDER": "false"})
    # HTTP proxy vars are inherited deliberately for downloads; app API clients ignore them.
    for path in ("tmp", "bin", "downloads", "cache", "lima"):
        (RUNTIME / path).mkdir(parents=True, exist_ok=True)
    return env


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, path: Path, digest: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and digest and sha256_file(path) == digest:
        return
    print("下载：" + path.name, flush=True)
    # Use curl's platform certificate store; never turn off certificate verification.
    for attempt in range(3):
        result = subprocess.run(["/usr/bin/curl", "-fL", "--connect-timeout", "25", "--max-time", "900",
                                 "-C", "-", "-o", str(path), url], env=environment())
        if result.returncode == 0:
            break
        if attempt == 2:
            raise RuntimeError("下载失败，可重新运行以继续下载：" + path.name)
    if digest and sha256_file(path) != digest:
        raise RuntimeError("下载校验失败，请删除 runtime/downloads 中对应压缩包后重试")


def unpack(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as handle:
        # Runtime archives are trusted only after SHA256 validation. Check traversal too.
        for member in handle.getmembers():
            dest = (target / member.name).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise RuntimeError("压缩包包含不安全路径")
            if member.issym() or member.islnk():
                link = (dest.parent / member.linkname).resolve()
                if not link.is_relative_to(target.resolve()):
                    raise RuntimeError("压缩包包含目录外链接")
        handle.extractall(target)


def install() -> None:
    os.umask(0o077)
    os.chdir(ROOT)
    env = environment()
    print("阶段：准备安装工具", flush=True)
    uv = RUNTIME / "bin/uv"
    if not uv.exists():
        existing = shutil.which("uv")
        if existing:
            shutil.copy2(Path(existing).resolve(), uv)
        else:
            # GitHub API provides the release asset's SHA256 digest.
            metadata = RUNTIME / "downloads/uv-release.json"
            download("https://api.github.com/repos/astral-sh/uv/releases/tags/0.13.0", metadata)
            release = json.loads(metadata.read_text())
            arch = "aarch64" if platform.machine() == "arm64" else "x86_64"
            name = f"uv-{arch}-apple-darwin.tar.gz"
            asset = next(a for a in release["assets"] if a["name"] == name)
            digest = asset.get("digest", "")
            if not digest.startswith("sha256:"):
                raise RuntimeError("官方发行版未提供校验摘要，未安装 uv")
            archive = RUNTIME / "downloads" / name
            download(asset["browser_download_url"], archive, digest.split(":", 1)[1])
            unpack(archive, RUNTIME / "uv-dist")
            shutil.copy2(next((RUNTIME / "uv-dist").glob("*/uv")), uv)
        uv.chmod(0o700)
    print("阶段：准备项目内 Python", flush=True)
    subprocess.run([str(uv), "python", "install", "3.12", "--no-bin"], env=env, check=True)
    candidates = sorted((RUNTIME / "python").glob("cpython-3.12.*-macos-*/bin/python3.12"))
    if not candidates:
        raise RuntimeError("未找到项目目录内的 Python")
    python = candidates[-1]
    print("阶段：安装 Python 依赖", flush=True)
    subprocess.run([str(uv), "sync", "--locked", "--python", str(python)], env=env, cwd=ROOT, check=True)
    print("阶段：准备表情语义模型", flush=True)
    subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/setup_sticker_rag.py")], env=env, cwd=ROOT, check=True)
    print("本地 Python、依赖与表情语义检索模型已就绪。", flush=True)


def main() -> None:
    with installation(ROOT):
        if ready(ROOT) and '--force' not in sys.argv:
            print("阶段：运行环境已就绪", flush=True)
            return
        # Invalidate the old marker before starting any mutable installation work.
        (RUNTIME / 'desktop-ready.json').unlink(missing_ok=True)
        install()
        complete(ROOT)
        print("阶段：运行环境已就绪", flush=True)


if __name__ == "__main__":
    main()
