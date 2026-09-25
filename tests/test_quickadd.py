# -*- coding: utf-8 -*-
"""app/quickadd.py：一句话快速添加日程的解析。"""
import unittest
from datetime import date, datetime

from app.quickadd import parse

NOW = datetime(2026, 9, 23, 14, 20)  # 周三下午


class QuickAddTests(unittest.TestCase):
    def p(self, text, **kw):
        return parse(text, now=NOW, **kw)

    def test_tomorrow_afternoon(self):
        r = self.p("明天下午3点 组会")
        self.assertEqual((r.title, r.at), ("组会", datetime(2026, 9, 24, 15, 0)))

    def test_colon_time_and_remind(self):
        r = self.p("周五 9:30 交周报 提前30分钟")
        self.assertEqual(r.at, datetime(2026, 9, 25, 9, 30))
        self.assertEqual((r.title, r.remind), ("交周报", 30))

    def test_past_weekday_rolls_to_next_week(self):
        self.assertEqual(self.p("周一 例会 10:00").at, datetime(2026, 9, 28, 10, 0))
        self.assertEqual(self.p("下周三 复查").at.date(), date(2026, 9, 30))

    def test_daily_repeat_rolls_past_time_to_tomorrow(self):
        r = self.p("每天 8点 背单词")
        self.assertEqual((r.title, r.repeat, r.at), ("背单词", "daily", datetime(2026, 9, 24, 8, 0)))

    def test_weekly_with_weekday(self):
        r = self.p("每周五 下午4点半 组会")
        self.assertEqual((r.repeat, r.at), ("weekly", datetime(2026, 9, 25, 16, 30)))

    def test_workdays(self):
        self.assertEqual(self.p("工作日 9:00 打卡").repeat, "workdays")

    def test_month_day_default_time(self):
        r = self.p("10月1日 国庆出游")
        self.assertEqual((r.title, r.at, r.explicit_time),
                         ("国庆出游", datetime(2026, 10, 1, 9, 0), False))

    def test_chinese_numerals(self):
        self.assertEqual(self.p("晚上八点一刻 跑步").at, datetime(2026, 9, 23, 20, 15))
        self.assertEqual(self.p("明早七点半 赶飞机").at, datetime(2026, 9, 24, 7, 30))
        self.assertEqual(self.p("十二点 午饭").at, datetime(2026, 9, 24, 12, 0))

    def test_base_day_when_no_date(self):
        r = self.p("开会 16:00", base_day=date(2026, 9, 26))
        self.assertEqual(r.at, datetime(2026, 9, 26, 16, 0))

    def test_today_without_time_after_nine(self):
        self.assertEqual(self.p("今天 交报告").at, datetime(2026, 9, 23, 15, 0))

    def test_filler_words_stripped(self):
        self.assertEqual(self.p("提醒我明天10点去取快递").title, "取快递")

    def test_empty_title_rejected(self):
        self.assertIsNone(self.p("明天 10:00"))
        self.assertIsNone(self.p("   "))

    def test_invalid_time_rejected(self):
        self.assertIsNone(self.p("25:00 睡觉"))

    def test_hour_remind(self):
        self.assertEqual(self.p("明天9点 面试 提前1小时").remind, 60)


if __name__ == "__main__":
    unittest.main()
