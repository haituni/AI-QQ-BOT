"""Persistent completion marker and lock for resumable desktop installation."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from contextlib import contextmanager


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ('pyproject.toml', 'uv.lock', 'scripts/bootstrap.py', 'scripts/setup_sticker_rag.py', 'bot/sticker_rag.py'):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def ready(root: Path) -> bool:
    python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    try:
        value = json.loads((root / 'runtime/desktop-ready.json').read_text(encoding='utf-8'))
        return python.is_file() and value == {'fingerprint': fingerprint(root)}
    except (OSError, ValueError):
        return False


def complete(root: Path) -> None:
    path = root / 'runtime/desktop-ready.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'fingerprint': fingerprint(root)}), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def installation(root: Path):
    path = root / 'runtime/desktop-install.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError('另一个安装正在进行，请等待完成后重试。') from error
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('另一个安装正在进行，请等待完成后重试。') from error
        yield
