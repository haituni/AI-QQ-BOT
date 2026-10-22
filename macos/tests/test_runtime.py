"""Installation must reuse the exact pinned image, without depending on registry availability."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('project_runtime', SCRIPTS / 'runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class ImageReuse(unittest.TestCase):
    def test_exact_local_image_skips_registry_request(self):
        with patch.object(runtime, 'lima', return_value=SimpleNamespace(returncode=0)) as lima:
            runtime.ensure_image()
        self.assertEqual(lima.call_count, 1)
        self.assertIn(runtime.IMAGE, lima.call_args.args)
        self.assertIn('inspect', lima.call_args.args)

    def test_missing_image_downloads_pinned_digest(self):
        with patch.object(runtime, 'lima', side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as lima:
            runtime.ensure_image()
        self.assertEqual(lima.call_count, 2)
        self.assertEqual(lima.call_args.args, ('shell', runtime.INSTANCE, 'sudo', 'docker', 'pull', runtime.IMAGE))
