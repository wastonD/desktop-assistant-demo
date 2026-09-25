# -*- coding: utf-8 -*-
"""解析 RSS 2.0 / Atom 源（标准库 xml.etree），只取标题/链接/guid/发布时间/摘要。纯函数，便于测试。"""
import html
import re
import xml.etree.ElementTree as ET

ATOM_NS = "{http://www.w3.org/2005/Atom}"
RSS1_NS = "{http://purl.org/rss/1.0/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


class ParseError(Exception):
    pass


def _text(el, path) -> str:
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def strip_html(markup: str) -> str:
    """资讯描述里常混着 HTML 标签，摘要/展示前先转纯文本。"""
    if not markup:
        return ""
    markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", markup)
    text = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _parse_root(xml_bytes: bytes):
    """XML 字节 → (格式种类, 根节点)；无法识别或解析失败抛 ParseError。供 parse_feed/parse_title 共用。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ParseError(f"XML 解析失败：{e}") from None
    local = root.tag.rsplit("}", 1)[-1].lower()
    if local == "rss" or root.find("channel") is not None:
        return "rss", root
    if local == "feed":
        return "atom", root
    if local == "rdf":
        return "rdf", root
    raise ParseError(f"不认识的资讯源格式：{root.tag}")


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """返回 [{title, link, guid, published, summary}, …]；无法识别的格式抛 ParseError。"""
    kind, root = _parse_root(xml_bytes)
    if kind == "rss":
        return _parse_rss(root)
    if kind == "atom":
        return _parse_atom(root)
    return _parse_rss1(root)


def parse_title(xml_bytes: bytes) -> str:
    """取源标题（订阅时用作默认源名称）。不是合法 RSS/Atom/RDF 时抛 ParseError，用于订阅时判断"这不是一个源"。"""
    kind, root = _parse_root(xml_bytes)
    if kind == "rss":
        channel = root.find("channel")
        title = _text(channel, "title") if channel is not None else ""
    elif kind == "atom":
        title = _text(root, f"{ATOM_NS}title")
    else:
        channel = root.find(f"{RSS1_NS}channel")
        title = _text(channel, f"{RSS1_NS}title") if channel is not None else ""
    return title or "未命名资讯源"


def _parse_rss(root) -> list[dict]:
    channel = root.find("channel")
    if channel is None:
        raise ParseError("RSS 缺少 channel 节点")
    items = []
    for item in channel.findall("item"):
        link = _text(item, "link")
        guid = _text(item, "guid") or link
        if not guid:
            continue
        items.append({
            "title": _text(item, "title") or "（无标题）",
            "link": link,
            "guid": guid,
            "published": _text(item, "pubDate"),
            "summary": _text(item, "description"),
        })
    return items


def _atom_link(entry) -> str:
    href = ""
    for link in entry.findall(f"{ATOM_NS}link"):
        rel = link.get("rel")
        if rel in (None, "alternate"):
            return link.get("href", "")
        href = href or link.get("href", "")
    return href


def _parse_atom(root) -> list[dict]:
    items = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        link = _atom_link(entry)
        guid = _text(entry, f"{ATOM_NS}id") or link
        if not guid:
            continue
        summary = _text(entry, f"{ATOM_NS}summary") or _text(entry, f"{ATOM_NS}content")
        items.append({
            "title": _text(entry, f"{ATOM_NS}title") or "（无标题）",
            "link": link,
            "guid": guid,
            "published": _text(entry, f"{ATOM_NS}updated") or _text(entry, f"{ATOM_NS}published"),
            "summary": summary,
        })
    return items


def _parse_rss1(root) -> list[dict]:
    """RSS 1.0（RDF）：item 与 channel 平级，元素带 RSS1 命名空间，日期在 dc:date。"""
    items = []
    for item in root.findall(f"{RSS1_NS}item"):
        link = _text(item, f"{RSS1_NS}link")
        guid = item.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or link
        if not guid:
            continue
        items.append({
            "title": _text(item, f"{RSS1_NS}title") or "（无标题）",
            "link": link,
            "guid": guid,
            "published": _text(item, f"{DC_NS}date"),
            "summary": _text(item, f"{RSS1_NS}description"),
        })
    return items