# -*- coding: utf-8 -*-
"""app/store.py 的单元测试（重点：due_reminders 的提前分钟/超时不补发逻辑）。

运行：python -m unittest discover tests
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app import store


class DueRemindersTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_not_yet_due(self):
        now = datetime(2026, 9, 22, 10, 0)
        store.add_event("组会", now + timedelta(minutes=30), remind_min=10)
        self.assertEqual(store.due_reminders(now), [])

    def test_within_remind_window(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("组会", now + timedelta(minutes=8), remind_min=10)
        due = store.due_reminders(now)
        self.assertEqual([e["id"] for e in due], [eid])

    def test_already_reminded_not_repeated(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("组会", now + timedelta(minutes=5), remind_min=10)
        store.mark_reminded(eid)
        self.assertEqual(store.due_reminders(now), [])

    def test_done_event_not_reminded(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("组会", now + timedelta(minutes=5), remind_min=10)
        store.set_done(eid)
        self.assertEqual(store.due_reminders(now), [])

    def test_overdue_over_two_hours_marked_without_notifying(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("昨天的事", now - timedelta(hours=3), remind_min=10)
        self.assertEqual(store.due_reminders(now), [])
        row = [e for e in store.all_events() if e["id"] == eid][0]
        self.assertEqual(row["reminded"], 1)  # 过期超 2 小时直接标记已提醒，不再补发

    def test_overdue_within_two_hours_still_fires(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("刚过去的事", now - timedelta(minutes=30), remind_min=10)
        due = store.due_reminders(now)
        self.assertEqual([e["id"] for e in due], [eid])

    def test_custom_remind_min_boundary(self):
        now = datetime(2026, 9, 22, 10, 0)
        # 提前 30 分钟提醒，事件在 31 分钟后：还没到窗口
        store.add_event("远一点", now + timedelta(minutes=31), remind_min=30)
        self.assertEqual(store.due_reminders(now), [])
        # 事件在 30 分钟后：正好进入窗口
        eid2 = store.add_event("到点了", now + timedelta(minutes=30), remind_min=30)
        self.assertEqual([e["id"] for e in store.due_reminders(now)], [eid2])


class RepeatTests(unittest.TestCase):
    """重复日程：完成/提醒后自动生成下一次，workdays 跳过周末，同一条只生成一次。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_daily_next_occurrence_on_complete(self):
        eid = store.add_event("晨会", datetime(2026, 9, 22, 9, 0), repeat="daily")
        store.set_done(eid)
        events = store.all_events()
        self.assertEqual(len(events), 2)
        nxt = [e for e in events if e["id"] != eid][0]
        self.assertEqual(nxt["at"], "2026-09-23 09:00")
        self.assertEqual(nxt["repeat"], "daily")
        self.assertEqual(nxt["done"], 0)
        self.assertEqual(nxt["reminded"], 0)

    def test_weekly_next_occurrence_crosses_month_on_remind(self):
        eid = store.add_event("周会", datetime(2026, 9, 28, 10, 0), remind_min=10, repeat="weekly")
        store.mark_reminded(eid, datetime(2026, 9, 28, 9, 50))
        events = store.all_events()
        nxt = [e for e in events if e["id"] != eid][0]
        self.assertEqual(nxt["at"], "2026-10-05 10:00")  # 跨月

    def test_workdays_skips_weekend(self):
        # 2026-09-25 是周五
        eid = store.add_event("站会", datetime(2026, 9, 25, 9, 30), repeat="workdays")
        store.set_done(eid)
        nxt = [e for e in store.all_events() if e["id"] != eid][0]
        self.assertEqual(nxt["at"], "2026-09-28 09:30")  # 跳过周六周日，落到下周一
        self.assertEqual(datetime.strptime(nxt["at"], "%Y-%m-%d %H:%M").weekday(), 0)

    def test_none_repeat_does_not_spawn(self):
        eid = store.add_event("一次性", datetime(2026, 9, 22, 9, 0), repeat="none")
        store.set_done(eid)
        self.assertEqual(len(store.all_events()), 1)

    def test_spawn_only_once_even_if_reminded_and_toggled_done_twice(self):
        eid = store.add_event("既提醒又反复完成", datetime(2026, 9, 22, 9, 0), repeat="daily")
        store.mark_reminded(eid, datetime(2026, 9, 22, 8, 50))
        store.set_done(eid, True)
        store.set_done(eid, False)   # 面板里取消勾选
        store.set_done(eid, True)    # 再勾上
        self.assertEqual(len(store.all_events()), 2)  # 只生成了一份下一次

    def test_catch_up_after_days_off_skips_stale_occurrences(self):
        """关机多天后开机：每天的日程只补生成一条"还没过期"的下一次，不逐次刷出一串过期日程。"""
        now = datetime(2026, 9, 30, 12, 0)  # 当天 09:00 那次也已过去 3 小时
        store.add_event("晨会", datetime(2026, 9, 22, 9, 0), repeat="daily")
        for _ in range(3):  # 模拟几轮 30 秒一次的提醒检查
            store.due_reminders(now)
        pending = [e for e in store.all_events() if not e["reminded"]]
        self.assertEqual([e["at"] for e in pending], ["2026-10-01 09:00"])
        self.assertEqual(len(store.all_events()), 2)

    def test_catch_up_keeps_occurrence_within_two_hours(self):
        """2 小时内刚过去的那一次仍然要生成并补提醒（与一次性日程的规则一致）。"""
        now = datetime(2026, 9, 24, 10, 0)
        store.add_event("晨会", datetime(2026, 9, 22, 9, 0), repeat="daily")
        store.due_reminders(now)
        pending = [e for e in store.all_events() if not e["reminded"]]
        self.assertEqual([e["at"] for e in pending], ["2026-09-24 09:00"])
        self.assertEqual([e["title"] for e in store.due_reminders(now)], ["晨会"])

    def test_bad_repeat_value_falls_back_to_none(self):
        eid = store.add_event("坏值", datetime(2026, 9, 22, 9, 0), repeat="monthly")
        row = [e for e in store.all_events() if e["id"] == eid][0]
        self.assertEqual(row["repeat"], "none")


class SnoozeTests(unittest.TestCase):
    """/稍后：只对"真正弹过提醒"的日程生效，过期静默跳过的不算。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_snooze_fires_again_after_minutes_then_stops(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("组会", now, remind_min=10)
        store.mark_reminded(eid, now)
        ev = store.snooze_last(15, now)
        self.assertEqual(ev["id"], eid)
        self.assertEqual(store.due_reminders(now + timedelta(minutes=10)), [])
        due = store.due_reminders(now + timedelta(minutes=15))
        self.assertEqual([e["id"] for e in due], [eid])
        # 触发一次后 snooze_until 被清掉，不会一直重复弹
        self.assertEqual(store.due_reminders(now + timedelta(minutes=20)), [])

    def test_snooze_default_is_ten_minutes(self):
        now = datetime(2026, 9, 22, 10, 0)
        eid = store.add_event("组会", now, remind_min=10)
        store.mark_reminded(eid, now)
        self.assertIsNotNone(store.snooze_last(now=now))
        self.assertEqual(store.due_reminders(now + timedelta(minutes=9)), [])
        self.assertEqual([e["id"] for e in store.due_reminders(now + timedelta(minutes=10))], [eid])

    def test_snooze_picks_most_recently_reminded(self):
        now = datetime(2026, 9, 22, 10, 0)
        e1 = store.add_event("先提醒的", now, remind_min=10)
        e2 = store.add_event("后提醒的", now, remind_min=10)
        store.mark_reminded(e1, now)
        store.mark_reminded(e2, now + timedelta(minutes=1))
        ev = store.snooze_last(10, now)
        self.assertEqual(ev["id"], e2)

    def test_snooze_with_nothing_reminded_returns_none(self):
        store.add_event("还没提醒过", datetime(2026, 9, 22, 10, 0))
        self.assertIsNone(store.snooze_last())

    def test_silently_expired_event_not_picked_by_snooze(self):
        now = datetime(2026, 9, 22, 10, 0)
        # 超过 2 小时的旧日程会被 due_reminders 静默标记，不算"真正提醒过"
        store.add_event("很久以前的", now - timedelta(hours=3), remind_min=10)
        store.due_reminders(now)
        self.assertIsNone(store.snooze_last(now=now))


class MigrationTests(unittest.TestCase):
    """旧库（没有 repeat/reminded_at/snooze_until/next_created 列）能被自动补齐，不丢数据。"""

    def test_old_schema_gets_new_columns_via_alter_table(self):
        tmp = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(tmp)
        conn.execute("""CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            at TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            remind_min INTEGER NOT NULL DEFAULT 10,
            reminded INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            desc TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("INSERT INTO events (title, at) VALUES (?, ?)", ("旧日程", "2026-09-20 10:00"))
        conn.commit()
        conn.close()
        with mock.patch.object(store, "DB_PATH", tmp):
            events = store.all_events()
            self.assertEqual(len(events), 1)
            e = events[0]
            self.assertEqual(e["title"], "旧日程")  # 老数据原样保留
            self.assertEqual(e["repeat"], "none")
            self.assertEqual(e["next_created"], 0)
            self.assertIsNone(e["reminded_at"])
            self.assertIsNone(e["snooze_until"])
            # 迁移后新功能照常可用
            eid = store.add_event("新日程", datetime(2026, 9, 22, 9, 0), repeat="daily")
            store.set_done(eid)
            self.assertEqual(len(store.all_events()), 3)


if __name__ == "__main__":
    unittest.main()
