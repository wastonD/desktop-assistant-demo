# -*- coding: utf-8 -*-
"""对话历史持久化：SQLite chat_history 表（data/assistant.db，复用 store.py 的库）。

只存自由聊天（用户输入 / 模型回复）；/ 指令的问答不经过这里——
保持"/ 指令不进模型上下文"的既有行为（app/commands.py 的功能分界）。
"""
import sqlite3

from .store import DB_PATH


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,              -- user / assistant
        content TEXT NOT NULL,
        created TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )""")
    return conn


def append(role: str, content: str) -> int:
    with _conn() as c:
        cur = c.execute("INSERT INTO chat_history (role, content) VALUES (?, ?)", (role, content))
        return cur.lastrowid


def recent(limit: int = 20) -> list[dict]:
    """最近 limit 条，按时间正序（老→新）返回，可直接显示或喂给模型。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def clear() -> None:
    with _conn() as c:
        c.execute("DELETE FROM chat_history")
