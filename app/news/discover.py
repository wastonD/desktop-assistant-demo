# -*- coding: utf-8 -*-
"""从 HTML 页面里自动发现 RSS/Atom 订阅链接（标准库 re + urllib.parse，不引入 HTML 解析库）。

只处理 <link rel="alternate" type="application/rss+xml|atom+xml" href="..."> 这一种常见约定，
相对地址按页面地址补全成绝对地址。找不到就返回 None，交给调用方提示用户。
"""
import re
from urllib.parse import urljoin

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'''([a-zA-Z0-9:_-]+)\s*=\s*("([^"]*)"|'([^']*)')''')


def _attrs(tag: str) -> dict:
    out = {}
    for m in _ATTR_RE.finditer(tag):
        key = m.group(1).lower()
        out[key] = m.group(3) if m.group(3) is not None else m.group(4)
    return out


def find_feed_link(html_bytes: bytes, base_url: str) -> str | None:
    """在 HTML 里找 <link rel=alternate type=rss/atom>，优先 RSS；相对地址补全为绝对地址。"""
    try:
        text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None
    atom_fallback = None
    for tag in _LINK_TAG_RE.findall(text):
        attrs = _attrs(tag)
        rel = (attrs.get("rel") or "").lower().split()
        typ = (attrs.get("type") or "").lower()
        href = attrs.get("href")
        if not href or "alternate" not in rel:
            continue
        if "rss+xml" in typ:
            return urljoin(base_url, href)
        if "atom+xml" in typ and atom_fallback is None:
            atom_fallback = urljoin(base_url, href)
    return atom_fallback
