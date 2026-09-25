# -*- coding: utf-8 -*-
"""单个邮箱账户的 IMAP 读取 / SMTP 发送。标准库 imaplib + smtplib。

所有网络操作都是阻塞的，调用方（service.py）负责放后台线程。
读取一律 readonly + BODY.PEEK，**不会把邮件标成已读**。
"""
import imaplib
import re
import smtplib
import ssl
import time

from . import credentials, oauth, parse
from .providers import server_settings

TIMEOUT = 25
PEEK_BYTES = 32768  # 只取每封前 32KB，够解析头和正文开头，大附件不下载

imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))


class MailError(Exception):
    pass


class AuthFailed(MailError):
    pass


def _imap_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MailClient:
    def __init__(self, account: dict, oauth_cfg: dict | None = None):
        self.account = account
        self.id = account["id"]
        self.address = account["address"]
        self.login_user = account.get("username") or self.address
        self.s = server_settings(account)
        self.oauth_cfg = (oauth_cfg or {}).get(self.s["oauth"] or "", {})
        self._imap: imaplib.IMAP4 | None = None

    # ---------- 认证 ----------
    def _password(self) -> str:
        pw = credentials.get_secret(credentials.password_key(self.id))
        if not pw:
            raise AuthFailed(f"{self.address} 还没保存授权码")
        return pw

    def _xoauth2(self) -> str:
        token = oauth.access_token(self.id, self.s["oauth"], self.oauth_cfg)
        return oauth.xoauth2_string(self.login_user, token)

    # ---------- IMAP ----------
    def imap(self) -> imaplib.IMAP4:
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except Exception:
                self._imap = None
        if not self.s["imap_host"]:
            raise MailError("未配置 IMAP 服务器")
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(self.s["imap_host"], self.s["imap_port"],
                                 ssl_context=ctx, timeout=TIMEOUT)
        try:
            if self.s["auth"] == "oauth2":
                xo = self._xoauth2()
                conn.authenticate("XOAUTH2", lambda _: xo.encode())
            else:
                conn.login(self.login_user, self._password())
        except imaplib.IMAP4.error as e:
            try:
                conn.logout()
            except Exception:
                pass
            raise AuthFailed(f"{self.address} 登录失败：{_short(e)}") from e
        if self.s["imap_id"]:
            self._send_id(conn)
        self._imap = conn
        return conn

    @staticmethod
    def _send_id(conn):
        """网易系要求客户端发 IMAP ID（RFC 2971），否则 SELECT 报 Unsafe Login。"""
        try:
            conn._simple_command(
                "ID", '("name" "DesktopAssistant" "version" "1.0" "vendor" "DesktopAssistant")')
        except Exception:
            pass

    def close(self):
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def unread_count(self, folder: str = "INBOX") -> int:
        conn = self.imap()
        typ, data = conn.status(_imap_quote(folder), "(UNSEEN)")
        if typ == "OK" and data and data[0]:
            m = re.search(rb"UNSEEN (\d+)", data[0])
            if m:
                return int(m.group(1))
        # 个别服务器 STATUS 不可靠，退回 SEARCH
        self._select(conn, folder)
        typ, data = conn.search(None, "UNSEEN")
        return len(data[0].split()) if typ == "OK" and data and data[0] else 0

    def _select(self, conn, folder: str) -> int:
        typ, data = conn.select(_imap_quote(folder), readonly=True)
        if typ != "OK":
            raise MailError(f"打开文件夹 {folder} 失败：{_short(data)}")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def recent(self, count: int = 10, unseen_only: bool = False, folder: str = "INBOX") -> list[dict]:
        """最近 count 封（新的在前）的摘要；unseen_only=True 只看未读。"""
        conn = self.imap()
        total = self._select(conn, folder)
        if total == 0:
            return []
        if unseen_only:
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            if not uids:
                return []
            uid_set = b",".join(uids[-count:]).decode()
            typ, data = conn.uid("FETCH", uid_set, f"(UID FLAGS BODY.PEEK[]<0.{PEEK_BYTES}>)")
        else:
            start = max(1, total - count + 1)
            typ, data = conn.fetch(f"{start}:{total}", f"(UID FLAGS BODY.PEEK[]<0.{PEEK_BYTES}>)")
        if typ != "OK":
            raise MailError(f"读取邮件失败：{_short(data)}")
        items = parse_fetch_response(data)
        out = []
        for meta, raw in items:
            summary = parse.summarize(raw)
            summary.update(uid=meta.get("uid"), seen="\\Seen" in meta.get("flags", ""),
                           account=self.id, folder=folder)
            out.append(summary)
        out.sort(key=lambda m: int(m["uid"] or 0), reverse=True)
        return out[:count]

    def fetch_full(self, uid: str, folder: str = "INBOX") -> dict:
        """单封完整正文（仍然 PEEK，不标已读）。"""
        conn = self.imap()
        self._select(conn, folder)
        typ, data = conn.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
        items = parse_fetch_response(data) if typ == "OK" else []
        if not items:
            raise MailError(f"找不到邮件 {uid}")
        import email
        meta, raw = items[0]
        summary = parse.summarize(raw, snippet_len=100)
        summary["body"] = parse.body_text(email.message_from_bytes(raw)).strip()
        summary.update(uid=meta.get("uid"), account=self.id, folder=folder)
        return summary

    def mark_seen(self, uid: str, folder: str = "INBOX") -> None:
        conn = self.imap()
        typ, _ = conn.select(_imap_quote(folder))
        if typ == "OK":
            conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")

    def sent_folder(self) -> str | None:
        """用 LIST 的 \\Sent 特殊用途标记找"已发送"文件夹（名字是修改版 UTF-7 原样返回）。"""
        conn = self.imap()
        typ, data = conn.list()
        if typ != "OK":
            return None
        fallback = None
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
            if not m:
                continue
            name = m.group("name").decode("ascii", "replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            if b"\\Sent" in m.group("flags"):
                return name
            if name.lower() in ("sent", "sent messages", "sent items", "[gmail]/sent mail",
                                "&XfJT0ZAB-"):  # &XfJT0ZAB- = 已发送
                fallback = name
        return fallback

    # ---------- SMTP ----------
    def send(self, msg, save_to_sent: bool = True) -> None:
        host, port, sec = self.s["smtp_host"], self.s["smtp_port"], self.s["smtp_security"]
        if not host:
            raise MailError("未配置 SMTP 服务器")
        ctx = ssl.create_default_context()
        if sec == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
            smtp.ehlo()
            smtp.starttls(context=ctx)
        try:
            smtp.ehlo()
            try:
                if self.s["auth"] == "oauth2":
                    xo = self._xoauth2()
                    smtp.auth("XOAUTH2", lambda challenge=None: xo, initial_response_ok=True)
                else:
                    smtp.login(self.login_user, self._password())
            except smtplib.SMTPAuthenticationError as e:
                raise AuthFailed(f"{self.address} SMTP 登录失败：{_short(e)}") from e
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        # Gmail/Outlook 会自动存已发送；国内邮箱 SMTP 发出的不会，补一份
        if save_to_sent and self.account.get("provider") not in ("gmail", "outlook", "office365"):
            try:
                folder = self.sent_folder()
                if folder:
                    self.imap().append(_imap_quote(folder) if not folder.startswith('"') else folder,
                                       "(\\Seen)", imaplib.Time2Internaldate(time.time()),
                                       msg.as_bytes())
            except Exception:
                pass  # 存副本失败不影响发送结果

    def test(self) -> str:
        """连通性测试：登录 IMAP 并读未读数。"""
        n = self.unread_count()
        return f"{self.address} 连接成功，收件箱 {n} 封未读"


def parse_fetch_response(data) -> list[tuple[dict, bytes]]:
    """imaplib FETCH 返回的 [(头, 字面量), b')', ...] → [(meta, raw_bytes)]。

    FLAGS/UID 可能出现在字面量之前或之后（服务器实现不同），两处都看。
    """
    items = []
    i = 0
    data = list(data or [])
    while i < len(data):
        part = data[i]
        if isinstance(part, tuple) and len(part) >= 2:
            head = part[0] or b""
            raw = part[1] or b""
            tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
            text = head + b" " + tail
            meta = {}
            m = re.search(rb"UID (\d+)", text)
            if m:
                meta["uid"] = m.group(1).decode()
            m = re.search(rb"FLAGS \(([^)]*)\)", text)
            meta["flags"] = m.group(1).decode(errors="replace") if m else ""
            items.append((meta, raw))
        i += 1
    return items


def _short(e) -> str:
    text = e if isinstance(e, str) else repr(e) if not isinstance(e, Exception) else str(e)
    if isinstance(e, list) and e and isinstance(e[0], bytes):
        text = e[0].decode(errors="replace")
    return text[:200]
