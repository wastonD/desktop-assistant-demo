# -*- coding: utf-8 -*-
"""邮件适配器：多账户 IMAP 读取 + SMTP 发送（QQ/163/126/Gmail/Outlook/Office365/新浪/阿里/iCloud）。

providers 预设 → accounts 配置（config/mail.json，无密码）→ credentials 凭据管理器
→ client 收发 → service 轮询/缓存/草稿。发信必须经用户确认（send_draft），模型只能起草。
"""
