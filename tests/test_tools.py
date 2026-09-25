# -*- coding: utf-8 -*-
"""app/tools.py 大模型工具调用层的单测（本次只补 add_event 的 repeat 参数）。

运行：python -m unittest discover tests
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from app import store, tools, wallpaper


class AddEventRepeatTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(wallpaper, "apply")
        p2.start()
        self.addCleanup(p2.stop)

    def _tomorrow(self) -> str:
        return (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    def test_add_event_with_daily_repeat(self):
        result = tools.execute("add_event", {
            "title": "晨会", "date": self._tomorrow(), "time": "09:00", "repeat": "daily"})
        self.assertIn("daily 重复", result)
        self.assertEqual(store.all_events()[0]["repeat"], "daily")

    def test_add_event_default_repeat_is_none(self):
        tools.execute("add_event", {"title": "一次性", "date": self._tomorrow(), "time": "09:00"})
        self.assertEqual(store.all_events()[0]["repeat"], "none")

    def test_add_event_invalid_repeat_falls_back_to_none(self):
        tools.execute("add_event", {
            "title": "坏值", "date": self._tomorrow(), "time": "09:00", "repeat": "monthly"})
        self.assertEqual(store.all_events()[0]["repeat"], "none")

    def test_list_events_shows_repeat_marker(self):
        day = self._tomorrow()
        tools.execute("add_event", {"title": "站会", "date": day, "time": "09:30", "repeat": "workdays"})
        result = tools.execute("list_events", {"date": day})
        self.assertIn("workdays重复", result)


if __name__ == "__main__":
    unittest.main()
