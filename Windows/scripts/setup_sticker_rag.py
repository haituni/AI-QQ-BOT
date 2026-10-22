"""Install pinned local sticker-retrieval assets inside this project."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from download_proxy import download_environment
from native_process import hidden

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot.sticker_rag import MODEL_FILES, MODEL_REVISION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy', help='Optional HTTP proxy for model download')
    args = parser.parse_args()
    os.umask(0o077)
    folder = ROOT / 'runtime/models/bge-small-zh-v1.5'
    folder.mkdir(parents=True, exist_ok=True)
    for name, (remote, expected) in MODEL_FILES.items():
        target = folder / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            print(name + ': verified')
            continue
        temporary = folder / (name + '.part')
        command = ['curl.exe', '--fail', '--location', '--silent', '--show-error', '--retry', '2', '--retry-delay', '2', '--connect-timeout', '25', '--max-time', '300']
        if args.proxy:
            command += ['--proxy', args.proxy]
        command += ['--output', str(temporary),
                    f'https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/{MODEL_REVISION}/{remote}']
        subprocess.run(command, env=download_environment(), check=True, **hidden())
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Model checksum mismatch: ' + name)
        temporary.replace(target)
        target.chmod(0o600)
    print('Local Chinese sticker retrieval model ready.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        print('错误：表情模型下载失败，请检查系统代理能否访问 Hugging Face，然后重试。', flush=True)
        raise SystemExit(1)
    except RuntimeError:
        print('错误：表情模型校验失败，请重试下载。', flush=True)
        raise SystemExit(1)
