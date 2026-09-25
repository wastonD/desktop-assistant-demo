# -*- coding: utf-8 -*-
"""晃脚（第五代）：翘着的那只脚掌绕脚踝做连续形变旋转，小腿不动。

前四代方案的教训：切割+旋转露缝、重叠分层出鬼影、条带偏移出发丝纹、
整段纵向伸缩看着像"拉长缩短"。本方案：
- 洪水填充把翘腿单独抠成一层；形变只作用在这一层，竖直那条腿和身体不参与
- 旋转权重沿"小腿轴线"用 smoothstep 从 0 过渡到 1（过渡带垂直于小腿，就像真实关节），
  映射处处连续，没有切口；过渡带以上是恒等映射，与静态部分逐像素一致
- 直接在物理分辨率的腿部图像上计算（与身体窗口同一套像素网格），拼接处整数像素对齐
- 启动时一次算好一个周期的帧（Pillow MESH 变换，预乘 alpha 重采样），运行时只轮流贴图

纯 Pillow，不依赖 Qt，便于测试；pet_window.py 负责 QImage 互转和显示。
"""
import math
from collections import deque

from PIL import Image, ImageFilter

CELL = 3  # 形变网格边长（物理像素）


def _smoothstep(e0, e1, x):
    t = min(1.0, max(0.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


def ease(phase: float) -> float:
    """0..1 周期 → 0..1 幅度：慢抬、在高处稍停、落下。"""
    s = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
    return s ** 1.3


def _leg_mask(img: Image.Image, seed, x_max) -> Image.Image:
    """从 seed 洪水填充不透明像素（限 x<x_max），再补上外圈半透明抗锯齿像素。"""
    w, h = img.size
    a = img.getchannel("A").load()
    mask = Image.new("L", (w, h), 0)
    m = mask.load()
    q = deque([seed])
    while q:
        x, y = q.popleft()
        if x < 0 or y < 0 or x >= min(w, x_max) or y >= h or m[x, y] or a[x, y] <= 8:
            continue
        m[x, y] = 255
        q.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    ring = mask.filter(ImageFilter.MaxFilter(3)).load()
    for y in range(h):
        for x in range(min(w, x_max)):
            if ring[x, y] and not m[x, y] and a[x, y] > 0:
                m[x, y] = 255
    return mask


class FootSwing:
    """build() 的结果：static（挖掉脚掌区的腿部底图）+ patches（每帧脚掌）+ 贴片位置。"""

    def __init__(self, static, patches, patch_pos, pad):
        self.static = static          # PIL RGBA，宽 = 原腿部宽 + pad
        self.patches = patches        # [PIL RGBA]，每帧一张
        self.patch_pos = patch_pos    # (x, y) 贴片左上角在 static 里的位置（物理像素）
        self.pad = pad                # 左侧补的透明像素数（物理）


def build(legs: Image.Image, k: float, sit_src: float, cfg: dict, pad: int) -> FootSwing | None:
    """legs：物理分辨率的腿部图（臀线以下，RGBA）；k：源图→物理的缩放比；
    sit_src：臀线在源图中的 y；cfg：character.json 的 foot_swing 段（源图坐标）；
    pad：左侧余量（物理像素，由调用方按 DPI 取整）。找不到翘腿时返回 None。"""
    W, H = legs.size
    canvas = Image.new("RGBA", (W + pad, H), (0, 0, 0, 0))
    canvas.paste(legs, (pad, 0))

    def to_phys(p):
        return p[0] * k + pad, p[1] * k - sit_src * k

    seed = tuple(round(v) for v in to_phys(cfg["seed"]))
    if not (0 <= seed[0] < canvas.width and 0 <= seed[1] < H):
        return None
    mask = _leg_mask(canvas, seed, round(cfg["x_max"] * k) + pad)
    if mask.getbbox() is None:
        return None

    top = to_phys(cfg["shin_top"])
    ankle = to_phys(cfg["ankle"])
    band = float(cfg.get("band", 40)) * k
    length = math.hypot(ankle[0] - top[0], ankle[1] - top[1])
    ux, uy = (ankle[0] - top[0]) / length, (ankle[1] - top[1]) / length

    # 贴片框：过渡带起点往上留 4px 恒等区，往下到底；左边到 0（脚尖上抬会向左越界，靠 pad 接住）
    mp = mask.load()
    ys = [y for y in range(H) for x in range(canvas.width)
          if mp[x, y] and (x - top[0]) * ux + (y - top[1]) * uy >= length - band - 4]
    if not ys:
        return None
    y0 = max(0, min(ys) - 4)
    x1 = min(canvas.width, round(cfg["x_max"] * k) + pad + 2)
    box = (0, y0, x1, H)

    box_mask = Image.new("L", canvas.size, 0)
    box_mask.paste(mask.crop(box), box[:2])
    static = canvas.copy()
    static.paste((0, 0, 0, 0), (0, 0), box_mask)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(canvas, (0, 0), box_mask)
    layer = layer.convert("RGBa")  # 预乘 alpha 重采样，边缘不发黑

    def inverse(x, y, deg):
        s = (x - top[0]) * ux + (y - top[1]) * uy
        w = _smoothstep(length - band, length + band / 2, s)
        if w <= 0:
            return x, y
        a = -math.radians(deg) * w
        dx, dy = x - ankle[0], y - ankle[1]
        return (ankle[0] + math.cos(a) * dx - math.sin(a) * dy,
                ankle[1] + math.sin(a) * dx + math.cos(a) * dy)

    frames = int(cfg.get("frames", 30))
    max_deg = float(cfg.get("angle_deg", 14))
    bx0, by0, bx1, by1 = box
    patches = []
    for i in range(frames):
        deg = max_deg * ease(i / frames)
        mesh = []
        for gy in range(by0, by1, CELL):
            for gx in range(bx0, bx1, CELL):
                ex, ey = min(gx + CELL, bx1), min(gy + CELL, by1)
                quad = []
                for cx, cy in ((gx, gy), (gx, ey), (ex, ey), (ex, gy)):  # 左上、左下、右下、右上
                    quad += inverse(cx, cy, deg)
                mesh.append(((gx - bx0, gy - by0, ex - bx0, ey - by0), quad))
        patch = layer.transform((bx1 - bx0, by1 - by0), Image.MESH, mesh, resample=Image.BICUBIC)
        patches.append(patch.convert("RGBA"))
    return FootSwing(static, patches, (bx0, by0), pad)
