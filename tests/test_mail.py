# -*- coding: utf-8 -*-
"""邮件适配器单元测试（不联网：用假的 IMAP/SMTP 对象）。运行：python -m unittest discover tests"""
import email
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import status
from app.mail import accounts, client, credentials, parse, providers, service

credentials.use_memory = True  # 测试绝不碰真实凭据管理器

RAW_GBK = (
    "From: =?gb2312?B?1cXI/Q==?= <zhangsan@163.com>\r\n"
    "To: me@qq.com\r\n"
    "Subject: =?gb2312?B?w/fM7L+qu+E=?=\r\n"
    "Date: Tue, 22 Sep 2026 09:30:00 +0800\r\n"
    "Message-ID: <abc@163.com>\r\n"
    "Content-Type: text/plain; charset=gb2312\r\n"
    "Content-Transfer-Encoding: base64\r\n\r\n"
).encode() + __import__("base64").encodebytes("明天下午三点开会，请准时。\r\n\r\n> 旧邮件引用".encode("gb18030"))

RAW_MULTI = b"""From: "Bob" <bob@gmail.com>
To: me@outlook.com
Subject: =?utf-8?q?Weekly_report_=E5=91=A8=E6=8A=A5?=
Date: Mon, 21 Sep 2026 18:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="XX"

--XX
Content-Type: text/html; charset=utf-8

<html><head><style>p{}</style></head><body><p>Hello&nbsp;there</p><br>See attached.</body></html>
--XX
Content-Type: application/pdf; name="r.pdf"
Content-Disposition: attachment; filename="=?utf-8?b?5ZGo5oqlLnBkZg==?="
Content-Transfer-Encoding: base64

JVBERi0=
--XX--
"""


class ParseTests(unittest.TestCase):
    def test_gbk_header_and_body(self):
        s = parse.summarize(RAW_GBK)
        self.assertEqual(s["subject"], "明天开会")
        self.assertEqual(s["from"], "张三 <zhangsan@163.com>")
        self.assertIn("明天下午三点开会", s["snippet"])
        self.assertNotIn("旧邮件引用", s["snippet"])
        self.assertTrue(s["date"].startswith("2026-09-22"))

    def test_html_fallback_and_attachment(self):
        s = parse.summarize(RAW_MULTI)
        self.assertEqual(s["subject"], "Weekly report 周报")
        self.assertIn("Hello there", s["snippet"])
        self.assertNotIn("p{}", s["snippet"])
        self.assertEqual(s["attachments"], ["周报.pdf"])

    def test_truncated_message_does_not_crash(self):
        s = parse.summarize(RAW_MULTI[:200])
        self.assertIn("Weekly", s["subject"])

    def test_reply_chain_cut(self):
        text = "好的收到\n\n在 2026年9月21日 周一 写道：\n以前的内容"
        self.assertEqual(parse.clean_snippet(text), "好的收到")

    def test_build_message_roundtrip(self):
        msg = parse.build_message("me@qq.com", ["a@b.com"], "测试主题", "正文内容", sender_name="小银")
        back = email.message_from_bytes(msg.as_bytes())
        self.assertEqual(parse.decode_mime_header(back["Subject"]), "测试主题")
        self.assertIn("正文内容", parse.body_text(back))


class ProviderTests(unittest.TestCase):
    def test_guess(self):
        self.assertEqual(providers.guess_provider("x@QQ.com"), "qq")
        self.assertEqual(providers.guess_provider("x@163.com"), "163")
        self.assertEqual(providers.guess_provider("x@hotmail.com"), "outlook")
        self.assertEqual(providers.guess_provider("x@gmail.com"), "gmail")
        self.assertIsNone(providers.guess_provider("x@mail.example.edu"))

    def test_settings(self):
        s = providers.server_settings({"provider": "163"})
        self.assertEqual((s["imap_host"], s["imap_port"], s["imap_id"]), ("imap.163.com", 993, True))
        s = providers.server_settings({"provider": "outlook"})
        self.assertEqual((s["auth"], s["smtp_security"]), ("oauth2", "starttls"))
        s = providers.server_settings({"provider": "custom", "imap_host": "mail.x.com", "smtp_port": 587,
                                       "smtp_security": "starttls"})
        self.assertEqual((s["imap_host"], s["smtp_port"]), ("mail.x.com", 587))


class FetchParseTests(unittest.TestCase):
    def test_flags_before_and_after_literal(self):
        data = [(b"1 (UID 11 FLAGS (\\Seen) BODY[]<0> {3}", b"abc"), b")",
                (b"2 (UID 12 BODY[]<0> {3}", b"def"), b" FLAGS ())"]
        items = client.parse_fetch_response(data)
        self.assertEqual([m["uid"] for m, _ in items], ["11", "12"])
        self.assertIn("\\Seen", items[0][0]["flags"])
        self.assertEqual(items[1][0]["flags"], "")


class FakeIMAP:
    """模拟 IMAP4_SSL：收件箱两封（一封已读），记录发出的命令。"""
    instances = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.commands, self.logged_in = host, [], None
        self.mails = {"11": (RAW_GBK, "\\Seen"), "12": (RAW_MULTI, "")}
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        if pw != "right-code":
            raise client.imaplib.IMAP4.error("LOGIN failed")
        self.logged_in = user
        return "OK", [b"ok"]

    def authenticate(self, mech, cb):
        self.commands.append(("AUTH", mech, cb(b"")))
        return "OK", [b"ok"]

    def _simple_command(self, name, *args):
        self.commands.append((name,) + args)
        return "OK", [b""]

    def noop(self):
        return "OK", [b""]

    def status(self, folder, what):
        return "OK", [b'"INBOX" (UNSEEN 1)']

    def select(self, folder, readonly=False):
        self.commands.append(("SELECT", folder, readonly))
        return "OK", [str(len(self.mails)).encode()]

    def fetch(self, rng, what):
        self.commands.append(("FETCH", rng, what))
        return "OK", self._resp(list(self.mails))

    def uid(self, cmd, *args):
        self.commands.append(("UID", cmd) + args)
        if cmd == "SEARCH":
            return "OK", [b" ".join(u.encode() for u, (_, f) in self.mails.items() if not f)]
        if cmd == "FETCH":
            return "OK", self._resp(args[0].split(","))
        return "OK", [b""]

    def _resp(self, uids):
        out = []
        for i, u in enumerate(uids, 1):
            raw, flags = self.mails[u]
            out += [(f"{i} (UID {u} FLAGS ({flags}) BODY[]<0> {{{len(raw)}}}".encode(), raw), b")"]
        return out

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "&XfJT0ZAB-"']

    def append(self, *args):
        self.commands.append(("APPEND",) + args[:2])
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class ClientTests(unittest.TestCase):
    def setUp(self):
        FakeIMAP.instances.clear()
        credentials._memory.clear()
        p = mock.patch.object(client.imaplib, "IMAP4_SSL", FakeIMAP)
        p.start()
        self.addCleanup(p.stop)

    def test_163_sends_id_and_reads_without_marking(self):
        credentials.set_secret(credentials.password_key("a"), "right-code")
        c = client.MailClient({"id": "a", "address": "me@163.com", "provider": "163"})
        self.assertEqual(c.unread_count(), 1)
        mails = c.recent(5)
        imap = FakeIMAP.instances[0]
        self.assertTrue(any(cmd[0] == "ID" for cmd in imap.commands))
        self.assertTrue(all(cmd[2] for cmd in imap.commands if cmd[0] == "SELECT"))  # readonly
        self.assertTrue(all("PEEK" in cmd[2] for cmd in imap.commands if cmd[0] == "FETCH"))
        self.assertEqual([m["uid"] for m in mails], ["12", "11"])
        self.assertFalse(mails[0]["seen"])
        self.assertTrue(mails[1]["seen"])

    def test_unseen_only(self):
        credentials.set_secret(credentials.password_key("a"), "right-code")
        c = client.MailClient({"id": "a", "address": "me@qq.com", "provider": "qq"})
        mails = c.recent(5, unseen_only=True)
        self.assertEqual([m["subject"] for m in mails], ["Weekly report 周报"])
        self.assertFalse(any(cmd[0] == "ID" for cmd in FakeIMAP.instances[0].commands))

    def test_wrong_password(self):
        credentials.set_secret(credentials.password_key("a"), "wrong")
        c = client.MailClient({"id": "a", "address": "me@qq.com", "provider": "qq"})
        with self.assertRaises(client.AuthFailed):
            c.unread_count()

    def test_missing_password(self):
        c = client.MailClient({"id": "zz", "address": "me@qq.com", "provider": "qq"})
        with self.assertRaises(client.AuthFailed):
            c.unread_count()

    def test_oauth_xoauth2(self):
        from app.mail import oauth
        oauth._access_cache["o"] = ("TOKEN", 1e12)
        c = client.MailClient({"id": "o", "address": "me@outlook.com", "provider": "outlook"})
        c.unread_count()
        auth = [cmd for cmd in FakeIMAP.instances[0].commands if cmd[0] == "AUTH"][0]
        self.assertEqual(auth[1], "XOAUTH2")
        self.assertEqual(auth[2], b"user=me@outlook.com\x01auth=Bearer TOKEN\x01\x01")

    def test_oauth_needs_login(self):
        from app.mail import oauth
        oauth._access_cache.clear()
        c = client.MailClient({"id": "o2", "address": "me@outlook.com", "provider": "outlook"})
        with self.assertRaises(oauth.NeedsLogin):
            c.unread_count()

    def test_sent_folder(self):
        credentials.set_secret(credentials.password_key("a"), "right-code")
        c = client.MailClient({"id": "a", "address": "me@163.com", "provider": "163"})
        self.assertEqual(c.sent_folder(), "&XfJT0ZAB-")


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host = host

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, u, p):
        if p != "right-code":
            raise client.smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        FakeIMAP.instances.clear()
        FakeSMTP.sent.clear()
        credentials._memory.clear()
        tmp = Path(tempfile.mkdtemp())
        for target, attr, value in (
                (service, "DB_PATH", tmp / "t.db"),
                (status, "STATUS_PATH", tmp / "status.json"),
                (accounts, "CONFIG_PATH", tmp / "mail.json"),
                (client.imaplib, "IMAP4_SSL", FakeIMAP),
                (client.smtplib, "SMTP_SSL", FakeSMTP)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_add_poll_status_and_cache(self):
        acc = accounts.add_account("me@163.com", secret="right-code")
        self.assertEqual(acc["provider"], "163")
        self.assertNotIn("right-code", accounts.CONFIG_PATH.read_text(encoding="utf-8"))
        res = service.poll_once()
        self.assertEqual(res.total_unread, 1)
        self.assertEqual(res.new_mail, [])  # 首次同步不算"新邮件"，避免开机刷屏
        self.assertEqual(status.read()["unread_mail"], 1)
        cached = service.cached_recent(unseen_only=True)
        self.assertEqual(len(cached), 1)
        self.assertIn("周报", service.format_list(cached))

    def test_poll_reports_auth_error(self):
        accounts.add_account("me@qq.com", secret="wrong")
        res = service.poll_once()
        self.assertIn("登录失败", list(res.errors.values())[0])
        self.assertIn("mail_errors", status.read())

    def test_draft_then_send(self):
        accounts.add_account("me@qq.com", secret="right-code")
        d = service.create_draft(None, "a@b.com", "你好", "正文")
        self.assertEqual(d["status"], "draft")
        self.assertEqual(FakeSMTP.sent, [])  # 起草不发送
        service.send_draft(d["id"])
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(service.get_draft(d["id"])["status"], "sent")
        appended = [c for c in FakeIMAP.instances[-1].commands if c[0] == "APPEND"]
        self.assertTrue(appended)  # 国内邮箱补存已发送
        with self.assertRaises(client.MailError):
            service.send_draft(d["id"])  # 不能重复发

    def test_duplicate_account(self):
        accounts.add_account("me@qq.com", secret="x")
        with self.assertRaises(ValueError):
            accounts.add_account("ME@qq.com")

    def test_mark_seen_updates_cache_and_status(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        self.assertEqual(status.read()["unread_mail"], 1)
        m = service.cached_recent(unseen_only=True)[0]
        result = service.mark_seen(m["account"], m["uid"])
        self.assertEqual(result["uid"], m["uid"])
        self.assertEqual(service.cached_recent(unseen_only=True), [])
        self.assertNotIn("unread_mail", status.read())  # 减到 0 时键被删掉（同 news 的 update() 语义）
        imap = FakeIMAP.instances[-1]
        self.assertTrue(any(cmd[:2] == ("UID", "STORE") for cmd in imap.commands))

    def test_mark_seen_unknown_account_raises(self):
        with self.assertRaises(client.MailError):
            service.mark_seen("不存在的账户", "1")

    def test_search_cached_matches_sender_subject_snippet(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        results = service.search_cached("weekly")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["subject"], "Weekly report 周报")
        self.assertEqual(service.search_cached("完全不会命中的关键词"), [])

    def test_create_reply_draft_fields(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        m = service.cached_recent(unseen_only=True)[0]  # Weekly report，来自 Bob <bob@gmail.com>
        d = service.create_reply_draft(m, "好的收到")
        self.assertEqual(d["to_addr"], "bob@gmail.com")
        self.assertTrue(d["subject"].startswith("Re: "))
        self.assertIn("好的收到", d["body"])
        self.assertIn("原始邮件", d["body"])
        self.assertEqual(d["in_reply_to"], m.get("message_id", ""))

    def test_create_reply_draft_no_double_re_prefix(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        m = dict(service.cached_recent(unseen_only=True)[0])
        m["subject"] = "Re: 已经是回复了"
        d = service.create_reply_draft(m, "")
        self.assertEqual(d["subject"], "Re: 已经是回复了")
        self.assertIn("请补充回复内容", d["body"])  # 正文留空时的占位提示

    def test_create_reply_draft_missing_sender_raises(self):
        with self.assertRaises(client.MailError):
            service.create_reply_draft({"from": "", "subject": "x", "account": "a"})


class MailCommandsTests(unittest.TestCase):
    """/邮件 已读、/邮件 搜、/回复 三条新指令，走 app/mail/commands.py 的分发。"""

    def setUp(self):
        from app.mail import commands as mail_commands
        FakeIMAP.instances.clear()
        FakeSMTP.sent.clear()
        credentials._memory.clear()
        mail_commands._last_list = []
        self.addCleanup(mail_commands.set_notifier, None)
        tmp = Path(tempfile.mkdtemp())
        for target, attr, value in (
                (service, "DB_PATH", tmp / "t.db"),
                (status, "STATUS_PATH", tmp / "status.json"),
                (accounts, "CONFIG_PATH", tmp / "mail.json"),
                (client.imaplib, "IMAP4_SSL", FakeIMAP),
                (client.smtplib, "SMTP_SSL", FakeSMTP)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.mail_commands = mail_commands

    def _wait_notify(self):
        done, results = threading.Event(), []
        self.mail_commands.set_notifier(lambda text: (results.append(text), done.set()))
        return done, results

    def test_mark_read_command_updates_cache(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        self.mail_commands.handle("邮件", [])  # 先列出未读，填充 _last_list
        done, results = self._wait_notify()
        reply = self.mail_commands.handle("邮件", ["已读", "1"])
        self.assertEqual(reply, "标记中…")
        self.assertTrue(done.wait(2), "后台标记任务没有在超时内完成")
        self.assertIn("已标记已读", results[0])
        self.assertEqual(service.cached_recent(unseen_only=True), [])

    def test_mark_read_bad_index(self):
        accounts.add_account("me@163.com", secret="right-code")
        self.assertIn("先用 /邮件 列出", self.mail_commands.handle("邮件", ["已读", "1"]))

    def test_search_command(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        reply = self.mail_commands.handle("邮件", ["搜", "weekly"])
        self.assertIn("Weekly report", reply)
        self.assertIn("没有匹配", self.mail_commands.handle("邮件", ["搜", "不存在的关键词"]))

    def test_search_without_keyword(self):
        accounts.add_account("me@163.com", secret="right-code")
        self.assertIn("格式", self.mail_commands.handle("邮件", ["搜"]))

    def test_reply_command_generates_draft(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        self.mail_commands.handle("邮件", [])  # 填充 _last_list
        reply = self.mail_commands.handle("回复", ["1", "好的，收到"])
        self.assertIn("已生成回复草稿", reply)
        self.assertIn("bob@gmail.com", reply)
        drafts = service.pending_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertTrue(drafts[0]["subject"].startswith("Re: "))
        self.assertIn("好的，收到", drafts[0]["body"])

    def test_reply_command_without_listing_first(self):
        self.assertIn("先用 /邮件 列出", self.mail_commands.handle("回复", ["1"]))

    def test_reply_command_bad_index(self):
        accounts.add_account("me@163.com", secret="right-code")
        service.poll_once()
        self.mail_commands.handle("邮件", [])
        self.assertIn("先用 /邮件 列出", self.mail_commands.handle("回复", ["99"]))


if __name__ == "__main__":
    unittest.main()
