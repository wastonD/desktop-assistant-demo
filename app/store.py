# -*- coding: utf-8 -*-
"""本地数据存储：SQLite（data/assistant.db）。目前只有日程表，后续邮件/资讯状态也放这里。"""
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "assistant.db"

REPEAT_KINDS = ("none", "daily", "weekly", "workdays")


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        at TEXT NOT NULL,              -- ISO 时间 YYYY-MM-DD HH:MM
        done INTEGER NOT NULL DEFAULT 0,
        remind_min INTEGER NOT NULL DEFAULT 10,   -- 提前几分钟提醒
        reminded INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )""")
    # 旧库兼容：新列一律用 ALTER TABLE 补上，不重建表
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    if "desc" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN desc TEXT NOT NULL DEFAULT ''")
    if "repeat" not in cols:
        # none/daily/weekly/workdays
        conn.execute("ALTER TABLE events ADD COLUMN repeat TEXT NOT NULL DEFAULT 'none'")
    if "next_created" not in cols:
        # 防止一条重复日程在"完成"和"提醒"都触发时生成两次下一次
        conn.execute("ALTER TABLE events ADD COLUMN next_created INTEGER NOT NULL DEFAULT 0")
    if "reminded_at" not in cols:
        # 只有真正弹过提醒（气泡/通知）才写这个字段，供 /稍后 找"最近一次提醒过的日程"；
        # 过期超 2 小时静默跳过的那种"已提醒"不写它，避免 /稍后 误指到从没弹出过的旧日程
        conn.execute("ALTER TABLE events ADD COLUMN reminded_at TEXT")
    if "snooze_until" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN snooze_until TEXT")
    return conn


def add_event(title: str, at: datetime, remind_min: int = 10, desc: str = "",
             repeat: str = "none") -> int:
    if repeat not in REPEAT_KINDS:
        repeat = "none"
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO events (title, at, remind_min, desc, repeat) VALUES (?, ?, ?, ?, ?)",
            (title, at.strftime("%Y-%m-%d %H:%M"), remind_min, desc, repeat))
        return cur.lastrowid


def all_events() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY at").fetchall()]


def db_mtime() -> float:
    try:
        return DB_PATH.stat().st_mtime
    except OSError:
        return 0.0


def events_on(day: date) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM events WHERE at LIKE ? ORDER BY at",
                         (day.strftime("%Y-%m-%d") + "%",)).fetchall()
        return [dict(r) for r in rows]


def today_events() -> list[dict]:
    return events_on(date.today())


def set_done(event_id: int, done: bool = True) -> None:
    with _conn() as c:
        row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone() if done else None
        c.execute("UPDATE events SET done=? WHERE id=?", (1 if done else 0, event_id))
    if done and row:
        _spawn_next(dict(row))


def delete_event(event_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM events WHERE id=?", (event_id,))


def due_reminders(now: datetime) -> list[dict]:
    """到达提醒时刻（at - remind_min）且未提醒过、未完成的日程；外加到了"稍后提醒"时间点的日程。

    过期超过 2 小时的不再补发（比如重启时翻出昨天的旧日程），直接静默标记已提醒（不算真正提醒过，
    不影响 /稍后、不生成下一次重复——见 _mark_reminded_silent）。
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE done=0 AND (reminded=0 OR snooze_until IS NOT NULL)").fetchall()
    due = []
    for r in rows:
        if r["snooze_until"]:
            snooze_at = datetime.strptime(r["snooze_until"], "%Y-%m-%d %H:%M")
            if snooze_at <= now:
                due.append(dict(r))
                with _conn() as c:
                    c.execute("UPDATE events SET snooze_until=NULL WHERE id=?", (r["id"],))
            continue
        at = datetime.strptime(r["at"], "%Y-%m-%d %H:%M")
        overdue = (now - at).total_seconds()
        if overdue > 2 * 3600:
            _mark_reminded_silent(dict(r), now)
            continue
        if (at - now).total_seconds() <= r["remind_min"] * 60:
            due.append(dict(r))
    return due


def mark_reminded(event_id: int, when: datetime | None = None) -> None:
    """标记真正提醒过（弹了气泡/通知）。记录 reminded_at 供 /稍后 使用，并按 repeat 生成下一次。"""
    when = when or datetime.now()
    with _conn() as c:
        row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        c.execute("UPDATE events SET reminded=1, reminded_at=? WHERE id=?",
                  (when.strftime("%Y-%m-%d %H:%M"), event_id))
    if row:
        _spawn_next(dict(row))


def _mark_reminded_silent(row: dict, now: datetime | None = None) -> None:
    """过期超 2 小时的补发跳过：只标记 reminded，不写 reminded_at（/稍后 不应指向它），
    但重复日程的下一次照常生成，避免因为一次没开机就断掉整个系列。"""
    with _conn() as c:
        c.execute("UPDATE events SET reminded=1 WHERE id=?", (row["id"],))
    # 关机多天后开机：直接跳到还没过期的那一次，不逐次补出一串过期日程（每 30 秒补一条会刷满面板）
    _spawn_next(row, skip_before=(now or datetime.now()) - timedelta(hours=2))


def snooze_last(minutes: int = 10, now: datetime | None = None) -> dict | None:
    """把最近一次真正提醒过的未完成日程，设成 N 分钟后再提醒一次。返回被稍后的日程；没有则 None。"""
    now = now or datetime.now()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM events WHERE done=0 AND reminded_at IS NOT NULL "
            "ORDER BY reminded_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        snooze_at = now + timedelta(minutes=minutes)
        c.execute("UPDATE events SET snooze_until=? WHERE id=?",
                  (snooze_at.strftime("%Y-%m-%d %H:%M"), row["id"]))
    return dict(row)


def _next_occurrence(at: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return at + timedelta(days=1)
    if repeat == "weekly":
        return at + timedelta(weeks=1)
    if repeat == "workdays":
        nxt = at + timedelta(days=1)
        while nxt.weekday() >= 5:  # 5=周六 6=周日
            nxt += timedelta(days=1)
        return nxt
    return at


def _spawn_next(row: dict, skip_before: datetime | None = None) -> None:
    """重复日程完成/提醒后自动生成下一次。next_created 保证同一条只生成一次
    （比如面板里勾选框先勾再取消再勾，或者一条日程既被提醒又被完成，都只会生成一份下一次）。"""
    repeat = row.get("repeat") or "none"
    if repeat not in ("daily", "weekly", "workdays"):
        return
    with _conn() as c:
        cur = c.execute("SELECT next_created FROM events WHERE id=?", (row["id"],)).fetchone()
        if cur is None or cur["next_created"]:
            return
        c.execute("UPDATE events SET next_created=1 WHERE id=?", (row["id"],))
        at = datetime.strptime(row["at"], "%Y-%m-%d %H:%M")
        next_at = _next_occurrence(at, repeat)
        while skip_before is not None and next_at < skip_before:
            next_at = _next_occurrence(next_at, repeat)
        c.execute("INSERT INTO events (title, at, remind_min, desc, repeat) VALUES (?, ?, ?, ?, ?)",
                  (row["title"], next_at.strftime("%Y-%m-%d %H:%M"), row["remind_min"],
                   row.get("desc", ""), repeat))


def snooze_event(event_id: int, minutes: int = 10, now: datetime | None = None) -> None:
    """指定日程 N 分钟后再提醒一次（提醒气泡上的"稍后"按钮）。"""
    at = (now or datetime.now()) + timedelta(minutes=minutes)
    with _conn() as c:
        c.execute("UPDATE events SET snooze_until=? WHERE id=?",
                  (at.strftime("%Y-%m-%d %H:%M"), event_id))


def get_event(event_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None


def update_event(event_id: int, title: str, at: datetime, remind_min: int = 10, desc: str = "",
                 repeat: str = "none") -> None:
    """面板里编辑日程。时间改到了将来就重新允许提醒（reminded 归零）。"""
    if repeat not in REPEAT_KINDS:
        repeat = "none"
    reset = at > datetime.now()
    with _conn() as c:
        c.execute("UPDATE events SET title=?, at=?, remind_min=?, desc=?, repeat=?"
                  + (", reminded=0, reminded_at=NULL, snooze_until=NULL" if reset else "")
                  + " WHERE id=?",
                  (title, at.strftime("%Y-%m-%d %H:%M"), remind_min, desc, repeat, event_id))


def events_between(start: date, end: date) -> list[dict]:
    """[start, end] 闭区间内的日程，按时间排序。"""
    with _conn() as c:
        rows = c.execute("SELECT * FROM events WHERE at >= ? AND at < ? ORDER BY at",
                         (start.strftime("%Y-%m-%d"),
                          (end + timedelta(days=1)).strftime("%Y-%m-%d"))).fetchall()
        return [dict(r) for r in rows]
