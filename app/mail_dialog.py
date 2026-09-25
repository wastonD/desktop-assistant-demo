# -*- coding: utf-8 -*-
"""邮箱账户管理对话框（托盘菜单「邮箱账户…」）。授权码只在这里输入，直接进 Windows 凭据管理器。

网络操作（测试连接、OAuth 登录）走后台线程，结果用信号切回界面线程。
"""
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

from .mail import accounts, oauth
from .mail.client import MailClient
from .mail.providers import PRESETS, guess_provider, server_settings


class _Bridge(QObject):
    text = Signal(str)


class MailAccountsDialog(QDialog):
    def __init__(self, parent=None, on_changed=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint)
        self.setWindowTitle("邮箱账户")
        self.resize(520, 460)
        self.on_changed = on_changed
        self._bridge = _Bridge()
        self._bridge.text.connect(self._set_info)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("已添加的账户："))
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, cb in (("测试连接", self._test), ("微软/Google 登录", self._oauth_login),
                          ("删除", self._remove)):
            b = QPushButton(label)
            b.clicked.connect(cb)
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(QLabel("添加账户："))
        form = QFormLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("例如 xxx@qq.com")
        self.addr.textChanged.connect(self._guess)
        self.provider = QComboBox()
        for key, p in PRESETS.items():
            self.provider.addItem(p["label"], key)
        self.provider.addItem("其他（手动填服务器）", "custom")
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.Password)
        self.imap_host = QLineEdit()
        self.smtp_host = QLineEdit()
        form.addRow("邮箱地址", self.addr)
        form.addRow("服务商", self.provider)
        form.addRow("授权码", self.secret)
        form.addRow("IMAP 服务器", self.imap_host)
        form.addRow("SMTP 服务器", self.smtp_host)
        lay.addLayout(form)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color: #777;")
        lay.addWidget(self.help)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add)
        lay.addWidget(add_btn)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info)

        self._provider_changed()
        self._reload()

    # ---------- 列表 ----------
    def _reload(self):
        self.list.clear()
        for a in accounts.load().get("accounts", []):
            label = PRESETS.get(a["provider"], {}).get("label", a["provider"])
            item = QListWidgetItem(f"{a['address']}　（{label}）")
            item.setData(Qt.UserRole, a["id"])
            self.list.addItem(item)

    def _selected(self):
        item = self.list.currentItem()
        return accounts.find(item.data(Qt.UserRole)) if item else None

    def _set_info(self, text):
        self.info.setText(text)

    def _bg(self, job, busy_text):
        self._set_info(busy_text)

        def run():
            try:
                self._bridge.text.emit(job())
            except Exception as e:
                self._bridge.text.emit(f"失败：{e}")
        threading.Thread(target=run, daemon=True).start()

    # ---------- 表单 ----------
    def _guess(self, text):
        p = guess_provider(text) if "@" in text else None
        if p:
            self.provider.setCurrentIndex(self.provider.findData(p))

    def _provider_changed(self):
        key = self.provider.currentData()
        preset = PRESETS.get(key, {})
        custom = key == "custom"
        self.imap_host.setEnabled(custom)
        self.smtp_host.setEnabled(custom)
        s = server_settings({"provider": key})
        self.imap_host.setText("" if custom else f"{s['imap_host']}:{s['imap_port']}")
        self.smtp_host.setText("" if custom else f"{s['smtp_host']}:{s['smtp_port']}")
        oauth_only = preset.get("auth") == ["oauth2"]
        self.secret.setEnabled(not oauth_only)
        self.secret.setPlaceholderText("此服务商用微软登录，无需授权码" if oauth_only else "授权码 / 应用专用密码（不是登录密码）")
        self.help.setText(preset.get("help", "填写服务器地址，格式 host:port（IMAP 用 SSL 993，SMTP 465 SSL 或 587 STARTTLS）"))

    def _add(self):
        key = self.provider.currentData()
        extra = {}
        if key == "custom":
            try:
                ih, ip = self.imap_host.text().strip().rsplit(":", 1)
                sh, sp = self.smtp_host.text().strip().rsplit(":", 1)
            except ValueError:
                QMessageBox.warning(self, "邮箱账户", "服务器格式：host:port")
                return
            extra = {"imap_host": ih, "imap_port": int(ip), "smtp_host": sh, "smtp_port": int(sp),
                     "smtp_security": "ssl" if sp == "465" else "starttls"}
        try:
            acc = accounts.add_account(self.addr.text(), key, self.secret.text() or None, **extra)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "邮箱账户", str(e))
            return
        self.secret.clear()
        self.addr.clear()
        self._reload()
        if self.on_changed:
            self.on_changed()
        if server_settings(acc)["auth"] == "oauth2":
            self._set_info(f"已添加 {acc['address']}，请选中它点「微软/Google 登录」完成授权。")
        else:
            self._bg(lambda: self._test_account(acc), f"已添加 {acc['address']}，正在测试连接…")

    # ---------- 操作 ----------
    @staticmethod
    def _test_account(acc):
        c = MailClient(acc, accounts.load().get("oauth"))
        try:
            return c.test()
        finally:
            c.close()

    def _test(self):
        acc = self._selected()
        if acc:
            self._bg(lambda: self._test_account(acc), "测试连接中…")

    def _remove(self):
        acc = self._selected()
        if not acc:
            return
        if QMessageBox.question(self, "邮箱账户", f"删除 {acc['address']}？（同时清除保存的授权码）") \
                != QMessageBox.Yes:
            return
        accounts.remove_account(acc["id"])
        self._reload()
        if self.on_changed:
            self.on_changed()

    def _oauth_login(self):
        acc = self._selected()
        if not acc:
            self._set_info("先在列表里选中账户")
            return
        s = server_settings(acc)
        if s["auth"] != "oauth2":
            self._set_info("这个账户用授权码登录，不需要这一步")
            return
        cfg = accounts.load().get("oauth", {}).get(s["oauth"], {})
        if not cfg.get("client_id"):
            self._set_info(f"需要先在 config/mail.json 的 oauth.{s['oauth']}.client_id 填应用 ID（见 docs/MAIL.md）")
            return
        oauth.login_in_background(
            acc["id"], s["oauth"], cfg, prompt_cb=self._bridge.text.emit,
            done_cb=lambda err: self._bridge.text.emit(
                f"登录失败：{err}" if err else f"{acc['address']} 授权完成"))
