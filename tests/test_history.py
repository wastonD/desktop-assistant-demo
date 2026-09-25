# -*- coding: utf-8 -*-
"""对话历史持久化单元测试。运行：python -m unittest discover tests"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(history, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_append_and_recent_order(self):
        history.append("user", "你好")
        history.append("assistant", "你好呀")
        rows = history.recent(20)
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
        self.assertEqual([r["content"] for r in rows], ["你好", "你好呀"])

    def test_recent_limits_and_keeps_newest(self):
        for i in range(5):
            history.append("user", f"消息{i}")
        rows = history.recent(3)
        self.assertEqual([r["content"] for r in rows], ["消息2", "消息3", "消息4"])

    def test_recent_empty(self):
        self.assertEqual(history.recent(10), [])

    def test_clear(self):
        history.append("user", "你好")
        history.clear()
        self.assertEqual(history.recent(10), [])


if __name__ == "__main__":
    unittest.main()
