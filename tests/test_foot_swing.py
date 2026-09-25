# -*- coding: utf-8 -*-
"""晃脚帧生成（app/foot_swing.py）的像素级回归测试：用真实素材、小尺寸，纯 Pillow 不依赖 Qt。"""
import json
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from app import foot_swing

ROOT = Path(__file__).resolve().parents[1]
CHAR = ROOT / "assets" / "character"


def _max_diff(a: Image.Image, b: Image.Image) -> int:
    return max(hi for _, hi in ImageChops.difference(a, b).getextrema())


class FootSwingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        meta = json.loads((CHAR / "character.json").read_text(encoding="utf-8"))
        cls.cfg = dict(meta["foot_swing"], frames=8)
        src = Image.open(CHAR / "pet.png").convert("RGBA")
        cls.k = 0.4
        W, H = round(src.width * cls.k), round(src.height * cls.k)
        full = src.resize((W, H), Image.LANCZOS)
        sit = round(H * meta["sit_ratio"])
        cls.legs = full.crop((0, sit, W, H))
        cls.sit_src = src.height * meta["sit_ratio"]
        cls.pad = 20
        cls.r = foot_swing.build(cls.legs, cls.k, cls.sit_src, cls.cfg, cls.pad)

    def _compose(self, i):
        comp = self.r.static.copy()
        comp.alpha_composite(self.r.patches[i], self.r.patch_pos)
        return comp

    def test_rest_frame_matches_original(self):
        ref = Image.new("RGBA", self.r.static.size, (0, 0, 0, 0))
        ref.paste(self.legs, (self.pad, 0))
        self.assertLessEqual(_max_diff(self._compose(0), ref), 1)

    def test_peak_frame_moves_foot(self):
        self.assertGreater(_max_diff(self._compose(0), self._compose(len(self.r.patches) // 2)), 60)

    def test_patch_top_rows_static_every_frame(self):
        """贴片上沿是恒等区：每一帧都与静止帧一致，否则贴片边界会露缝。"""
        band = (0, 0, self.r.patches[0].width, 3)
        for p in self.r.patches:
            self.assertLessEqual(_max_diff(p.crop(band), self.r.patches[0].crop(band)), 1)

    def test_other_leg_untouched(self):
        """竖直那条腿（x_max 右侧）在所有帧里都不动。"""
        x_split = round(self.cfg["x_max"] * self.k) + self.pad + 3
        right = (x_split, 0, self.r.static.width, self.r.static.height)
        rest = self._compose(0).crop(right)
        for i in range(len(self.r.patches)):
            self.assertLessEqual(_max_diff(self._compose(i).crop(right), rest), 1)

    def test_toes_not_clipped(self):
        peak = self.r.patches[len(self.r.patches) // 2]
        self.assertGreater(peak.getbbox()[0] + self.r.patch_pos[0], 0)

    def test_bad_seed_returns_none(self):
        cfg = dict(self.cfg, seed=[5000, 5000])
        self.assertIsNone(foot_swing.build(self.legs, self.k, self.sit_src, cfg, self.pad))


if __name__ == "__main__":
    unittest.main()
