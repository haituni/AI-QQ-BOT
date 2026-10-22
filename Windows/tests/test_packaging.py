"""Release bundles must not contain runtime data or depend on the parent repository."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
spec = importlib.util.spec_from_file_location('release_package', SCRIPTS/'package_desktop.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleasePackaging(unittest.TestCase):
    def test_only_allowlisted_source_and_launcher_are_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('README.md','pyproject.toml','uv.lock','bot/server.py','initial/roleplay.json',
                         'scripts/macos/Launcher.swift','栖.app/Contents/MacOS/QiLauncher',
                         'config/settings.json','data/memory.sqlite3','logs/server.log',
                         '.venv/bin/python','scripts/__pycache__/sample.pyc','docs/.DS_Store'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('placeholder')
            exe=root/'栖.app/Contents/MacOS/QiLauncher';exe.chmod(0o755)
            output=release.package(root,root/'build/clean.zip')
            with zipfile.ZipFile(output) as bundle:
                names=bundle.namelist()
                self.assertIn('栖-macOS/initial/roleplay.json',names)
                self.assertFalse(any(n.endswith(('settings.json','.sqlite3','.log','.pyc','.DS_Store')) for n in names))
                self.assertTrue(bundle.getinfo('栖-macOS/栖.app/Contents/MacOS/QiLauncher').external_attr >> 16 & 0o111)

    def test_refuse_release_without_native_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(RuntimeError):release.package(root,root/'clean.zip')
