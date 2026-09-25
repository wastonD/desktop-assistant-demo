# -*- coding: utf-8 -*-
"""标准库抓取 RSS/Atom 源（urllib，零新依赖）。"""
import urllib.error
import urllib.request

TIMEOUT = 15
USER_AGENT = "DesktopAssistant/1.0 news-reader"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"


class FetchError(Exception):
    pass


def fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"网络错误：{e}") from None
