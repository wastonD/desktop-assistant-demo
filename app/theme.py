# -*- coding: utf-8 -*-
"""统一视觉：AI 简约风。

中性灰白（zinc 色阶）+ 近黑文字 + 黑色主按钮；只在"AI 的痕迹"上用一点紫→蓝渐变（头像环、思考点、
选中指示）。细边框、大圆角、留白；阴影是预渲染缓存的位图（不用 QGraphicsDropShadowEffect——
它每次重绘都要对整块面板做模糊，悬停/滚动都会卡）。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import QMenu, QWidget

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "assets" / "ui"

# ---------- 色板（zinc）----------
BG = "#FFFFFF"
SURFACE = "#FFFFFF"
SURFACE_2 = "#F4F4F5"     # 次级面：输入框、悬停
SURFACE_3 = "#FAFAFA"     # 侧栏
LINE = "#E4E4E7"
LINE_2 = "#EFEFF1"
TEXT = "#18181B"
TEXT_2 = "#71717A"
TEXT_3 = "#A1A1AA"
ACCENT = "#18181B"        # 主操作 = 近黑
ACCENT_DARK = "#000000"
ACCENT_SOFT = "#F4F4F5"
AI_A = "#8B5CF6"          # 紫
AI_B = "#3B82F6"          # 蓝
AI_SOFT = "#F3F0FF"
WARM = "#E5484D"          # 逾期/提醒
WARM_SOFT = "#FEF2F2"
SKY = "#3B82F6"
SKY_SOFT = "#EFF6FF"
LAVENDER = "#8B5CF6"
LAVENDER_SOFT = "#F5F3FF"
GREEN = "#16A34A"

FONT_FAMILY = '"Microsoft YaHei UI", "Segoe UI", sans-serif'


def qcolor(hex_str: str, alpha: int = 255) -> QColor:
    c = QColor(hex_str)
    c.setAlpha(alpha)
    return c


def ai_gradient(x0, y0, x1, y1) -> QLinearGradient:
    g = QLinearGradient(x0, y0, x1, y1)
    g.setColorAt(0.0, QColor(AI_A))
    g.setColorAt(1.0, QColor(AI_B))
    return g


def ui_font(px: int = 13, bold: bool = False) -> QFont:
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def asset(name: str) -> str:
    return (UI_DIR / name).as_posix()


# ---------- 预渲染阴影卡片 ----------
_shadow_cache = {}


def _shadow_pixmap(w: int, h: int, radius: int, blur: int, alpha: int, dpr: float) -> QPixmap:
    key = (w, h, radius, blur, alpha, round(dpr, 2))
    pm = _shadow_cache.get(key)
    if pm is not None:
        return pm
    pw, ph = round(w * dpr), round(h * dpr)
    pad = round(blur * dpr)
    im = Image.new("L", (pw + 2 * pad, ph + 2 * pad), 0)
    ImageDraw.Draw(im).rounded_rectangle((pad, pad, pad + pw, pad + ph), radius=round(radius * dpr),
                                         fill=alpha)
    im = im.filter(ImageFilter.GaussianBlur(blur * dpr / 2.2))
    rgba = Image.new("RGBA", im.size, (24, 24, 27, 0))
    rgba.putalpha(im)
    qimg = QImage(rgba.tobytes("raw", "RGBA"), rgba.width, rgba.height,
                  QImage.Format_RGBA8888).copy()
    pm = QPixmap.fromImage(qimg)
    pm.setDevicePixelRatio(dpr)
    if len(_shadow_cache) > 40:
        _shadow_cache.clear()
    _shadow_cache[key] = pm
    return pm


def paint_card(p: QPainter, rect: QRectF, radius: float = 16, shadow: int = 22,
               dy: float = 6, fill: str = BG, border: str = LINE, shadow_alpha: int = 34,
               dpr: float = 1.0):
    """画一张带柔和阴影的圆角卡片（阴影位图按尺寸缓存）。"""
    if shadow:
        pm = _shadow_pixmap(round(rect.width()), round(rect.height()), round(radius), shadow,
                            shadow_alpha, dpr)
        p.drawPixmap(QRectF(rect.x() - shadow, rect.y() - shadow + dy,
                            rect.width() + 2 * shadow, rect.height() + 2 * shadow), pm,
                     QRectF(0, 0, pm.width(), pm.height()))
    path = QPainterPath()
    path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    p.setPen(QPen(QColor(border), 1))
    p.setBrush(QColor(fill))
    p.drawPath(path)


class CardWindow(QWidget):
    """无边框置顶/普通窗口的基类：自己画阴影卡片，内容放在 self.body 里。"""

    def __init__(self, flags, margin=18, radius=16, top_flat=False, shadow=22):
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._margin, self._radius, self._top_flat, self._shadow = margin, radius, top_flat, shadow
        self.body = QWidget(self)
        self.body.setObjectName("cardbody")
        self.body.setAttribute(Qt.WA_TranslucentBackground)

    def card_rect(self) -> QRectF:
        m = self._margin
        top = 0 if self._top_flat else m - 6
        return QRectF(m, top, self.width() - 2 * m, self.height() - top - m - 4)

    def resizeEvent(self, e):
        r = self.card_rect()
        self.body.setGeometry(r.toRect())
        super().resizeEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.card_rect()
        if self._top_flat:  # 贴屏幕顶边：上沿延伸出去，只露下面两个圆角
            r = QRectF(r.x(), r.y() - self._radius, r.width(), r.height() + self._radius)
        paint_card(p, r, self._radius, self._shadow, dpr=self.devicePixelRatioF())


# ---------- 样式片段 ----------
BASE_QSS = f"""
QWidget {{ font-family: {FONT_FAMILY}; color: {TEXT}; }}
QLabel {{ background: transparent; }}
QLineEdit, QDateTimeEdit, QSpinBox, QComboBox {{
    background: {SURFACE}; border: 1px solid {LINE}; border-radius: 10px;
    padding: 6px 10px; color: {TEXT}; font-size: 13px;
    selection-background-color: #DDD6FE; selection-color: {TEXT}; }}
QLineEdit:focus, QDateTimeEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid #A1A1AA; }}
QComboBox::drop-down, QDateTimeEdit::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {LINE};
    selection-background-color: {SURFACE_2}; selection-color: {TEXT}; outline: none; }}
QPushButton {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 9px;
    padding: 6px 14px; color: {TEXT}; font-size: 13px; }}
QPushButton:hover {{ background: {SURFACE_2}; }}
QPushButton:pressed {{ background: {LINE}; }}
QPushButton:disabled {{ color: {TEXT_3}; background: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: bold; }}
QPushButton#primary:hover {{ background: #27272A; }}
QPushButton#primary:disabled {{ background: #D4D4D8; border-color: #D4D4D8; color: white; }}
QPushButton#ghost {{ background: transparent; border: none; color: {TEXT_2}; }}
QPushButton#ghost:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QPushButton#ghost:disabled {{ color: #D4D4D8; }}
QPushButton#chip {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 13px;
    padding: 4px 11px; font-size: 12px; color: {TEXT_2}; }}
QPushButton#chip:hover {{ border-color: #A1A1AA; color: {TEXT}; }}
QPushButton#chip:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#icon {{ background: transparent; border: none; border-radius: 8px; padding: 0;
    font-size: 14px; color: {TEXT_2}; }}
QPushButton#icon:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #D4D4D8; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #A1A1AA; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
QToolTip {{ background: {TEXT}; color: white; border: none; border-radius: 6px;
    padding: 4px 8px; font-size: 12px; }}
QCheckBox {{ spacing: 10px; font-size: 13px; color: {TEXT}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 8px;
    border: 1.5px solid #C4C4CC; background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {TEXT_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
    image: url({asset("check.svg")}); }}
"""

MENU_QSS = f"""
QMenu {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 12px; padding: 5px;
    font-family: {FONT_FAMILY}; font-size: 13px; color: {TEXT}; }}
QMenu::item {{ padding: 7px 28px 7px 12px; border-radius: 7px; background: transparent; }}
QMenu::item:selected {{ background: {SURFACE_2}; color: {TEXT}; }}
QMenu::item:disabled {{ color: {TEXT_3}; }}
QMenu::separator {{ height: 1px; background: {LINE_2}; margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
"""


def card_qss(obj: str, radius: int = 16, alpha: int = 255) -> str:
    """兼容旧调用：纯色圆角卡片样式（新代码用 CardWindow）。"""
    return (f"#{obj} {{ background: rgba(255,255,255,{alpha}); border-radius: {radius}px;"
            f" border: 1px solid {LINE}; }}")


def add_shadow(widget: QWidget, *a, **k) -> None:
    """已废弃：实时模糊阴影太重。保留空实现以兼容旧调用。"""
    return None


def style_menu(menu: QMenu) -> QMenu:
    """圆角浅色菜单（需要透明底才显示得出圆角）。子菜单也一并处理。"""
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground)
    menu.setStyleSheet(MENU_QSS)
    for act in menu.actions():
        if act.menu():
            style_menu(act.menu())
    return menu


def apply_app_style(app) -> None:
    app.setStyleSheet(f"""
QToolTip {{ background: {TEXT}; color: white; border: none; border-radius: 6px;
    padding: 4px 8px; font-size: 12px; }}
""")
