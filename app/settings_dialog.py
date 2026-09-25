# -*- coding: utf-8 -*-
"""壁纸看板设置对话框：底图、位置、宽度、字号、面板不透明度，改动后自动应用（0.9s 防抖）。"""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QPushButton, QSlider)

from . import wallpaper


class WallpaperSettings(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("壁纸看板设置")
        self.setMinimumWidth(380)
        self.cfg = wallpaper._load_config()

        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.timeout.connect(self._apply)

        form = QFormLayout(self)

        self.chk = QCheckBox("启用壁纸日程看板")
        self.chk.setChecked(bool(self.cfg.get("enabled", True)))
        self.chk.toggled.connect(self._on_enabled)
        form.addRow(self.chk)

        row = QHBoxLayout()
        base = self.cfg.get("base_image")
        self.base_label = QLabel(Path(base).name if base else "（自动快照当前壁纸）")
        self.base_label.setStyleSheet("color: gray")
        pick = QPushButton("选择底图…")
        pick.clicked.connect(self._pick_base)
        row.addWidget(self.base_label, 1)
        row.addWidget(pick)
        form.addRow("底图", row)

        self._add_slider(form, "横向位置", "pos_x_ratio", 2, 90,
                         round(self.cfg.get("pos_x_ratio", 0.655) * 100), "%")
        self._add_slider(form, "纵向位置", "pos_y_ratio", 2, 80,
                         round(self.cfg.get("pos_y_ratio", 0.10) * 100), "%")
        self._add_slider(form, "面板宽度", "panel_width", 320, 760,
                         int(self.cfg.get("panel_width", 480)), "px")
        self._add_slider(form, "字号", "text_scale", 70, 150,
                         round(self.cfg.get("text_scale", 1.0) * 100), "%")
        op = self.cfg.get("panel_opacity") or 165
        self._add_slider(form, "面板不透明度", "panel_opacity", 40, 255, int(op), "")

        tip = QLabel("拖动滑块后约 1 秒自动应用到壁纸")
        tip.setStyleSheet("color: gray; font-size: 12px")
        form.addRow(tip)

    def _add_slider(self, form, label, key, mn, mx, val, unit):
        row = QHBoxLayout()
        slider = QSlider(Qt.Horizontal)
        slider.setRange(mn, mx)
        slider.setValue(max(mn, min(mx, val)))
        value_label = QLabel(f"{slider.value()}{unit}")
        value_label.setMinimumWidth(48)

        def on_change(v):
            value_label.setText(f"{v}{unit}")
            if key in ("pos_x_ratio", "pos_y_ratio"):
                self.cfg[key] = v / 100.0
            elif key == "text_scale":
                self.cfg[key] = v / 100.0
            else:
                self.cfg[key] = v
            wallpaper._save_config(self.cfg)
            self._apply_timer.start(900)

        slider.valueChanged.connect(on_change)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        form.addRow(label, row)

    def _pick_base(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择底图", "", "图片 (*.jpg *.jpeg *.png *.bmp *.webp)")
        if path:
            self.cfg["base_image"] = path
            self.base_label.setText(Path(path).name)
            wallpaper._save_config(self.cfg)
            self._apply_timer.start(100)

    def _on_enabled(self, checked):
        self.cfg["enabled"] = bool(checked)
        wallpaper._save_config(self.cfg)
        if checked:
            self._apply_timer.start(100)
        elif self.cfg.get("base_image"):
            wallpaper.set_raw(self.cfg["base_image"])  # 关闭看板：还原纯底图

    def _apply(self):
        wallpaper.apply_async(force=True)
