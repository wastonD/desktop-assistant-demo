# -*- coding: utf-8 -*-
"""app/commands.py 指令分发的单元测试（本次新增 /资讯、/清空 两条，顺带盖一下已有的分发逻辑）。

运行：python -m unittest discover tests
"""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from app import commands, history, store, wallpaper
from app.mail import accounts as mail_accounts
from app.news import config as news_config


class CommandsDispatchTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for target, attr, value in (
                (history, "DB_PATH", tmp / "t.db"),
                (news_config, "CONFIG_PATH", tmp / "news.json"),
                (mail_accounts, "CONFIG_PATH", tmp / "mail.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_clear_wipes_persisted_history(self):
        history.append("user", "你好")
        self.assertEqual(commands.handle("/清空"), "对话历史已清空。")
        self.assertEqual(history.recent(10), [])

    def test_news_dispatch_without_feeds(self):
        reply = commands.handle("/资讯")
        self.assertIn("还没有配置资讯源", reply)

    def test_news_help(self):
        self.assertEqual(commands.handle("/资讯 帮助"), news_commands_help())

    def test_mail_help_unaffected(self):
        from app.mail import commands as mail_commands
        self.assertEqual(commands.handle("/邮件 帮助"), mail_commands.HELP)

    def test_help_mentions_new_commands(self):
        text = commands.handle("/帮助")
        self.assertIn("/资讯", text)
        self.assertIn("/清空", text)

    def test_unknown_command_falls_back_to_help(self):
        self.assertIn("不认识这个指令", commands.handle("/不存在的指令"))

    def test_non_slash_text_returns_none(self):
        self.assertIsNone(commands.handle("普通聊天"))


def news_commands_help():
    from app.news import commands as news_commands
    return news_commands.HELP


class ScheduleCommandTests(unittest.TestCase):
    """/日程 指令的时间解析（含新增的重复日程写法），壁纸刷新 mock 掉不碰真实系统调用。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(wallpaper, "apply_async")
        p2.start()
        self.addCleanup(p2.stop)

    def test_add_today_hhmm(self):
        reply = commands.handle("/日程 组会 18:30")
        self.assertIn("组会", reply)
        events = store.all_events()
        today = date.today().strftime("%Y-%m-%d")
        self.assertEqual(events[0]["at"], f"{today} 18:30")
        self.assertEqual(events[0]["remind_min"], 10)

    def test_add_full_date_with_remind_min(self):
        reply = commands.handle("/日程 报销截止 2026-09-25 09:00 30")
        self.assertIn("报销截止", reply)
        events = store.all_events()
        self.assertEqual(events[0]["at"], "2026-09-25 09:00")
        self.assertEqual(events[0]["remind_min"], 30)

    def test_bad_time_format_rejected(self):
        reply = commands.handle("/日程 乱写 明天下午")
        self.assertIn("格式", reply)
        self.assertEqual(store.all_events(), [])

    def test_no_args_lists_today(self):
        self.assertEqual(commands.handle("/日程"), "今天没有日程。")

    def test_add_with_daily_repeat(self):
        reply = commands.handle("/日程 晨会 09:00 每天")
        self.assertIn("每天重复", reply)
        self.assertEqual(store.all_events()[0]["repeat"], "daily")

    def test_add_with_remind_and_workdays_repeat(self):
        reply = commands.handle("/日程 站会 2026-09-25 09:30 15 工作日")
        self.assertIn("工作日重复", reply)
        e = store.all_events()[0]
        self.assertEqual(e["repeat"], "workdays")
        self.assertEqual(e["remind_min"], 15)

    def test_add_without_repeat_word_unaffected(self):
        reply = commands.handle("/日程 组会 18:30 20")
        self.assertNotIn("重复", reply)
        e = store.all_events()[0]
        self.assertEqual(e["repeat"], "none")
        self.assertEqual(e["remind_min"], 20)

    def test_view_tomorrow(self):
        tomorrow = date.today() + timedelta(days=1)
        commands.handle(f"/日程 明天的事 {tomorrow:%Y-%m-%d} 09:00")
        reply = commands.handle("/日程 明天")
        self.assertIn("明天的事", reply)

    def test_view_specific_date(self):
        commands.handle("/日程 报销 2026-09-25 09:00")
        reply = commands.handle("/日程 2026-09-25")
        self.assertIn("报销", reply)

    def test_view_specific_date_empty(self):
        reply = commands.handle("/日程 2099-01-01")
        self.assertIn("没有日程", reply)

    def test_repeat_marker_shown_in_listing(self):
        commands.handle("/日程 晨会 09:00 每天")
        today = date.today().strftime("%Y-%m-%d")
        reply = commands.handle(f"/日程 {today}")
        self.assertIn("（每天）", reply)


class SnoozeCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(store, "DB_PATH", tmp / "t.db")
        p.start()
        self.addCleanup(p.stop)

    def test_snooze_without_recent_reminder(self):
        self.assertIn("没有弹出过的提醒", commands.handle("/稍后"))

    def test_snooze_after_reminder_with_custom_minutes(self):
        eid = store.add_event("组会", datetime.now() + timedelta(minutes=5))
        store.mark_reminded(eid)
        reply = commands.handle("/稍后 20")
        self.assertIn("20 分钟后", reply)
        self.assertIn("组会", reply)

    def test_snooze_default_is_ten_minutes(self):
        eid = store.add_event("组会", datetime.now())
        store.mark_reminded(eid)
        self.assertIn("10 分钟后", commands.handle("/稍后"))


if __name__ == "__main__":
    unittest.main()
