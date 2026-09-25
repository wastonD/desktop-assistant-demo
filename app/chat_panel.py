# -*- coding: utf-8 -*-
"""对话视图（AI 简约风）：点宠物弹出的面板右栏。生成在后台线程跑，不卡宠物动画。

小银的话是纯文本（左侧小头像），你的话是浅灰气泡；"正在输入"是紫蓝渐变的三个点；
输入 / 自动补全本地指令；快捷芯片一键跑常用指令。
"""
import threading

from PySide6.QtCore import QObject, QRectF, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QCompleter, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from . import commands, history, llm, theme
from .widgets import pet_avatar

MAX_HISTORY = 20  # 传给模型的最近消息条数，也是启动时恢复的历史条数
USER_MAX_W = 260
BOT_MAX_W = 330

COMMANDS = ["/今天", "/记 ", "/日程", "/日程 明天", "/完成 ", "/删除 ", "/稍后", "/收纳",
            "/收纳 整理", "/收纳 撤销", "/收纳 放回", "/收纳 打开", "/邮件", "/邮件 刷新",
            "/邮件 帮助", "/回复 ", "/草稿", "/发送 ", "/资讯", "/资讯 刷新", "/资讯 帮助",
            "/资讯 订阅 ", "/壁纸", "/清空", "/帮助"]


class _Worker(QObject):
    done = Signal(str)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    def run(self):
        try:
            reply = llm.generate(self.messages, status_cb=self.status.emit)
            self.done.emit(reply)
        except Exception as e:
            self.failed.emit(str(e))


class _Notifier(QObject):
    """后台任务（收信、发信）完成后从任意线程回报到面板。"""
    message = Signal(str)


def _measure(label: QLabel, text: str, max_w: int, pad: int) -> None:
    """按真实字号量宽度后定宽（样式表的 font-size 要等 polish 后才生效，不能靠它量）。"""
    fm = label.fontMetrics()
    widest = max((fm.horizontalAdvance(ln) for ln in text.split("\n")), default=0)
    label.setFixedWidth(min(widest + pad, max_w))


class _Msg(QWidget):
    """一条消息。role: user / assistant / system。"""

    def __init__(self, role, text, dpr):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 3, 0, 3)
        h.setSpacing(10)
        lb = QLabel(text)
        lb.setTextFormat(Qt.PlainText)
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        lb.setFont(theme.ui_font(12 if role == "system" else 13))
        self.label = lb
        if role == "user":
            _measure(lb, text, USER_MAX_W, 30)
            lb.setStyleSheet(f"background: {theme.SURFACE_2}; color: {theme.TEXT};"
                             " border-radius: 14px; padding: 8px 13px; font-size: 13px;")
            h.addStretch(1)
            h.addWidget(lb)
        elif role == "system":
            _measure(lb, text, BOT_MAX_W, 26)
            lb.setStyleSheet(f"background: {theme.WARM_SOFT}; color: #B42318; border-radius: 10px;"
                             " padding: 7px 11px; font-size: 12px;")
            h.addSpacing(34)
            h.addWidget(lb)
            h.addStretch(1)
        else:
            av = QLabel()
            av.setPixmap(pet_avatar(24, dpr, ring=True))
            av.setFixedSize(24, 24)
            h.addWidget(av, 0, Qt.AlignTop)
            _measure(lb, text, BOT_MAX_W, 6)
            lb.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; padding-top: 3px;")
            h.addWidget(lb)
            h.addStretch(1)


class _Dots(QWidget):
    """"正在输入"：三个渐变色的点依次起伏。"""

    def __init__(self):
        super().__init__()
        self.setFixedSize(40, 16)
        self._t = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._timer.start(60)

    def _step(self):
        self._t += 1
        self.update()

    def paintEvent(self, _):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        for i in range(3):
            k = (math.sin(self._t * 0.25 - i * 0.9) + 1) / 2
            c = theme.qcolor(theme.AI_A if i == 0 else (theme.AI_B if i == 2 else "#6D6AF2"),
                             round(110 + 145 * k))
            p.setBrush(c)
            p.drawEllipse(QRectF(4 + i * 12, 6 - 3 * k, 6, 6))


class _Typing(QWidget):
    def __init__(self, dpr):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 3, 0, 3)
        h.setSpacing(10)
        av = QLabel()
        av.setPixmap(pet_avatar(24, dpr, ring=True))
        av.setFixedSize(24, 24)
        h.addWidget(av, 0, Qt.AlignTop)
        h.addWidget(_Dots(), 0, Qt.AlignVCenter)
        h.addStretch(1)


class ChatView(QWidget):
    close_requested = Signal()

    def __init__(self, pet):
        super().__init__()
        self.setStyleSheet(theme.BASE_QSS)
        self._pet = pet
        self._history: list[dict] = []
        self._busy = False
        self._worker = None
        self._typing = None
        self._notifier = _Notifier()
        self._notifier.message.connect(self._on_notify)
        # 邮件/资讯指令模块按需加载（见 commands._mail_mod），这里只登记回报通道
        commands.UI_HOOKS["notify"] = self._notifier.message.emit
        import sys
        for mod in ("app.mail.commands", "app.news.commands"):
            if mod in sys.modules:
                sys.modules[mod].set_notifier(self._notifier.message.emit)
        self._dpr = pet.devicePixelRatioF() if pet is not None else 1.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 14, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        av = QLabel()
        av.setPixmap(pet_avatar(34, self._dpr, ring=True))
        av.setFixedSize(34, 34)
        top.addWidget(av)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        name = QLabel("小银")
        name.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.status_label = QLabel()
        self.status_label.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
        tcol.addWidget(name)
        tcol.addWidget(self.status_label)
        top.addLayout(tcol, 1)
        for text, tip, cb in (("⟲", "清空对话", self._clear), ("✕", "收起（Esc）", self.close_requested.emit)):
            b = QPushButton(text)
            b.setObjectName("icon")
            b.setFixedSize(28, 28)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(cb)
            top.addWidget(b)
        lay.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setStyleSheet("background: transparent;")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self.msg_lay = QVBoxLayout(host)
        self.msg_lay.setContentsMargins(0, 0, 8, 0)
        self.msg_lay.setSpacing(8)
        self.msg_lay.addStretch(1)
        self.scroll.setWidget(host)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _a, b: self.scroll.verticalScrollBar().setValue(b))
        lay.addWidget(self.scroll, 1)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        for label, action in (("今日安排", "/今天"), ("桌面收纳", self._open_drawer),
                              ("未读邮件", "/邮件"), ("资讯", "/资讯"), ("指令", "/帮助")):
            chip = QPushButton(label)
            chip.setObjectName("chip")
            chip.setCursor(Qt.PointingHandCursor)
            if callable(action):
                chip.clicked.connect(action)
            else:
                chip.clicked.connect(lambda _=False, c=action: self._run_local(c))
            chips.addWidget(chip)
        chips.addStretch(1)
        lay.addLayout(chips)

        box = QWidget()
        box.setObjectName("inputbox")
        box.setAttribute(Qt.WA_StyledBackground)
        box.setStyleSheet(f"#inputbox {{ background: white; border: 1px solid {theme.LINE};"
                          " border-radius: 14px; }")
        bl = QHBoxLayout(box)
        bl.setContentsMargins(12, 5, 5, 5)
        self.input = QLineEdit()
        self.input.setPlaceholderText("和小银说点什么，/ 开头是本地指令")
        self.input.setStyleSheet("QLineEdit { border: none; background: transparent; padding: 4px 0; }")
        self.input.returnPressed.connect(self._send)
        comp = QCompleter(QStringListModel(COMMANDS), self.input)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.popup().setStyleSheet(
            f"QListView {{ background: white; border: 1px solid {theme.LINE}; border-radius: 8px;"
            f" padding: 4px; font-size: 13px; color: {theme.TEXT}; outline: none; }}"
            f"QListView::item {{ padding: 5px 8px; border-radius: 6px; }}"
            f"QListView::item:selected {{ background: {theme.SURFACE_2}; color: {theme.TEXT}; }}")
        self.input.setCompleter(comp)
        send = QPushButton("↑")
        send.setObjectName("primary")
        send.setFixedSize(30, 30)
        send.setCursor(Qt.PointingHandCursor)
        send.setStyleSheet("QPushButton { border-radius: 15px; padding: 0; font-size: 15px; }")
        send.clicked.connect(self._send)
        bl.addWidget(self.input, 1)
        bl.addWidget(send)
        lay.addWidget(box)

        self._update_status()
        self._restore_history()
        if not self._history:
            self._append("assistant", "……有事就说。/ 开头是本地指令，比如 /今天、/收纳。")

    def _restore_history(self):
        for row in history.recent(MAX_HISTORY):
            self._append(row["role"], row["content"])
            self._history.append({"role": row["role"], "content": row["content"]})

    def _update_status(self, text=None):
        if text:
            self.status_label.setText("✦ " + text)
            self.status_label.setStyleSheet(f"color: {theme.AI_A}; font-size: 11px;")
            return
        ok, _ = llm.availability()
        prov = llm.load_config().get("provider", "")
        if not ok:
            label = "本地指令可用 · 未接入模型"
        elif prov == "local":
            label = "本地模型 · " + ("在线" if llm.local_running() else "待命")
        else:
            label = "云端模型 · 在线"
        self.status_label.setText(label)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")

    def focus_input(self):
        if not self._busy:
            self._update_status()
        self.input.setFocus()

    # ---------- 对话 ----------
    def _append(self, role, text):
        self.msg_lay.addWidget(_Msg(role, text, self._dpr))

    def _show_typing(self, on):
        if on and self._typing is None:
            self._typing = _Typing(self._dpr)
            self.msg_lay.addWidget(self._typing)
        elif not on and self._typing is not None:
            self._typing.deleteLater()
            self._typing = None

    def _fx(self, thinking):
        fx = getattr(self._pet, "fx", None)
        if fx is not None:
            fx.set_thinking(thinking)

    def _on_notify(self, text):
        self._append("assistant", text)
        hub = self.window()
        if hub is not None and not hub.isVisible() and hasattr(hub, "popup"):
            hub.popup()

    def _open_drawer(self):
        drawer = getattr(self._pet, "drawer", None)
        if drawer is not None:
            drawer.open_drawer()

    def _run_local(self, cmd):
        reply = commands.handle(cmd)
        if reply:
            self._append("assistant", reply)

    def _clear(self):
        history.clear()
        self._history = []
        while self.msg_lay.count() > 1:
            it = self.msg_lay.takeAt(1)
            if it.widget():
                it.widget().deleteLater()
        self._typing = None
        self._append("assistant", "对话历史已清空。")

    def _send(self):
        text = self.input.text().strip()
        if not text or self._busy:
            return
        self.input.clear()
        if text.lstrip("/／") in ("清空", "clear"):
            self._clear()
            return
        self._append("user", text)
        local = commands.handle(text)  # 功能分界第一层：/ 指令走本地
        if local is not None:
            self._append("assistant", local)
            return
        ok, reason = llm.availability()  # 第二层：自由对话需要大模型
        if not ok:
            self._append("system",
                         f"自由聊天需要接入大模型（当前：{reason}）。\n"
                         "日程、提醒、收纳不受影响，输入 /帮助 看本地指令；\n"
                         "要启用聊天请在 config/llm.json 配置本地模型或 API。")
            return
        self._history.append({"role": "user", "content": text})
        history.append("user", text)
        self._busy = True
        if llm.load_config().get("provider") == "local" and not llm.local_running():
            self._update_status("正在唤醒本地模型…")
        else:
            self._update_status("思考中…")
        self._show_typing(True)
        self._fx(True)
        self._worker = _Worker(list(self._history[-MAX_HISTORY:]))
        self._worker.done.connect(self._on_reply)
        self._worker.failed.connect(self._on_error)
        self._worker.status.connect(self._update_status)
        threading.Thread(target=self._worker.run, daemon=True).start()

    def _finish(self):
        self._busy = False
        self._show_typing(False)
        self._fx(False)
        self._update_status()

    def _on_reply(self, reply):
        self._finish()
        self._history.append({"role": "assistant", "content": reply})
        history.append("assistant", reply)
        self._append("assistant", reply)

    def _on_error(self, err):
        self._finish()
        self._append("system", f"出错了：{err}")
