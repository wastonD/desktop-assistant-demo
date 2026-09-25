# -*- coding: utf-8 -*-
"""资讯源配置 config/news.json（RSS/Atom 源列表）。

含个人订阅内容，已加入 .gitignore；模板见 config/news.example.json。
源直接手改 JSON 添加/删除，不做账户管理那样的对话框（订阅源不涉及密码，改配置文件更直接）。
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "news.json"

DEFAULT_CONFIG = {
    "poll_min": 60,          # 轮询间隔（分钟），资讯不必像邮件那样频繁
    "per_feed_limit": 10,    # 每个源每次最多取几条
    "summarize": True,       # 有可用大模型时是否生成中文摘要
    "summarize_limit": 5,    # 每次轮询最多摘要几条，避免占用模型太久
    "feeds": [],
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save(cfg: dict) -> None:
    """写回 config/news.json；文件不存在时会自动创建（含父目录）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def enabled_feeds(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load()
    return [f for f in cfg.get("feeds", []) if f.get("enabled", True) and f.get("url") and f.get("id")]


def _make_id(url: str, existing: set[str]) -> str:
    """id 由域名生成（去掉 www.），和已有 id 冲突时加数字后缀。"""
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    base = re.sub(r"[^a-z0-9]+", "_", host).strip("_") or "feed"
    fid, n = base, 2
    while fid in existing:
        fid, n = f"{base}_{n}", n + 1
    return fid


def find_feed(ref: str, cfg: dict | None = None) -> dict | None:
    """按序号（1 起）或 id 找订阅源。"""
    feeds = (cfg or load()).get("feeds", [])
    ref = (ref or "").strip()
    if ref.isdigit() and 1 <= int(ref) <= len(feeds):
        return feeds[int(ref) - 1]
    for f in feeds:
        if ref.lower() == f.get("id", "").lower():
            return f
    return None


def add_feed(url: str, title: str = "") -> dict:
    """新增订阅源，写入 config/news.json；url 重复会报错。"""
    url = url.strip()
    cfg = load()
    feeds = cfg.setdefault("feeds", [])
    if any(f.get("url") == url for f in feeds):
        raise ValueError(f"{url} 已经订阅过了")
    feed = {"id": _make_id(url, {f["id"] for f in feeds}),
            "title": title.strip() or url, "url": url, "enabled": True}
    feeds.append(feed)
    save(cfg)
    return feed


def remove_feed(ref: str) -> dict | None:
    """按序号或 id 退订，返回被删的源；找不到返回 None。"""
    cfg = load()
    feed = find_feed(ref, cfg)
    if not feed:
        return None
    cfg["feeds"] = [f for f in cfg.get("feeds", []) if f.get("id") != feed["id"]]
    save(cfg)
    return feed
