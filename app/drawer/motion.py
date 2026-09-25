# -*- coding: utf-8 -*-
"""抽屉手感的纯逻辑：阻尼弹簧（解析解）、橡皮筋、速度估计、投射终点、拖拽/甩动/点击状态机。

设计要点：
- 把手长在抽屉前板上，拖动时 1:1 跟手，全程可打断（动画中再抓住从当前位置和速度接着走）；
- 松手：投射终点 pos + v/1000·r/(1-r)（r=0.998，Apple WWDC18），离哪个停靠点近去哪个；
  松手速度直接作为弹簧初速度（速度交接），甩得快抽屉就走得快；
- 越界：橡皮筋 f(x) = (1 − 1/(x·c/d + 1))·d，c=0.55，越拉越沉、永远到不了 d；
- 弹簧用解析解逐帧求值：与帧率无关、不会发散。打开 ζ=0.9、收起 ζ=1。
  （实测：ζ=0.8 时到底后 15px 的回弹要慢慢爬 250ms，整个开合 ~470ms，手感发涩；
  ζ=0.9 时回弹 ~1.5px 看不出来，~250ms 到位。）
坐标：pos = 抽屉从"收起"位置往下拉出的距离（像素），0 = 收起，travel = 完全打开。
"""
import math

STIFFNESS = 420.0          # k（质量 1）：约 0.25 秒到位
ZETA_OPEN = 0.9
ZETA_CLOSE = 1.0
RUBBER_C = 0.55
RUBBER_D = 120.0           # 橡皮筋最大越界距离（像素）
CLICK_SLOP = 4.0           # 按下到松开移动不超过这么多像素 = 点击
PROJECT_RATE = 0.998
SETTLE_POS, SETTLE_VEL = 0.5, 20.0


def spring_step(x: float, v: float, target: float, dt: float, k: float = STIFFNESS,
                zeta: float = 1.0, m: float = 1.0) -> tuple:
    """阻尼弹簧的解析解：从 (x, v) 出发经过 dt 秒后的 (x, v)。"""
    d = x - target
    w0 = math.sqrt(k / m)
    if zeta < 1.0:
        wd = w0 * math.sqrt(1.0 - zeta * zeta)
        a, b = d, (v + zeta * w0 * d) / wd
        e = math.exp(-zeta * w0 * dt)
        c, s = math.cos(wd * dt), math.sin(wd * dt)
        nd = e * (a * c + b * s)
        nv = e * ((-zeta * w0 * a + wd * b) * c + (-zeta * w0 * b - wd * a) * s)
    elif zeta == 1.0:
        a, b = d, v + w0 * d
        e = math.exp(-w0 * dt)
        nd = (a + b * dt) * e
        nv = (b - w0 * (a + b * dt)) * e
    else:
        r = math.sqrt(zeta * zeta - 1.0)
        r1, r2 = -w0 * (zeta - r), -w0 * (zeta + r)
        c2 = (v - r1 * d) / (r2 - r1)
        c1 = d - c2
        e1, e2 = math.exp(r1 * dt), math.exp(r2 * dt)
        nd = c1 * e1 + c2 * e2
        nv = r1 * c1 * e1 + r2 * c2 * e2
    return target + nd, nv


def rubber_band(over: float, dim: float = RUBBER_D, c: float = RUBBER_C) -> float:
    """越界 over 像素时实际显示的越界量（over≥0）。"""
    if over <= 0:
        return 0.0
    return (1.0 - 1.0 / (over * c / dim + 1.0)) * dim


def project(v: float, rate: float = PROJECT_RATE) -> float:
    """以速度 v（像素/秒）松手后"惯性还能滑多远"。"""
    return v / 1000.0 * rate / (1.0 - rate)


class VelocityTracker:
    """最近 window 秒内的采样做最小二乘斜率（比首尾两点抗抖）。"""

    def __init__(self, window: float = 0.08):
        self.window = window
        self.samples = []

    def reset(self):
        self.samples = []

    def add(self, t: float, y: float):
        self.samples.append((t, y))
        cut = t - self.window
        while len(self.samples) > 2 and self.samples[0][0] < cut:
            self.samples.pop(0)

    def velocity(self, now: float | None = None) -> float:
        s = self.samples
        if now is not None and s and now - s[-1][0] > self.window:
            return 0.0                       # 停住了一会儿才松手 = 没有甩
        if len(s) < 2:
            return 0.0
        n = len(s)
        mt = sum(t for t, _ in s) / n
        my = sum(y for _, y in s) / n
        den = sum((t - mt) ** 2 for t, _ in s)
        if den <= 1e-12:
            return 0.0
        return sum((t - mt) * (y - my) for t, y in s) / den


class DrawerMotion:
    """抽屉位置状态机。窗口层每帧调 tick(dt)，按返回的 pos 摆放抽屉和把手（两者同一个 y 关系）。"""

    def __init__(self, travel: float = 600.0):
        self.travel = float(travel)
        self.pos = 0.0
        self.vel = 0.0
        self.target = 0.0
        self.state = "idle"                # idle | drag | spring
        self._press = None                 # (手指 y, 按下时的 pos, 时刻)
        self._moved = False
        self.tracker = VelocityTracker()

    # ---- 查询
    @property
    def is_open(self) -> bool:
        return self.target >= self.travel

    @property
    def settled(self) -> bool:
        return self.state == "idle"

    @property
    def fully_closed(self) -> bool:
        return self.state == "idle" and self.pos <= 0.0 and self.target <= 0.0

    def set_travel(self, travel: float):
        was_open = self.is_open
        self.travel = float(travel)
        if was_open:
            self.target = self.travel
            if self.state == "idle":
                self.pos = self.travel

    # ---- 手势
    def press(self, y: float, t: float):
        """按住把手（动画途中也可以抓住）。"""
        self._press = (y, self.pos, t)
        self._moved = False
        self.state = "drag"
        self.vel = 0.0
        self.tracker.reset()
        self.tracker.add(t, y)

    def drag(self, y: float, t: float):
        if self._press is None:
            return
        y0, p0, _ = self._press
        if abs(y - y0) > CLICK_SLOP:
            self._moved = True
        self.tracker.add(t, y)
        raw = p0 + (y - y0)
        if raw > self.travel:
            self.pos = self.travel + rubber_band(raw - self.travel)
        elif raw < 0:
            self.pos = -rubber_band(-raw)
        else:
            self.pos = raw

    def release(self, y: float, t: float) -> str:
        """松手。返回 "click"（原地点了一下）/"open"/"close"。"""
        if self._press is None:
            return "none"
        self.drag(y, t)
        moved = self._moved
        self._press = None
        if not moved:
            self.toggle()
            return "click"
        v = self.tracker.velocity(t)
        landing = self.pos + project(v)
        self.animate_to(landing > self.travel / 2, v)
        return "open" if self.is_open else "close"

    # ---- 程序驱动（点击、快捷键、Esc、失焦）
    def toggle(self):
        self.animate_to(not self.is_open, self.vel)

    def animate_to(self, open_: bool, velocity: float | None = None):
        self.target = self.travel if open_ else 0.0
        if velocity is not None:
            self.vel = velocity
        self.state = "spring"

    def snap(self, open_: bool):
        self.target = self.pos = self.travel if open_ else 0.0
        self.vel = 0.0
        self.state = "idle"

    def tick(self, dt: float) -> bool:
        """推进弹簧。返回是否还在动。"""
        if self.state != "spring":
            return self.state == "drag"
        zeta = ZETA_OPEN if self.target > 0 else ZETA_CLOSE
        self.pos, self.vel = spring_step(self.pos, self.vel, self.target, dt, zeta=zeta)
        if abs(self.pos - self.target) < SETTLE_POS and abs(self.vel) < SETTLE_VEL:
            self.pos, self.vel, self.state = self.target, 0.0, "idle"
            return False
        return True
