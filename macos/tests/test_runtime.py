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


class DockerProvisioning(unittest.TestCase):
    def test_failed_docker_setup_reports_cause_and_stops(self):
        import subprocess
        with patch.object(runtime, 'lima', side_effect=subprocess.CalledProcessError(100, ['sh'])):
            with self.assertRaisesRegex(RuntimeError, 'Docker 安装或启动失败'):
                runtime.ensure_docker()

    def test_retry_replaces_old_blocking_config_without_deleting_disks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root/'lima/qqbot'
            instance.mkdir(parents=True)
            (instance/'disk').write_bytes(b'preserved-disk')
            (instance/'lima.yaml').write_text('provision: old\nprobes: old\n')
            binary = root/'lima-bin/bin/limactl'
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch.object(runtime, 'RUNTIME', root), patch.object(runtime, 'download'), \
                 patch.object(runtime.platform, 'system', return_value='Darwin'), \
                 patch.object(runtime.platform, 'machine', return_value='arm64'), \
                 patch.object(runtime, 'lima'), patch.object(runtime, 'ensure_image') as image, \
                 patch.object(runtime, 'ensure_docker', side_effect=RuntimeError('setup failed')):
                with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                    runtime.install()
            image.assert_not_called()
            self.assertEqual((instance/'disk').read_bytes(), b'preserved-disk')
            self.assertNotIn('provision:', (instance/'lima.yaml').read_text())
            self.assertNotIn('probes:', (instance/'lima.yaml').read_text())

    def test_apt_falls_back_without_disabling_signature_verification(self):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root/'calls'
            # Only direct apt fails; inherited proxy mode succeeds and installs Docker.
            (root/'apt-get').write_text('#!/bin/sh\necho "$*" >> "$CALL_LOG"\n'
                'case "$*" in *Acquire::http::Proxy=DIRECT*) exit 1;; esac\n'
                'case "$*" in *install*) printf "#!/bin/sh\\nexit 0\\n" > "$STUB_BIN/docker"; chmod +x "$STUB_BIN/docker";; esac\n')
            (root/'systemctl').write_text('#!/bin/sh\nexit 0\n')
            for name in ('apt-get', 'systemctl'):
                (root/name).chmod(0o700)
            script = runtime.DOCKER_SETUP.replace('/etc/systemd', str(root/'etc/systemd'))
            env = dict(os.environ, PATH=str(root)+':/usr/bin:/bin', CALL_LOG=str(log), STUB_BIN=str(root))
            result = subprocess.run(['/bin/sh'], input=script, text=True, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 4)
            self.assertIn('Acquire::http::Proxy=DIRECT', calls[0])
            self.assertNotIn('Acquire::http::Proxy=DIRECT', calls[1])
            self.assertIn('APT::Update::Error-Mode=any', calls[1])
            self.assertFalse(any('allow-unauthenticated' in c for c in calls))
            self.assertEqual((root/'etc/systemd/system/docker.service.d/qq-companion-env.conf').read_text(),
                             '[Service]\nEnvironmentFile=-/etc/environment\n')

    def test_image_download_failure_has_bounded_retries(self):
        with patch.object(runtime, 'lima', return_value=SimpleNamespace(returncode=1)) as lima:
            with self.assertRaisesRegex(RuntimeError, '镜像下载失败'):
                runtime.ensure_image()
        self.assertEqual(lima.call_count, 4)  # inspect + three pulls

    def test_start_also_repairs_incomplete_installations(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runtime, 'RUNTIME', Path(tmp)), patch.object(runtime, 'environment'), \
             patch.object(runtime.os, 'chdir'), patch.object(runtime, 'install') as install, \
             patch.object(sys, 'argv', ['runtime.py', 'start']):
            runtime.main()
            install.assert_called_once()
