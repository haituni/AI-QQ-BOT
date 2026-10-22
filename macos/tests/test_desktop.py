"""Desktop launcher must stop only its project and clean temporary sessions."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('desktop_launcher', SCRIPTS / 'desktop.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class Desktop(unittest.TestCase):
    def test_stopped_status_never_reads_credentials(self):
        with patch.object(desktop, 'running', return_value=False), patch.object(desktop, 'ROOT') as root:
            self.assertEqual(desktop.status(), '服务已停止')
            root.__truediv__.assert_not_called()

    def test_failed_server_stop_does_not_stop_vm(self):
        with patch.object(sys, 'argv', ['desktop.py', 'stop']), \
                patch.object(desktop, 'stop', side_effect=RuntimeError('stop failed')), \
                patch.object(desktop.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                desktop.main()
            run.assert_not_called()

    def test_probe_always_logs_out_even_if_status_fails(self):
        opener = MagicMock()
        login = MagicMock()
        login.__enter__.return_value.headers.get.return_value = 'qqbot_session=test; HttpOnly'
        opener.open.side_effect = [login, OSError('unavailable'), MagicMock()]
        with patch.object(desktop, 'running', return_value=True), \
                patch.object(desktop, 'ROOT'), \
                patch.object(desktop.urllib.request, 'build_opener', return_value=opener):
            desktop.ROOT.__truediv__.return_value.read_text.return_value = 'test-token'
            with self.assertRaises(OSError):
                desktop.status()
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/logout'))
        self.assertEqual(request.get_header('X-bot-request'), '1')
