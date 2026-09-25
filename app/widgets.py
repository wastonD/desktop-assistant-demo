# -*- coding: utf-8 -*-
"""几个通用小部件：流式布局、进度环、头像、桌面层辅助。"""
import ctypes
import ctypes.wintypes
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLayout, QWidget

from . import theme

ROOT = Path(__file__).resolve().parents[1]
PET_PNG = ROOT / "assets" / "character" / "pet.png"
AVATAR_BOX = (252, 110, 442, 300)  # pet.png 里脸部的正方形（源图像素）


class FlowLayout(QLayout):
    """自动换行的网格（Qt 官方示例的 Python 版）。"""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    def _do_layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > rect.right() + 1 and line_h > 0:
                x, y = rect.x(), y + line_h + self._spacing
                nx, line_h = x + hint.width() + self._spacing, 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x, line_h = nx, max(line_h, hint.height())
        return y + line_h - rect.y()


class ProgressRing(QWidget):
    """今日完成度的小圆环：中间写 done/total。"""

    def __init__(self, size=46, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._done, self._total = 0, 0

    def set_value(self, done, total):
        if (done, total) != (self._done, self._total):
            self._done, self._total = done, total
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(4, 4, self.width() - 8, self.height() - 8)
        p.setPen(QPen(theme.qcolor(theme.ACCENT_SOFT), 5, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(r)
        if self._total:
            frac = self._done / self._total
            p.setPen(QPen(theme.qcolor(theme.ACCENT), 5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(r, 90 * 16, -round(frac * 360 * 16))
        p.setPen(theme.qcolor(theme.TEXT))
        p.setFont(theme.ui_font(11, True))
        text = f"{self._done}/{self._total}" if self._total else "—"
        p.drawText(self.rect(), Qt.AlignCenter, text)


_avatar_cache = {}


def pet_avatar(size: int, dpr: float = 1.0, ring: bool = True) -> QPixmap:
    """从立绘裁出圆形头像（带薄荷色细描边）。"""
    key = (size, dpr, ring)
    if key in _avatar_cache:
        return _avatar_cache[key]
    phys = round(size * dpr)
    src = QImage(str(PET_PNG))
    x0, y0, x1, y1 = AVATAR_BOX
    face = src.copy(x0, y0, x1 - x0, y1 - y0).scaled(
        phys, phys, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = QPixmap(phys, phys)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, phys, phys))
    p.fillPath(path, QColor("#F4F4F5"))
    p.setClipPath(path)
    p.drawImage(0, 0, face)
    p.setClipping(False)
    if ring:  # AI 渐变细环
        pen_w = max(1.5, 1.6 * dpr)
        p.setPen(QPen(QBrush(theme.ai_gradient(0, 0, phys, phys)), pen_w))
        p.drawEllipse(QRectF(pen_w / 2, pen_w / 2, phys - pen_w, phys - pen_w))
    p.end()
    out.setDevicePixelRatio(dpr)
    _avatar_cache[key] = out
    return out


class DesktopLayer:
    """让若干普通（非置顶）窗口像桌面挂件一样常驻壁纸之上。

    关键发现：点击桌面时 Windows 会把桌面提升到普通层顶端盖住挂件。
    对策：监听前台变化，前台是桌面（Progman/WorkerW）就把挂件无激活地拉回其上；5 秒轮询兜底。
    """
    _SWP = 0x0002 | 0x0001 | 0x0010  # NOMOVE | NOSIZE | NOACTIVATE

    def __init__(self):
        self._widgets = []
        user32 = ctypes.windll.user32
        proc_type = ctypes.WINFUNCTYPE(
            None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
            ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD)
        self._proc = proc_type(lambda *a: self.raise_all())
        self._hook = user32.SetWinEventHook(0x0003, 0x0003, 0, self._proc, 0, 0, 0)
        self._timer = QTimer()
        self._timer.timeout.connect(self.raise_all)
        self._timer.start(5000)

    def add(self, widget: QWidget):
        self._widgets.append(widget)
        # 启动瞬间桌面往往已是前台，不会再有前台变化事件；延迟强制拉起一次
        QTimer.singleShot(600, lambda: self._raise(widget))

    @staticmethod
    def desktop_is_foreground() -> bool:
        user32 = ctypes.windll.user32
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(user32.GetForegroundWindow(), cls, 64)
        return cls.value in ("Progman", "WorkerW")

    def raise_all(self, force=False):
        if not force and not self.desktop_is_foreground():
            return
        for w in self._widgets:
            self._raise(w)

    def _raise(self, w):
        if w.isVisible():
            ctypes.windll.user32.SetWindowPos(int(w.winId()), 0, 0, 0, 0, 0, self._SWP)


_trim_timer = None


def trim_memory(delay_ms: int = 1500):
    """空闲时收一下内存：Python 垃圾回收 + 把暂时用不到的页交还系统（工作集收缩）。
    多次调用会合并成一次（防抖），不会影响交互。"""
    global _trim_timer
    if _trim_timer is None:
        _trim_timer = QTimer()
        _trim_timer.setSingleShot(True)

        def run():
            import gc
            gc.collect()
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
            k32.SetProcessWorkingSetSizeEx.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_size_t,
                                                       ctypes.c_size_t, ctypes.wintypes.DWORD]
            k32.SetProcessWorkingSetSizeEx(k32.GetCurrentProcess(), ctypes.c_size_t(-1),
                                           ctypes.c_size_t(-1), 0)
        _trim_timer.timeout.connect(run)
    _trim_timer.start(delay_ms)
