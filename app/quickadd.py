# -*- coding: utf-8 -*-
"""快速添加日程：一句中文里抽出日期、时间、重复、提前量，剩下的当标题（纯规则，不需要大模型）。

例：
  明天下午3点 组会              → 明天 15:00「组会」
  周五 9:30 交周报 提前30分钟    → 本周五（已过则下周五）09:30，提前 30 分钟
  每天 8点 背单词                → 每天重复
  10月1日 国庆出游               → 没写时间默认 09:00
"""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "末": 5}
DEFAULT_TIME = time(9, 0)


@dataclass
class Parsed:
    title: str
    at: datetime
    repeat: str = "none"
    remind: int = 10
    explicit_time: bool = True


def _cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return CN_NUM.get(a, 0) * 10 + (CN_NUM.get(b, 0) if b else 0)
    return CN_NUM.get(s, 0)


NUM = r"(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})"
_TIME_COLON = re.compile(r"(上午|早上|早晨|早|中午|下午|晚上|傍晚|夜里|凌晨)?\s*(\d{1,2})\s*[:：]\s*(\d{2})")
_TIME_CN = re.compile(
    rf"(上午|早上|早晨|早|中午|下午|晚上|傍晚|夜里|凌晨)?\s*({NUM})\s*[点時时]"
    rf"(?:\s*(半|一刻|三刻|({NUM})\s*分?))?")
_PERIOD_ONLY = re.compile(r"(上午|早上|早晨|中午|下午|晚上|傍晚)")
_REMIND = re.compile(rf"提前\s*({NUM})\s*(分钟|分|小时|个小时)(?:提醒)?")
_REL_DAY = re.compile(r"(大后天|后天|明天|明早|明晚|今天|今晚|今早)")
_WEEK = re.compile(r"(下下|下个?|这个?|本)?\s*(?:周|星期|礼拜)([一二三四五六日天末])")
_MD = re.compile(r"(?:(\d{4})\s*[年/-]\s*)?(\d{1,2})\s*(?:月|/|-)\s*(\d{1,2})\s*(?:日|号)?")
_REPEAT = [(re.compile(r"每(?:个)?工作日|工作日"), "workdays"),
           (re.compile(r"每天|每日|天天"), "daily"),
           (re.compile(r"每(?:个)?(?:周|星期|礼拜)(?=[一二三四五六日天])|每周"), "weekly")]
_FILLER = re.compile(r"^(?:提醒我|记得|要|去|在|的|，|,|。|\s)+|(?:，|,|。|的|\s)+$")


def _apply_period(h: int, period: str | None) -> int:
    if not period:
        return h
    if period in ("下午", "晚上", "傍晚", "夜里") and h < 12:
        return h + 12
    if period == "中午" and h < 11:
        return h + 12
    if period == "凌晨" and h == 12:
        return 0
    return h


def parse(text: str, base_day: date | None = None, now: datetime | None = None) -> Parsed | None:
    """解析失败（抽完什么都不剩，或时间非法）返回 None。base_day 是没写日期时落到的那天。"""
    now = now or datetime.now()
    today = now.date()
    base_day = base_day or today
    s = text.strip()
    if not s:
        return None

    repeat = "none"
    for rx, kind in _REPEAT:
        m = rx.search(s)
        if m:
            repeat = kind
            keep_week = kind == "weekly" and s[m.end():m.end() + 1] in WEEKDAY
            s = s[:m.start()] + (" 周" if keep_week else " ") + s[m.end():]
            break

    remind = 10
    m = _REMIND.search(s)
    if m:
        n = _cn_to_int(m.group(1))
        remind = n * 60 if "小时" in m.group(2) else n
        s = s[:m.start()] + " " + s[m.end():]

    day, period_hint = None, None
    m = _REL_DAY.search(s)
    if m:
        w = m.group(1)
        offset = {"今天": 0, "今晚": 0, "今早": 0, "明天": 1, "明早": 1, "明晚": 1,
                  "后天": 2, "大后天": 3}[w]
        day = today + timedelta(days=offset)
        period_hint = "晚上" if w.endswith("晚") else ("上午" if w.endswith("早") else None)
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = _WEEK.search(s)
        if m:
            prefix, wd = m.group(1) or "", WEEKDAY[m.group(2)]
            monday = today - timedelta(days=today.weekday())
            if prefix.startswith("下下"):
                day = monday + timedelta(days=14 + wd)
            elif prefix.startswith("下"):
                day = monday + timedelta(days=7 + wd)
            else:
                day = monday + timedelta(days=wd)
                if day < today and not prefix:  # "周一"已过 → 下周一；"这周一"就是这周一
                    day += timedelta(days=7)
            s = s[:m.start()] + " " + s[m.end():]
        else:
            m = _MD.search(s)
            if m and (m.group(1) or "月" in m.group(0) or "/" in m.group(0) or "-" in m.group(0)):
                year = int(m.group(1)) if m.group(1) else today.year
                try:
                    day = date(year, int(m.group(2)), int(m.group(3)))
                except ValueError:
                    return None
                if not m.group(1) and day < today - timedelta(days=1):
                    day = date(year + 1, day.month, day.day)  # "1月3日"在年底说 = 明年
                s = s[:m.start()] + " " + s[m.end():]

    hh = mm = None
    m = _TIME_COLON.search(s)
    if m:
        hh, mm = _apply_period(int(m.group(2)), m.group(1) or period_hint), int(m.group(3))
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = _TIME_CN.search(s)
        if m:
            hh = _apply_period(_cn_to_int(m.group(2)), m.group(1) or period_hint)
            tail = m.group(3)
            mm = 0
            if tail == "半":
                mm = 30
            elif tail == "一刻":
                mm = 15
            elif tail == "三刻":
                mm = 45
            elif m.group(4):
                mm = _cn_to_int(m.group(4))
            s = s[:m.start()] + " " + s[m.end():]
        else:
            m = _PERIOD_ONLY.search(s)
            if m:  # 只说了"下午开会"：给个该时段的典型时间
                hh, mm = {"上午": 9, "早上": 8, "早晨": 8, "中午": 12, "下午": 14,
                          "晚上": 20, "傍晚": 18}[m.group(1)], 0
                s = s[:m.start()] + " " + s[m.end():]
            elif period_hint:
                hh, mm = (20 if period_hint == "晚上" else 8), 0
    if hh is not None and not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None

    title = re.sub(r"\s+", " ", s).strip()
    title = _FILLER.sub("", title).strip()
    if not title:
        return None

    explicit = hh is not None
    t = time(hh, mm) if explicit else DEFAULT_TIME
    if day is None:
        day = base_day
        at = datetime.combine(day, t)
        # 只写了时间、落在今天且已经过了 → 顺延到明天（"8点背单词"下午说的就是明早）
        if day == today and at < now - timedelta(minutes=1):
            at += timedelta(days=1)
    else:
        at = datetime.combine(day, t)
        if not explicit and at < now:  # "今天 交报告"没写时间且 9 点已过 → 下一个整点
            at = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return Parsed(title, at, repeat, remind, explicit)
