# -*- coding: utf-8 -*-
"""app/agenda.py 与 store 新增的 update_event/events_between。"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from app import agenda, store

NOW = datetime(2026, 9, 23, 10, 0)


class AgendaTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_next_event_and_countdown(self):
        store.add_event("早会", datetime(2026, 9, 23, 9, 0))
        eid = store.add_event("组会", datetime(2026, 9, 23, 11, 30))
        e = agenda.next_event(NOW)
        self.assertEqual(e["id"], eid)
        self.assertEqual(agenda.countdown(e, NOW), "还有 1 小时 30 分")

    def test_overdue_excludes_done_and_repeat(self):
        a = store.add_event("交表", datetime(2026, 9, 22, 9, 0))
        b = store.add_event("已做", datetime(2026, 9, 22, 9, 0))
        store.set_done(b)
        store.add_event("打卡", datetime(2026, 9, 22, 9, 0), repeat="daily")
        self.assertEqual([e["id"] for e in agenda.overdue(NOW)], [a])

    def test_free_slots(self):
        store.add_event("组会", datetime(2026, 9, 23, 14, 0))
        slots = agenda.free_slots(date(2026, 9, 23), NOW)
        self.assertEqual(slots, [(datetime(2026, 9, 23, 10, 0), datetime(2026, 9, 23, 14, 0)),
                                 (datetime(2026, 9, 23, 15, 0), datetime(2026, 9, 23, 22, 0))])

    def test_brief(self):
        self.assertEqual(agenda.brief(NOW), "今天没有安排。")
        store.add_event("组会", datetime(2026, 9, 23, 14, 0))
        self.assertIn("下一个是 14:00 组会", agenda.brief(NOW))

    def test_update_event_resets_reminder_when_moved_to_future(self):
        eid = store.add_event("组会", datetime(2026, 9, 23, 9, 0))
        store.mark_reminded(eid)
        future = datetime.now().replace(microsecond=0).replace(year=datetime.now().year + 1)
        store.update_event(eid, "组会改期", future, 15, "带电脑", "none")
        e = store.get_event(eid)
        self.assertEqual((e["title"], e["reminded"], e["remind_min"], e["desc"]),
                         ("组会改期", 0, 15, "带电脑"))

    def test_events_between(self):
        store.add_event("a", datetime(2026, 9, 21, 9, 0))
        store.add_event("b", datetime(2026, 9, 23, 23, 59))
        store.add_event("c", datetime(2026, 9, 24, 0, 0))
        got = [e["title"] for e in store.events_between(date(2026, 9, 22), date(2026, 9, 23))]
        self.assertEqual(got, ["b"])

    def test_plan_text(self):
        store.add_event("组会", datetime(2026, 9, 23, 14, 0))
        text = agenda.plan_text(now=NOW)
        self.assertIn("今天 · 9月23日 周三", text)
        self.assertIn("空闲：10:00-14:00、15:00-22:00", text)


if __name__ == "__main__":
    unittest.main()
