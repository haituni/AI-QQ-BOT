"""Strict, atomic configuration. Models can edit only a small allowlist."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from . import roleplay, toolkit, life

ROOT = Path(__file__).resolve().parent.parent
CHAT_KEY = re.compile(r"^(group|private):[1-9][0-9]{4,14}$")
QQ = re.compile(r"^[1-9][0-9]{4,14}$")


def conversation_defaults() -> dict[str, Any]:
    return {"name": "", "allowed": False, "proactive": False,
            "min_minutes": 60, "max_minutes": 180, "reply_timing": True, "max_reply_delay": 45,
            "start": "09:00", "end": "23:00", "daily_limit": 3,
            "persona": "", "tools_enabled": True, "tool_allowlist": [], "character_id": "", "user_persona_id": "", "preset_id": "", "worldbook_ids": []}


def defaults() -> dict[str, Any]:
    return {"revision": 0, "admin_qq": [], "bot_qq": "", "paused": False,
            "api_key": "", "model": "deepseek-flash",
            "persona": "你是一位温和、有好奇心、说话自然的聊天伙伴。不要每句话都接话，不要刷屏。",
            "history_messages": 40, "daily_model_limit": 500,
            "life": life.defaults(), "conversations": {}, "tools": toolkit.defaults(), "roleplay": roleplay.defaults(), "bridge_token": secrets.token_urlsafe(32),
            "bridge_web_token": secrets.token_urlsafe(32)}


def integer(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low}～{high} 的整数")
    return value


def boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} 必须为 true 或 false")
    return value


def string(value: Any, limit: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} 必须是最多 {limit} 字的文字")
    return value


def validate(cfg: dict[str, Any]) -> dict[str, Any]:
    if set(cfg) != set(defaults()):
        raise ValueError("配置字段不完整或含未知字段")
    integer(cfg["revision"], 0, 10**12, "revision")
    if not isinstance(cfg["admin_qq"], list) or len(cfg["admin_qq"]) > 10:
        raise ValueError("管理 QQ 必须是列表，最多 10 个")
    for qq in cfg["admin_qq"]:
        if not isinstance(qq, str) or not QQ.fullmatch(qq):
            raise ValueError("管理 QQ 格式不正确")
    if cfg["bot_qq"] and not QQ.fullmatch(str(cfg["bot_qq"])):
        raise ValueError("机器人 QQ 格式不正确")
    string(cfg["bot_qq"], 15, "机器人 QQ")
    if cfg["bot_qq"] in cfg["admin_qq"]:
        raise ValueError("机器人 QQ 和管理 QQ 必须不同")
    boolean(cfg["paused"], "暂停")
    string(cfg["api_key"], 300, "API Key")
    if not isinstance(cfg["model"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", cfg["model"]):
        raise ValueError("模型名称格式不正确")
    string(cfg["persona"], 12000, "性格")
    integer(cfg["history_messages"], 4, 100, "历史消息条数")
    integer(cfg["daily_model_limit"], 1, 10000, "每日模型调用上限")
    for key in ("bridge_token", "bridge_web_token"):
        if len(string(cfg[key], 200, key)) < 24:
            raise ValueError("接入密钥过短")
    if not isinstance(cfg["conversations"], dict) or len(cfg["conversations"]) > 300:
        raise ValueError("最多配置 300 个会话")
    for key, conv in cfg["conversations"].items():
        if not isinstance(key, str) or not CHAT_KEY.fullmatch(key):
            raise ValueError("会话格式应为 group:群号 或 private:QQ号")
        if not isinstance(conv, dict) or set(conv) != set(conversation_defaults()):
            raise ValueError("会话含未知字段或缺少字段")
        string(conv["name"], 100, "会话名称")
        string(conv["persona"], 12000, "会话性格")
        boolean(conv["allowed"], "会话授权")
        boolean(conv["proactive"], "主动聊天")
        boolean(conv["reply_timing"], "自然回复节奏")
        integer(conv["max_reply_delay"], 2, 120, "最长回复等待秒数")
        if conv["proactive"] and not conv["allowed"]:
            raise ValueError("主动聊天需要先授权该会话")
        integer(conv["min_minutes"], 1, 10080, "最短间隔")
        integer(conv["max_minutes"], conv["min_minutes"], 10080, "最长间隔")
        integer(conv["daily_limit"], 1, 100, "每日主动消息上限")
        for field in ("start", "end"):
            if not isinstance(conv[field], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", conv[field]):
                raise ValueError("时段格式为 HH:MM")
    life.validate(cfg['life'])
    toolkit.validate(cfg['tools'])
    for conv in cfg['conversations'].values():
        boolean(conv['tools_enabled'], '会话工具开关')
        toolkit.names(conv['tool_allowlist'])
    roleplay.validate(cfg["roleplay"])
    roleplay.bindings(cfg["roleplay"], cfg["conversations"])
    return cfg


def migrate(value: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(value)
    value.setdefault('roleplay', roleplay.defaults())
    value.setdefault('tools', toolkit.defaults())
    if 'life' not in value and value['tools']['enabled']:
        value['tools']['enabled_tools'] = list(dict.fromkeys(value['tools']['enabled_tools'] + list(life.CATALOG)))
    value.setdefault('life', life.defaults())
    for conv in value.get('conversations', {}).values():
        for key in ('character_id', 'user_persona_id', 'preset_id', 'worldbook_ids', 'tools_enabled', 'tool_allowlist', 'reply_timing', 'max_reply_delay'):
            conv.setdefault(key, copy.deepcopy(conversation_defaults()[key]))
    return value


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Windows cannot open directories through os.open; file fsync + replace remain atomic.
        if os.name != 'nt':
            folder = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
    finally:
        tmp.unlink(missing_ok=True)


class Config:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.frozen = False
        from .recovery import recover_config
        recover_config(root)
        self.path = root / "config/settings.json"
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            original = copy.deepcopy(value)
            # Additive migration preserves existing credentials, cards and permissions.
            value = migrate(value)
            self.value = validate(value)
            if original != value:
                backup = self.path.with_name("settings.before-life.json" if "life" not in original else "settings.before-tools.json" if "tools" not in original else "settings.before-character-studio.json")
                if not backup.exists():
                    atomic_json(backup, original)
                atomic_json(self.path, self.value)
        else:
            self.value = defaults()
            seed = root / "initial/roleplay.json"
            if seed.exists():
                self.value["roleplay"] = roleplay.validate(json.loads(seed.read_text(encoding="utf-8")))
        if not self.path.exists():
            atomic_json(self.path, self.value)
        token_path = root / "config/control_token"
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(40))
            token_path.chmod(0o600)
        self.control_token = token_path.read_text().strip()

    def public(self) -> dict[str, Any]:
        result = copy.deepcopy(self.value)
        result["has_api_key"] = bool(result.pop("api_key"))
        result.pop("bridge_token")
        result.pop("bridge_web_token")
        return result

    def update(self, patch: dict[str, Any], *, model: bool = False) -> dict[str, Any]:
        if self.frozen:
            raise ValueError('数据维护期间暂不能修改设置，请稍后重试')
        if not isinstance(patch, dict):
            raise ValueError("配置修改必须为对象")
        permitted = {"paused", "persona", "history_messages", "daily_model_limit", "conversations", "roleplay_selection"}
        if not model:
            permitted |= {"admin_qq", "bot_qq", "api_key", "model", "roleplay", "tools", "life"}
        if set(patch) - permitted:
            raise ValueError("包含不允许修改的配置字段")
        result = copy.deepcopy(self.value)
        for key, value in patch.items():
            if key == "roleplay_selection":
                if not isinstance(value, dict) or set(value) - {"active_character", "active_persona", "active_preset"}:
                    raise ValueError("只能选择已有的角色、用户人设和生成预设")
                result["roleplay"].update(value)
            elif key == "conversations":
                if not isinstance(value, dict):
                    raise ValueError("conversations 必须为对象")
                for chat, changes in value.items():
                    if not isinstance(changes, dict) or set(changes) - set(conversation_defaults()):
                        raise ValueError("会话配置字段不正确")
                    if model and set(changes) & {'tools_enabled', 'tool_allowlist'}:
                        raise ValueError('工具权限只能在本机工具菜单或会话设置中修改')
                    conv = result["conversations"].setdefault(chat, conversation_defaults())
                    conv.update(changes)
                    if not conv["allowed"]:
                        conv["proactive"] = False
            else:
                result[key] = value
        result["revision"] += 1
        validate(result)
        atomic_json(self.path, result)
        self.value = result
        return self.public()

    def conversation(self, key: str) -> dict[str, Any]:
        return self.value["conversations"].get(key, conversation_defaults())

    def allowed(self, key: str) -> bool:
        return not self.value["paused"] and bool(self.conversation(key)["allowed"])
