"""Windows backend must own its containers and reuse pinned images."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('project_runtime', SCRIPTS / 'runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

class DockerRuntime(unittest.TestCase):
    def test_refuse_container_without_ownership(self):
        info = {'Config': {'Labels': {}}}
        with patch.object(runtime, 'docker', return_value=SimpleNamespace(returncode=0, stdout=json.dumps([info]))):
            with self.assertRaises(RuntimeError):
                runtime.inspect()

    def test_missing_container_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'BASE', Path(tmp)/'napcat'), \
             patch.object(runtime, 'environment'), patch.object(runtime, 'inspect', return_value=None), \
             patch.object(runtime, 'docker') as docker, patch.object(sys, 'argv', ['runtime.py', 'stop']):
            runtime.main()
            docker.assert_not_called()

    def test_stop_before_docker_install_needs_no_docker(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'BASE', Path(tmp)/'napcat'), \
             patch.object(runtime, 'environment'), patch.object(runtime, 'docker') as docker, \
             patch.object(sys, 'argv', ['runtime.py', 'stop']):
            runtime.main()
            docker.assert_not_called()

    def test_exact_local_image_skips_registry(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'BASE', Path(tmp)/'napcat'), \
             patch.object(runtime, 'environment'), patch.object(runtime, 'Config') as cfg, \
             patch.object(runtime, 'docker', return_value=SimpleNamespace(returncode=0, stdout='linux\n')) as docker, \
             patch.object(sys, 'argv', ['runtime.py', 'install']):
            cfg.return_value.value = {'bot_qq': ''}
            runtime.main()
            self.assertFalse(any(call.args[0] == 'pull' for call in docker.call_args_list))
            self.assertTrue((Path(tmp)/'napcat/installed').is_dir())

    def test_reuse_matching_container_preserves_login(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(runtime, 'BASE', Path(tmp)), \
             patch.object(runtime, 'Config') as cfg, patch.object(runtime, 'inspect') as inspect, \
             patch.object(runtime, 'docker') as docker:
            cfg.return_value.value = {'bot_qq': '123456789', 'bridge_token': 'test', 'bridge_web_token': 'test-web'}
            inspect.return_value = None
            runtime.deploy()
            self.assertTrue(any(c.args[0] == 'run' for c in docker.call_args_list))
            docker.reset_mock()
            inspect.return_value = {'Config': {'Image': runtime.IMAGE, 'Env': ['ACCOUNT=123456789']}, 'State': {'Running': True}}
            runtime.deploy()
            docker.assert_not_called()

    def test_pull_output_is_streamed_and_timeout_is_bounded(self):
        with patch.object(runtime.shutil, 'which', return_value='docker.exe'), \
             patch.object(runtime.subprocess, 'run') as run:
            runtime.docker('pull', runtime.IMAGE, check=False, stream=True)
        self.assertFalse(run.call_args.kwargs['capture_output'])
        self.assertEqual(run.call_args.kwargs['timeout'], 1200)

    def test_failed_pull_retries_and_preserves_install_completion_rule(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'BASE', Path(tmp)/'napcat'), \
             patch.object(runtime, 'environment'), patch.object(runtime, 'Config') as cfg, \
             patch.object(runtime, 'docker', side_effect=[SimpleNamespace(stdout='linux\n', returncode=0),
                 SimpleNamespace(returncode=1), *[SimpleNamespace(returncode=1)]*3]) as docker, \
             patch.object(sys, 'argv', ['runtime.py', 'install']):
            cfg.return_value.value = {'bot_qq': ''}
            with self.assertRaisesRegex(RuntimeError, 'Docker Desktop 的代理'):
                runtime.main()
            pulls = [c for c in docker.call_args_list if c.args[0] == 'pull']
            self.assertEqual(len(pulls), 3)
            self.assertTrue(all(c.kwargs.get('stream') for c in pulls))
            self.assertFalse((Path(tmp)/'napcat/installed').exists())

    def test_transient_pull_failure_can_complete_installation(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'BASE', Path(tmp)/'napcat'), \
             patch.object(runtime, 'environment'), patch.object(runtime, 'Config') as cfg, \
             patch.object(runtime, 'docker', side_effect=[SimpleNamespace(stdout='linux\n', returncode=0),
                 SimpleNamespace(returncode=1), SimpleNamespace(returncode=1),
                 SimpleNamespace(returncode=0)]) as docker, \
             patch.object(sys, 'argv', ['runtime.py', 'install']):
            cfg.return_value.value = {'bot_qq': ''}
            runtime.main()
            self.assertEqual(len([c for c in docker.call_args_list if c.args[0] == 'pull']), 2)
            self.assertTrue((Path(tmp)/'napcat/installed').is_dir())
