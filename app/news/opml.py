# -*- coding: utf-8 -*-
"""解析 OPML 订阅列表（标准库 xml.etree），供 /资讯 导入 批量订阅使用。"""
import xml.etree.ElementTree as ET

from .parse import ParseError


def parse_outlines(xml_bytes: bytes) -> list[dict]:
    """返回 [{title, url}, …]；outline 可能嵌套分类文件夹，递归展开。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ParseError(f"OPML 解析失败：{e}") from None
    body = root.find("body")
    if body is None:
        raise ParseError("OPML 缺少 body 节点")
    feeds: list[dict] = []

    def walk(el):
        for outline in el.findall("outline"):
            url = outline.get("xmlUrl")
            if url:
                title = outline.get("title") or outline.get("text") or ""
                feeds.append({"title": title.strip(), "url": url.strip()})
            walk(outline)  # 分类文件夹本身也是 outline，没有 xmlUrl，递归展开其子项

    walk(body)
    return feeds
