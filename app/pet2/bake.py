# -*- coding: utf-8 -*-
"""用现有切层素材离线预合成"在家坐姿"整帧（呼吸 × 尾摆 × 晃脚，外加每帧的闭眼补丁）。

输入：assets/character/ 下的 layers/body.png、tail.png、blink.png 和 character.json（与 v1 同一套，只读）。
输出：<out>/home_idle/000.png…（整帧，画布 = 立绘四周加余量）与 <out>/home_idle_eyes/000.png…（闭眼补丁）。

为什么能做到"零接缝"：
- 每一帧都在源分辨率上一次合成完（尾巴 → 身体 → 晃脚的脚掌），显示时整帧只缩放一次；
- 呼吸 = 臀线以上按比例纵向伸缩、臀线以下恒等（MESH 连续映射，臀线处连续，没有切口）；
- 晃脚沿用 v1 第五代 foot_swing.build（脚掌绕脚踝连续形变），只是换到源分辨率上算；
- 闭眼补丁用"同一帧、同一呼吸相位、眼睛闭上"的整帧裁出来（四周多存 24px 给缩放用），和整帧逐像素一致。

周期：一圈 LOOP_S 秒。尾巴 1 个周期、呼吸 1 个周期、晃脚 2 个周期（1.8s），所以首尾无缝循环。
纯 Pillow，不依赖 Qt。
"""
import json
import math
from pathlib import Path

from PIL import Image, ImageChops

from .. import foot_swing

PAD_L, PAD_T, PAD_R, PAD_B = 40, 8, 28, 0   # 画布余量：翘起的脚尖在左、摆动的尾尖在右、吸气时头顶往上
LOOP_S = 3.6
FPS = 15
BREATH_AMP = 0.006          # 与 v1 相同（臀线以上 0.6%）
EYE_BOX_GROW = 6            # 闭眼区域外扩（呼吸会让眼睛上下挪 ~3px）
PATCH_MARGIN = 24           # 补丁四周多存的像素（显示缩放时给 LANCZOS 取样）


class BakeError(RuntimeError):
    pass


def _open(path):
    im = Image.open(path)
    im.load()
    return im.convert("RGBA")


def load_inputs(char_dir: Path) -> dict:
    meta = json.loads((char_dir / "character.json").read_text(encoding="utf-8"))
    layers = meta.get("layers") or {}
    need = {k: char_dir / layers.get(k, f"layers/{k}.png") for k in ("body", "tail", "blink")}
    missing = [str(p) for p in need.values() if not p.exists()]
    if missing:
        raise BakeError("缺少切层素材：" + "、".join(missing))
    return {"meta": meta, **{k: _open(p) for k, p in need.items()}}


def canvas_size(src_size) -> tuple:
    return src_size[0] + PAD_L + PAD_R, src_size[1] + PAD_T + PAD_B


def squash_upper(img: Image.Image, sit_y: float, squash: float) -> Image.Image:
    """臀线以上以臀线为锚纵向伸缩（squash>1 = 吸气变高），臀线以下原样。映射在臀线处连续。"""
    if abs(squash - 1.0) < 1e-9:
        return img
    w, h = img.size
    top_src = sit_y - sit_y / squash
    # MESH：输出矩形 ← 源四边形（左上、左下、右下、右上）
    mesh = [((0, 0, w, math.floor(sit_y)), (0, top_src, 0, math.floor(sit_y) / squash + top_src,
                                            w, math.floor(sit_y) / squash + top_src, w, top_src)),
            ((0, math.floor(sit_y), w, h), (0, math.floor(sit_y), 0, h, w, h, w, math.floor(sit_y)))]
    out = img.convert("RGBa").transform((w, h), Image.MESH, mesh, resample=Image.BICUBIC)
    return out.convert("RGBA")


def _place(img: Image.Image, size) -> Image.Image:
    c = Image.new("RGBA", size, (0, 0, 0, 0))
    c.paste(img, (PAD_L, PAD_T))
    return c


class HomeBaker:
    """准备一次（抠晃脚、摆好图层），然后按帧号合成。"""

    def __init__(self, inputs: dict, fps: int = FPS, loop_s: float = LOOP_S):
        meta = inputs["meta"]
        self.fps, self.loop_s = fps, loop_s
        self.count = round(fps * loop_s)
        if abs(self.count - fps * loop_s) > 1e-6:
            raise BakeError(f"{loop_s}s × {fps}fps 不是整数帧")
        body, tail, blink = inputs["body"], inputs["tail"], inputs["blink"]
        self.src_size = body.size
        self.size = canvas_size(body.size)
        H = body.size[1]
        self.sit = float(meta.get("sit_ratio", 0.642)) * H            # 714.5：臀下沿（台沿）
        self.sit_row = round(self.sit)
        self.tail_amp = float(meta.get("tail_amplitude_deg", 4.5))
        self.pivot = (meta["tail_pivot"][0] + PAD_L, meta["tail_pivot"][1] + PAD_T)
        self.tail = _place(tail, self.size)
        self.body_open = _place(body, self.size)
        self.body_closed = _place(Image.alpha_composite(body, blink), self.size)
        bb = blink.getbbox() or (0, 0, 1, 1)
        g = EYE_BOX_GROW
        self.eye_box = (bb[0] - g + PAD_L, bb[1] - g + PAD_T, bb[2] + g + PAD_L, bb[3] + g + PAD_T)

        # 晃脚：v1 第五代算法，源分辨率（k=1），脚尖越界靠 pad=PAD_L 接住
        self.foot = None
        self.foot_frames = 0
        fc = meta.get("foot_swing")
        if fc and fc.get("enabled", True):
            period = float(fc.get("period_s", 1.8))
            per = round(period * fps)
            if abs(loop_s / period - round(loop_s / period)) > 1e-6 or self.count % per:
                raise BakeError(f"晃脚周期 {period}s 不能整除循环 {loop_s}s")
            legs = body.crop((0, self.sit_row, body.width, H))
            cfg = dict(fc, frames=per)
            res = foot_swing.build(legs, 1.0, self.sit_row, cfg, PAD_L)
            if res is not None:
                self.foot, self.foot_frames = res, per
                padded = Image.new("RGBA", res.static.size, (0, 0, 0, 0))
                padded.paste(legs, (PAD_L, 0))
                diff = ImageChops.difference(padded.getchannel("A"), res.static.getchannel("A"))
                self.foot_hole = diff.point(lambda v: 255 if v else 0)   # 被挖掉、由贴片接管的像素

    # ---- 每帧参数（公开出来便于测试）
    def phase(self, i: int) -> float:
        return (i / self.count) % 1.0

    def tail_angle(self, i: int) -> float:
        return self.tail_amp * math.sin(2 * math.pi * self.phase(i))

    def squash(self, i: int) -> float:
        return 1.0 + BREATH_AMP * math.sin(2 * math.pi * self.phase(i))

    def foot_index(self, i: int) -> int:
        return i % self.foot_frames if self.foot_frames else 0

    def compose(self, i: int, eyes_closed: bool = False) -> Image.Image:
        # 尾巴（最底层）：绕根部旋转；Qt 的 rotate(正角) 是顺时针，Pillow 是逆时针，取反保持和 v1 一致
        frame = self.tail.convert("RGBa").rotate(-self.tail_angle(i), resample=Image.BICUBIC,
                                                  center=self.pivot).convert("RGBA")
        body = self.body_closed if eyes_closed else self.body_open
        body = squash_upper(body, self.sit + PAD_T, self.squash(i))
        if self.foot is not None:
            hole = Image.new("L", self.size, 0)
            hole.paste(self.foot_hole, (0, self.sit_row + PAD_T))
            body = body.copy()
            body.paste((0, 0, 0, 0), (0, 0), hole)
            patch = self.foot.patches[self.foot_index(i)]
            px, py = self.foot.patch_pos
            layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
            layer.paste(patch, (px, py + self.sit_row + PAD_T))
            body = Image.alpha_composite(body, layer)
        return Image.alpha_composite(frame, body)

    def eyes_patch_box(self) -> tuple:
        m = PATCH_MARGIN
        x0, y0, x1, y1 = self.eye_box
        return max(0, x0 - m), max(0, y0 - m), min(self.size[0], x1 + m), min(self.size[1], y1 + m)

    def clip_json(self, frames_dir="baked/home_idle", eyes_dir="baked/home_idle_eyes") -> dict:
        m = PATCH_MARGIN
        return {
            "frames": frames_dir + "/{:03d}.png", "count": self.count, "fps": self.fps,
            "loop": "loop", "anchor": [PAD_L, self.sit + PAD_T], "size": list(self.size),
            "seated": True,
            "overlays": {"eyes_closed": {"frames": eyes_dir + "/{:03d}.png",
                                         "box": list(self.eye_box), "margin": m}},
        }


def bake_home(char_dir: Path, out_dir: Path, fps: int = FPS, loop_s: float = LOOP_S,
              progress=None, compress_level: int = 1) -> dict:
    """把在家循环写成 PNG；返回清单里 home_idle 片段的 JSON（帧路径相对 anims/ 目录）。"""
    baker = HomeBaker(load_inputs(char_dir), fps, loop_s)
    fdir, edir = out_dir / "home_idle", out_dir / "home_idle_eyes"
    fdir.mkdir(parents=True, exist_ok=True)
    edir.mkdir(parents=True, exist_ok=True)
    box = baker.eyes_patch_box()
    for i in range(baker.count):
        baker.compose(i).save(fdir / f"{i:03d}.png", compress_level=compress_level)
        baker.compose(i, eyes_closed=True).crop(box).save(edir / f"{i:03d}.png",
                                                          compress_level=compress_level)
        if progress:
            progress(i + 1, baker.count)
    return baker.clip_json()
