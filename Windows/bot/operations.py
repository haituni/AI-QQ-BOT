"""Local operations: verified snapshots, safe diagnostics and bounded runtime jobs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import time
from pathlib import Path

from .config import ROOT, Config, atomic_json, validate, migrate
from .store import Store


class Operations:
    def __init__(self, config: Config, store: Store, *, offline: bool):
        self.config, self.store, self.root, self.offline = config, store, config.root, offline
        self.started = time.time()
        self.task: asyncio.Task | None = None
        self.data_busy = ''
        self.data_task: asyncio.Task | None = None
        self.tasks: dict[str, dict] = {}
        self.folder = self.root / 'backups'
        self.folder.mkdir(exist_ok=True, mode=0o700)
        self.job_path = self.root / 'runtime/operation.json'
        try:
            self.job = json.loads(self.job_path.read_text()) if self.job_path.exists() else None
            if self.job is not None and (not isinstance(self.job, dict) or self.job.get('status') not in {'running', 'succeeded', 'failed', 'interrupted'}
                    or self.job.get('command') not in ('install', 'start', 'stop')
                    or not isinstance(self.job.get('started'), (int, float))
                    or not isinstance(self.job.get('lines'), list)
                    or not all(isinstance(line, str) for line in self.job['lines'])):
                raise ValueError('Invalid job metadata')
        except (ValueError, OSError):
            self.job = None
            self.store.audit('runtime_job', '上次环境任务记录无法读取；可重新执行环境操作')
        if self.job and self.job['status'] == 'running':
            self.job.update(status='interrupted', finished=time.time())
            atomic_json(self.job_path, self.job)
        self.integrity = self.store.db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'

    def backups(self) -> list[dict]:
        items = []
        for path in sorted(self.folder.glob('snapshot-*'), reverse=True):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                manifest = json.loads((path / 'manifest.json').read_text())
                items.append({'id': path.name, 'time': manifest['time'], 'reason': manifest['reason'],
                              'bytes': sum((path / name).stat().st_size for name in ('settings.json', 'memory.sqlite3'))})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(items, key=lambda item: item["time"], reverse=True)

    def snapshot(self, reason: str = 'manual') -> dict:
        pages = self.store.db.execute('PRAGMA page_count').fetchone()[0]
        page_bytes = self.store.db.execute('PRAGMA page_size').fetchone()[0]
        if shutil.disk_usage(self.root).free < pages * page_bytes * 2 + 64 * 1024**2:
            raise ValueError('可用磁盘空间不足以创建安全备份，请先释放空间')
        identifier = 'snapshot-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
        path = self.folder / identifier
        path.mkdir(mode=0o700)
        try:
            atomic_json(path / 'settings.json', self.config.value)
            target = sqlite3.connect(path / 'memory.sqlite3')
            try:
                self.store.db.backup(target)
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库备份校验失败')
            finally:
                target.close()
            (path / 'memory.sqlite3').chmod(0o600)
            hashes = {name: self.digest(path / name) for name in ('settings.json', 'memory.sqlite3')}
            atomic_json(path / 'manifest.json', {'version': 1, 'time': time.time(), 'reason': reason, 'hashes': hashes})
        except BaseException:
            shutil.rmtree(path)
            raise
        self.store.audit('backup_created', identifier)
        # Bounded local retention; newest eight complete snapshots only.
        for old in self.backups()[8:]:
            shutil.rmtree(self.folder / old['id'])
        return next(item for item in self.backups() if item['id'] == identifier)

    @staticmethod
    def digest(path: Path) -> str:
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()

    def restore(self, identifier: str) -> None:
        if not isinstance(identifier, str) or not re.fullmatch(r'snapshot-\d{8}-\d{6}-[a-f0-9]{6}', identifier):
            raise ValueError('备份编号无效')
        path = self.folder / identifier
        if path.is_symlink() or not path.is_dir():
            raise ValueError('备份不存在')
        manifest = json.loads((path / 'manifest.json').read_text())
        for name in ('settings.json', 'memory.sqlite3'):
            file = path / name
            if file.is_symlink() or self.digest(file) != manifest.get('hashes', {}).get(name):
                raise ValueError('备份完整性校验失败，未恢复任何数据')
        candidate = validate(migrate(json.loads((path / 'settings.json').read_text())))
        # Current connection credentials and admin access never roll back silently.
        for key in ('admin_qq', 'bot_qq', 'api_key', 'bridge_token', 'bridge_web_token'):
            candidate[key] = copy.deepcopy(self.config.value[key])
        candidate.update(paused=True, revision=self.config.value['revision'] + 1)
        validate(candidate)
        source = sqlite3.connect(f'file:{path / "memory.sqlite3"}?mode=ro', uri=True)
        source.row_factory = sqlite3.Row
        try:
            from .recovery import SCHEMA_VERSION
            if source.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
                raise ValueError('备份来自更高版本程序，请使用对应版本恢复')
            if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('备份数据库损坏')
            messages = [tuple(r) for r in source.execute('SELECT id,chat,role,sender,text,time FROM messages')]
            memory_tables = {}
            names = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('archive','memory_notes','memory_cursor','adaptation','adaptation_state','emotion_state','emotion_control','life_media','life_messages','life_state','life_posts','sticker_vectors'):
                if table in names:
                    old = [r[1] for r in source.execute('PRAGMA table_info('+table+')')]
                    current = [r[1] for r in self.store.db.execute('PRAGMA table_info('+table+')')]
                    if old != current:
                        raise ValueError('备份表结构不兼容，未修改数据：'+table)
                memory_tables[table] = [tuple(r) for r in source.execute('SELECT * FROM '+table)] if table in names else []
            memory_settings = source.execute("SELECT value FROM memory_control WHERE key='settings'").fetchone() if 'memory_control' in names else None
            states = []
            for row in source.execute('SELECT chat,value FROM state'):
                state = json.loads(row['value'])
                state['generation'] = max(state.get('generation', 0), self.store.state(row['chat'])['generation']) + 1
                state['next_at'] = 0
                states.append((row['chat'], json.dumps(state, ensure_ascii=False)))
            # Invalidate conversations absent from the backup as well.
            backed = {r[0] for r in states}
            for row in self.store.db.execute('SELECT chat FROM state').fetchall():
                if row['chat'] not in backed:
                    current = self.store.state(row['chat'])
                    current.update(memory='', generation=current['generation']+1, next_at=0)
                    states.append((row['chat'], json.dumps(current)))
        finally:
            source.close()
        self.snapshot('before_restore')
        try:
            self.store.db.execute('BEGIN IMMEDIATE')
            self.store.db.execute('DELETE FROM messages')
            self.store.db.execute('DELETE FROM state')
            self.store.db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', messages)
            self.store.db.executemany('INSERT INTO state VALUES(?,?)', states)
            for table, rows in memory_tables.items():
                self.store.db.execute('DELETE FROM '+table)
                if rows:
                    self.store.db.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
            if 'archive' not in names:
                from .memory import archive_message
                for row in messages:
                    archive_message(self.store.db,*row[1:])
            self.store.db.execute("DELETE FROM memory_control WHERE key='settings'")
            if memory_settings:
                self.store.db.execute("INSERT INTO memory_control VALUES('settings',?)",(memory_settings[0],))
            from .memory import rebuild_indexes
            rebuild_indexes(self.store.db)
            # Database changes and their corresponding config are one transaction.
            # If power is lost afterwards, Config loads this journal before startup.
            self.store.db.execute('INSERT OR REPLACE INTO restore_journal VALUES(1,?)',
                                  (json.dumps(candidate,ensure_ascii=False),))
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise
        atomic_json(self.config.path, candidate)
        self.store.db.execute('DELETE FROM restore_journal WHERE id=1')
        self.store.db.commit()
        self.config.value = candidate
        self.store.audit('backup_restored', identifier + '；已暂停；保留当前账号、密钥、调用额度与去重记录')

    async def _data_operation(self, action: str, argument: str = ''):
        if self.data_busy or self.store.maintenance:
            raise ValueError('已有数据维护正在执行，请稍后重试')
        if action in {'restore', 'vacuum'} and not self.config.value['paused']:
            raise ValueError('请先暂停机器人，再执行数据维护')
        if action in {'restore', 'vacuum'} and self.task and not self.task.done():
            raise ValueError('请等待 QQ 环境任务完成后再进行数据维护')
        self.data_busy = action
        self.config.frozen = True
        exclusive = action != 'snapshot'
        if exclusive:
            self.store.maintenance = True
        # Independent connections stay entirely inside the worker thread. Never
        # disable SQLite thread checks on the live connection.
        worker = copy.copy(self)
        worker.config = copy.copy(self.config)
        worker.config.value = copy.deepcopy(self.config.value)
        worker.store = copy.copy(self.store)
        def run():
            db = sqlite3.connect(self.store.path, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA secure_delete=ON')
            db.execute('PRAGMA synchronous=FULL')
            worker.store.db = db
            try:
                if action == 'snapshot':
                    return worker.snapshot(argument), None
                if action == 'restore':
                    worker.restore(argument)
                    return None, worker.config.value
                if action == 'vacuum':
                    db.execute('VACUUM')
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute("DELETE FROM memory_control WHERE key='vacuum_pending'")
                    db.commit()
                    return None, None
                raise ValueError('不支持的数据维护')
            finally:
                db.close()
        self.data_task = asyncio.create_task(asyncio.to_thread(run))
        cancelled = False
        try:
            # Cancelling the HTTP handler must not unlock an ongoing disk operation.
            while True:
                try:
                    result, config = await asyncio.shield(self.data_task)
                    break
                except asyncio.CancelledError:
                    if self.data_task.cancelled():
                        raise RuntimeError('数据维护工作任务意外取消')
                    cancelled = True
            if config is not None:
                self.config.value = config
            if action == 'vacuum':
                self.store.vacuum_pending = False
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            pending = bool(self.store.db.execute('SELECT 1 FROM restore_journal WHERE id=1').fetchone())
            self.store.maintenance = pending
            self.config.frozen = pending
            self.data_busy = ''

    async def snapshot_async(self, reason: str = 'manual') -> dict:
        return await self._data_operation('snapshot', reason)

    async def restore_async(self, identifier: str) -> None:
        if getattr(self, 'services', None) and not self.idle(*self.services):
            raise ValueError('仍有请求正在结束，请稍后再恢复')
        await self._data_operation('restore', identifier)

    def storage(self) -> dict:
        sizes = {name: path.stat().st_size if path.exists() else 0
                 for name,path in [('database',self.store.path),('wal',Path(str(self.store.path)+'-wal'))]}
        sizes['total'] = sum(sizes.values())
        return sizes

    def idle(self, engine, ai, bridge) -> bool:
        return (not ai.active and not bridge.pending and not ai.tools.active
                and not any(not task.done() for task in engine.workers.values())
                and not any(not task.done() for task in ai.memory.tasks.values())
                and not any(lock.locked() for lock in engine.locks.values()))

    def status(self, bridge, ai, engine) -> dict:
        cfg = self.config.value
        disk = shutil.disk_usage(self.root)
        checks = [
            ('account', '账号设置', bool(cfg['admin_qq'] and cfg['bot_qq']), '填写不同的管理 QQ 与机器人 QQ', 'settings'),
            ('model', '模型密钥', bool(cfg['api_key']), '保存 DeepSeek 密钥，再手动测试连接', 'settings'),
            ('bridge', 'QQ 登录', bool(bridge.online and bridge.connected), '启动 QQ 接入，打开登录页扫码', 'settings'),
            ('identity', '登录账号核对', bool(bridge.self_id and bridge.self_id == cfg['bot_qq']), '确认扫码账号与配置的机器人 QQ 一致', 'settings'),
            ('authorization', '会话授权', any(c['allowed'] for c in cfg['conversations'].values()), '添加会话并显式开启授权', 'conversations'),
        ]
        from .ai import day_now
        blockers = []
        if self.offline: blockers.append('当前为离线验证环境')
        if cfg['paused']: blockers.append('机器人已暂停')
        if not self.integrity: blockers.append('数据库检查未通过，请查看运行与数据')
        if self.store.used(day_now()) >= cfg['daily_model_limit']: blockers.append('今日模型调用已达上限')
        if time.time() < ai.retry_at: blockers.append('模型连接暂时保护中')
        if self.store.maintenance: blockers.append('数据维护中；如恢复中断，请重启服务完成配置恢复')
        if any(task['state'] != 'running' for task in self.tasks.values()):
            blockers.append('后台服务正在恢复，请查看运行与数据')
        storage = self.storage()
        warnings = []
        if disk.free < 1024**3: warnings.append('磁盘剩余不足 1 GB，请尽快释放空间')
        if storage['total'] >= 1024**3: warnings.append('记忆数据库超过 1 GB，请检查归档容量与备份空间')
        ready = all(item[2] for item in checks) and not blockers
        return {'ready': ready, 'uptime': time.time()-self.started, 'integrity': self.integrity,
                'blockers': blockers,
                'free_bytes': disk.free, 'checks': [{'id': i, 'title': t, 'ok': ok, 'hint': hint, 'page': page} for i,t,ok,hint,page in checks],
                'model': {'active': ai.active, 'last_success': ai.last_success, 'last_error': ai.last_error, 'retry_at': ai.retry_at,
                          'requests': ai.queue.active, 'waiting': ai.queue.waiting},
                'tasks': self.tasks, 'storage': storage, 'warnings': warnings, 'data_busy': self.data_busy,
                'errors': [dict(row) for row in self.store.db.execute('SELECT * FROM error_traces ORDER BY time DESC LIMIT 5')],
                'queue': sum(q.qsize() for q in engine.queues.values()),
                'workers': sum(not t.done() for t in engine.workers.values()),
                'metrics': self.store.metrics(time.time()-86400),
                'deliveries': [dict(r) for r in self.store.db.execute('SELECT * FROM deliveries ORDER BY id DESC LIMIT 50')],
                'job': self.job, 'backups': self.backups(),
                'runtime_installed': (self.root / 'runtime/napcat/installed').is_dir()}

    def begin(self, command: str) -> dict:
        if not isinstance(command, str) or command not in {'install', 'start', 'stop'}:
            raise ValueError('不支持的服务操作')
        if self.offline or self.root.resolve() != ROOT.resolve():
            raise ValueError('离线验证环境禁止操作真实 QQ 服务')
        if self.task and not self.task.done():
            raise ValueError('已有环境任务正在运行，请等待完成')
        if command == 'start' and not self.config.value['bot_qq']:
            raise ValueError('请先保存机器人 QQ 号')
        self.job = {'id': secrets.token_hex(8), 'command': command, 'status': 'running', 'started': time.time(), 'lines': []}
        atomic_json(self.job_path, self.job)
        self.task = asyncio.create_task(self.run(command))
        return self.job

    def redact(self, line: str) -> str:
        line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
        for key in ('api_key', 'bridge_token', 'bridge_web_token'):
            value = self.config.value[key]
            if value:
                line = line.replace(value, '[密钥已隐藏]')
        line = re.sub(r'(?i)(bearer\s+|token[=: ]+)[^\s,;]+', r'\1[已隐藏]', line)
        return line[:1000]

    async def run(self, command: str) -> None:
        process = None
        try:
            env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONPYCACHEPREFIX=str(self.root/'runtime/cache/pycache'), TMPDIR=str(self.root/'runtime/tmp'))
            process = await asyncio.create_subprocess_exec(__import__('sys').executable, str(ROOT/'scripts/runtime.py'), command,
                cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != 'nt', limit=2**20,
                **({'creationflags': __import__('subprocess').CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            async with asyncio.timeout(1800):
                async for raw in process.stdout:
                    self.job['lines'] = (self.job['lines'] + [self.redact(raw.decode('utf-8', errors='replace').rstrip())])[-80:]
                    atomic_json(self.job_path, self.job)
                code = await process.wait()
            self.job.update(status='succeeded' if code == 0 else 'failed', exit_code=code)
        except asyncio.CancelledError:
            self.job['status'] = 'interrupted'
            raise
        except Exception as error:
            self.job.update(status='failed', lines=self.job['lines'] + ['环境任务未完成：' + type(error).__name__])
        finally:
            if process and process.returncode is None:
                try:
                    process.terminate() if os.name == 'nt' else os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 5)
                except (ProcessLookupError, TimeoutError):
                    if process.returncode is None:
                        process.kill() if os.name == 'nt' else os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
            self.job['finished'] = time.time()
            atomic_json(self.job_path, self.job)
            self.store.audit('runtime_job', command + ': ' + self.job['status'])

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.data_task and not self.data_task.done():
            await asyncio.shield(self.data_task)

    async def maintain(self) -> None:
        while True:
            await asyncio.sleep(3600)
            snapshots = self.backups()
            if not snapshots or time.time() - snapshots[0]['time'] >= 86400:
                try:
                    await self.snapshot_async('daily')
                except Exception as error:
                    self.store.failure('backup_error', error)
            engine, ai, bridge = getattr(self, 'services', (None, None, None))
            if (engine and self.store.vacuum_pending and self.config.value['paused']
                    and not self.data_busy and self.idle(engine, ai, bridge)):
                try:
                    await self._data_operation('vacuum')
                except Exception as error:
                    self.store.failure('maintenance_error', error)
