# -*- coding: utf-8 -*-
"""邮件后台服务：轮询所有账户 → 缓存最近邮件 → 未读总数写 status.json；草稿与确认发送。

- 轮询在后台线程跑（MailService.poll_async），界面线程只拿缓存，不碰网络。
- 发信一律两步：先生成草稿（模型或 / 指令都只能到这一步），用户确认后才真正发出。
"""
import json
import re
import sqlite3
import threading
from datetime import datetime
from email.utils import parseaddr

from .. import status
from ..store import DB_PATH
from . import accounts, parse
from .client import AuthFailed, MailClient, MailError
from .oauth import NeedsLogin, OAuthError

_lock = threading.Lock()


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS mail_cache (
        account TEXT NOT NULL, uid TEXT NOT NULL, folder TEXT NOT NULL DEFAULT 'INBOX',
        data TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
        fetched TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        PRIMARY KEY (account, folder, uid))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mail_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT NOT NULL, to_addr TEXT NOT NULL, cc TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL, body TEXT NOT NULL, in_reply_to TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',   -- draft / sent / failed / discarded
        error TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))""")
    return conn


# ---------------- 轮询 ----------------

class PollResult:
    def __init__(self):
        self.unread: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.needs_login: list[str] = []
        self.new_mail: list[dict] = []   # 本次新出现的未读（用于冒气泡）

    @property
    def total_unread(self) -> int:
        return sum(self.unread.values())


def poll_once() -> PollResult:
    """同步轮询全部账户（阻塞，放后台线程调用）。"""
    cfg = accounts.load()
    res = PollResult()
    with _lock:
        for acc in accounts.enabled_accounts(cfg):
            client = MailClient(acc, cfg.get("oauth"))
            try:
                res.unread[acc["id"]] = client.unread_count()
                recent = client.recent(int(cfg.get("recent_count", 10)))
                res.new_mail.extend(_save_cache(acc["id"], recent))
            except NeedsLogin:
                res.needs_login.append(acc["id"])
            except (AuthFailed, MailError, OAuthError) as e:
                res.errors[acc["id"]] = str(e)
            except OSError as e:  # 断网、超时、DNS
                res.errors[acc["id"]] = f"网络错误：{e}"
            finally:
                client.close()
    status.update(unread_mail=res.total_unread if res.unread else None,
                  mail_checked=datetime.now().strftime("%Y-%m-%d %H:%M"),
                  mail_errors=res.errors or None)
    return res


def _save_cache(account_id: str, mails: list[dict]) -> list[dict]:
    """写缓存，返回此前没见过的未读邮件。"""
    new = []
    with _db() as c:
        known = {r["uid"] for r in c.execute(
            "SELECT uid FROM mail_cache WHERE account=? AND folder='INBOX'", (account_id,))}
        first_run = not known
        for m in mails:
            if m["uid"] not in known and not m["seen"] and not first_run:
                new.append(m)
            c.execute("INSERT OR REPLACE INTO mail_cache (account, uid, folder, data, seen) "
                      "VALUES (?, ?, ?, ?, ?)",
                      (account_id, m["uid"], m.get("folder", "INBOX"),
                       json.dumps(m, ensure_ascii=False), 1 if m["seen"] else 0))
        # 只保留每账户最近 200 封缓存
        c.execute("""DELETE FROM mail_cache WHERE account=? AND rowid NOT IN (
            SELECT rowid FROM mail_cache WHERE account=? ORDER BY CAST(uid AS INTEGER) DESC LIMIT 200)""",
                  (account_id, account_id))
    return new


def cached_recent(limit: int = 10, unseen_only: bool = False, account: str | None = None) -> list[dict]:
    """从缓存读（不联网），按时间新→旧。"""
    sql = "SELECT data FROM mail_cache WHERE 1=1"
    args: list = []
    if unseen_only:
        sql += " AND seen=0"
    if account:
        sql += " AND account=?"
        args.append(account)
    with _db() as c:
        rows = [json.loads(r["data"]) for r in c.execute(sql, args)]
    rows.sort(key=lambda m: (m.get("date") or "", int(m.get("uid") or 0)), reverse=True)
    return rows[:limit]


def mark_seen(account_ref: str, uid: str) -> dict | None:
    """把某封邮件标为已读：IMAP 打标（真正改服务器状态）+ 同步本地缓存 seen 字段 + 未读总数。

    返回缓存里这封邮件的数据（找不到缓存记录时返回 None，IMAP 标记仍然会执行）。
    """
    cfg = accounts.load()
    acc = accounts.find(account_ref, cfg)
    if not acc:
        raise MailError(f"找不到账户：{account_ref}")
    client = MailClient(acc, cfg.get("oauth"))
    try:
        client.mark_seen(uid)
    finally:
        client.close()
    with _db() as c:
        row = c.execute("SELECT data, seen FROM mail_cache WHERE account=? AND uid=?",
                        (acc["id"], uid)).fetchone()
        was_unseen = bool(row) and not row["seen"]
        if row:
            c.execute("UPDATE mail_cache SET seen=1 WHERE account=? AND uid=?", (acc["id"], uid))
    if was_unseen:
        current = status.read().get("unread_mail")
        if isinstance(current, int) and current > 0:
            status.update(unread_mail=(current - 1) or None)
    return json.loads(row["data"]) if row else None


def search_cached(keyword: str, limit: int = 15) -> list[dict]:
    """在缓存里搜发件人/主题/摘要（不联网），按时间新→旧。"""
    keyword = keyword.strip().lower()
    with _db() as c:
        rows = [json.loads(r["data"]) for r in c.execute("SELECT data FROM mail_cache")]
    matched = [m for m in rows if keyword in " ".join(
        str(m.get(k, "")) for k in ("from", "subject", "snippet")).lower()]
    matched.sort(key=lambda m: (m.get("date") or "", int(m.get("uid") or 0)), reverse=True)
    return matched[:limit]


def format_list(mails: list[dict], with_account: bool = False) -> str:
    if not mails:
        return "没有邮件。"
    lines = []
    for i, m in enumerate(mails, 1):
        dot = "·" if m.get("seen") else "●"
        acc = f"[{m['account']}] " if with_account else ""
        sender = m.get("from", "").split("<")[0].strip() or m.get("from", "")
        lines.append(f"{dot} {i}. {acc}{m.get('date', '')[5:]} {sender}：{m.get('subject', '')}")
    return "\n".join(lines)


class MailService:
    """挂在 main.py 上：定时触发后台轮询，结果通过回调交给界面线程（回调里请用信号切线程）。"""

    def __init__(self, on_result=None):
        self.on_result = on_result
        self._running = False
        self.last: PollResult | None = None

    def poll_async(self) -> bool:
        if self._running or not accounts.enabled_accounts():
            return False
        self._running = True

        def run():
            try:
                self.last = poll_once()
                if self.on_result:
                    self.on_result(self.last)
            finally:
                self._running = False
        threading.Thread(target=run, daemon=True, name="mail-poll").start()
        return True


# ---------------- 草稿与发送 ----------------

def create_draft(account_ref: str | None, to: str, subject: str, body: str,
                 cc: str = "", in_reply_to: str = "") -> dict:
    cfg = accounts.load()
    accs = accounts.enabled_accounts(cfg)
    if not accs:
        raise MailError("还没有添加邮箱账户")
    acc = accounts.find(account_ref, cfg) if account_ref else accs[0]
    if not acc:
        raise MailError(f"找不到账户：{account_ref}")
    if "@" not in to:
        raise MailError(f"收件人地址不对：{to}")
    with _db() as c:
        cur = c.execute("INSERT INTO mail_drafts (account, to_addr, cc, subject, body, in_reply_to) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (acc["id"], to.strip(), cc.strip(), subject.strip(), body, in_reply_to))
        did = cur.lastrowid
    return get_draft(did)


def create_reply_draft(cached_mail: dict, body: str = "") -> dict:
    """基于缓存里的一封邮件生成回复草稿：收件人=原发件人地址，主题补 Re:，带 In-Reply-To，正文后附引用摘要。"""
    _, addr = parseaddr(cached_mail.get("from", ""))
    if not addr:
        raise MailError("这封邮件看不出发件人地址，没法回复")
    subject = cached_mail.get("subject", "") or "（无主题）"
    if not re.match(r"(?i)^re[:：]\s*", subject):
        subject = f"Re: {subject}"
    quote = ("\n\n----- 原始邮件 -----\n"
            f"发件人：{cached_mail.get('from', '')}\n"
            f"时间：{cached_mail.get('date', '')}\n"
            f"主题：{cached_mail.get('subject', '')}\n\n"
            f"{cached_mail.get('snippet', '')}")
    full_body = (body.strip() or "（请补充回复内容）") + quote
    return create_draft(cached_mail.get("account"), addr, subject, full_body,
                        in_reply_to=cached_mail.get("message_id", ""))


def get_draft(draft_id: int) -> dict | None:
    with _db() as c:
        r = c.execute("SELECT * FROM mail_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(r) if r else None


def pending_drafts() -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mail_drafts WHERE status='draft' ORDER BY id DESC")]


def format_draft(d: dict) -> str:
    cc = f"\n抄送：{d['cc']}" if d.get("cc") else ""
    return (f"草稿 #{d['id']}（{d['account']}）\n收件人：{d['to_addr']}{cc}\n"
            f"主题：{d['subject']}\n——\n{d['body']}")


def discard_draft(draft_id: int) -> None:
    with _db() as c:
        c.execute("UPDATE mail_drafts SET status='discarded' WHERE id=? AND status='draft'", (draft_id,))


def send_draft(draft_id: int) -> str:
    """真正发出。只应由用户的确认动作调用（/发送 编号 或面板按钮），不暴露给模型。"""
    d = get_draft(draft_id)
    if not d or d["status"] != "draft":
        raise MailError(f"没有待发送的草稿 #{draft_id}")
    cfg = accounts.load()
    acc = accounts.find(d["account"], cfg)
    if not acc:
        raise MailError(f"草稿对应的账户 {d['account']} 已删除")
    split = lambda s: [x.strip() for x in s.replace("；", ",").replace(";", ",").split(",") if x.strip()]
    msg = parse.build_message(acc["address"], split(d["to_addr"]), d["subject"], d["body"],
                              cc=split(d["cc"]), in_reply_to=d["in_reply_to"],
                              sender_name=acc.get("name", ""))
    client = MailClient(acc, cfg.get("oauth"))
    try:
        client.send(msg)
    except Exception as e:
        with _db() as c:
            c.execute("UPDATE mail_drafts SET status='failed', error=? WHERE id=?", (str(e), draft_id))
        raise
    finally:
        client.close()
    with _db() as c:
        c.execute("UPDATE mail_drafts SET status='sent' WHERE id=?", (draft_id,))
    return f"已发送给 {d['to_addr']}：{d['subject']}"
