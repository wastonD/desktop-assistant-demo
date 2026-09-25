# -*- coding: utf-8 -*-
"""本地指令层：不依赖大模型的结构化命令，对话面板里以 / 开头触发。

这是"功能分界"的核心：日程、壁纸、提醒是纯本地功能，没有大模型也完整可用；
大模型只是在此之上叠加自然语言理解和写作能力。
"""
from datetime import date, datetime, timedelta

from . import agenda, history, quickadd, store, wallpaper
from .drawer import core as drawer_core

REPEAT_WORDS = {"每天": "daily", "每周": "weekly", "工作日": "workdays"}
REPEAT_LABEL = {"daily": "每天", "weekly": "每周", "workdays": "工作日"}

# 界面钩子：main.py 注入（commands 本身不依赖 Qt），比如 "open_drawer"、"notify"
UI_HOOKS: dict = {}

# 邮件/资讯模块很重（imaplib/ssl/urllib…，约 8MB 常驻），只有真用到这些指令时才加载
MAIL_CMDS = {"邮件", "mail", "回复", "邮箱", "草稿", "发送", "send", "丢弃"}
NEWS_CMDS = {"资讯", "news"}


def _mail_mod():
    from .mail import commands as m
    if UI_HOOKS.get("notify"):
        m.set_notifier(UI_HOOKS["notify"])
    return m


def _news_mod():
    from .news import commands as m
    if UI_HOOKS.get("notify"):
        m.set_notifier(UI_HOOKS["notify"])
    return m

HELP = ("本地指令（无需大模型）：\n"
        "/今天 —— 今日安排 + 空闲时段 + 逾期未完成\n"
        "/记 一句话 —— 快速记日程，如：/记 明天下午3点 组会 提前30分钟\n"
        "/日程 —— 查看今日日程　/日程 明天　/日程 2026-09-21 —— 查看指定日期\n"
        "/日程 标题 时间 [提前分钟] [每天|每周|工作日] —— 添加日程（末尾可加重复周期）\n"
        "    时间写 18:30 表示今天，或写 2026-09-21 09:00\n"
        "/完成 编号 —— 标记完成　/删除 编号 —— 删除\n"
        "/稍后 [分钟] —— 最近提醒过的日程再提醒一次，默认 10 分钟后\n"
        "/收纳 —— 桌面收纳概况　/收纳 整理　/收纳 撤销　/收纳 放回　/收纳 打开（Ctrl+Alt+D）\n"
        "/壁纸 —— 立即刷新壁纸看板\n"
        "/邮件 /邮箱 /回复 /草稿 /发送 —— 邮件（/邮件 帮助 看详细）\n"
        "/资讯 —— 资讯（/资讯 帮助 看详细）\n"
        "/清空 —— 清空对话历史\n"
        "/帮助 —— 显示本条")


def _format_row(e: dict) -> str:
    mark = "√" if e["done"] else "·"
    rep = f"（{REPEAT_LABEL[e['repeat']]}）" if e.get("repeat", "none") in REPEAT_LABEL else ""
    return f"{mark} #{e['id']} {e['at'][11:16]} {e['title']}{rep}"


def _list_day(day: date, label: str) -> str:
    events = store.events_on(day)
    if not events:
        return f"{label}没有日程。"
    return "\n".join(_format_row(e) for e in events)


def _list_today() -> str:
    return _list_day(date.today(), "今天")


def _parse_view_date(word: str) -> date | None:
    """把 '今天'/'明天'/'后天'/'2026-09-25' 转成日期；看不懂返回 None（交给 _add 当标题处理）。"""
    word = word.strip()
    if word in ("今天", "today"):
        return date.today()
    if word in ("明天", "tomorrow"):
        return date.today() + timedelta(days=1)
    if word in ("后天",):
        return date.today() + timedelta(days=2)
    try:
        return datetime.strptime(word, "%Y-%m-%d").date()
    except ValueError:
        return None


def _add(args) -> str:
    if not args:
        return "格式：/日程 标题 18:30 [提前分钟] [每天|每周|工作日]"
    title, rest = args[0], list(args[1:])
    repeat = "none"
    if rest and rest[-1] in REPEAT_WORDS:
        repeat = REPEAT_WORDS[rest.pop()]
    remind = 10
    if rest and rest[-1].isdigit() and ":" not in rest[-1]:
        remind = int(rest.pop())
    try:
        if len(rest) == 1 and ":" in rest[0]:
            h, m = rest[0].split(":")
            at = datetime.combine(date.today(), datetime.min.time()).replace(
                hour=int(h), minute=int(m))
        elif len(rest) == 2:
            at = datetime.strptime(rest[0] + " " + rest[1], "%Y-%m-%d %H:%M")
        else:
            return ("格式：/日程 标题 18:30 [提前分钟] [每天|每周|工作日]，"
                    "或 /日程 标题 2026-09-21 09:00")
    except ValueError:
        return "时间没看懂。格式：18:30 或 2026-09-21 09:00"
    eid = store.add_event(title, at, remind, repeat=repeat)
    wallpaper.apply_async()  # 面板指令跑在界面线程，壁纸渲染必须异步
    rep_text = f"，{REPEAT_LABEL[repeat]}重复" if repeat != "none" else ""
    return f"记好了 #{eid}：{at:%m-%d %H:%M} {title}（提前 {remind} 分钟提醒{rep_text}）"


def _snooze(args) -> str:
    minutes = 10
    if args and args[0].isdigit():
        minutes = int(args[0])
    ev = store.snooze_last(minutes)
    if not ev:
        return "最近没有弹出过的提醒可以稍后。"
    return f"好，{minutes} 分钟后再提醒你「{ev['title']}」。"


def _quick(args) -> str:
    r = quickadd.parse(" ".join(args))
    if r is None:
        return "没看懂。试试：/记 明天下午3点 组会　或　/记 每天 8:00 背单词"
    eid = store.add_event(r.title, r.at, r.remind, repeat=r.repeat)
    wallpaper.apply_async()
    rep = f"，{REPEAT_LABEL[r.repeat]}重复" if r.repeat in REPEAT_LABEL else ""
    return (f"记好了 #{eid}：{agenda.day_label(r.at.date())} {r.at:%H:%M} {r.title}"
            f"（提前 {r.remind} 分钟提醒{rep}）")


def _drawer(args) -> str:
    sub = args[0] if args else ""
    if sub in ("打开", "open"):
        opener = UI_HOOKS.get("open_drawer")
        if opener:
            opener()
            return "抽屉拉开了。"
        return "界面没启动，打不开抽屉。"
    from . import desktop_clean
    if sub in ("整理", "收", "收纳", "干净"):
        desktop_clean.set_all_icons_hidden(True)
        return "桌面图标藏起来了，文件都还在桌面文件夹里，从抽屉（Ctrl+Alt+D）打开。/收纳 显示 可以显示回来。"
    if sub in ("显示", "放回", "还原"):
        desktop_clean.set_all_icons_hidden(False)
        legacy = drawer_core.legacy_items()
        if legacy:
            res = drawer_core.restore_legacy()
            return "桌面图标显示回来了。旧版收纳夹里的：" + drawer_core.summary(res, "放回原处")
        return "桌面图标显示回来了。"
    if sub in ("撤销", "撤回", "undo"):
        op = drawer_core.undo_last()
        if op is None:
            return "没有可以撤销的分类操作。"
        return f"撤销了「{op['label']}」（{op['at']}）。"
    items = drawer_core.scan()
    desk = drawer_core.desktop_items(items)
    groups = drawer_core.group(desk)
    parts = [f"{drawer_core.LABELS[k]} {len(v)}" for k, v in groups.items() if v]
    legacy = sum(1 for i in items if i.location == "stash")
    state = "图标已隐藏" if desktop_clean.all_icons_hidden() else "图标显示中"
    return (f"桌面上 {len(desk)} 项（{state}，文件都在桌面文件夹里）。\n"
            + ("按类别：" + "、".join(parts) + "\n" if parts else "")
            + (f"旧版收纳夹里还有 {legacy} 项，/收纳 放回 可以原样放回。\n" if legacy else "")
            + "/收纳 整理 —— 藏起桌面图标（文件不动）\n"
              "/收纳 显示 —— 显示桌面图标\n/收纳 打开 —— 拉开抽屉")


def handle(text: str) -> str | None:
    """是本地指令则执行并返回回复文本；否则返回 None（交给大模型）。"""
    text = text.strip()
    if not text.startswith(("/", "／")):
        return None
    parts = text.lstrip("/／").split()
    if not parts:
        return HELP
    cmd, args = parts[0], parts[1:]
    try:
        if cmd in ("帮助", "help"):
            return HELP
        if cmd in MAIL_CMDS:
            mail_commands = _mail_mod()
            if cmd in ("邮件", "mail") and args[:1] == ["帮助"]:
                return mail_commands.HELP
            mail_reply = mail_commands.handle(cmd, args)
            if mail_reply is not None:
                return mail_reply
        if cmd in NEWS_CMDS:
            news_commands = _news_mod()
            if args[:1] == ["帮助"]:
                return news_commands.HELP
            news_reply = news_commands.handle(cmd, args)
            if news_reply is not None:
                return news_reply
        if cmd in ("清空", "clear"):
            history.clear()
            return "对话历史已清空。"
        if cmd in ("今天", "规划", "today"):
            return agenda.plan_text()
        if cmd in ("记", "记一下", "add"):
            return _quick(args)
        if cmd in ("收纳", "整理桌面"):
            return _drawer(args)
        if cmd == "壁纸":
            wallpaper.apply_async(force=True)
            return "壁纸刷新中，几秒后生效。"
        if cmd == "日程":
            if not args:
                return _list_today()
            if len(args) == 1:
                day = _parse_view_date(args[0])
                if day:
                    return _list_day(day, args[0] if args[0] not in ("今天", "today") else "今天")
            return _add(args)
        if cmd == "完成" and args:
            store.set_done(int(args[0]))
            wallpaper.apply_async()
            return f"#{args[0]} 已完成。"
        if cmd == "删除" and args:
            store.delete_event(int(args[0]))
            wallpaper.apply_async()
            return f"#{args[0]} 已删除。"
        if cmd in ("稍后", "snooze"):
            return _snooze(args)
    except (ValueError, KeyError) as e:
        return f"指令出错：{e}"
    return "不认识这个指令。\n\n" + HELP
