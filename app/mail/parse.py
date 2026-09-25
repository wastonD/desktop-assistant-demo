# -*- coding: utf-8 -*-
"""邮件解析：头部解码（中文 GBK/GB2312/Big5 等容错）、正文纯文本提取、摘要。纯函数，便于测试。"""
import email
import html
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

# 常见误标：声明 gb2312 实际含 GBK/GB18030 字符
_CHARSET_ALIASES = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030",
                    "ks_c_5601-1987": "cp949", "unknown-8bit": "utf-8"}


def _decode_bytes(data: bytes, charset: str | None) -> str:
    cs = (charset or "utf-8").lower().strip('"')
    cs = _CHARSET_ALIASES.get(cs, cs)
    for c in (cs, "utf-8", "gb18030"):
        try:
            return data.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def decode_mime_header(value) -> str:
    if value is None:
        return ""
    value = str(value)
    try:
        parts = decode_header(value)
    except Exception:
        return value
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(_decode_bytes(text, charset))
        else:
            out.append(text)
    # decode_header 会把编码词之间的空白保留成独立片段，这里合并多余空白
    return re.sub(r"\s+", " ", "".join(out)).strip()


def format_address(value) -> str:
    """'=?utf-8?b?5byg5LiJ?= <zs@x.com>' → '张三 <zs@x.com>'；只有地址时返回地址。"""
    decoded = decode_mime_header(value)
    pairs = getaddresses([decoded])
    if not pairs:
        return decoded
    name, addr = pairs[0]
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name or decoded


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    text = re.sub(r"<[^>]+>", " ", markup)
    return html.unescape(text)


def _part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    return _decode_bytes(payload, part.get_content_charset())


def body_text(msg) -> str:
    """优先 text/plain，没有再从 text/html 转；跳过附件。"""
    plain, rich = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        if (part.get("Content-Disposition") or "").lower().startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            rich.append(_html_to_text(_part_text(part)))
    text = "\n".join(plain) if any(p.strip() for p in plain) else "\n".join(rich)
    return text


def clean_snippet(text: str, limit: int = 200) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">"):  # 引用的历史邮件
            continue
        if re.match(r"^(-{2,}\s*(original message|原始邮件)|在 .+写道[:：]|On .+wrote:)", s, re.I):
            break  # 回复链往下都是旧内容
        if s:
            lines.append(s)
    snippet = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return snippet[:limit] + ("…" if len(snippet) > limit else "")


def attachment_names(msg) -> list[str]:
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(decode_mime_header(fn))
    return names


def summarize(raw: bytes, snippet_len: int = 200) -> dict:
    """原始邮件字节 → 摘要 dict（from/to/subject/date/snippet/attachments）。截断的邮件也尽量解析。"""
    msg = email.message_from_bytes(raw)
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        date_str = dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""
    except (TypeError, ValueError, IndexError):
        date_str = ""
    try:
        text = body_text(msg)
    except Exception:
        text = ""
    return {
        "from": format_address(msg.get("From")),
        "to": decode_mime_header(msg.get("To")),
        "subject": decode_mime_header(msg.get("Subject")) or "（无主题）",
        "date": date_str,
        "message_id": (msg.get("Message-ID") or "").strip(),
        "snippet": clean_snippet(text, snippet_len),
        "attachments": attachment_names(msg),
    }


def build_message(sender: str, to: list[str], subject: str, body: str,
                  cc: list[str] | None = None, in_reply_to: str = "",
                  sender_name: str = ""):
    """构造纯文本邮件（UTF-8）。"""
    from email.message import EmailMessage
    from email.utils import formataddr, formatdate, make_msgid

    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


__all__ = ["decode_mime_header", "format_address", "body_text", "clean_snippet",
           "summarize", "build_message", "make_header"]
