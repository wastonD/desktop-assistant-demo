# -*- coding: utf-8 -*-
"""资讯源框架单元测试（不联网：mock 掉 fetch.fetch）。运行：python -m unittest discover tests"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import llm, status
from app.news import config, fetch, parse, service

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>示例频道</title>
<item>
<title>标题一</title>
<link>https://example.com/1</link>
<guid>https://example.com/1</guid>
<pubDate>Tue, 22 Sep 2026 08:00:00 +0800</pubDate>
<description><![CDATA[<p>这是第一条资讯的正文，&nbsp;讲了一些事情。</p>]]></description>
</item>
<item>
<title>标题二</title>
<link>https://example.com/2</link>
<guid>https://example.com/2</guid>
<pubDate>Tue, 22 Sep 2026 09:00:00 +0800</pubDate>
<description>第二条资讯正文。</description>
</item>
</channel>
</rss>""".encode("utf-8")

RSS_XML_UPDATED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>示例频道</title>
<item>
<title>标题一</title>
<link>https://example.com/1</link>
<guid>https://example.com/1</guid>
<pubDate>Tue, 22 Sep 2026 08:00:00 +0800</pubDate>
<description>这是第一条资讯的正文。</description>
</item>
<item>
<title>标题二</title>
<link>https://example.com/2</link>
<guid>https://example.com/2</guid>
<pubDate>Tue, 22 Sep 2026 09:00:00 +0800</pubDate>
<description>第二条资讯正文。</description>
</item>
<item>
<title>标题三（新）</title>
<link>https://example.com/3</link>
<guid>https://example.com/3</guid>
<pubDate>Tue, 22 Sep 2026 10:00:00 +0800</pubDate>
<description>第三条资讯，是新出现的。</description>
</item>
</channel>
</rss>""".encode("utf-8")

ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>示例 Atom 源</title>
<entry>
<title>Atom 标题一</title>
<link href="https://example.com/a1" rel="alternate"/>
<id>urn:uuid:1</id>
<updated>2026-09-22T08:00:00Z</updated>
<summary>Atom 摘要正文一。</summary>
</entry>
<entry>
<title>Atom 标题二</title>
<link href="https://example.com/a2"/>
<id>urn:uuid:2</id>
<updated>2026-09-22T09:00:00Z</updated>
<content type="html">&lt;p&gt;Atom 内容二&lt;/p&gt;</content>
</entry>
</feed>""".encode("utf-8")

NOT_XML = b"<html><body>\xff\xfe not xml</body></html>"
UNKNOWN_ROOT = b"<opml version=\"1.0\"><body/></opml>"


class ParseTests(unittest.TestCase):
    def test_parse_rss(self):
        items = parse.parse_feed(RSS_XML)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "标题一")
        self.assertEqual(items[0]["link"], "https://example.com/1")
        self.assertEqual(items[0]["guid"], "https://example.com/1")
        self.assertTrue(items[0]["published"].startswith("Tue, 22 Sep 2026"))
        self.assertIn("<p>", items[0]["summary"])  # 原始 HTML，展示/摘要前才 strip_html

    def test_parse_atom_prefers_alternate_link(self):
        items = parse.parse_feed(ATOM_XML)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["link"], "https://example.com/a1")
        self.assertEqual(items[0]["guid"], "urn:uuid:1")
        self.assertEqual(items[0]["summary"], "Atom 摘要正文一。")
        # 第二条没有 summary，退化用 content
        self.assertIn("Atom 内容二", items[1]["summary"])

    def test_unknown_format_raises(self):
        with self.assertRaises(parse.ParseError):
            parse.parse_feed(UNKNOWN_ROOT)

    def test_broken_xml_raises(self):
        with self.assertRaises(parse.ParseError):
            parse.parse_feed(NOT_XML)

    def test_strip_html(self):
        text = parse.strip_html("<p>你好&nbsp;世界</p><br>下一行<script>evil()</script>")
        self.assertNotIn("<", text)
        self.assertNotIn("evil", text)
        self.assertIn("你好", text)
        self.assertIn("下一行", text)

    def test_parse_title_rss_atom_rdf(self):
        self.assertEqual(parse.parse_title(RSS_XML), "示例频道")
        self.assertEqual(parse.parse_title(ATOM_XML), "示例 Atom 源")

    def test_parse_title_not_a_feed_raises(self):
        with self.assertRaises(parse.ParseError):
            parse.parse_title(UNKNOWN_ROOT)


HTML_WITH_RSS_LINK = (
    b'<html><head><title>Blog</title>'
    b'<link rel="alternate" type="application/rss+xml" title="Blog Feed" href="feed.xml">'
    b'</head><body>hi</body></html>')
HTML_WITH_ATOM_LINK = (
    b'<html><head>'
    b'<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
    b'</head><body>hi</body></html>')
HTML_NO_FEED_LINK = b"<html><head><title>Blog</title></head><body>hi</body></html>"


class DiscoverTests(unittest.TestCase):
    def test_finds_relative_rss_link(self):
        from app.news import discover
        url = discover.find_feed_link(HTML_WITH_RSS_LINK, "https://blog.example.com/index.html")
        self.assertEqual(url, "https://blog.example.com/feed.xml")

    def test_finds_absolute_atom_link(self):
        from app.news import discover
        url = discover.find_feed_link(HTML_WITH_ATOM_LINK, "https://blog.example.com/index.html")
        self.assertEqual(url, "https://blog.example.com/atom.xml")

    def test_prefers_rss_over_atom_even_when_atom_appears_first(self):
        from app.news import discover
        html = (b'<html><head>'
                b'<link rel="alternate" type="application/atom+xml" href="/atom.xml">'
                b'<link rel="alternate" type="application/rss+xml" href="feed.xml">'
                b'</head><body>hi</body></html>')
        url = discover.find_feed_link(html, "https://blog.example.com/")
        self.assertEqual(url, "https://blog.example.com/feed.xml")

    def test_no_link_returns_none(self):
        from app.news import discover
        self.assertIsNone(discover.find_feed_link(HTML_NO_FEED_LINK, "https://blog.example.com/"))


OPML_SAMPLE = b"""<?xml version="1.0"?>
<opml version="1.0"><body>
<outline text="Tech" title="Tech">
<outline type="rss" text="Feed A" title="Feed A" xmlUrl="https://a.example.com/rss.xml"/>
<outline type="rss" text="Feed B" xmlUrl="https://b.example.com/atom.xml"/>
</outline>
<outline type="rss" text="Top Feed" xmlUrl="https://c.example.com/rss.xml"/>
</body></opml>"""


class OpmlTests(unittest.TestCase):
    def test_flattens_nested_categories(self):
        from app.news import opml
        feeds = opml.parse_outlines(OPML_SAMPLE)
        self.assertEqual([f["url"] for f in feeds],
                         ["https://a.example.com/rss.xml", "https://b.example.com/atom.xml",
                          "https://c.example.com/rss.xml"])
        self.assertEqual(feeds[0]["title"], "Feed A")
        self.assertEqual(feeds[1]["title"], "Feed B")  # 没有 title 属性，退化用 text

    def test_missing_body_raises(self):
        from app.news import opml
        with self.assertRaises(parse.ParseError):
            opml.parse_outlines(b"<opml version=\"1.0\"></opml>")

    def test_broken_xml_raises(self):
        from app.news import opml
        with self.assertRaises(parse.ParseError):
            opml.parse_outlines(NOT_XML)


class FetchTests(unittest.TestCase):
    def test_http_error_wrapped(self):
        import urllib.error

        def raise_http(*a, **kw):
            raise urllib.error.HTTPError("u", 404, "not found", {}, None)

        with mock.patch("app.news.fetch.urllib.request.urlopen", side_effect=raise_http):
            with self.assertRaises(fetch.FetchError):
                fetch.fetch("https://example.com/rss.xml")

    def test_success_returns_bytes(self):
        cm = mock.MagicMock()
        cm.read.return_value = RSS_XML
        cm.__enter__.return_value = cm
        with mock.patch("app.news.fetch.urllib.request.urlopen", return_value=cm):
            self.assertEqual(fetch.fetch("https://example.com/rss.xml"), RSS_XML)


FEEDS_CFG = {
    "poll_min": 60, "per_feed_limit": 10, "summarize": True, "summarize_limit": 5,
    "feeds": [
        {"id": "rss_feed", "name": "RSS 源", "url": "https://example.com/rss.xml", "enabled": True},
        {"id": "atom_feed", "name": "Atom 源", "url": "https://example.com/atom.xml", "enabled": True},
        {"id": "off_feed", "name": "停用源", "url": "https://example.com/off.xml", "enabled": False},
    ],
}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for target, attr, value in (
                (service, "DB_PATH", tmp / "t.db"),
                (status, "STATUS_PATH", tmp / "status.json"),
                (config, "CONFIG_PATH", tmp / "news.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        config.CONFIG_PATH.write_text(json.dumps(FEEDS_CFG), encoding="utf-8")
        self._urls = {"https://example.com/rss.xml": RSS_XML,
                     "https://example.com/atom.xml": ATOM_XML}
        p = mock.patch.object(service.fetch, "fetch", side_effect=self._fake_fetch)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(llm, "availability", return_value=(False, "未配置模型"))
        p.start()
        self.addCleanup(p.stop)

    def _fake_fetch(self, url, timeout=15):
        if url not in self._urls:
            raise fetch.FetchError("404")
        return self._urls[url]

    def test_first_poll_caches_but_not_new_and_no_summary_without_llm(self):
        res = service.poll_once()
        self.assertEqual(res.total_new, 0)  # 首次同步不算"新资讯"，避免开机刷屏
        self.assertEqual(res.errors, {})
        cached = service.cached_recent(limit=50, unread_only=False)
        self.assertEqual(len(cached), 4)  # 2 RSS + 2 Atom，停用源不抓
        self.assertTrue(all(it["summary"] == "" for it in cached))  # 没有模型，只有标题
        self.assertEqual(status.read()["news_ready"], 4)

    def test_repoll_same_feed_no_duplicate(self):
        service.poll_once()
        res = service.poll_once()
        self.assertEqual(res.total_new, 0)
        self.assertEqual(len(service.cached_recent(limit=50, unread_only=False)), 4)

    def test_new_item_detected_on_next_poll(self):
        service.poll_once()
        self._urls["https://example.com/rss.xml"] = RSS_XML_UPDATED
        res = service.poll_once()
        self.assertEqual(res.total_new, 1)
        self.assertEqual(res.new_items[0]["title"], "标题三（新）")
        self.assertEqual(len(service.cached_recent(limit=50, unread_only=False)), 5)
        self.assertEqual(status.read()["news_ready"], 5)

    def test_fetch_error_reported_per_feed(self):
        del self._urls["https://example.com/atom.xml"]
        res = service.poll_once()
        self.assertIn("atom_feed", res.errors)
        self.assertEqual(len(service.cached_recent(limit=50, unread_only=False)), 2)

    def test_summary_generated_when_llm_available_and_budget_respected(self):
        with mock.patch.object(llm, "availability", return_value=(True, "ok")), \
                mock.patch.object(llm, "generate", return_value="一句话摘要") as gen:
            cfg = dict(FEEDS_CFG, summarize_limit=1)
            with mock.patch.object(config, "load", return_value=cfg):
                service.poll_once()
            self.assertEqual(gen.call_count, 1)  # 配额按整次轮询算，不是按源
        cached = {it["title"]: it for it in service.cached_recent(limit=50, unread_only=False)}
        summarized = [it for it in cached.values() if it["summary"]]
        self.assertEqual(len(summarized), 1)
        self.assertEqual(summarized[0]["summary"], "一句话摘要")

    def test_summary_failure_falls_back_to_empty(self):
        with mock.patch.object(llm, "availability", return_value=(True, "ok")), \
                mock.patch.object(llm, "generate", side_effect=llm.LLMError("挂了")):
            res = service.poll_once()
        self.assertEqual(res.errors, {})  # 摘要失败不影响抓取本身
        cached = service.cached_recent(limit=50, unread_only=False)
        self.assertTrue(all(it["summary"] == "" for it in cached))

    def test_mark_read_updates_unread_count_and_status(self):
        service.poll_once()
        unread = service.cached_recent(limit=50)
        self.assertEqual(len(unread), 4)
        service.mark_read([it["id"] for it in unread[:2]])
        self.assertEqual(service.unread_count(), 2)
        self.assertEqual(status.read()["news_ready"], 2)
        self.assertEqual(len(service.cached_recent(limit=50)), 2)

    def test_mark_all_read_clears_status_key(self):
        service.poll_once()
        ids = [it["id"] for it in service.cached_recent(limit=50)]
        service.mark_read(ids)
        self.assertNotIn("news_ready", status.read())  # update() 里 None 值会删掉这个键

    def test_format_list(self):
        service.poll_once()
        items = service.cached_recent(limit=50)
        text = service.format_list(items)
        self.assertIn("标题一", text)
        self.assertIn("[rss_feed]", text)
        self.assertTrue(text.startswith("●"))

    def test_format_list_empty(self):
        self.assertEqual(service.format_list([]), "没有资讯。")

    def test_poll_async_skips_without_feeds(self):
        with mock.patch.object(config, "load", return_value={"feeds": []}):
            svc = service.NewsService()
            self.assertFalse(svc.poll_async())


class ConfigTests(unittest.TestCase):
    def test_enabled_feeds_filters_disabled_and_incomplete(self):
        cfg = {"feeds": [
            {"id": "a", "url": "https://x", "enabled": True},
            {"id": "b", "url": "https://y", "enabled": False},
            {"url": "https://z", "enabled": True},   # 缺 id
            {"id": "d", "enabled": True},             # 缺 url
        ]}
        self.assertEqual([f["id"] for f in config.enabled_feeds(cfg)], ["a"])

    def test_load_defaults_when_missing(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(config, "CONFIG_PATH", tmp / "news.json"):
            cfg = config.load()
        self.assertEqual(cfg["feeds"], [])
        self.assertEqual(cfg["poll_min"], 60)


class ConfigFeedTests(unittest.TestCase):
    """订阅管理：add_feed/remove_feed/find_feed，config/news.json 不存在时自动创建。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(config, "CONFIG_PATH", tmp / "news.json")
        p.start()
        self.addCleanup(p.stop)

    def test_add_feed_creates_file_with_id_from_domain(self):
        self.assertFalse(config.CONFIG_PATH.exists())
        feed = config.add_feed("https://www.example.com/rss.xml", "示例频道")
        self.assertTrue(config.CONFIG_PATH.exists())
        self.assertEqual(feed["id"], "example_com")
        self.assertEqual(feed["title"], "示例频道")
        self.assertEqual(config.load()["feeds"], [feed])

    def test_add_feed_duplicate_url_raises(self):
        config.add_feed("https://example.com/rss.xml", "A")
        with self.assertRaises(ValueError):
            config.add_feed("https://example.com/rss.xml", "A 换个名字")

    def test_add_feed_id_dedup_suffix(self):
        f1 = config.add_feed("https://example.com/a.xml", "A")
        f2 = config.add_feed("https://example.com/b.xml", "B")  # 同域名，id 要加后缀
        self.assertEqual(f1["id"], "example_com")
        self.assertEqual(f2["id"], "example_com_2")

    def test_add_feed_no_title_falls_back_to_url(self):
        feed = config.add_feed("https://example.com/rss.xml")
        self.assertEqual(feed["title"], "https://example.com/rss.xml")

    def test_find_feed_by_index_and_id(self):
        f1 = config.add_feed("https://a.example.com/rss.xml", "A")
        config.add_feed("https://b.example.com/rss.xml", "B")
        self.assertEqual(config.find_feed("1")["id"], f1["id"])
        self.assertEqual(config.find_feed(f1["id"])["id"], f1["id"])
        self.assertIsNone(config.find_feed("不存在"))

    def test_remove_feed_by_index(self):
        config.add_feed("https://a.example.com/rss.xml", "A")
        config.add_feed("https://b.example.com/rss.xml", "B")
        removed = config.remove_feed("1")
        self.assertEqual(removed["title"], "A")
        self.assertEqual([f["title"] for f in config.load()["feeds"]], ["B"])

    def test_remove_unknown_returns_none(self):
        self.assertIsNone(config.remove_feed("不存在"))


class SubscribeServiceTests(unittest.TestCase):
    """service.probe/subscribe/import_opml：网络一律 mock 掉 service.fetch.fetch。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(config, "CONFIG_PATH", tmp / "news.json")
        p.start()
        self.addCleanup(p.stop)
        self._urls: dict[str, bytes] = {}
        p = mock.patch.object(service.fetch, "fetch", side_effect=self._fake_fetch)
        p.start()
        self.addCleanup(p.stop)

    def _fake_fetch(self, url, timeout=15):
        if url not in self._urls:
            raise fetch.FetchError("404")
        return self._urls[url]

    def test_probe_direct_feed(self):
        self._urls["https://example.com/rss.xml"] = RSS_XML
        info = service.probe("https://example.com/rss.xml")
        self.assertEqual(info, {"url": "https://example.com/rss.xml", "title": "示例频道"})

    def test_probe_discovers_from_html_page(self):
        self._urls["https://blog.example.com/"] = HTML_WITH_RSS_LINK
        self._urls["https://blog.example.com/feed.xml"] = RSS_XML
        info = service.probe("https://blog.example.com/")
        self.assertEqual(info, {"url": "https://blog.example.com/feed.xml", "title": "示例频道"})

    def test_probe_no_feed_and_no_link_fails(self):
        self._urls["https://blog.example.com/"] = HTML_NO_FEED_LINK
        with self.assertRaises(service.SubscribeError):
            service.probe("https://blog.example.com/")

    def test_probe_fetch_error(self):
        with self.assertRaises(service.SubscribeError):
            service.probe("https://not-reachable.example.com/")

    def test_subscribe_writes_config(self):
        self._urls["https://example.com/rss.xml"] = RSS_XML
        feed = service.subscribe("https://example.com/rss.xml")
        self.assertEqual(feed["id"], "example_com")
        self.assertEqual(feed["title"], "示例频道")
        self.assertEqual(len(config.load()["feeds"]), 1)

    def test_subscribe_rejects_bad_scheme(self):
        with self.assertRaises(service.SubscribeError):
            service.subscribe("example.com/rss.xml")

    def test_subscribe_duplicate_raises(self):
        self._urls["https://example.com/rss.xml"] = RSS_XML
        service.subscribe("https://example.com/rss.xml")
        with self.assertRaises(service.SubscribeError):
            service.subscribe("https://example.com/rss.xml")

    def test_import_opml_batch(self):
        p = Path(tempfile.mkdtemp()) / "feeds.opml"
        p.write_bytes(OPML_SAMPLE)
        res = service.import_opml(str(p))
        self.assertEqual(len(res["added"]), 3)
        self.assertEqual(res["skipped"], [])
        self.assertEqual(len(config.load()["feeds"]), 3)

    def test_import_opml_skips_existing(self):
        config.add_feed("https://a.example.com/rss.xml", "已经订阅过")
        p = Path(tempfile.mkdtemp()) / "feeds.opml"
        p.write_bytes(OPML_SAMPLE)
        res = service.import_opml(str(p))
        self.assertEqual(len(res["added"]), 2)
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(res["skipped"][0][0], "https://a.example.com/rss.xml")

    def test_import_opml_missing_file_raises(self):
        with self.assertRaises(service.SubscribeError):
            service.import_opml("/tmp/does-not-exist-xyz.opml")


class CommandsTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for target, attr, value in (
                (service, "DB_PATH", tmp / "t.db"),
                (status, "STATUS_PATH", tmp / "status.json"),
                (config, "CONFIG_PATH", tmp / "news.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_no_feeds_configured(self):
        from app.news import commands as news_commands
        self.assertIn("还没有配置资讯源", news_commands.handle("资讯", []))

    def test_list_marks_read(self):
        from app.news import commands as news_commands
        config.CONFIG_PATH.write_text(json.dumps(FEEDS_CFG), encoding="utf-8")
        with mock.patch.object(service.fetch, "fetch",
                               side_effect=lambda url, timeout=15: RSS_XML if "rss" in url else ATOM_XML), \
                mock.patch.object(llm, "availability", return_value=(False, "no model")):
            service.poll_once()
        first = news_commands.handle("资讯", [])
        self.assertIn("标题一", first)
        second = news_commands.handle("资讯", [])
        self.assertIn("没有未读资讯", second)

    def test_unknown_command_returns_none(self):
        from app.news import commands as news_commands
        self.assertIsNone(news_commands.handle("日程", []))

    def test_subscribe_and_list_and_unsubscribe(self):
        from app.news import commands as news_commands
        done, results = threading.Event(), []
        news_commands.set_notifier(lambda text: (results.append(text), done.set()))
        self.addCleanup(news_commands.set_notifier, None)
        with mock.patch.object(service, "subscribe",
                               return_value={"id": "example_com", "title": "示例频道"}):
            reply = news_commands.handle("资讯", ["订阅", "https://example.com/rss.xml"])
        self.assertEqual(reply, "订阅中…")
        self.assertTrue(done.wait(2), "后台订阅任务没有在超时内完成")
        self.assertIn("订阅成功", results[0])
        self.assertIn("示例频道", results[0])

        config.add_feed("https://example.com/rss.xml", "示例频道")
        listed = news_commands.handle("资讯", ["源"])
        self.assertIn("示例频道", listed)
        self.assertIn("example_com", listed)

        unsub = news_commands.handle("资讯", ["退订", "1"])
        self.assertIn("已退订", unsub)
        self.assertEqual(config.load()["feeds"], [])

    def test_subscribe_failure_reported_via_notifier(self):
        from app.news import commands as news_commands
        done, results = threading.Event(), []
        news_commands.set_notifier(lambda text: (results.append(text), done.set()))
        self.addCleanup(news_commands.set_notifier, None)
        with mock.patch.object(service, "subscribe", side_effect=service.SubscribeError("抓取失败：404")):
            news_commands.handle("资讯", ["订阅", "https://bad.example.com/"])
        self.assertTrue(done.wait(2))
        self.assertIn("订阅失败", results[0])
        self.assertIn("抓取失败", results[0])

    def test_list_feeds_empty(self):
        from app.news import commands as news_commands
        self.assertIn("还没有订阅任何资讯源", news_commands.handle("资讯", ["源"]))

    def test_unsubscribe_unknown_ref(self):
        from app.news import commands as news_commands
        self.assertIn("没找到资讯源", news_commands.handle("资讯", ["退订", "9"]))

    def test_subscribe_without_url_shows_usage(self):
        from app.news import commands as news_commands
        self.assertIn("格式", news_commands.handle("资讯", ["订阅"]))

    def test_import_without_path_shows_usage(self):
        from app.news import commands as news_commands
        self.assertIn("格式", news_commands.handle("资讯", ["导入"]))

    def test_import_command_reports_result(self):
        from app.news import commands as news_commands
        done, results = threading.Event(), []
        news_commands.set_notifier(lambda text: (results.append(text), done.set()))
        self.addCleanup(news_commands.set_notifier, None)
        with mock.patch.object(service, "import_opml",
                               return_value={"added": [{"id": "a"}, {"id": "b"}], "skipped": []}):
            reply = news_commands.handle("资讯", ["导入", "/tmp/feeds.opml"])
        self.assertEqual(reply, "导入中…")
        self.assertTrue(done.wait(2))
        self.assertIn("新增 2 个", results[0])


RDF_SAMPLE = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://x.org/"><title>X</title></channel>
  <item rdf:about="https://x.org/1"><title>RDF \xe6\xa0\x87\xe9\xa2\x98</title>
    <link>https://x.org/1</link><dc:date>2026-09-22T08:00:00+08:00</dc:date>
    <description>desc</description></item>
</rdf:RDF>"""


class Rss1Tests(unittest.TestCase):
    def test_rdf(self):
        from app.news import parse as news_parse
        items = news_parse.parse_feed(RDF_SAMPLE)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "RDF 标题")
        self.assertEqual(items[0]["guid"], "https://x.org/1")
        self.assertTrue(items[0]["published"].startswith("2026-09-22"))


if __name__ == "__main__":
    unittest.main()