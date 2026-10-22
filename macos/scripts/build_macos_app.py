#!/usr/bin/env python3
"""Build a relocatable launcher beside the project; requires Xcode command-line tools."""
import os
import plistlib
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
app = ROOT / '栖.app'
contents = app / 'Contents'
macos = contents / 'MacOS'
macos.mkdir(parents=True, exist_ok=True)
with (contents / 'Info.plist').open('wb') as handle:
    plistlib.dump({'CFBundleName': '栖', 'CFBundleDisplayName': '栖',
                  'CFBundleIdentifier': 'local.qqcompanion.launcher.macos',
                  'CFBundleVersion': '1', 'CFBundleShortVersionString': '0.1.0',
                  'CFBundleExecutable': 'QiLauncher', 'CFBundlePackageType': 'APPL',
                  'LSUIElement': True, 'LSMinimumSystemVersion': '12.0',
                  'NSHighResolutionCapable': True}, handle)
env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(ROOT / 'runtime/cache/swift'))
arch = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
subprocess.run(['/usr/bin/swiftc', '-target', arch + '-apple-macosx12.0', '-swift-version', '5', '-O', '-framework', 'AppKit',
                str(ROOT / 'scripts/macos/Launcher.swift'), '-o', str(macos / 'QiLauncher')],
               env=env, check=True)
subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)], check=True)
print('已生成：' + str(app))
