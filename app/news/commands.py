# -*- coding: utf-8 -*-
"""资讯相关的 / 本地指令（基础层，不需要大模型；有模型时缓存里会带摘要，见 service.py）。

刷新涉及网络，后台线程执行，完成后通过 notify(text) 回报——notify 由界面层注册
（线程安全：界面层用 Qt 信号转回主线程），做法与 app/mail/commands.py 一致。
"""
import threading

from . import config, service

HELP = ("资讯指令：\n"
        "/资讯 —— 未读资讯（缓存，看过即标记已读）　/资讯 刷新 —— 立即抓取\n"
        "/资讯 订阅 网址 —— 添加订阅（自动识别 RSS/Atom，或从网页自动发现订阅链接）\n"
        "/资讯 源 —— 列出已订阅的源　/资讯 退订 序号或id —— 取消订阅\n"
        "/资讯 导入 OPML文件路径 —— 从 OPML 批量订阅\n"
        "    也可以直接改 config/news.json（模板 config/news.example.json）")

_notify = None
_last_list: list[dict] = []  # /资讯 列出的顺序，供将来 /资讯 看 序号 之类扩展使用


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


def _news(args) -> str:
    global _last_list
    if not config.enabled_feeds():
        return "还没有配置资讯源。编辑 config/news.json 添加 RSS/Atom 源（模板见 config/news.example.json）。"
    sub = args[0] if args else ""
    if sub == "刷新":
        def job():
            res = service.poll_once()
            parts = [f"抓取完成：{res.total_new} 条新资讯"]
            parts += [f"{f}：{e}" for f, e in res.errors.items()]
            return "\n".join(parts)
        return _bg(job, "抓取")
    _last_list = service.cached_recent(limit=15)
    if not _last_list:
        return "没有未读资讯。试试 /资讯 刷新"
    text = service.format_list(_last_list)
    service.mark_read([i["id"] for i in _last_list])
    return text


def _subscribe(args) -> str:
    if not args:
        return "格式：/资讯 订阅 网址"

    def job():
        feed = service.subscribe(args[0])
        return f"订阅成功：{feed['title']}（{feed['id']}）"
    return _bg(job, "订阅")


def _list_feeds() -> str:
    feeds = config.load().get("feeds", [])
    if not feeds:
        return "还没有订阅任何资讯源。/资讯 订阅 网址 来添加。"
    lines = []
    for i, f in enumerate(feeds, 1):
        state = "" if f.get("enabled", True) else "　已停用"
        lines.append(f"{i}. {f.get('title', f['id'])}（{f['id']}）{state}\n   {f['url']}")
    return "\n".join(lines)


def _unsubscribe(args) -> str:
    if not args:
        return "格式：/资讯 退订 序号或id（先用 /资讯 源 查看）"
    feed = config.remove_feed(args[0])
    if not feed:
        return f"没找到资讯源：{args[0]}"
    return f"已退订：{feed.get('title', feed['id'])}"


def _import_opml(args) -> str:
    if not args:
        return "格式：/资讯 导入 OPML文件路径"
    path = " ".join(args)  # 路径可能含空格，不切分

    def job():
        res = service.import_opml(path)
        parts = [f"导入完成：新增 {len(res['added'])} 个"]
        if res["skipped"]:
            parts.append("跳过：" + "；".join(f"{u}（{why}）" for u, why in res["skipped"]))
        return "\n".join(parts)
    return _bg(job, "导入")


def handle(cmd: str, args: list[str]) -> str | None:
    """是资讯指令就返回回复文本，否则 None。"""
    if cmd not in ("资讯", "news"):
        return None
    sub = args[0] if args else ""
    if sub == "订阅":
        return _subscribe(args[1:])
    if sub == "源":
        return _list_feeds()
    if sub in ("退订", "取消订阅"):
        return _unsubscribe(args[1:])
    if sub == "导入":
        return _import_opml(args[1:])
    return _news(args)
