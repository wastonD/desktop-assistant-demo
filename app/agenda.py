# -*- coding: utf-8 -*-
"""日程规划的小算盘（纯逻辑）：下一个日程、逾期未完成、空闲时段、一句话概览。

面板、问候气泡、/今天 指令共用。
"""
from datetime import date, datetime, timedelta

from . import store

WEEKDAYS = "一二三四五六日"
DAY_START, DAY_END = 8, 22       # 算空闲时段的范围
DEFAULT_BLOCK_MIN = 60           # 日程没有结束时间，按占用 1 小时估算


def _at(e: dict) -> datetime:
    return datetime.strptime(e["at"], "%Y-%m-%d %H:%M")


def day_label(d: date, today: date | None = None) -> str:
    today = today or date.today()
    rel = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get((d - today).days)
    base = f"{d.month}月{d.day}日 周{WEEKDAYS[d.weekday()]}"
    return f"{rel} · {base}" if rel else base


def next_event(now: datetime | None = None) -> dict | None:
    """今天之内、还没完成、还没开始（或开始不到 30 分钟）的第一个日程。"""
    now = now or datetime.now()
    for e in store.events_on(now.date()):
        if not e["done"] and _at(e) >= now - timedelta(minutes=30):
            return e
    return None


def countdown(e: dict, now: datetime | None = None) -> str:
    now = now or datetime.now()
    mins = round((_at(e) - now).total_seconds() / 60)
    if mins <= 0:
        return "进行中" if mins > -30 else "已开始"
    if mins < 60:
        return f"还有 {mins} 分钟"
    h, m = divmod(mins, 60)
    return f"还有 {h} 小时" + (f" {m} 分" if m else "")


def overdue(now: datetime | None = None, days: int = 7) -> list[dict]:
    """之前几天（不含今天）没勾掉的（不含重复日程：重复的会自动滚到下一次）。
    今天过了点的不算在这里——它们就在今天的列表里。"""
    now = now or datetime.now()
    rows = store.events_between(now.date() - timedelta(days=days), now.date() - timedelta(days=1))
    return [e for e in rows if not e["done"] and e.get("repeat", "none") == "none"]


def free_slots(day: date, now: datetime | None = None, min_minutes: int = 60) -> list[tuple]:
    """当天 8:00~22:00 里没被日程占用、且不短于 min_minutes 的空档 [(开始, 结束)]。"""
    now = now or datetime.now()
    start = datetime.combine(day, datetime.min.time()).replace(hour=DAY_START)
    end = start.replace(hour=DAY_END)
    if day == now.date():
        start = max(start, now.replace(second=0, microsecond=0))
    busy = sorted((_at(e), _at(e) + timedelta(minutes=DEFAULT_BLOCK_MIN))
                  for e in store.events_on(day) if not e["done"])
    slots, cur = [], start
    for b0, b1 in busy:
        if b0 > cur and (b0 - cur).total_seconds() >= min_minutes * 60:
            slots.append((cur, min(b0, end)))
        cur = max(cur, b1)
        if cur >= end:
            break
    if end > cur and (end - cur).total_seconds() >= min_minutes * 60:
        slots.append((cur, end))
    return [s for s in slots if s[1] > s[0]]


def brief(now: datetime | None = None) -> str:
    """一句话今日概览，给问候气泡用。"""
    now = now or datetime.now()
    all_today = store.events_on(now.date())
    todays = [e for e in all_today if not e["done"]]
    later = [e for e in todays if _at(e) >= now]
    parts = []
    if not all_today:
        parts.append("今天没有安排")
    elif not todays:
        parts.append(f"今天的 {len(all_today)} 件事都做完了")
    elif later:
        nxt = later[0]
        parts.append(f"今天还有 {len(later)} 件事，下一个是 {nxt['at'][11:16]} {nxt['title']}")
    else:
        parts.append(f"今天的 {len(todays)} 件事时间都过了，记得勾掉")
    od = overdue(now)
    if od:
        parts.append(f"另外有 {len(od)} 件逾期没完成")
    return "，".join(parts) + "。"


def plan_text(day: date | None = None, now: datetime | None = None) -> str:
    """/今天 指令：当天安排 + 空闲时段。"""
    now = now or datetime.now()
    day = day or now.date()
    lines = [day_label(day, now.date())]
    events = store.events_on(day)
    if events:
        for e in events:
            mark = "√" if e["done"] else "·"
            lines.append(f"{mark} {e['at'][11:16]} {e['title']}")
    else:
        lines.append("没有日程")
    slots = free_slots(day, now)
    if slots:
        lines.append("空闲：" + "、".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in slots))
    od = overdue(now) if day == now.date() else []
    if od:
        lines.append("逾期未完成：" + "、".join(f"#{e['id']} {e['title']}" for e in od))
    return "\n".join(lines)
