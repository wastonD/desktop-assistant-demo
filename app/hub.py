# -*- coding: utf-8 -*-
"""点宠物弹出的面板：左边日程、右边对话，一个窗口看全今天的事、顺手跟小银说话。

- 出现在宠物头部附近（她在任务栏上 → 头的左上方；她跑到屏幕上面 → 头的下方），淡入 + 轻微上浮；
- 不会因为点了别处就消失（方便边看边干活）；再点宠物、Esc、✕、Ctrl+Alt+X 收起；
- 面板开着时宠物原地待命，不乱跑。
"""
from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from . import theme
from .agenda_view import AgendaView
from .chat_panel import ChatView

W, H = 800, 560


class Hub(theme.CardWindow):
    def __init__(self, pet):
        super().__init__(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
                         margin=20, radius=18, shadow=26)
        self._pet = pet
        lay = QHBoxLayout(self.body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.agenda = AgendaView(compact=True)
        self.agenda.setFixedWidth(330)
        line = QWidget()
        line.setFixedWidth(1)
        line.setStyleSheet(f"background: {theme.LINE_2};")
        self.chat = ChatView(pet)
        self.chat.close_requested.connect(self.hide_animated)
        lay.addWidget(self.agenda)
        lay.addWidget(line)
        lay.addWidget(self.chat, 1)
        self.resize(W + 40, H + 40)
        self._anim = None

    # 兼容旧接口（toggle / popup / isVisible）
    def toggle(self):
        if self.isVisible():
            self.hide_animated()
        else:
            self.popup()

    def popup(self):
        target = self._place()
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.move(target + QPoint(0, 10))
            self.show()
        self.raise_()
        self.activateWindow()
        self.chat.focus_input()
        self._animate(target, 1.0)

    def _place(self) -> QPoint:
        head = self._pet.head_point()
        scr = QApplication.primaryScreen().availableGeometry()
        w, h = self.width(), self.height()
        x = round(head.x() - w + 150)
        y = round(head.y() - h - 4)
        if y < scr.top() + 8:          # 她在屏幕上半部：放到头的下方
            y = round(head.y() + 40)
        x = max(scr.left() + 4, min(x, scr.right() - w - 4))
        y = max(scr.top() + 4, min(y, scr.bottom() - h - 4))
        return QPoint(x, y)

    def _animate(self, pos: QPoint, opacity: float, on_done=None):
        if self._anim is not None:
            for a in self._anim:
                a.stop()
        a1 = QPropertyAnimation(self, b"pos", self)
        a1.setDuration(170)
        a1.setEndValue(pos)
        a1.setEasingCurve(QEasingCurve.OutCubic)
        a2 = QPropertyAnimation(self, b"windowOpacity", self)
        a2.setDuration(150)
        a2.setEndValue(opacity)
        if on_done:
            a2.finished.connect(on_done)
        a1.start()
        a2.start()
        self._anim = (a1, a2)

    def hide_animated(self):
        if not self.isVisible():
            return
        self._animate(self.pos() + QPoint(0, 8), 0.0, self._after_hide)

    def _after_hide(self):
        self.hide()
        self.destroy()  # 释放原生窗口的绘制缓冲（~5MB）；控件都还在，下次弹出不用重建
        from .widgets import trim_memory
        trim_memory()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.agenda.form.isVisible():
                self.agenda._close_form()
            else:
                self.hide_animated()
            return
        super().keyPressEvent(e)
