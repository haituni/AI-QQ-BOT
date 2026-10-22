"""Regression checks for browser-only connectivity and occupied ports."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import download_proxy
import control


class DownloadProxy(unittest.TestCase):
    def test_system_proxy_reaches_curl_environment_and_loopback_bypasses(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(download_proxy, 'getproxies', return_value={
            'https': 'http://127.0.0.1:7897', 'no': 'internal.example'
        }):
            env = download_proxy.download_environment()
        self.assertEqual(env['https_proxy'], 'http://127.0.0.1:7897')
        self.assertIn('internal.example', env['no_proxy'])
        self.assertIn('127.0.0.1', env['no_proxy'])
        self.assertEqual(env['no_proxy'], env['NO_PROXY'])
        self.assertNotIn('https_proxy', os.environ)

    def test_explicit_environment_proxy_takes_precedence(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://custom:8080'}, clear=True):
            env = download_proxy.download_environment()
        self.assertEqual(env['https_proxy'], 'http://custom:8080')

    def test_bypass_only_environment_still_uses_system_proxy(self):
        with patch.dict(os.environ, {'NO_PROXY': 'intranet'}, clear=True), \
             patch.object(download_proxy, 'system_proxies', return_value={'https': 'http://localhost:7897'}):
            env = download_proxy.download_environment()
        self.assertEqual(env['https_proxy'], 'http://localhost:7897')
        self.assertIn('intranet', env['no_proxy'])

    def test_no_proxy_does_not_invent_a_proxy(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(download_proxy, 'getproxies', return_value={}), patch.object(download_proxy, 'system_proxies', return_value={}):
            env = download_proxy.download_environment()
        self.assertNotIn('https_proxy', env)


class StartupConflict(unittest.TestCase):
    def test_foreign_listener_is_reported_without_starting_or_killing_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(control, 'ROOT', root), patch.object(control, 'Config'), \
                 patch.object(control, 'environment', return_value={}), \
                 patch.object(control, 'running', return_value=False), \
                 patch.object(control.socket, 'socket') as probe, \
                 patch.object(control.subprocess, 'Popen') as spawn:
                probe.return_value.__enter__.return_value.connect_ex.return_value = 0
                with self.assertRaisesRegex(RuntimeError, '8765.*旧版'):
                    control.start()
                spawn.assert_not_called()

    def test_existing_project_instance_does_not_probe_or_spawn(self):
        with patch.object(control, 'Config'), patch.object(control, 'ROOT'), \
             patch.object(control, 'environment', return_value={}), \
             patch.object(control, 'running', return_value=True), \
             patch.object(control.socket, 'socket') as probe, \
             patch.object(control.subprocess, 'Popen') as spawn, \
             patch.object(control.subprocess, 'run'), patch('webbrowser.open'):
            control.start()
            probe.assert_not_called()
            spawn.assert_not_called()


class ModelDownload(unittest.TestCase):
    def test_download_uses_system_proxy_retries_and_checks_content(self):
        import hashlib
        import setup_sticker_rag as setup
        payload = b'verified-model'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def download(command, **kwargs):
                Path(command[command.index('--output') + 1]).write_bytes(payload)
            with patch.object(setup, 'ROOT', root), \
                 patch.object(setup, 'MODEL_FILES', {'model.onnx': ('onnx/model.onnx', hashlib.sha256(payload).hexdigest())}), \
                 patch.object(sys, 'argv', ['setup_sticker_rag.py']), \
                 patch.object(setup, 'download_environment', return_value={'https_proxy': 'http://localhost:7897'}), \
                 patch.object(setup.subprocess, 'run', side_effect=download) as run:
                setup.main()
                self.assertEqual(run.call_args.kwargs['env']['https_proxy'], 'http://localhost:7897')
                self.assertIn('--retry', run.call_args.args[0])
                setup.main()
                self.assertEqual(run.call_count, 1)
            self.assertEqual((root/'runtime/models/bge-small-zh-v1.5/model.onnx').read_bytes(), payload)

    def test_bad_download_is_never_promoted_to_installed_model(self):
        import setup_sticker_rag as setup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def download(command, **kwargs):
                Path(command[command.index('--output') + 1]).write_bytes(b'corrupt')
            with patch.object(setup, 'ROOT', root), \
                 patch.object(setup, 'MODEL_FILES', {'model.onnx': ('model.onnx', '0' * 64)}), \
                 patch.object(sys, 'argv', ['setup_sticker_rag.py']), \
                 patch.object(setup.subprocess, 'run', side_effect=download):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    setup.main()
            self.assertFalse((root/'runtime/models/bge-small-zh-v1.5/model.onnx').exists())
