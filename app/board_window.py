# -*- coding: utf-8 -*-
"""桌面常驻日程卡片（可选，默认不显示——日程已经和对话一起放进点宠物弹出的面板）。

内容就是 agenda_view.AgendaView；这里只负责：桌面挂件层（widgets.DesktopLayer）、拖动记位、折叠。
右键宠物 →「桌面常驻日程」开关，状态记在 config/board.json 的 visible。
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout

from . import theme
from .agenda_view import AgendaView
from .widgets import DesktopLayer

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "board.json"


class BoardWindow(theme.CardWindow):
    def __init__(self):
        super().__init__(Qt.FramelessWindowHint | Qt.Tool, margin=16, radius=16)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._drag = None
        self._layer = None
        self.cfg = self._load_cfg()
        lay = QVBoxLayout(self.body)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = AgendaView()
        lay.addWidget(self.view)
        self.fold_btn = QPushButton("—")
        self.fold_btn.setObjectName("icon")
        self.fold_btn.setFixedSize(24, 24)
        self.fold_btn.setCursor(Qt.PointingHandCursor)
        self.fold_btn.setToolTip("折叠/展开")
        self.fold_btn.setStyleSheet(theme.BASE_QSS)
        self.fold_btn.clicked.connect(self._toggle_fold)
        self.view.header_extra.addWidget(self.fold_btn, 0, Qt.AlignTop)
        self.resize(360, 600)
        self._restore_pos()
        self._apply_fold()

    @property
    def wanted(self) -> bool:
        return bool(self.cfg.get("visible", False))

    def set_wanted(self, on: bool):
        self.cfg["visible"] = on
        self._save_cfg()
        self.setVisible(on)
        if on and self._layer:
            self._layer.raise_all(force=True)

    def attach_to_desktop(self, layer: DesktopLayer | None = None):
        self._layer = layer or DesktopLayer()
        self._layer.add(self)

    def reload(self):
        self.view.reload()

    # ---------- 折叠 ----------
    def _toggle_fold(self):
        self.cfg["folded"] = not self.cfg.get("folded", False)
        self._apply_fold()
        self._save_cfg()

    def _apply_fold(self):
        folded = bool(self.cfg.get("folded", False))
        self.view.set_folded(folded)
        self.fold_btn.setText("＋" if folded else "—")
        if folded:
            self.setFixedHeight(150)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.resize(self.width(), 600)

    # ---------- 拖动与位置 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 80:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.cfg.update({"x": self.x(), "y": self.y()})
            self._save_cfg()

    @staticmethod
    def _load_cfg():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_cfg(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.cfg), encoding="utf-8")

    def _restore_pos(self):
        geo = QApplication.primaryScreen().availableGeometry()
        x, y = round(geo.width() * 0.66), round(geo.height() * 0.08)
        try:
            x, y = int(self.cfg.get("x", x)), int(self.cfg.get("y", y))
        except (TypeError, ValueError):
            pass
        x = max(geo.left(), min(x, geo.right() - self.width()))
        y = max(geo.top(), min(y, geo.bottom() - 120))
        self.move(x, y)


class LazyBoard:
    """桌面常驻日程卡片默认关着：关着的时候连窗口都不建（省几 MB），用户勾选时才创建。"""

    def __init__(self, layer: DesktopLayer):
        self._layer = layer
        self._win = None
        self.cfg = BoardWindow._load_cfg()
        if self.wanted:
            self._ensure().show()

    def _ensure(self):
        if self._win is None:
            self._win = BoardWindow()
            self._win.attach_to_desktop(self._layer)
        return self._win

    @property
    def wanted(self) -> bool:
        return bool((self._win.cfg if self._win else self.cfg).get("visible", False))

    def isVisible(self) -> bool:
        return self._win is not None and self._win.isVisible()

    def set_wanted(self, on: bool):
        if on:
            self._ensure().set_wanted(True)
        elif self._win is not None:
            self._win.set_wanted(False)
            self._win.deleteLater()
            self._win = None
            self.cfg = BoardWindow._load_cfg()
