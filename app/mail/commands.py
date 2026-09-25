# -*- coding: utf-8 -*-
"""邮件相关的 / 本地指令（基础层，不需要大模型）。由 app/commands.py 分发过来。

涉及网络的操作（刷新、发送）一律后台线程执行，完成后通过 notify(text) 回报——
notify 由界面层注册（线程安全：界面层用 Qt 信号转回主线程）。
"""
import threading

from . import accounts, service
from .client import MailClient, MailError
from .providers import PRESETS

HELP = ("邮件指令：\n"
        "/邮件 —— 未读邮件（缓存）　/邮件 全部 —— 最近邮件\n"
        "/邮件 刷新 —— 立即收取　/邮件 看 序号 —— 查看正文\n"
        "/邮件 已读 序号 —— 标记已读（同步服务器）　/邮件 搜 关键词 —— 在缓存里搜\n"
        "/邮箱 —— 账户列表与状态　/邮箱 测试 序号 —— 测试连接\n"
        "    添加/删除账户：托盘菜单「邮箱账户…」（授权码不经过聊天框）\n"
        "/回复 序号 [正文] —— 生成回复草稿（先 /邮件 列出定位序号）\n"
        "/草稿 —— 待发草稿　/发送 编号 —— 确认发出　/丢弃 编号")

_notify = None
_last_list: list[dict] = []  # /邮件 列出的顺序，供 /邮件 看 序号 使用


def set_notifier(fn) -> None:
    global _notify
    _notify = fn


def _bg(job, label: str) -> str:
    def run():
        try:
            text = job()
        except Exception as e:
            text = f"{label}失败：{e}"
        if _notify:
            _notify(text)
    threading.Thread(target=run, daemon=True).start()
    return f"{label}中…"


def _mail(args) -> str:
    global _last_list
    if not accounts.enabled_accounts():
        return "还没有邮箱账户。托盘菜单「邮箱账户…」里添加（QQ/163/Gmail 用授权码，Outlook 用微软登录）。"
    sub = args[0] if args else ""
    if sub == "刷新":
        def job():
            res = service.poll_once()
            parts = [f"收取完成：共 {res.total_unread} 封未读"]
            parts += [f"{a}：{e}" for a, e in res.errors.items()]
            parts += [f"{a}：需要重新登录（托盘「邮箱账户…」）" for a in res.needs_login]
            return "\n".join(parts)
        return _bg(job, "收取")
    if sub == "看":
        if len(args) < 2 or not args[1].isdigit() or not (1 <= int(args[1]) <= len(_last_list)):
            return "先用 /邮件 列出，再 /邮件 看 序号"
        m = _last_list[int(args[1]) - 1]
        acc = accounts.find(m["account"])
        if not acc:
            return "这封邮件的账户已删除"

        def job():
            c = MailClient(acc, accounts.load().get("oauth"))
            try:
                full = c.fetch_full(m["uid"], m.get("folder", "INBOX"))
            finally:
                c.close()
            body = full["body"]
            body = body[:1500] + ("\n…（已截断）" if len(body) > 1500 else "")
            att = f"\n附件：{'、'.join(full['attachments'])}" if full["attachments"] else ""
            return f"{full['subject']}\n来自：{full['from']}　{full['date']}{att}\n——\n{body}"
        return _bg(job, "读取")
    if sub == "已读":
        if len(args) < 2 or not args[1].isdigit() or not (1 <= int(args[1]) <= len(_last_list)):
            return "先用 /邮件 列出，再 /邮件 已读 序号"
        m = _last_list[int(args[1]) - 1]

        def job():
            service.mark_seen(m["account"], m["uid"])
            return f"已标记已读：{m.get('subject', '')}"
        return _bg(job, "标记")
    if sub == "搜":
        if len(args) < 2:
            return "格式：/邮件 搜 关键词"
        keyword = " ".join(args[1:])
        _last_list = service.search_cached(keyword, limit=15)
        if not _last_list:
            return f"缓存里没有匹配“{keyword}”的邮件。"
        multi = len(accounts.enabled_accounts()) > 1
        return f"搜索“{keyword}”（{len(_last_list)} 封）：\n" + service.format_list(_last_list, with_account=multi)
    unseen = sub != "全部"
    _last_list = service.cached_recent(limit=15, unseen_only=unseen)
    multi = len(accounts.enabled_accounts()) > 1
    checked = service.status.read().get("mail_checked", "从未")
    head = f"{'未读' if unseen else '最近'}邮件（上次收取 {checked}）：\n"
    if not _last_list:
        return head + ("没有未读邮件。" if unseen else "缓存为空，试试 /邮件 刷新")
    return head + service.format_list(_last_list, with_account=multi)


def _accounts(args) -> str:
    cfg = accounts.load()
    accs = cfg.get("accounts", [])
    if args and args[0] == "测试":
        acc = accounts.find(args[1], cfg) if len(args) > 1 else (accs[0] if accs else None)
        if not acc:
            return "没找到这个账户"

        def job():
            c = MailClient(acc, cfg.get("oauth"))
            try:
                return c.test()
            finally:
                c.close()
        return _bg(job, "测试连接")
    if not accs:
        return "还没有邮箱账户。托盘菜单「邮箱账户…」里添加。"
    errors = service.status.read().get("mail_errors") or {}
    lines = []
    for i, a in enumerate(accs, 1):
        label = PRESETS.get(a["provider"], {}).get("label", a["provider"])
        state = "停用" if not a.get("enabled", True) else ("异常：" + errors[a["id"]] if a["id"] in errors else "正常")
        lines.append(f"{i}. {a['address']}（{label}）{state}")
    return "\n".join(lines)


def _drafts() -> str:
    ds = service.pending_drafts()
    if not ds:
        return "没有待发送的草稿。"
    return "\n\n".join(service.format_draft(d) for d in ds) + "\n\n确认发出：/发送 编号"


def _send(args) -> str:
    if not args or not args[0].isdigit():
        return "格式：/发送 草稿编号（先用 /草稿 查看）"
    did = int(args[0])
    d = service.get_draft(did)
    if not d or d["status"] != "draft":
        return f"没有待发送的草稿 #{did}"
    return _bg(lambda: service.send_draft(did), f"发送草稿 #{did} ")


def _reply(args) -> str:
    if not args or not args[0].isdigit() or not (1 <= int(args[0]) <= len(_last_list)):
        return "先用 /邮件 列出定位序号，再 /回复 序号 [正文]"
    m = _last_list[int(args[0]) - 1]
    body = " ".join(args[1:])
    try:
        d = service.create_reply_draft(m, body)
    except MailError as e:
        return f"生成回复草稿失败：{e}"
    return (f"已生成回复草稿 #{d['id']}，尚未发送。确认无误后 /发送 {d['id']}（或 /丢弃 {d['id']}）。\n"
            + service.format_draft(d))


def handle(cmd: str, args: list[str]) -> str | None:
    """是邮件指令就返回回复文本，否则 None。"""
    if cmd in ("邮件", "mail"):
        return _mail(args)
    if cmd in ("回复",):
        return _reply(args)
    if cmd in ("邮箱",):
        return _accounts(args)
    if cmd in ("草稿",):
        return _drafts()
    if cmd in ("发送", "send"):
        return _send(args)
    if cmd in ("丢弃",) and args and args[0].isdigit():
        service.discard_draft(int(args[0]))
        return f"草稿 #{args[0]} 已丢弃"
    return None
