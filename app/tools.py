# -*- coding: utf-8 -*-
"""大模型可调用的工具（OpenAI tools 协议）。这是增强层：把自然语言变成本地操作。

新增工具 = 在 SCHEMAS 加声明 + 在 execute 加分支。实际操作全部复用基础层代码
（store/wallpaper），模型只负责理解意图，动作本身是确定性的。
"""
import json
from datetime import date, datetime, timedelta

from . import store, wallpaper
from .mail import accounts as mail_accounts, service as mail_service
from .news import service as news_service

SCHEMAS = [
    {"type": "function", "function": {
        "name": "add_event",
        "description": "为用户添加一条日程提醒",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "日程标题，简短"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD；用户说今天/明天/后天时换算成具体日期"},
            "time": {"type": "string", "description": "时间 HH:MM，24 小时制"},
            "remind_min": {"type": "integer", "description": "提前几分钟提醒，用户没说就填 10"},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "workdays"],
                      "description": "重复周期：不重复填 none，每天 daily，每周 weekly，"
                                     "工作日（跳过周六周日）workdays，用户没说就填 none"}},
            "required": ["title", "date", "time"]}}},
    {"type": "function", "function": {
        "name": "list_events",
        "description": "查看某天的日程列表",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "日期 YYYY-MM-DD，默认今天"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "complete_event",
        "description": "把某条日程标记为已完成",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "integer", "description": "日程编号"}},
            "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "delete_event",
        "description": "删除某条日程",
        "parameters": {"type": "object", "properties": {
            "event_id": {"type": "integer", "description": "日程编号"}},
            "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "refresh_wallpaper",
        "description": "立即重新渲染壁纸日程看板",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_mail",
        "description": "查看用户邮箱里的邮件列表（发件人、主题、时间、摘要），每封带 ref 编号",
        "parameters": {"type": "object", "properties": {
            "unread_only": {"type": "boolean", "description": "只看未读，默认 true"},
            "limit": {"type": "integer", "description": "最多几封，默认 8"},
            "refresh": {"type": "boolean", "description": "是否先联网收取最新邮件，默认 false（用缓存）"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_mail",
        "description": "读取某封邮件的完整正文",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "list_mail 返回的 ref，形如 账户:uid"}},
            "required": ["ref"]}}},
    {"type": "function", "function": {
        "name": "draft_mail",
        "description": "起草一封邮件（不会发出，用户确认后才发送）。回复邮件时填 reply_to_ref",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "description": "收件人邮箱，多个用逗号分隔"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "正文，纯文本"},
            "account": {"type": "string", "description": "用哪个邮箱发，可填地址；不填用第一个账户"},
            "reply_to_ref": {"type": "string", "description": "被回复邮件的 ref（可选）"}},
            "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "list_news",
        "description": "查看缓存的资讯列表（标题、有模型时带中文摘要），不联网",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "最多几条，默认 8"},
            "unread_only": {"type": "boolean", "description": "只看未读，默认 true"}},
            "required": []}}},
]


def _parse_date(text):
    text = (text or "").strip()
    if not text or text in ("今天", "today"):
        return date.today()
    if text in ("明天", "tomorrow"):
        return date.today() + timedelta(days=1)
    if text in ("后天",):
        return date.today() + timedelta(days=2)
    return datetime.strptime(text, "%Y-%m-%d").date()


def execute(name: str, args: dict) -> str:
    """执行工具，返回给模型看的结果字符串（也会体现在它的答复里）。"""
    try:
        if name == "add_event":
            day = _parse_date(args.get("date"))
            h, m = str(args["time"]).split(":")
            at = datetime.combine(day, datetime.min.time()).replace(hour=int(h), minute=int(m))
            remind = int(args.get("remind_min") or 10)
            repeat = str(args.get("repeat") or "none")
            if repeat not in store.REPEAT_KINDS:
                repeat = "none"
            eid = store.add_event(str(args["title"]), at, remind, repeat=repeat)
            wallpaper.apply()
            rep_text = f"，{repeat} 重复" if repeat != "none" else ""
            return f"已添加日程 #{eid}：{at:%Y-%m-%d %H:%M} {args['title']}，提前 {remind} 分钟提醒{rep_text}"
        if name == "list_events":
            day = _parse_date(args.get("date"))
            events = store.events_on(day)
            if not events:
                return f"{day:%Y-%m-%d} 没有日程"
            return "\n".join(
                f"#{e['id']} {e['at'][11:16]} {e['title']}{'（已完成）' if e['done'] else ''}"
                f"{'（' + e['repeat'] + '重复）' if e.get('repeat', 'none') != 'none' else ''}"
                for e in events)
        if name == "complete_event":
            store.set_done(int(args["event_id"]))
            wallpaper.apply()
            return f"日程 #{args['event_id']} 已标记完成"
        if name == "delete_event":
            store.delete_event(int(args["event_id"]))
            wallpaper.apply()
            return f"日程 #{args['event_id']} 已删除"
        if name == "list_mail":
            return _list_mail(args)
        if name == "read_mail":
            return _read_mail(str(args["ref"]))
        if name == "draft_mail":
            return _draft_mail(args)
        if name == "list_news":
            return _list_news(args)
        if name == "refresh_wallpaper":
            wallpaper.apply(force=True)
            return "壁纸已刷新"
        return f"未知工具：{name}"
    except (KeyError, ValueError, TypeError) as e:
        return f"工具执行失败：{e}"
    except Exception as e:  # 邮件的网络/认证错误也要回给模型，而不是让整轮对话崩掉
        return f"工具执行失败：{e}"


def _list_mail(args) -> str:
    if not mail_accounts.enabled_accounts():
        return "用户还没有配置邮箱账户（托盘菜单「邮箱账户…」可添加）"
    note = ""
    if args.get("refresh"):
        res = mail_service.poll_once()
        if res.errors or res.needs_login:
            note = "\n部分账户收取失败：" + "；".join(
                [f"{a} {e}" for a, e in res.errors.items()] + [f"{a} 需重新登录" for a in res.needs_login])
    mails = mail_service.cached_recent(limit=int(args.get("limit") or 8),
                                       unseen_only=args.get("unread_only", True) is not False)
    if not mails:
        return "没有符合条件的邮件" + note
    lines = [f"ref={m['account']}:{m['uid']} {'已读' if m.get('seen') else '未读'} {m.get('date', '')} "
             f"来自 {m.get('from', '')}\n  主题：{m.get('subject', '')}\n  摘要：{m.get('snippet', '')}"
             for m in mails]
    return "\n".join(lines) + note


def _split_ref(ref: str):
    account_id, _, uid = ref.rpartition(":")
    acc = mail_accounts.find(account_id)
    if not acc or not uid:
        raise ValueError(f"无效的邮件 ref：{ref}")
    return acc, uid


def _read_mail(ref: str) -> str:
    from .mail.client import MailClient
    acc, uid = _split_ref(ref)
    client = MailClient(acc, mail_accounts.load().get("oauth"))
    try:
        m = client.fetch_full(uid)
    finally:
        client.close()
    body = m["body"][:3000]
    return (f"主题：{m['subject']}\n来自：{m['from']}\n时间：{m['date']}\nMessage-ID：{m['message_id']}\n"
            f"附件：{'、'.join(m['attachments']) or '无'}\n正文：\n{body}")


def _draft_mail(args) -> str:
    in_reply_to, subject = "", str(args["subject"])
    if args.get("reply_to_ref"):
        acc, uid = _split_ref(str(args["reply_to_ref"]))
        cached = [m for m in mail_service.cached_recent(limit=500, account=acc["id"]) if m["uid"] == uid]
        if cached:
            in_reply_to = cached[0].get("message_id", "")
    d = mail_service.create_draft(args.get("account"), str(args["to"]), subject, str(args["body"]),
                                  in_reply_to=in_reply_to)
    return (f"已生成草稿 #{d['id']}，尚未发送。请把草稿内容念给用户，并告诉用户确认无误后输入 /发送 {d['id']} 发出"
            f"（或 /丢弃 {d['id']}）。\n" + mail_service.format_draft(d))


def _list_news(args) -> str:
    items = news_service.cached_recent(limit=int(args.get("limit") or 8),
                                       unread_only=args.get("unread_only", True) is not False)
    if not items:
        return "没有符合条件的资讯（可以提示用户 /资讯 刷新，或先在 config/news.json 配置资讯源）"
    return "\n".join(f"《{i['title']}》{i.get('summary') or ''}" for i in items)


def execute_call(tool_call: dict) -> dict:
    """把一次 tool_call 变成 role=tool 的消息。"""
    fn = tool_call.get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    result = execute(fn.get("name", ""), args if isinstance(args, dict) else {})
    return {"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": result}
