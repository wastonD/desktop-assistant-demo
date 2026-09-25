# -*- coding: utf-8 -*-
"""资讯后台服务：轮询所有源 → 按 guid/链接去重存 SQLite → 可选大模型中文摘要 → 未读数写 status.json。

- 轮询在后台线程跑（NewsService.poll_async），界面线程只读缓存，不碰网络。
- llm.availability() 为假时只存标题不摘要——功能分界：基础层不依赖大模型（app/commands.py 同一原则）。
"""
import sqlite3
import threading
from pathlib import Path

from .. import llm, status
from ..store import DB_PATH
from . import config, discover, fetch, opml, parse


class SubscribeError(Exception):
    pass


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS news_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_id TEXT NOT NULL,
        guid TEXT NOT NULL,
        title TEXT NOT NULL,
        link TEXT NOT NULL DEFAULT '',
        published TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        read INTEGER NOT NULL DEFAULT 0,
        fetched TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE (feed_id, guid))""")
    return conn


# ---------------- 订阅（/资讯 订阅 /资讯 导入） ----------------

def probe(url: str) -> dict:
    """把网址解析成可订阅的源：先当 RSS/Atom 直接解析；失败就当 HTML 页面找自动发现的订阅链接。

    返回 {"url": 实际订阅地址, "title": 源标题}；两种方式都失败抛 SubscribeError。
    """
    try:
        raw = fetch.fetch(url)
    except fetch.FetchError as e:
        raise SubscribeError(f"抓取失败：{e}") from None
    try:
        return {"url": url, "title": parse.parse_title(raw)}
    except parse.ParseError:
        pass
    feed_url = discover.find_feed_link(raw, url)
    if not feed_url:
        raise SubscribeError("这个网址不是 RSS/Atom 源，页面里也没找到自动发现的订阅链接")
    try:
        raw2 = fetch.fetch(feed_url)
    except fetch.FetchError as e:
        raise SubscribeError(f"自动发现的订阅地址抓取失败：{feed_url}（{e}）") from None
    try:
        return {"url": feed_url, "title": parse.parse_title(raw2)}
    except parse.ParseError as e:
        raise SubscribeError(f"自动发现的订阅地址解析失败：{feed_url}（{e}）") from None


def subscribe(url: str) -> dict:
    """订阅一个网址（联网，放后台线程调用），成功后写入 config/news.json。"""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise SubscribeError("请提供完整网址（http:// 或 https:// 开头）")
    info = probe(url)
    try:
        return config.add_feed(info["url"], info["title"])
    except ValueError as e:
        raise SubscribeError(str(e)) from None


def import_opml(path: str) -> dict:
    """从 OPML 文件批量订阅（联网，放后台线程调用）。返回 {'added': [...], 'skipped': [(url, 原因), ...]}。"""
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as e:
        raise SubscribeError(f"读取 OPML 文件失败：{e}") from None
    try:
        entries = opml.parse_outlines(raw)
    except parse.ParseError as e:
        raise SubscribeError(str(e)) from None
    if not entries:
        raise SubscribeError("OPML 文件里没有找到订阅源")
    added, skipped = [], []
    for e in entries:
        title = e["title"]
        if not title:
            try:
                title = probe(e["url"])["title"]
            except SubscribeError:
                title = ""  # 探测失败就用 id 兜底，不因单个源联网失败中断整批导入
        try:
            added.append(config.add_feed(e["url"], title))
        except ValueError as ve:
            skipped.append((e["url"], str(ve)))
    return {"added": added, "skipped": skipped}


class PollResult:
    def __init__(self):
        self.new_items: list[dict] = []
        self.errors: dict[str, str] = {}

    @property
    def total_new(self) -> int:
        return len(self.new_items)


def poll_once() -> PollResult:
    """同步轮询全部源（阻塞，放后台线程调用）。"""
    cfg = config.load()
    res = PollResult()
    # 摘要配额按整次轮询算（而不是按源），避免源一多就把本地模型占用很久
    budget = {"left": int(cfg.get("summarize_limit", 5))}
    for feed in config.enabled_feeds(cfg):
        try:
            xml_bytes = fetch.fetch(feed["url"])
            entries = parse.parse_feed(xml_bytes)
            res.new_items.extend(_save_new(feed, entries, cfg, budget))
        except (fetch.FetchError, parse.ParseError) as e:
            res.errors[feed["id"]] = str(e)
        except OSError as e:  # 断网、超时、DNS
            res.errors[feed["id"]] = f"网络错误：{e}"
    n = unread_count()
    status.update(news_ready=n if n else None)
    return res


def _save_new(feed: dict, entries: list[dict], cfg: dict, budget: dict) -> list[dict]:
    """写入新条目（按 guid 去重），返回本次新出现的条目。首次同步该源时不算"新"，避免开机刷屏。"""
    per_feed = int(cfg.get("per_feed_limit", 10))
    want_summary = bool(cfg.get("summarize", True)) and llm.availability()[0]
    with _db() as c:
        known = {r["guid"] for r in c.execute(
            "SELECT guid FROM news_items WHERE feed_id=?", (feed["id"],))}
    first_run = not known
    fresh = [e for e in entries[:per_feed] if e["guid"] not in known]
    # 摘要必须在数据库事务之外做：本地模型一条要几秒到十几秒，期间占着写锁会让
    # 收信/记日程/存对话等其他线程报 database is locked
    for e in fresh:
        e["_summary"] = ""
        if want_summary and budget["left"] > 0:
            e["_summary"] = _summarize(e)
            budget["left"] -= 1
    new = []
    with _db() as c:
        for e in fresh:
            c.execute("INSERT OR IGNORE INTO news_items "
                      "(feed_id, guid, title, link, published, summary) VALUES (?, ?, ?, ?, ?, ?)",
                      (feed["id"], e["guid"], e["title"], e["link"], e["published"], e["_summary"]))
            if not first_run:
                new.append({"feed_id": feed["id"], "title": e["title"], "link": e["link"],
                            "summary": e["_summary"]})
        # 每个源只留最近 200 条，避免无限增长
        c.execute("""DELETE FROM news_items WHERE feed_id=? AND id NOT IN (
            SELECT id FROM news_items WHERE feed_id=? ORDER BY id DESC LIMIT 200)""",
                  (feed["id"], feed["id"]))
    return new


def _summarize(entry: dict) -> str:
    text = parse.strip_html(entry.get("summary", ""))[:800]
    prompt = ("用一句话（不超过 40 字）中文概括下面这条资讯，不要加引号、不要复述标题：\n"
             f"标题：{entry['title']}\n内容：{text or '（无正文，只有标题）'}")
    try:
        return llm.generate([{"role": "user", "content": prompt}], use_tools=False).strip()
    except Exception:
        return ""  # 摘要失败不影响抓取，退化为只有标题


def unread_count() -> int:
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM news_items WHERE read=0").fetchone()[0]


def cached_recent(limit: int = 15, unread_only: bool = True) -> list[dict]:
    """从缓存读（不联网），按 id 新→旧。"""
    sql = "SELECT * FROM news_items"
    if unread_only:
        sql += " WHERE read=0"
    sql += " ORDER BY id DESC LIMIT ?"
    with _db() as c:
        return [dict(r) for r in c.execute(sql, (limit,))]


def mark_read(ids: list[int]) -> None:
    if not ids:
        return
    with _db() as c:
        c.executemany("UPDATE news_items SET read=1 WHERE id=?", [(i,) for i in ids])
    n = unread_count()
    status.update(news_ready=n if n else None)


def format_list(items: list[dict]) -> str:
    if not items:
        return "没有资讯。"
    lines = []
    for i, it in enumerate(items, 1):
        dot = "·" if it.get("read") else "●"
        extra = f"　{it['summary']}" if it.get("summary") else ""
        lines.append(f"{dot} {i}. [{it['feed_id']}] {it['title']}{extra}")
    return "\n".join(lines)


class NewsService:
    """挂在 main.py 上：定时触发后台抓取，结果通过回调交给界面线程（回调里请用信号切线程）。"""

    def __init__(self, on_result=None):
        self.on_result = on_result
        self._running = False
        self.last: PollResult | None = None

    def poll_async(self) -> bool:
        if self._running or not config.enabled_feeds():
            return False
        self._running = True

        def run():
            try:
                self.last = poll_once()
                if self.on_result:
                    self.on_result(self.last)
            finally:
                self._running = False
        threading.Thread(target=run, daemon=True, name="news-poll").start()
        return True
