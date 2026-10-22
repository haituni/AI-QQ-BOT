"""Package only source, initial roles, docs and the native launcher, never runtime data."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import platform
import stat
import zipfile

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE = {'__pycache__', '.DS_Store', 'obj', 'bin', 'build', 'config', 'data', 'runtime', 'logs', 'backups', '.venv', '.git', 'characters'}


def source_files(root: Path):
    for name in ('README.md', 'pyproject.toml', 'uv.lock'):
        yield root / name
    for folder in ('bot', 'static', 'initial', 'docs', 'scripts', 'tests'):
        for path in sorted((root / folder).rglob('*')):
            relative = path.relative_to(root)
            if path.is_file() and not path.is_symlink() and not EXCLUDE.intersection(relative.parts) and path.suffix != '.pyc':
                yield path
    for suffix in ('*.command', '*.cmd'):
        yield from sorted(root.glob(suffix))


def package(root: Path, output: Path) -> Path:
    windows = (root / 'scripts/windows/Launcher.cs').exists()
    launcher = root / ('栖.exe' if windows else '栖.app')
    if not launcher.exists():
        raise RuntimeError('请先构建桌面应用，再打包。')
    paths = list(source_files(root))
    paths += [launcher] if windows else [p for p in launcher.rglob('*') if p.is_file()]
    # Preserve POSIX modes so macOS command tools and the app remain executable after extraction.
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.partial.zip')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(root)
            if path.is_symlink():
                raise RuntimeError('发行文件中存在未预期的符号链接：' + str(relative))
            info = zipfile.ZipInfo.from_file(path, arcname=str(Path('栖-Windows' if windows else '栖-macOS') / relative))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (path.stat().st_mode & 0o777)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    temporary.replace(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    label = 'Windows-x64' if (ROOT/'scripts/windows/Launcher.cs').exists() else 'macos-' + platform.machine()
    destination = args.output or ROOT/'build'/('栖-' + label + '.zip')
    print(package(ROOT, destination))
