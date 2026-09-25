# -*- coding: utf-8 -*-
"""资讯源框架：可配置 RSS/Atom 源抓取 + 可选大模型摘要（无模型时只列标题）。

config（config/news.json，源列表）→ fetch（urllib 抓取）→ parse（xml.etree 解析 RSS/Atom）
→ service（去重缓存 SQLite news_items、摘要、未读数写 status.json）→ commands（/ 指令）。
"""
