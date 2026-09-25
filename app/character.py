# -*- coding: utf-8 -*-
"""角色素材加载：assets/character/ 目录就是"换装接口"。

必需：pet.png（整图，透明底）。
可选：character.json + layers/（body/tail/blink 分层）——存在则启用尾巴摆动和眨眼；
缺失时自动退化为整图静态显示。更换形象 = 替换该目录内容（工具见 tools/make_layers.py）。

内存：四张原图（705x1113 RGBA）一共 ~12MB。它们只在按屏幕缩放生成显示用位图时需要，
所以按需从磁盘读（full/body/tail/blink 是懒加载属性），用完 release() 丢掉；改体型时再读一次。
"""
import json
from pathlib import Path

from PySide6.QtGui import QImage, QImageReader


class Character:
    def __init__(self, char_dir: Path):
        self.dir = char_dir
        meta = {}
        meta_path = char_dir / "character.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        self.meta = meta
        self.sit_ratio = float(meta.get("sit_ratio", 0.642))
        self._paths = {"full": char_dir / "pet.png"}
        for key, rel in meta.get("layers", {}).items():
            p = char_dir / rel
            if p.exists():
                self._paths[key] = p
        self._cache = {}
        size = QImageReader(str(self._paths["full"])).size()   # 只读文件头，不解码
        self.size = (size.width(), size.height())
        self.tail_pivot = meta.get("tail_pivot")
        self.tail_bbox = meta.get("tail_bbox")
        self.tail_amplitude = float(meta.get("tail_amplitude_deg", 4.5))
        self.tail_period = float(meta.get("tail_period_s", 3.5))
        lo, hi = meta.get("blink_interval_s", [3.0, 7.0])
        self.blink_interval = (float(lo), float(hi))
        self.blink_duration = float(meta.get("blink_duration_ms", 130)) / 1000.0

    def _get(self, key):
        if key not in self._paths:
            return None
        img = self._cache.get(key)
        if img is None:
            img = QImage(str(self._paths[key]))
            if img.isNull():
                return None
            self._cache[key] = img
        return img

    @property
    def full(self):
        return self._get("full")

    @property
    def body(self):
        return self._get("body")

    @property
    def tail(self):
        return self._get("tail")

    @property
    def blink(self):
        return self._get("blink")

    @property
    def has_blink(self):
        return "blink" in self._paths

    def release(self):
        """丢掉原图（显示用位图已经生成好了）。"""
        self._cache.clear()

    @property
    def animated(self):
        return "body" in self._paths and "tail" in self._paths and self.tail_pivot is not None
