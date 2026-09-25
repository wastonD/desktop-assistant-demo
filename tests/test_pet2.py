# -*- coding: utf-8 -*-
"""宠物 v2（app/pet2）：清单、播放器、帧（预乘/补丁/命中/缓存）、烘焙、几何、行为状态机、置顶守护、窗口层。

窗口层用 FakeBackend（记录每次提交），配置文件指到临时目录——绝不碰用户的 config/pet.json。
"""
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops

from app.pet2 import anim, bake, brain, frames, layout, zorder
from app.pet2 import engine_of

ROOT = Path(__file__).resolve().parents[1]
CHAR = ROOT / "assets" / "character"


def _manifest(clips, root=Path(".")):
    return anim.parse_manifest({"version": 1, "source_size": [100, 160], "clips": clips}, root)


class EngineSwitchTests(unittest.TestCase):
    def test_engine_default_v1(self):
        self.assertEqual(engine_of({}), "v1")
        self.assertEqual(engine_of({"engine": "V2 "}), "v2")
        self.assertEqual(engine_of({"engine": "v3"}), "v1")
        self.assertEqual(engine_of(None), "v1")


class ManifestTests(unittest.TestCase):
    def test_parse_and_defaults(self):
        m = _manifest({"a": {"frames": "a/{:02d}.png", "count": 4, "fps": 8,
                             "events": {"2": "step"}, "anchor": [10, 20],
                             "overlays": {"eyes_closed": {"frames": "e/{}.png", "box": [1, 2, 30, 40],
                                                          "margin": 5}}}})
        c = m.clips["a"]
        self.assertEqual((c.count, c.fps, c.loop, c.anchor), (4, 8.0, "loop", (10.0, 20.0)))
        self.assertEqual(c.events, {2: "step"})
        self.assertEqual(c.overlays["eyes_closed"].margin, 5)
        self.assertAlmostEqual(c.duration, 0.5)
        self.assertTrue(m.has("a"))
        self.assertFalse(m.has("a", "b"))
        self.assertEqual(c.frame_path(Path("/r"), 3), Path("/r/a/03.png"))

    def test_errors(self):
        bad = [
            {"a": {"frames": "x.png", "count": 2, "fps": 5}},                 # 没有 {}
            {"a": {"frames": "x{}.png", "count": 0, "fps": 5}},
            {"a": {"frames": "x{}.png", "count": 2, "fps": 5, "loop": "bounce"}},
            {"a": {"frames": "x{}.png", "count": 2, "fps": 5, "next": "nope"}},
            {"a": {"frames": "x{}.png", "count": 2, "fps": 5, "events": {"5": "e"}}},
            {"a": {"frames": "x{}.png", "count": 2, "fps": 5,
                   "overlays": {"o": {"frames": "o{}.png", "box": [5, 5, 1, 1]}}}},
            {"a": {"count": 2, "fps": 5}},
        ]
        for clips in bad:
            with self.assertRaises(anim.ManifestError, msg=clips):
                _manifest(clips)
        with self.assertRaises(anim.ManifestError):
            anim.parse_manifest({"version": 2, "source_size": [1, 1]}, Path("."))

    def test_missing_files(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a").mkdir()
        Image.new("RGBA", (4, 4)).save(tmp / "a" / "0.png")
        m = _manifest({"a": {"frames": "a/{}.png", "count": 2, "fps": 5}}, tmp)
        self.assertEqual(m.missing_files(), [tmp / "a" / "1.png"])

    def test_repo_manifest(self):
        m = anim.load_manifest(CHAR / "anims" / "manifest.json")
        c = m.clips["home_idle"]
        self.assertTrue(c.seated)
        self.assertEqual(c.count, round(bake.FPS * bake.LOOP_S))
        self.assertEqual(c.fps, bake.FPS)
        self.assertEqual(tuple(c.size), bake.canvas_size((705, 1113)))
        self.assertIn("eyes_closed", c.overlays)
        self.assertIsNotNone(m.point("head_top"))


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.m = _manifest({
            "loop": {"frames": "l{}.png", "count": 4, "fps": 10, "events": {"2": "mid"}},
            "once": {"frames": "o{}.png", "count": 3, "fps": 10, "loop": "once", "next": "loop"},
            "pp": {"frames": "p{}.png", "count": 4, "fps": 10, "loop": "pingpong"},
            "hold": {"frames": "h{}.png", "count": 5, "fps": 10, "loop": "hold"},
        })

    def test_frame_index_modes(self):
        c = self.m.clips
        self.assertEqual([anim.frame_index(c["loop"], i / 10) for i in range(6)], [0, 1, 2, 3, 0, 1])
        self.assertEqual([anim.frame_index(c["once"], i / 10) for i in range(5)], [0, 1, 2, 2, 2])
        self.assertEqual([anim.frame_index(c["pp"], i / 10) for i in range(8)], [0, 1, 2, 3, 2, 1, 0, 1])
        self.assertEqual({anim.frame_index(c["hold"], i / 10) for i in range(8)}, {0})

    def test_events_and_loop(self):
        p = anim.Player(self.m)
        p.play("loop")
        ev = p.advance(0.25)            # 走过第 1、2 帧
        self.assertIn(("event", "mid"), ev)
        ev = p.advance(0.2)             # 进入下一圈
        self.assertIn(("loop", "loop"), ev)
        self.assertEqual(p.frame().index, 0)

    def test_big_dt_events_bounded(self):
        p = anim.Player(self.m)
        p.play("loop")
        ev = p.advance(1000.0)
        self.assertLessEqual(sum(1 for e in ev if e[0] == "event"), 3)

    def test_once_then_next(self):
        p = anim.Player(self.m)
        p.play("once")
        self.assertEqual(p.advance(0.1), [])
        ev = p.advance(0.25)
        self.assertIn(("end", "once"), ev)
        self.assertEqual(p.clip.name, "loop")
        self.assertEqual(p.frame().index, 0)

    def test_speed_change_no_jump(self):
        p = anim.Player(self.m)
        p.play("loop")
        p.advance(0.15)
        i0 = p.frame().index
        p.speed = 0.5
        p.advance(0.02)
        self.assertEqual(p.frame().index, i0)

    def test_blend(self):
        p = anim.Player(self.m)
        p.play("loop")
        p.advance(0.1)
        p.play("pp", blend=0.2)
        ref = p.frame()
        self.assertEqual((ref.prev_clip, ref.mix), ("loop", 0.0))
        p.advance(0.1)
        self.assertAlmostEqual(p.frame().mix, 0.5)
        p.advance(0.15)
        self.assertIsNone(p.frame().prev_clip)

    def test_same_clip_no_restart(self):
        p = anim.Player(self.m)
        p.play("loop")
        p.advance(0.2)
        p.play("loop", speed=2.0, restart=False)
        self.assertEqual(p.frame().index, 2)
        self.assertEqual(p.speed, 2.0)


class FramesTests(unittest.TestCase):
    def test_premultiply_matches_reference(self):
        rng = random.Random(3)
        px = [(rng.randrange(256), rng.randrange(256), rng.randrange(256), rng.randrange(256))
              for _ in range(500)]
        im = Image.new("RGBA", (500, 1))
        im.putdata(px)
        data = frames.to_premul_bgra(im)
        for i, p in enumerate(px):
            ref = frames.premultiply_pixel(*p)
            got = tuple(data[i * 4:i * 4 + 4])
            self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(ref, got)), (p, ref, got))
            self.assertEqual(got[3], p[3])
        # 全透明像素颜色必须为 0（ULW 预乘要求）
        self.assertEqual(frames.premultiply_pixel(200, 100, 50, 0), (0, 0, 0, 0))

    def test_patch_equals_full_frame_crop(self):
        rng = random.Random(1)
        W, H = 240, 320
        big = Image.new("RGBA", (W, H))
        big.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256),
                      rng.choice([0, 255, rng.randrange(256)])) for _ in range(W * H)])
        for scale in (0.3257, 0.4891, 0.21):
            dst = frames.scale_size((W, H), scale)
            full = frames.frame_from_image(big, dst)
            box, m = (90, 100, 170, 150), 24
            patch = big.crop((box[0] - m, box[1] - m, box[2] + m, box[3] + m))
            f = frames.scale_patch(patch, (box[0] - m, box[1] - m), (W, H), dst, box)
            worst = 0
            for y in range(f.h):
                row = full.data[((f.oy + y) * full.w + f.ox) * 4:((f.oy + y) * full.w + f.ox + f.w) * 4]
                got = f.data[y * f.w * 4:(y + 1) * f.w * 4]
                worst = max(worst, max(abs(a - b) for a, b in zip(row, got)))
            self.assertLessEqual(worst, 1, scale)    # 只允许浮点舍入的 ±1，没有接缝

    def test_hit_and_bbox(self):
        im = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        im.putpixel((3, 4), (255, 0, 0, 255))
        im.putpixel((5, 5), (255, 0, 0, 10))
        f = frames.frame_from_image(im)
        self.assertTrue(f.hit(3.6, 4.2))
        self.assertFalse(f.hit(5, 5))           # 太透明
        self.assertTrue(f.hit(5, 5, threshold=5))
        self.assertFalse(f.hit(-1, 0))
        self.assertFalse(f.hit(100, 100))
        self.assertEqual(f.bbox(), (3, 4, 6, 6))

    def test_cache_budget_keeps_pinned(self):
        c = frames.FrameCache(budget_bytes=1000)
        mk = lambda n: [frames.Frame(10, 10, bytes(400))] * n
        c.pinned.add("home")
        c.put("home", mk(2))                    # 800B
        c.put("walk", mk(1))                    # 超预算 → 淘汰 walk 以外最旧的非钉住项
        self.assertIsNotNone(c.get("home"))
        c.put("jump", mk(1))
        self.assertIsNotNone(c.get("home"))
        self.assertLessEqual(len(c.names()), 2)
        self.assertEqual(frames.estimate_bytes((100, 200), 0.5, 3), 50 * 100 * 4 * 3)


class BakeTests(unittest.TestCase):
    """用仓库里真实的切层素材烘焙（只在内存里，不写文件）。"""

    @classmethod
    def setUpClass(cls):
        cls.b = bake.HomeBaker(bake.load_inputs(CHAR))
        cls.ref = Image.alpha_composite(cls.b.tail, cls.b.body_open).convert("RGBa")

    def test_loop_params(self):
        b = self.b
        self.assertEqual(b.count, 54)
        self.assertEqual(b.foot_frames, 27)
        self.assertAlmostEqual(b.tail_angle(0), 0.0)
        self.assertAlmostEqual(b.squash(0), 1.0)
        self.assertEqual(b.foot_index(0), b.foot_index(b.count))     # 首尾无缝
        self.assertAlmostEqual(b.tail_angle(b.count), b.tail_angle(0), places=6)

    def test_rest_frame_equals_layers(self):
        """第 0 帧（不伸缩、尾巴 0°、脚 0°）与原图层直接叠加逐像素一致（±1）。"""
        d = ImageChops.difference(self.b.compose(0).convert("RGBa"), self.ref)
        self.assertLessEqual(max(x[1] for x in d.getextrema()), 1)

    def test_no_seam_at_sit_line(self):
        """呼吸伸缩最大的一帧：臀线上下几行的不透明度不能比原图少（没有发丝缝）。"""
        b = self.b
        i = round(b.count / 4)                   # sin 最大
        f = b.compose(i)
        row = round(b.sit) + bake.PAD_T
        fa, ra = f.getchannel("A"), self.ref.split()[3]
        for y in range(row - 3, row + 4):
            for x in range(0, f.width, 3):
                r = ra.getpixel((x, y))
                if r >= 250:
                    self.assertGreaterEqual(fa.getpixel((x, y)), r - 6, (x, y))

    def test_legs_static_except_foot(self):
        b = self.b
        f = b.compose(13).convert("RGBa")        # 脚抬到接近最高
        y0 = round(b.sit) + bake.PAD_T + 4
        region = (0, y0, f.width, f.height)
        diff = ImageChops.difference(f.crop(region), self.ref.crop(region)).convert("L")
        hole = Image.new("L", b.size, 0)
        hole.paste(b.foot_hole, (0, b.sit_row + bake.PAD_T))
        px, py = b.foot.patch_pos
        pw, ph = b.foot.patches[0].size
        allowed = (px, py + b.sit_row + bake.PAD_T, px + pw, py + b.sit_row + bake.PAD_T + ph)
        bb = diff.point(lambda v: 255 if v > 2 else 0).getbbox()
        self.assertIsNotNone(bb)                 # 脚确实动了
        bb = (bb[0], bb[1] + y0, bb[2], bb[3] + y0)
        self.assertTrue(allowed[0] <= bb[0] and allowed[1] <= bb[1] and bb[2] <= allowed[2]
                        and bb[3] <= allowed[3], (bb, allowed))

    def test_eyes_patch_covers_blink(self):
        b = self.b
        box = b.eyes_patch_box()
        bb = Image.open(CHAR / "layers" / "blink.png").getbbox()
        self.assertLessEqual(box[0], bb[0] + bake.PAD_L)
        self.assertLessEqual(box[1], bb[1] + bake.PAD_T)
        self.assertGreaterEqual(box[2], bb[2] + bake.PAD_L)
        self.assertGreaterEqual(box[3], bb[3] + bake.PAD_T)
        self.assertEqual(b.clip_json()["overlays"]["eyes_closed"]["margin"], bake.PATCH_MARGIN)

    def test_bad_period(self):
        with self.assertRaises(bake.BakeError):
            bake.HomeBaker(bake.load_inputs(CHAR), fps=15, loop_s=3.5)


class LayoutTests(unittest.TestCase):
    def test_scale_and_origin(self):
        s = layout.logical_scale(48, 0.642, 1.8, 1113)
        self.assertAlmostEqual(s * 1113, 48 / 0.358 * 1.8)
        o = layout.window_origin((1000.0, 952.0), (40, 722.5), s * 1.5, 1.5)
        self.assertTrue(all(isinstance(v, int) for v in o))
        x, y = layout.canvas_to_world((40, 722.5), o, s * 1.5, 1.5)
        self.assertAlmostEqual(x, 1000.0, delta=0.5)
        self.assertAlmostEqual(y, 952.0, delta=0.5)

    def test_crop_and_rects(self):
        self.assertEqual(layout.crop_rows_for_taskbar(300, 1000, 1200), 200)
        self.assertEqual(layout.crop_rows_for_taskbar(300, 1000, 900), 0)
        self.assertEqual(layout.crop_rows_for_taskbar(300, 1000, 2000), 300)
        self.assertTrue(layout.rects_intersect((0, 0, 10, 10), (5, 5, 20, 20)))
        self.assertFalse(layout.rects_intersect((0, 0, 10, 10), (10, 0, 20, 10)))
        self.assertEqual(layout.fps_interval_ms(15), 66)
        self.assertEqual(layout.fps_interval_ms(500), 15)


ROAM = {"home_idle", "stand_up", "stand_idle", "walk", "sit_down"}


class BrainTests(unittest.TestCase):
    def mk(self, clips=("home_idle",), activity=1, seed=0):
        b = brain.Brain(set(clips), 500.0, 1000.0, now=0.0, activity=activity, rng=random.Random(seed))
        b.set_world(brain.World(1000.0, 0.0, 2000.0))
        b.clip_lengths = {"stand_up": 0.6, "sit_down": 0.6}
        return b

    def test_no_roam_without_clips(self):
        b = self.mk()
        self.assertFalse(b.can_roam)
        b.tick(1e6, 0.03)
        self.assertEqual(b.state, "home")
        self.assertFalse(b.leave_home(0.0))
        self.assertEqual(b.clip(0.0), ("home_idle", 1.0))

    def test_activity_zero_stays(self):
        b = self.mk(ROAM, activity=0)
        self.assertFalse(b.can_roam)

    def test_leave_and_walk_and_home(self):
        b = self.mk(ROAM)
        b.tick(b.next_leave + 0.01, 0.03)
        self.assertEqual(b.state, "standup")
        self.assertEqual(b.clip(0)[0], "stand_up")
        t = b.t_state + 0.7
        b.tick(t, 0.03)
        self.assertEqual(b.state, "stand")
        b._walk_to(700.0, t)
        for k in range(400):
            t += 0.05
            b.tick(t, 0.05)
            if b.state != "walk":
                break
        self.assertAlmostEqual(b.x, 700.0)
        self.assertEqual(b.facing, 1)
        b.go_home(t)                             # 在地板上：走一小段坐下 = 回家
        for k in range(600):
            t += 0.05
            b.tick(t, 0.05)
            if b.state == "home":
                break
        self.assertEqual(b.state, "home")
        self.assertIn(("home_x", b.x), b.take_requests())

    def test_slide_and_click(self):
        b = self.mk()
        b.press(600, 990, 1.0)
        self.assertFalse(b.move(602, 990, 1.01))  # 小于点击阈值，不动
        b.release(602, 990, 1.05)
        self.assertEqual(b.take_requests(), [("click",)])
        b.press(600, 990, 2.0)
        self.assertTrue(b.move(640, 900, 2.05))   # 没有 dangle：往上拖也只横移
        self.assertEqual((b.state, b.x, b.y), ("slide", 540.0, 1000.0))
        b.release(640, 900, 2.1)
        self.assertEqual(b.state, "home")
        self.assertEqual(b.take_requests(), [("home_x", 540.0)])

    def test_pick_up_fall_land(self):
        b = self.mk(ROAM | {"dangle"}, seed=5)
        b.press(600, 990, 1.0)
        self.assertTrue(b.move(600, 900, 1.02))
        self.assertEqual(b.state, "drag")
        self.assertEqual(b.clip(0)[0], "dangle")
        b.move(650, 700, 1.05)
        b.release(650, 700, 1.06)
        self.assertEqual(b.state, "fall")
        t = 1.06
        for _ in range(200):
            t += 0.02
            b.tick(t, 0.02)
            if b.state != "fall":
                break
        self.assertEqual(b.state, "land")
        self.assertEqual(b.y, 1000.0)

    def test_jump_to_platform(self):
        b = self.mk(ROAM | {"jump"}, seed=2)
        b.state = "stand"
        plat = ("w1", 300.0, 900.0, 700.0)
        b.set_world(brain.World(1000.0, 0.0, 2000.0, [plat]))
        b._jump_to(plat, 600.0, 0.0)
        t = 0.0
        for _ in range(300):
            t += 0.016
            b.tick(t, 0.016)
            if b.state != "jump":
                break
        self.assertEqual(b.state, "land")
        self.assertEqual(b.platform, plat)
        self.assertEqual(b.y, 700.0)
        b.platform_moved("w1", 350.0, 950.0, 650.0)   # 窗口被拖走：跟着走
        self.assertEqual((b.y, b.platform[1]), (650.0, 350.0))
        b.platform_moved("w1", 0, 0, None)             # 窗口没了：掉下去
        self.assertEqual(b.state, "fall")

    def test_sleep_and_reactions(self):
        b = self.mk()
        b.set_sleeping(True)
        self.assertEqual(b.clip(0.0), ("home_idle", brain.SLEEP_SPEED))
        self.assertTrue(b.eyes_closed())
        b.set_sleeping(False)
        b.petted(10.0)
        self.assertEqual(b.clip(10.1), ("home_idle", brain.HAPPY_SPEED))   # 没有素材：只加速
        b2 = self.mk({"home_idle", "home_petted", "home_sleep"})
        b2.petted(10.0)
        self.assertEqual(b2.clip(10.1)[0], "home_petted")
        b2.tick(12.0, 0.03)
        self.assertEqual(b2.clip(12.0)[0], "home_idle")
        b2.set_sleeping(True)
        self.assertEqual(b2.clip(13.0), ("home_sleep", 1.0))
        self.assertFalse(b2.eyes_closed())

    def test_busy_or_sleep_blocks_leaving(self):
        b = self.mk(ROAM)
        b.set_busy(True)
        b.tick(b.next_leave + 1, 0.03)
        self.assertEqual(b.state, "home")
        b.set_busy(False)
        b.set_sleeping(True)
        b.tick(b.next_leave + 2, 0.03)
        self.assertEqual(b.state, "home")


class ZorderTests(unittest.TestCase):
    def test_coalescer_merges_without_dropping(self):
        c = zorder.Coalescer(window=0.016, min_gap=0.25, fallback=1.5)
        c.ran(0.0)
        c.mark(0.10)
        c.mark(0.105)
        self.assertFalse(c.due(0.11))            # 合并窗口没过
        self.assertFalse(c.due(0.20))            # 离上次执行不足 min_gap：推迟而不是丢
        self.assertTrue(c.pending)
        self.assertTrue(c.due(0.26))
        self.assertAlmostEqual(c.next_check_in(0.20), 0.05)
        c.ran(0.26)
        self.assertFalse(c.pending)
        self.assertFalse(c.due(1.0))
        self.assertTrue(c.due(1.8))              # 低频兜底

    def test_needs_raise(self):
        me = (100, 100, 300, 400)
        tray = ((0, 380, 2000, 450), True, True, 42)
        self.assertTrue(zorder.needs_raise(me, [tray], own_pid=7))
        self.assertFalse(zorder.needs_raise(me, [((0, 380, 2000, 450), True, True, 7)], 7))  # 自己进程的
        self.assertFalse(zorder.needs_raise(me, [((0, 380, 2000, 450), False, True, 42)], 7))  # 不可见
        self.assertFalse(zorder.needs_raise(me, [((0, 380, 2000, 450), True, False, 42)], 7))  # 非置顶
        self.assertFalse(zorder.needs_raise(me, [((500, 0, 900, 90), True, True, 42)], 7))     # 不相交


# ---------------------------------------------------------------- 窗口层（offscreen + 假后端）
class FakeBackend:
    hwnd = 0

    def __init__(self):
        self.commits = []
        self.moves = []
        self.visible = False

    def set_handler(self, fn):
        self.handler = fn

    def commit(self, data, w, h, x, y):
        n = len(data) if isinstance(data, (bytes, bytearray)) else memoryview(data).nbytes
        assert n >= w * h * 4, (n, w, h)
        self.commits.append((w, h, x, y, bytes(data[:w * h * 4])))
        return True

    def move(self, x, y):
        self.moves.append((x, y))
        return True

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def close(self):
        self.visible = False


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app import pet_window
        self.tmp = Path(tempfile.mkdtemp())
        cfg = mock.patch.object(pet_window, "CONFIG_PATH", self.tmp / "pet.json")
        cfg.start()
        self.addCleanup(cfg.stop)
        # 小画布的假素材：4 帧，每帧一个不透明方块（位置随帧变），外加闭眼补丁
        anims = self.tmp / "anims"
        (anims / "f").mkdir(parents=True)
        (anims / "e").mkdir()
        for i in range(4):
            im = Image.new("RGBA", (200, 320), (0, 0, 0, 0))
            im.paste((200, 50, 50, 255), (60 + i * 5, 40, 140 + i * 5, 300))
            im.save(anims / "f" / f"{i}.png")
            im.crop((20, 20, 180, 140)).save(anims / "e" / f"{i}.png")
        (anims / "manifest.json").write_text(json.dumps({
            "version": 1, "source_size": [200, 320], "points": {"head_top": [100, 40]},
            "clips": {"home_idle": {"frames": "f/{}.png", "count": 4, "fps": 10, "anchor": [0, 200],
                                    "size": [200, 320], "seated": True,
                                    "overlays": {"eyes_closed": {"frames": "e/{}.png",
                                                                 "box": [44, 44, 156, 116],
                                                                 "margin": 24}}}}}),
            encoding="utf-8")
        from app.pet2.window import PetWindowV2
        self.be = FakeBackend()
        self.pet = PetWindowV2(backend=self.be, manifest_path=anims / "manifest.json", auto_bake=False)
        self.pet._timer.stop()
        self.addCleanup(self.pet.cleanup)
        deadline = time.monotonic() + 10
        while self.pet.cache.get("home_idle") is None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertIsNotNone(self.pet.cache.get("home_idle"))

    def _inside(self):
        """当前帧不透明区域中心的屏幕物理坐标。"""
        pet = self.pet
        x0, y0, x1, y1 = pet._current()[0].bbox()
        return pet._origin[0] + (x0 + x1) // 2, pet._origin[1] + (y0 + y1) // 2

    def test_commit_is_single_call_with_position(self):
        pet, be = self.pet, self.be
        pet.show()
        self.assertTrue(be.visible)
        n0 = len(be.commits)
        self.assertGreaterEqual(n0, 1)
        w, h, x, y, data = be.commits[-1]
        self.assertEqual((w, h), pet._frame_size)
        self.assertEqual(len(data), w * h * 4)
        self.assertEqual((x, y), pet._origin)
        pet._render()                             # 什么都没变：不提交
        self.assertEqual(len(be.commits), n0)
        pet.player.advance(0.1)                   # 下一帧
        pet._render()
        self.assertEqual(len(be.commits), n0 + 1)

    def test_blink_uses_patch_same_commit(self):
        pet, be = self.pet, self.be
        pet.show()
        n0 = len(be.commits)
        pet._blink_until = time.monotonic() + 10
        pet._render()
        self.assertEqual(len(be.commits), n0 + 1)
        self.assertEqual(be.commits[-1][:2], pet._frame_size)

    def test_slide_moves_without_redraw(self):
        pet, be = self.pet, self.be
        pet.show()
        x0, y0 = self._inside()
        be.handler("down", x0, y0)
        n0 = len(be.commits)
        be.handler("move", x0 + 30, y0)
        self.assertEqual(len(be.commits), n0)     # 只挪位置
        self.assertTrue(be.moves)
        be.handler("up", x0 + 30, y0)
        self.assertEqual(pet.brain.state, "home")
        self.assertIn("x", pet.cfg)

    def test_click_opens_panel_and_miss_is_ignored(self):
        pet, be = self.pet, self.be
        pet.show()
        with mock.patch.object(type(pet), "_toggle_chat") as tog:
            x0, y0 = self._inside()
            be.handler("down", x0, y0)
            be.handler("up", x0, y0)
            self.assertEqual(tog.call_count, 1)
            be.handler("down", pet._origin[0] + 1, pet._origin[1] + 1)   # 透明处
            be.handler("up", pet._origin[0] + 1, pet._origin[1] + 1)
            self.assertEqual(tog.call_count, 1)

    def test_head_point_and_scale_change(self):
        pet = self.pet
        pet.show()
        hp = pet.head_point()
        self.assertGreater(hp.x(), 0)
        old = pet._frame_size
        pet._set_scale(2.4)
        self.assertNotEqual(pet._frame_size, old)
        self.assertEqual(json.loads((self.tmp / "pet.json").read_text(encoding="utf-8"))["scale"], 2.4)

    def test_over_taskbar_off_crops(self):
        pet, be = self.pet, self.be
        pet.cfg["over_taskbar"] = False
        pet._last_key = None
        pet._render()
        w, h, x, y, _ = be.commits[-1]
        self.assertLessEqual(y + h, round(pet._taskbar_top * pet._dpr))

    def test_tick_runs(self):
        pet = self.pet
        pet.show()
        for _ in range(5):
            pet._tick()
        self.assertEqual(pet.brain.state, "home")

    def test_fullscreen_hides_and_returns(self):
        """全屏应用在前台（explorer 去掉了任务栏的 TOPMOST）→ 藏起来；退出全屏 → 回来。
        藏着的时候帧照常提交（分层窗口隐藏时保留内容），回来不需要重画。"""
        pet, be = self.pet, self.be
        pet.show()
        be.hwnd = 1
        with mock.patch.object(zorder, "windows_above", return_value=[]), \
                mock.patch.object(zorder, "taskbar_dropped", return_value=True):
            pet._guard_z()
        self.assertFalse(be.visible)
        self.assertFalse(pet.fx.emote.isVisible())
        with mock.patch.object(zorder, "windows_above", return_value=[]), \
                mock.patch.object(zorder, "taskbar_dropped", return_value=False):
            pet._guard_z()
        self.assertTrue(be.visible)
        self.assertFalse(pet._fs_hidden)

    def test_z_event_checks_immediately(self):
        """被任务栏压住要当场抬（不等 15fps 的 tick）；30ms 内的第二个事件推迟到定时器，不丢。"""
        pet = self.pet
        pet.show()
        with mock.patch.object(type(pet), "_guard_z", autospec=True,
                               side_effect=lambda s: s._z.ran(time.monotonic())) as g:
            pet._z.ran(time.monotonic() - 1)
            pet._on_z_event()
            self.assertEqual(g.call_count, 1)
            pet._on_z_event()
            self.assertEqual(g.call_count, 1)
            self.assertTrue(pet._z_timer.isActive())


@unittest.skipUnless(os.name == "nt", "原生分层窗口只在 Windows 上")
class NativeBackendTests(unittest.TestCase):
    """真建一个原生窗口（放在屏幕外、不显示）：独立线程、所有者、位置、重建、关闭。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import ctypes
        import ctypes.wintypes as wt
        from app.pet2 import backend
        self.u, self.wt = ctypes.windll.user32, wt
        self.b = backend.create_backend()
        self.addCleanup(self.b.close)

    def _rect(self):
        import ctypes
        r = self.wt.RECT()
        self.u.GetWindowRect(self.wt.HWND(self.b.hwnd), ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def _wait(self, cond, sec=2.0):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_commit_move_any_buffer_type(self):
        from PySide6.QtGui import QImage
        b = self.b
        self.assertNotEqual(b._thread.ident, __import__("threading").get_ident())   # 窗口在自己的线程
        qi = QImage(40, 30, QImage.Format_ARGB32_Premultiplied)
        qi.fill(0x80800000)
        for data in (bytes(40 * 30 * 4), bytearray(40 * 30 * 4), qi.bits()):
            self.assertTrue(b.commit(data, 40, 30, -7000, -7000))
        self.assertTrue(self._wait(lambda: self._rect() == (-7000, -7000, -6960, -6970)))
        b.move(-6900, -6950)
        self.assertTrue(self._wait(lambda: self._rect() == (-6900, -6950, -6860, -6920)))

    def test_recreated_after_destroyed(self):
        """explorer 重启会连带销毁被任务栏拥有的窗口：窗口线程重建、补交最后一帧、通知 Qt 重新布局。"""
        b = self.b
        got = []
        b.set_handler(lambda k, x, y: got.append(k))
        b.commit(bytes(20 * 20 * 4), 20, 20, -7100, -7100)
        self.assertTrue(self._wait(lambda: self._rect() == (-7100, -7100, -7080, -7080)))
        old = b.hwnd
        self.u.PostMessageW(self.wt.HWND(old), 0x0010, 0, 0)          # WM_CLOSE → 在窗口线程里 DestroyWindow
        self.assertTrue(self._wait(lambda: b.hwnd and b.hwnd != old and "display" in got, 3.0))
        self.assertTrue(self._wait(lambda: self._rect() == (-7100, -7100, -7080, -7080)))

    def test_close_stops_thread(self):
        self.b.close()
        self.assertFalse(self.b._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
