# -*- coding: utf-8 -*-
"""帧图：读取 → 预乘 alpha → 按屏幕缩放一次 → BGRA 字节；按 alpha 命中测试；按预算缓存。

只依赖 Pillow（不依赖 Qt），可在任何平台测试。
UpdateLayeredWindow 要求 32bpp、**预乘 alpha** 的位图；小端机器上预乘 BGRA 的内存布局
正好是 Qt 的 QImage.Format_ARGB32_Premultiplied，所以同一份字节 Qt 和 GDI 都能直接用。
"""
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


def premultiply_pixel(r: int, g: int, b: int, a: int) -> tuple:
    """参考实现（单像素）：RGBA 直通 → 预乘 BGRA。四舍五入同 Pillow 的 MULDIV255。"""
    def mul(c):
        t = c * a + 128
        return (t + (t >> 8)) >> 8
    return mul(b), mul(g), mul(r), a


def to_premul_bgra(im: Image.Image) -> bytes:
    """RGBA（直通）或 RGBa（已预乘）图像 → 预乘 BGRA 字节（行优先、无填充，stride = 4*w）。"""
    if im.mode == "RGBA":
        im = im.convert("RGBa")
    elif im.mode != "RGBa":
        im = im.convert("RGBA").convert("RGBa")
    r, g, b, a = im.split()
    return Image.merge("RGBa", (b, g, r, a)).tobytes()


def scale_size(canvas: tuple, scale: float) -> tuple:
    """画布按比例缩放后的整数像素尺寸（整帧和补丁都用这一个函数，保证映射一致）。"""
    return max(1, round(canvas[0] * scale)), max(1, round(canvas[1] * scale))


def resize_premul(im: Image.Image, size: tuple, box=None) -> Image.Image:
    """在预乘空间里缩放（边缘不发黑），返回 RGBa。"""
    if im.mode != "RGBa":
        im = im.convert("RGBA").convert("RGBa")
    if im.size == tuple(size) and box is None:
        return im
    return im.resize(tuple(size), Image.LANCZOS, box=box)


@dataclass
class Frame:
    """一帧显示用位图：预乘 BGRA 字节 + 尺寸（物理像素）。ox/oy = 在整帧里的位置（补丁用）。"""
    w: int
    h: int
    data: bytes
    ox: int = 0
    oy: int = 0

    @property
    def nbytes(self) -> int:
        return len(self.data)

    def alpha_at(self, x: int, y: int) -> int:
        if 0 <= x < self.w and 0 <= y < self.h:
            return self.data[(y * self.w + x) * 4 + 3]
        return 0

    def hit(self, x: float, y: float, threshold: int = 24) -> bool:
        """这个点算不算"点到她"：alpha 低于阈值的发梢、阴影、透明处都不算。"""
        return self.alpha_at(math.floor(x), math.floor(y)) >= threshold

    def bbox(self):
        """不透明像素的包围盒 (x0, y0, x1, y1)；全透明返回 None。"""
        a = Image.frombuffer("L", (self.w, self.h), self.data[3::4], "raw", "L", 0, 1)
        return a.getbbox()


def frame_from_image(im: Image.Image, size: tuple | None = None) -> Frame:
    """PIL 图像（源分辨率）→ 缩放到 size（物理像素）的 Frame。"""
    im = resize_premul(im, size or im.size)
    return Frame(im.width, im.height, to_premul_bgra(im))


def load_frame(path: Path, size: tuple | None = None) -> Frame:
    with Image.open(path) as im:
        im.load()
        return frame_from_image(im, size)


def scale_patch(patch: Image.Image, origin: tuple, canvas: tuple, dst_size: tuple,
                box: tuple) -> Frame:
    """把"整帧里的一小块"（例如闭眼补丁）缩放到显示分辨率，结果和"先缩整帧再裁这块"逐像素一致。

    patch：画布坐标里 origin=(ox, oy) 处的一块（四周要多存一圈边，给缩放滤波器取样）；
    canvas：整帧画布尺寸；dst_size：整帧缩放后的尺寸；box：真正要贴的区域（画布坐标）。
    做法：按整帧的映射算出 box 覆盖的目标像素范围（整数），再用 resize(box=…) 以同样的比例重采样，
    Pillow 会用 box 外面的像素做滤波支撑——所以多存的那圈边必须 ≥ 滤波半径（LANCZOS=3 个目标像素）。"""
    sx = canvas[0] / dst_size[0]
    sy = canvas[1] / dst_size[1]
    X0 = max(0, math.floor(box[0] / sx))
    Y0 = max(0, math.floor(box[1] / sy))
    X1 = min(dst_size[0], math.ceil(box[2] / sx))
    Y1 = min(dst_size[1], math.ceil(box[3] / sy))
    ox, oy = origin
    src_box = (X0 * sx - ox, Y0 * sy - oy, X1 * sx - ox, Y1 * sy - oy)
    im = resize_premul(patch, (X1 - X0, Y1 - Y0), box=src_box)
    return Frame(im.width, im.height, to_premul_bgra(im), X0, Y0)


def estimate_bytes(canvas: tuple, scale: float, frames: int) -> int:
    w, h = scale_size(canvas, scale)
    return w * h * 4 * frames


class FrameCache:
    """按片段缓存已缩放好的帧，总字节数超预算时淘汰最久没用的片段（正在用的不淘汰）。"""

    def __init__(self, budget_bytes: int = 40 * 1024 * 1024):
        self.budget = budget_bytes
        self._clips: OrderedDict = OrderedDict()   # 名字 → [Frame]
        self.pinned: set = set()

    def put(self, name: str, frames: list):
        self._clips[name] = frames
        self._clips.move_to_end(name)
        self._evict()

    def get(self, name: str):
        frames = self._clips.get(name)
        if frames is not None:
            self._clips.move_to_end(name)
        return frames

    def drop(self, name: str):
        self._clips.pop(name, None)

    def clear(self):
        self._clips.clear()

    @property
    def nbytes(self) -> int:
        return sum(f.nbytes for fs in self._clips.values() for f in fs if f is not None)

    def names(self):
        return list(self._clips)

    def _evict(self):
        for name in list(self._clips):
            if self.nbytes <= self.budget:
                return
            if name not in self.pinned:
                del self._clips[name]
