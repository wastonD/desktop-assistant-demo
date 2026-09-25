# -*- coding: utf-8 -*-
"""宠物的对话气泡：浅色圆角卡片，尾巴指向头顶；淡入；可带按钮（比如提醒的"完成 / 稍后"）。

点气泡正文 = 收起；鼠标停在气泡上时不会自动消失。
"""
from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme

TAIL_H = 10
MAX_TEXT_W = 280


class _Card(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.tail_x = 40.0
        self.accent = theme.ACCENT

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - TAIL_H - 1)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        tx = max(22.0, min(self.tail_x, r.right() - 22))
        tail = QPainterPath()
        tail.moveTo(tx - 9, r.bottom() - 1)
        tail.quadTo(tx + 2, r.bottom() + 4, tx + 6, r.bottom() + TAIL_H - 0.5)
        tail.quadTo(tx + 5, r.bottom() + 3, tx + 9, r.bottom() - 1)
        tail.closeSubpath()
        p.setPen(QPen(theme.qcolor(theme.LINE), 1))
        p.setBrush(theme.qcolor(theme.SURFACE))
        p.drawPath(path.united(tail))
        if self.accent and self.accent != theme.ACCENT:  # 标题前的小圆点（提醒=红、邮件=蓝…）
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor(self.accent))
            p.drawEllipse(QRectF(14, 16, 6, 6))


class Bubble(QWidget):
    def __init__(self, anchor: QWidget):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._anchor = anchor
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 14)
        self.card = _Card(self)
        outer.addWidget(self.card)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(16, 10, 14, 11 + TAIL_H)
        lay.setSpacing(8)
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {theme.TEXT_2}; font-size: 11px; font-weight: bold; padding-left: 10px;"
                                 f" font-family: {theme.FONT_FAMILY};")
        self.label = QLabel()
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px;"
                                 f" font-family: {theme.FONT_FAMILY};")
        self.title.setFont(theme.ui_font(11, True))
        self.label.setFont(theme.ui_font(13))  # 量宽度要用真实字号（样式表 polish 前不生效）
        lay.addWidget(self.title)
        lay.addWidget(self.label)
        self.btn_box = QWidget()
        self.btn_row = QHBoxLayout(self.btn_box)
        self.btn_row.setContentsMargins(0, 0, 0, 0)
        self.btn_row.setSpacing(6)
        lay.addWidget(self.btn_box)
        self.card.setStyleSheet(theme.BASE_QSS)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)
        self._anim = None

    def paintEvent(self, _):
        """阴影画在外层窗口（子控件画不出自己范围）。"""
        p = QPainter(self)
        g = self.card.geometry()
        w, h = g.width(), g.height() - TAIL_H
        pm = theme._shadow_pixmap(w, h, 14, 12, 30, self.devicePixelRatioF())
        p.drawPixmap(QRectF(g.x() - 12, g.y() - 12 + 4, w + 24, h + 24), pm,
                     QRectF(0, 0, pm.width(), pm.height()))

    def show_text(self, text, duration_ms=12000, title="", actions=None, accent=None):
        """actions: [(按钮文字, 回调)]；点按钮后执行回调并收起气泡。"""
        self.card.accent = (accent or theme.ACCENT) if title else theme.ACCENT
        self.title.setText(title)
        self.title.setVisible(bool(title))
        self.label.setText(text)
        fm = self.label.fontMetrics()
        widest = max((fm.horizontalAdvance(ln) for ln in text.split("\n")), default=0)
        self.label.setFixedWidth(min(MAX_TEXT_W, max(90, widest + 6)))
        while self.btn_row.count():
            it = self.btn_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.btn_box.setVisible(bool(actions))
        if actions:
            self.btn_row.addStretch(1)
            for i, (label, cb) in enumerate(actions):
                b = QPushButton(label)
                b.setObjectName("primary" if i == 0 else "ghost")
                b.setCursor(Qt.PointingHandCursor)
                b.setStyleSheet("QPushButton { padding: 4px 12px; font-size: 12px; }")
                b.clicked.connect(lambda _=False, f=cb: (f(), self._fade_out(True)))
                self.btn_row.addWidget(b)
        # 先让布局吃下新的固定宽度，再按内容收缩（否则会沿用上一条长消息的宽度）
        self.card.layout().activate()
        self.layout().activate()
        self.resize(self.sizeHint())
        self._place()
        if self._anim:
            self._anim.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._anim = anim
        self._duration = duration_ms
        self._hide_timer.start(duration_ms)

    def _place(self):
        a = self._anchor
        head = a.head_point() if hasattr(a, "head_point") else \
            QPointF(a.x() + a.width() / 2, a.y())
        screen = QApplication.primaryScreen().availableGeometry()
        # 气泡整体放在头的左边（头右上方留给小表情），尾巴从右下角指向头顶
        x = round(head.x() - self.width() + 46)
        x = max(screen.left() + 8, min(x, screen.right() - self.width() - 8))
        y = max(screen.top() + 8, round(head.y() - self.height() + 6))
        if (self.x(), self.y()) != (x, y):
            self.move(x, y)
        tail_x = head.x() - x - self.card.x()
        if abs(tail_x - self.card.tail_x) > 0.5:
            self.card.tail_x = tail_x
            self.card.update()

    def _fade_out(self, force=False):
        if not self.isVisible():
            return
        if self.underMouse() and not force:  # 正在看，先别走
            self._hide_timer.start(2500)
            return
        if self._anim:
            self._anim.stop()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(220)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.0)
        anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim

    def mousePressEvent(self, event):
        self._hide_timer.stop()
        self.hide()
