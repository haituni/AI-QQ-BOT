"""Desktop bootstrap completion, retries and concurrent installation protection."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import install_state
import bootstrap


class InstallationState(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        for name in ('pyproject.toml', 'uv.lock', 'scripts/bootstrap.py', 'scripts/setup_sticker_rag.py', 'bot/sticker_rag.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('seed', encoding='utf-8')
        python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
        python.parent.mkdir(parents=True)
        python.write_bytes(b'placeholder')

    def test_partial_venv_does_not_count_as_completed_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.assertFalse(install_state.ready(root))
            install_state.complete(root)
            self.assertTrue(install_state.ready(root))
            (root/'uv.lock').write_text('new dependencies')
            self.assertFalse(install_state.ready(root))

    def test_corrupt_marker_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root/'runtime').mkdir()
            (root/'runtime/desktop-ready.json').write_text('{')
            self.assertFalse(install_state.ready(root))

    def test_failed_install_does_not_leave_ready_marker_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            with patch.object(bootstrap, 'ROOT', root), patch.object(bootstrap, 'RUNTIME', root/'runtime'), \
                 patch.object(sys, 'argv', ['bootstrap.py']), \
                 patch.object(bootstrap, 'install', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    bootstrap.main()
            self.assertFalse(install_state.ready(root))
            with patch.object(bootstrap, 'ROOT', root), patch.object(bootstrap, 'RUNTIME', root/'runtime'), \
                 patch.object(sys, 'argv', ['bootstrap.py']), patch.object(bootstrap, 'install') as install:
                bootstrap.main()
                bootstrap.main()
                install.assert_called_once()
            self.assertTrue(install_state.ready(root))

    def test_force_repair_invalidates_old_marker_before_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            install_state.complete(root)
            with patch.object(bootstrap, 'ROOT', root), patch.object(bootstrap, 'RUNTIME', root/'runtime'), \
                 patch.object(sys, 'argv', ['bootstrap.py', '--force']), \
                 patch.object(bootstrap, 'install', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError): bootstrap.main()
            self.assertFalse(install_state.ready(root))

    def test_two_installers_cannot_modify_environment_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with install_state.installation(root):
                with self.assertRaises(RuntimeError):
                    with install_state.installation(root): pass
            with install_state.installation(root): pass
