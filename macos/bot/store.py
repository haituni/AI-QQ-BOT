"""SQLite history, durable scheduler state, event deduplication and audit trail."""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
import secrets
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: Path):
        folder = root / "data"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = folder / 'memory.sqlite3'
        self.maintenance = False
        self.vacuum_pending = False
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        from .recovery import SCHEMA_VERSION
        if self.db.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
            self.db.close()
            raise ValueError('数据库版本比当前程序新，未修改数据')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages(
          id INTEGER PRIMARY KEY, chat TEXT NOT NULL, role TEXT NOT NULL,
          sender TEXT NOT NULL, text TEXT NOT NULL, time REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat,id);
        CREATE TABLE IF NOT EXISTS state(chat TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, time REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, time REAL, kind TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, chat TEXT, time REAL, updated REAL,
          status TEXT, kind TEXT);
        CREATE TABLE IF NOT EXISTS model_metrics(id INTEGER PRIMARY KEY, time REAL, ok INTEGER,
          elapsed REAL, input_tokens INTEGER, output_tokens INTEGER);
        CREATE INDEX IF NOT EXISTS model_metrics_time ON model_metrics(time);
        CREATE TABLE IF NOT EXISTS tool_usage(day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tool_runs(id INTEGER PRIMARY KEY, time REAL, chat TEXT, name TEXT,
          status TEXT, elapsed REAL, detail TEXT);
        CREATE TABLE IF NOT EXISTS restore_journal(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS error_traces(id TEXT PRIMARY KEY,time REAL,kind TEXT,trace TEXT);
        """)
        from .life import init_store as init_life
        init_life(self.db)
        from .emotion import init_store as init_emotion
        init_emotion(self.db)
        from .memory import init_store
        init_store(self.db)
        from .usage import init_usage
        init_usage(self.db)
        self.vacuum_pending = bool(self.db.execute("SELECT 1 FROM memory_control WHERE key='vacuum_pending'").fetchone())
        self.db.commit()
        self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        # A crash after submitting a send can never prove whether QQ accepted it.
        self.db.execute("UPDATE deliveries SET status='unknown',updated=? WHERE status='pending'", (time.time(),))
        self.db.execute("UPDATE tool_runs SET status='interrupted',detail='上次服务退出时未完成' WHERE status='running'")
        self.db.commit()
        (folder / "memory.sqlite3").chmod(0o600)

    def once(self, identifier: str) -> bool:
        cursor = self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (identifier, time.time()))
        inserted = cursor.rowcount == 1
        self.db.execute("DELETE FROM seen WHERE time < ?", (time.time() - 7 * 86400,))
        self.db.commit()
        return inserted

    def add(self, chat: str, role: str, sender: str, text: str) -> None:
        from .memory import archive_message
        stamp = time.time()
        self.db.execute("INSERT INTO messages(chat,role,sender,text,time) VALUES(?,?,?,?,?)",
                        (chat, role, sender, text[:8000], stamp))
        archive_message(self.db, chat, role, sender, text[:8000], stamp)
        self.db.execute("DELETE FROM messages WHERE chat=? AND id NOT IN (SELECT id FROM messages WHERE chat=? ORDER BY id DESC LIMIT 2000)", (chat, chat))
        self.db.commit()

    def history(self, chat: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM messages WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def state(self, chat: str) -> dict[str, Any]:
        row = self.db.execute("SELECT value FROM state WHERE chat=?", (chat,)).fetchone()
        return json.loads(row[0]) if row else {"memory": "", "generation": 0, "next_at": 0,
                                               "last_sent": 0, "day": "", "count": 0}

    def save_state(self, chat: str, state: dict[str, Any]) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (chat, json.dumps(state, ensure_ascii=False)))
        self.db.commit()

    def clear(self, chat: str) -> None:
        state = self.state(chat)
        state.update(memory="", manual_memory="", generation=state.get("generation", 0) + 1)
        subjects = [r[0] for r in self.db.execute("SELECT DISTINCT sender FROM archive WHERE chat=? AND role='user'", (chat,))]
        if chat.startswith("private:"):
            subjects.append(chat.split(":", 1)[1])
        self.db.executemany("DELETE FROM emotion_state WHERE subject=?", [(subject,) for subject in subjects])
        from .memory import clear_chat
        clear_chat(self.db, chat)
        self.db.execute("DELETE FROM messages WHERE chat=?", (chat,))
        self.db.execute("DELETE FROM life_media WHERE chat=? AND saved=0", (chat,))
        self.db.execute("DELETE FROM life_messages WHERE chat=?", (chat,))
        self.save_state(chat, state)
        # A shared QQ may also have an in-flight decision in another conversation.
        for row in self.db.execute("SELECT chat FROM state WHERE chat<>?", (chat,)).fetchall():
            other = self.state(row[0])
            other["generation"] += 1
            self.save_state(row[0], other)
        # secure_delete already clears deleted payloads. Reclaim disk during idle
        # maintenance rather than copying the entire database on the message loop.
        self.vacuum_pending = True
        self.db.execute("INSERT OR REPLACE INTO memory_control VALUES('vacuum_pending','true')")
        self.db.commit()

    def failure(self, kind: str, error: BaseException, *, identifier: str = '') -> str:
        """Keep stack locations, never exception messages, locals or source lines."""
        identifier = identifier or secrets.token_hex(6)
        frames = traceback.extract_tb(error.__traceback__)[-24:]
        trace = '\n'.join(f'{Path(frame.filename).name}:{frame.lineno} in {frame.name}' for frame in frames)
        trace = type(error).__name__ + '\n' + trace
        self.db.execute('INSERT INTO error_traces VALUES(?,?,?,?)', (identifier,time.time(),kind,trace))
        self.db.execute('DELETE FROM error_traces WHERE id NOT IN (SELECT id FROM error_traces ORDER BY time DESC LIMIT 200)')
        self.audit(kind, f'{identifier}: {type(error).__name__}')
        return identifier

    def reserve_call(self, day: str, maximum: int) -> bool:
        row = self.db.execute("SELECT calls FROM usage WHERE day=?", (day,)).fetchone()
        if row and row[0] >= maximum:
            return False
        self.db.execute("INSERT INTO usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
        self.db.commit()
        return True

    def used(self, day: str) -> int:
        row = self.db.execute("SELECT calls FROM usage WHERE day=?", (day,)).fetchone()
        return row[0] if row else 0

    def audit(self, kind: str, detail: str) -> None:
        # No raw message contents, API keys or model output in the audit trail.
        self.db.execute("INSERT INTO audit(time,kind,detail) VALUES(?,?,?)", (time.time(), kind, detail[:500]))
        self.db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 500)")
        self.db.commit()

    def audits(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 60")]

    def delivery(self, chat: str, kind: str) -> int:
        now = time.time()
        cursor = self.db.execute("INSERT INTO deliveries(chat,time,updated,status,kind) VALUES(?,?,?,'pending',?)", (chat, now, now, kind))
        self.db.execute("DELETE FROM deliveries WHERE id NOT IN (SELECT id FROM deliveries ORDER BY id DESC LIMIT 3000)")
        self.db.commit()
        return cursor.lastrowid

    def receipt(self, identifier: int, status: str) -> None:
        self.db.execute("UPDATE deliveries SET status=?,updated=? WHERE id=?", (status, time.time(), identifier))
        self.db.commit()

    def model_metric(self, ok: bool, elapsed: float, usage: dict, *, model: str = "未知", purpose: str = "chat", chat: str = "") -> None:
        from .usage import record
        record(self.db, usage, model, purpose, chat=chat)
        tokens = [usage.get(key, 0) for key in ('prompt_tokens', 'completion_tokens')]
        tokens = [v if type(v) is int and 0 <= v <= 10**9 else 0 for v in tokens]
        self.db.execute("INSERT INTO model_metrics(time,ok,elapsed,input_tokens,output_tokens) VALUES(?,?,?,?,?)", (time.time(), int(ok), elapsed, *tokens))
        self.db.execute("DELETE FROM model_metrics WHERE time<?", (time.time() - 30 * 86400,))
        self.db.commit()

    def metrics(self, since: float) -> dict:
        row = self.db.execute("SELECT count(*) total,coalesce(sum(ok),0) success,coalesce(avg(elapsed),0) latency,coalesce(sum(input_tokens),0) input_tokens,coalesce(sum(output_tokens),0) output_tokens FROM model_metrics WHERE time>=?", (since,)).fetchone()
        return dict(row)

    def message_page(self, chat: str, before: int = 0, query: str = '') -> dict:
        rows = self.db.execute("SELECT * FROM messages WHERE chat=? AND (?=0 OR id<?) AND instr(text,?)>0 ORDER BY id DESC LIMIT 51", (chat, before, before, query)).fetchall()
        return {'messages': [dict(r) for r in reversed(rows[:50])], 'more': len(rows) > 50,
                'before': rows[49]['id'] if len(rows) > 50 else 0}

    def activity(self, kind: str = '', before: int = 0) -> dict:
        rows = self.db.execute("SELECT * FROM audit WHERE (?='' OR kind=?) AND (?=0 OR id<?) ORDER BY id DESC LIMIT 51", (kind, kind, before, before)).fetchall()
        return {'items': [dict(r) for r in rows[:50]], 'more': len(rows) > 50,
                'before': rows[49]['id'] if len(rows) > 50 else 0}

    def reserve_tool(self, day: str, maximum: int) -> bool:
        if self.tool_used(day) >= maximum:
            return False
        self.db.execute('INSERT INTO tool_usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1', (day,))
        self.db.commit()
        return True

    def tool_used(self, day: str) -> int:
        row = self.db.execute('SELECT calls FROM tool_usage WHERE day=?', (day,)).fetchone()
        return row[0] if row else 0

    def tool_begin(self, chat: str, name: str) -> int:
        cursor = self.db.execute("INSERT INTO tool_runs(time,chat,name,status,elapsed,detail) VALUES(?,?,?,'running',0,'')", (time.time(), chat, name))
        self.db.execute('DELETE FROM tool_runs WHERE id NOT IN (SELECT id FROM tool_runs ORDER BY id DESC LIMIT 1000)')
        self.db.commit()
        return cursor.lastrowid

    def tool_finish(self, identifier: int, status: str, elapsed: float, detail: str) -> None:
        self.db.execute('UPDATE tool_runs SET status=?,elapsed=?,detail=? WHERE id=?', (status, elapsed, detail[:200], identifier))
        self.db.commit()

    def tool_runs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('SELECT * FROM tool_runs ORDER BY id DESC LIMIT 40')]
