"""Complete the config half of a committed restore before loading configuration."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def recover_config(root: Path) -> bool:
    path = root / 'data/memory.sqlite3'
    if not path.exists():
        return False
    db = sqlite3.connect(path)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
            raise ValueError('数据库版本比当前程序新，请使用对应版本，未修改数据')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='restore_journal' AND type='table'").fetchone():
            return False
        row = db.execute('SELECT config FROM restore_journal WHERE id=1').fetchone()
        if not row:
            return False
        from .config import atomic_json, validate, migrate
        candidate = validate(migrate(json.loads(row[0])))
        candidate['paused'] = True
        atomic_json(root / 'config/settings.json', candidate)
        db.execute('DELETE FROM restore_journal WHERE id=1')
        db.commit()
        return True
    finally:
        db.close()
