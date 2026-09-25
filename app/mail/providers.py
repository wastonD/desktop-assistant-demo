# -*- coding: utf-8 -*-
"""热门邮箱的服务器预设。账户配置里写 provider 名即可，也可用 custom 自填主机端口。

auth 取值：
- password：授权码 / 应用专用密码，IMAP LOGIN + SMTP AUTH（QQ/163/126/Gmail/新浪/阿里）
- oauth2：XOAUTH2（Outlook/Hotmail 个人版自 2024-09 起、Office365 均已停用密码登录）
"""

PRESETS = {
    "qq": {
        "label": "QQ 邮箱", "domains": ["qq.com", "foxmail.com", "vip.qq.com"],
        "imap": ("imap.qq.com", 993), "smtp": ("smtp.qq.com", 465, "ssl"),
        "auth": ["password"],
        "help": "网页版 设置→账号→开启 IMAP/SMTP 服务，按提示生成授权码（16 位），填授权码而不是 QQ 密码",
    },
    "163": {
        "label": "网易 163", "domains": ["163.com"],
        "imap": ("imap.163.com", 993), "smtp": ("smtp.163.com", 465, "ssl"),
        "auth": ["password"], "imap_id": True,
        "help": "网页版 设置→POP3/SMTP/IMAP→开启 IMAP/SMTP 服务，获取授权码",
    },
    "126": {
        "label": "网易 126", "domains": ["126.com"],
        "imap": ("imap.126.com", 993), "smtp": ("smtp.126.com", 465, "ssl"),
        "auth": ["password"], "imap_id": True,
        "help": "同 163：设置→POP3/SMTP/IMAP 开启并获取授权码",
    },
    "yeah": {
        "label": "网易 yeah.net", "domains": ["yeah.net"],
        "imap": ("imap.yeah.net", 993), "smtp": ("smtp.yeah.net", 465, "ssl"),
        "auth": ["password"], "imap_id": True,
        "help": "同 163：设置里开启 IMAP 并获取授权码",
    },
    "gmail": {
        "label": "Gmail", "domains": ["gmail.com", "googlemail.com"],
        "imap": ("imap.gmail.com", 993), "smtp": ("smtp.gmail.com", 465, "ssl"),
        "auth": ["password", "oauth2"], "oauth": "google",
        "help": "Google 账号需先开两步验证，再到 myaccount.google.com/apppasswords 生成 16 位应用专用密码",
    },
    "outlook": {
        "label": "Outlook / Hotmail", "domains": ["outlook.com", "hotmail.com", "live.com", "msn.com"],
        "imap": ("outlook.office365.com", 993), "smtp": ("smtp-mail.outlook.com", 587, "starttls"),
        "auth": ["oauth2"], "oauth": "microsoft",
        "help": "微软已停用密码登录，只能 OAuth2：首次使用按提示在浏览器输入设备码登录一次",
    },
    "office365": {
        "label": "Office 365（学校/企业）", "domains": [],
        "imap": ("outlook.office365.com", 993), "smtp": ("smtp.office365.com", 587, "starttls"),
        "auth": ["oauth2"], "oauth": "microsoft",
        "help": "OAuth2 设备码登录；部分学校租户禁止第三方应用，需要管理员放行",
    },
    "sina": {
        "label": "新浪邮箱", "domains": ["sina.com", "sina.cn"],
        "imap": ("imap.sina.com", 993), "smtp": ("smtp.sina.com", 465, "ssl"),
        "auth": ["password"],
        "help": "设置→客户端 pop/imap/smtp 开启 IMAP，使用授权码",
    },
    "aliyun": {
        "label": "阿里邮箱（个人）", "domains": ["aliyun.com"],
        "imap": ("imap.aliyun.com", 993), "smtp": ("smtp.aliyun.com", 465, "ssl"),
        "auth": ["password"],
        "help": "设置→POP3/SMTP/IMAP 开启 IMAP",
    },
    "icloud": {
        "label": "iCloud", "domains": ["icloud.com", "me.com", "mac.com"],
        "imap": ("imap.mail.me.com", 993), "smtp": ("smtp.mail.me.com", 587, "starttls"),
        "auth": ["password"],
        "help": "appleid.apple.com 生成 App 专用密码",
    },
}


def guess_provider(address: str) -> str | None:
    """按邮箱域名猜预设名；猜不到返回 None（用 custom 或手选 office365）。"""
    domain = address.rsplit("@", 1)[-1].lower().strip()
    for name, p in PRESETS.items():
        if domain in p["domains"]:
            return name
    return None


def server_settings(account: dict) -> dict:
    """合并预设与账户里的覆盖项，得到 imap_host/imap_port/smtp_host/smtp_port/smtp_security。"""
    preset = PRESETS.get(account.get("provider") or "", {})
    imap = preset.get("imap", (None, 993))
    smtp = preset.get("smtp", (None, 465, "ssl"))
    return {
        "imap_host": account.get("imap_host") or imap[0],
        "imap_port": int(account.get("imap_port") or imap[1]),
        "smtp_host": account.get("smtp_host") or smtp[0],
        "smtp_port": int(account.get("smtp_port") or smtp[1]),
        "smtp_security": account.get("smtp_security") or smtp[2],
        "imap_id": bool(account.get("imap_id", preset.get("imap_id", False))),
        "auth": account.get("auth") or (preset.get("auth") or ["password"])[0],
        "oauth": account.get("oauth") or preset.get("oauth"),
    }
