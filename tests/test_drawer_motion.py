# -*- coding: utf-8 -*-
"""app/drawer/motion.py：弹簧解析解、橡皮筋、速度估计、拖拽/甩动/点击（纯逻辑）。"""
import math
import unittest

from app.drawer import motion as M


def run(mo, seconds=3.0, dt=1 / 60):
    trace = []
    t = 0.0
    while t < seconds:
        moving = mo.tick(dt)
        trace.append(mo.pos)
        t += dt
        if not moving:
            break
    return trace


class SpringTests(unittest.TestCase):
    def test_converges_all_regimes(self):
        for zeta in (0.5, 0.8, 1.0, 1.4):
            x, v = 0.0, 0.0
            for _ in range(600):
                x, v = M.spring_step(x, v, 500.0, 1 / 120, zeta=zeta)
            self.assertAlmostEqual(x, 500.0, delta=0.5, msg=zeta)

    def test_frame_rate_independent(self):
        """解析解：一步走 0.1s 与十步各 0.01s 结果相同。"""
        for zeta in (0.8, 1.0, 1.3):
            a = M.spring_step(0.0, 300.0, 400.0, 0.1, zeta=zeta)
            x, v = 0.0, 300.0
            for _ in range(10):
                x, v = M.spring_step(x, v, 400.0, 0.01, zeta=zeta)
            self.assertAlmostEqual(a[0], x, places=6)
            self.assertAlmostEqual(a[1], v, places=4)

    def test_critical_no_overshoot_under_bounces(self):
        x, v, top = 0.0, 0.0, 0.0
        for _ in range(400):
            x, v = M.spring_step(x, v, 100.0, 1 / 120, zeta=1.0)
            top = max(top, x)
        self.assertLessEqual(top, 100.0 + 1e-6)
        x, v, top = 0.0, 0.0, 0.0
        for _ in range(400):
            x, v = M.spring_step(x, v, 100.0, 1 / 120, zeta=0.8)
            top = max(top, x)
        self.assertGreater(top, 100.5)          # 轻微回弹
        self.assertLess(top, 105.0)

    def test_initial_velocity_carries(self):
        slow = M.spring_step(0.0, 0.0, 500.0, 0.05, zeta=1.0)[0]
        fast = M.spring_step(0.0, 3000.0, 500.0, 0.05, zeta=1.0)[0]
        self.assertGreater(fast, slow + 50)


class RubberAndProjectTests(unittest.TestCase):
    def test_rubber_band(self):
        self.assertEqual(M.rubber_band(0), 0.0)
        self.assertEqual(M.rubber_band(-5), 0.0)
        vals = [M.rubber_band(x) for x in (10, 50, 200, 1000, 1e6)]
        self.assertTrue(all(a < b for a, b in zip(vals, vals[1:])))   # 单调
        self.assertTrue(all(v < M.RUBBER_D for v in vals))              # 到不了 d
        self.assertLess(M.rubber_band(100), 100)                         # 越拉越沉
        self.assertAlmostEqual(M.rubber_band(1e-3), 1e-3 * M.RUBBER_C, places=6)  # 起点斜率 = c

    def test_project(self):
        self.assertAlmostEqual(M.project(1000), 499.0)
        self.assertAlmostEqual(M.project(-1000), -499.0)
        self.assertEqual(M.project(0), 0.0)

    def test_velocity_tracker(self):
        vt = M.VelocityTracker()
        for i in range(10):
            vt.add(i * 0.01, 5 + 800 * i * 0.01)
        self.assertAlmostEqual(vt.velocity(0.09), 800.0, delta=1)
        self.assertEqual(vt.velocity(1.0), 0.0)      # 停住很久才松手
        vt.reset()
        self.assertEqual(vt.velocity(), 0.0)


class DrawerMotionTests(unittest.TestCase):
    def test_click_toggles_with_spring(self):
        mo = M.DrawerMotion(600)
        mo.press(10, 0.0)
        self.assertEqual(mo.release(12, 0.05), "click")
        self.assertTrue(mo.is_open)
        trace = run(mo)
        self.assertEqual(mo.pos, 600)
        self.assertTrue(mo.settled)
        self.assertGreater(max(trace), 600)          # 拉到底轻轻回弹
        mo.press(610, 5.0)
        mo.release(610, 5.02)
        run(mo)
        self.assertTrue(mo.fully_closed)

    def test_drag_follows_one_to_one_and_rubber(self):
        mo = M.DrawerMotion(600)
        mo.press(0, 0.0)
        mo.drag(250, 0.1)
        self.assertEqual(mo.pos, 250)
        mo.drag(800, 0.2)
        self.assertGreater(mo.pos, 600)
        self.assertLess(mo.pos, 600 + M.RUBBER_D)
        mo.drag(-100, 0.3)
        self.assertLess(mo.pos, 0)
        self.assertGreater(mo.pos, -M.RUBBER_D)

    def test_release_by_position(self):
        mo = M.DrawerMotion(600)
        mo.press(0, 0.0)
        mo.drag(400, 0.1)
        mo.release(400, 0.5)                          # 停了一会儿才松手：按位置（>一半）
        self.assertTrue(mo.is_open)
        mo2 = M.DrawerMotion(600)
        mo2.press(0, 0.0)
        mo2.drag(200, 0.1)
        mo2.release(200, 0.5)
        self.assertFalse(mo2.is_open)

    def test_fling_small_distance_opens(self):
        mo = M.DrawerMotion(600)
        mo.press(0, 0.0)
        for i in range(1, 6):                         # 50ms 内拉 120px：约 2400px/s
            mo.drag(i * 24, i * 0.01)
        self.assertEqual(mo.release(120, 0.05), "open")
        self.assertGreater(mo.vel, 1000)              # 速度交接给弹簧

    def test_fling_up_closes_even_when_mostly_open(self):
        mo = M.DrawerMotion(600)
        mo.snap(True)
        mo.press(600, 0.0)
        for i in range(1, 6):
            mo.drag(600 - i * 20, i * 0.01)
        self.assertEqual(mo.release(500, 0.05), "close")
        run(mo)
        self.assertTrue(mo.fully_closed)

    def test_grab_mid_animation_continues_from_current(self):
        mo = M.DrawerMotion(600)
        mo.animate_to(True)
        for _ in range(8):
            mo.tick(1 / 60)
        mid = mo.pos
        self.assertTrue(0 < mid < 600)
        mo.press(100, 1.0)
        self.assertEqual(mo.pos, mid)                 # 不跳
        mo.drag(130, 1.05)
        self.assertAlmostEqual(mo.pos, mid + 30)

    def test_set_travel_keeps_open(self):
        mo = M.DrawerMotion(600)
        mo.snap(True)
        mo.set_travel(700)
        self.assertEqual((mo.pos, mo.target), (700, 700))


if __name__ == "__main__":
    unittest.main()
