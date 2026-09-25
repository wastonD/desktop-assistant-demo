# -*- coding: utf-8 -*-
"""自由活动：小银从任务栏上站起来，在屏幕里走动、跳上窗口顶边、累了找地方坐。

分工：
- 在家（坐在任务栏上沿）仍由 PetWindow 的三窗口系统负责（原立绘 + 晃脚等，零改动）；
- 离家时 PetWindow 三窗口隐藏，由这里的 RoamWindow（单个置顶透明窗）画：
  站立/行走/跳跃/被拎起 = pet_rig 纸偶；坐在窗口顶边 = 原立绘（尾巴、眨眼、晃脚帧照旧）；
- 站起/坐下用"上半身对齐的交叉淡化"：两种画法的上半身像素完全重合，只有下半身在变。

世界坐标 = 逻辑像素。每个状态只记一个"落点"(gx, gy)：站着=脚底中点，坐着=臀下沿（台沿）中点。
平台 = 任务栏上沿（地板）+ 可见窗口的顶边（按 z 序扣掉被上层窗口挡住的部分）。
"""
import ctypes
import ctypes.wintypes
import math
import os
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from . import pet_rig as R

SIT_Y = 714.5                 # 立绘里臀下沿（台沿）的 y（= sit_ratio × 高）
BOX_X0, BOX_Y0 = -60, -60     # RoamWindow 左上角对应的源图坐标
BOX_X1 = 830
BOX_Y1_STAND = R.GROUND_Y + 60                       # 站着：窗口只要到脚底
BOX_Y1_SIT = R.GROUND_Y + (1113 - 714.5) + 40         # 坐着/交叉淡化：立绘的腿垂到台沿以下
SEAT_DROP = R.GROUND_Y - SIT_Y  # 站着和坐着时，上半身的高度差（源图像素）

GRAVITY = 2400.0              # 逻辑像素/秒²
WALK_SPEED_SRC = R.STRIDE / (0.6 * 1.05)  # 源图像素/秒：支撑相 0.6 周期走完一个步长 → 脚不打滑
WALK_PERIOD = 1.05            # 一个步态周期（秒）
MIN_PLATFORM = 70             # 平台最短可站长度（逻辑像素）

ACTIVITY = {  # 活泼度：在家坐多久（秒）才出门；离家后多久（秒）想回家
    0: None,
    1: ((150, 420), (240, 600)),
    2: ((50, 160), (360, 900)),
}

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi


# ---------------------------------------------------------------- 平台扫描
def _frame_rect(hwnd):
    r = ctypes.wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # 扩展边框
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    return r.left, r.top, r.right, r.bottom


def _cloaked(hwnd):
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def scan_windows(own_pid):
    """按 z 序（上→下）返回可见顶层窗口 [(hwnd, (l,t,r,b) 物理像素)]。"""
    out = []
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    cls = ctypes.create_unicode_buffer(64)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                         "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"):
            return True
        ex = user32.GetWindowLongW(hwnd, -20)
        if ex & 0x00000080:  # WS_EX_TOOLWINDOW
            return True
        if _cloaked(hwnd):
            return True
        rect = _frame_rect(hwnd)
        if rect and rect[2] - rect[0] > 80 and rect[3] - rect[1] > 60:
            out.append((hwnd, rect))
        return True
    user32.EnumWindows(proc_t(cb), 0)
    return out


def _subtract(segs, a, b):
    res = []
    for x0, x1 in segs:
        if b <= x0 or a >= x1:
            res.append((x0, x1))
        else:
            if a > x0:
                res.append((x0, a))
            if b < x1:
                res.append((b, x1))
    return res


def platforms(windows, dpr, screen, floor_y, headroom):
    """把窗口顶边变成可站立的平台 [(hwnd, x0, x1, y)]（逻辑像素）。第一个永远是地板。"""
    plats = [(None, screen.left(), screen.right(), floor_y)]
    above = []
    for hwnd, (l, t, r, b) in windows:
        L, T, Rr, B = l / dpr, t / dpr, r / dpr, b / dpr
        if T - headroom >= screen.top() and T < floor_y - 40:
            segs = [(max(L, screen.left()), min(Rr, screen.right()))]
            for (aL, aT, aR, aB) in above:  # 被上层窗口挡住的那段顶边不能站
                if aT <= T - 2 <= aB:
                    segs = _subtract(segs, aL - 20, aR + 20)
            for x0, x1 in segs:
                if x1 - x0 >= MIN_PLATFORM:
                    plats.append((hwnd, x0, x1, T))
        above.append((L, T, Rr, B))
    return plats


# ---------------------------------------------------------------- 窗口
class RoamWindow(QWidget):
    def __init__(self, ctl):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.ctl = ctl

    def paintEvent(self, _):
        self.ctl.paint(self)

    def mousePressEvent(self, e):
        self.ctl.on_press(e)

    def mouseMoveEvent(self, e):
        self.ctl.on_move(e)

    def mouseReleaseEvent(self, e):
        self.ctl.on_release(e)

    def contextMenuEvent(self, e):
        self.ctl.pet.contextMenuEvent(e)


class RoamController:
    """离家活动的大脑 + 渲染。PetWindow 每帧调用 tick(now, dt)。"""

    def __init__(self, pet, character):
        self.pet = pet
        self.parts = R.RigParts(char=character)  # 懒加载：第一次画纸偶时才切图，切完就丢原图
        self.win = RoamWindow(self)
        self.away = False
        self.state = "home"
        self.gx = self.gy = 0.0          # 落点（逻辑像素）
        self.vx = self.vy = 0.0
        self.facing = -1
        self.platform = None             # 当前所在平台 (hwnd, x0, x1, y)
        self._plats = []
        self._last_scan = 0.0
        self.t_state = 0.0               # 进入当前状态的时刻
        self.dur = 0.0                   # 当前状态计划时长
        self.target_x = None
        self.jump = None                 # (x0,y0,x1,y1,T,plat)
        self.walk_phase = 0.0
        self.trans = 0.0                 # 0=完全坐姿 1=完全站姿（交叉淡化用）
        self.stamina = 1.0
        self.swing = 0.0                 # 被拎起时的摆角
        self.swing_v = 0.0
        self._drag = None
        self._press = None
        self._last_mouse = None
        self._plat_offset = None         # 坐/站在窗口上时相对窗口左边的偏移
        self._pid = os.getpid()
        self._next_leave = time.monotonic() + self._rand_home()
        self._want_home_at = 0.0
        self._s = 1.0
        self._pm_cache = {}
        self._then = None
        self._land_at = -9.0
        self._crouch_until = 0.0
        self._ox = self._oy = 0.0
        self._last_paint = 0.0

    # ---------------------------------------------------------- 配置
    @property
    def activity(self):
        try:
            return int(self.pet.cfg.get("activity", 1))
        except (TypeError, ValueError):
            return 1

    def _rand_home(self):
        a = ACTIVITY.get(self.activity)
        return random.uniform(*a[0]) if a else 1e9

    def _rand_away(self):
        a = ACTIVITY.get(self.activity) or ACTIVITY[1]
        return random.uniform(*a[1])

    # ---------------------------------------------------------- 几何
    def _scale(self):
        return self.pet._src_scale

    def _floor(self):
        _, top, _ = self.pet._taskbar_geometry()
        return float(top)

    def _layout(self) -> bool:
        """把窗口摆到落点。返回是否真的移动/改了尺寸（调用方据此同步重绘，避免旧画面在新位置闪一帧）。"""
        s = self._scale()
        seated = self.trans < 1.0 or self.state in ("sit", "standup", "sitdown")
        y1 = BOX_Y1_SIT if seated else BOX_Y1_STAND
        size = (math.ceil((BOX_X1 - BOX_X0) * s), math.ceil((y1 - BOX_Y0) * s))
        changed = False
        if s != self._s:
            self._s = s
            self._pm_cache.clear()
        if (self.win.width(), self.win.height()) != size:
            self.win.resize(*size)
            changed = True
        wx = round(self.gx - (R.CX - BOX_X0) * s)
        wy = round(self.gy - (R.GROUND_Y - BOX_Y0) * s)
        if (self.win.x(), self.win.y()) != (wx, wy):
            self.win.move(wx, wy)
            changed = True
        # 源图原点在窗口里的精确（带小数）位置：保证坐姿立绘和 PetWindow 的像素对齐，交接时不跳
        self._ox = self.gx - R.CX * s - wx
        self._oy = self.gy - R.GROUND_Y * s - wy
        return changed

    def src_to_world(self, sx, sy, mirror=True):
        s = self._scale()
        if mirror and self.facing > 0:
            sx = 2 * R.CX - sx
        sy += SEAT_DROP * (1.0 - self.trans)  # 坐姿/交叉淡化时整体下移（见 paint）
        return QPointF(self.gx + (sx - R.CX) * s, self.gy + (sy - R.GROUND_Y) * s)

    def head_point(self):
        from .pet_fx import HEAD_TOP
        return self.src_to_world(*HEAD_TOP, mirror=self.facing > 0 and self.trans >= 1.0)

    def emote_anchor(self):
        from .pet_fx import EMOTE_ANCHOR
        return self.src_to_world(*EMOTE_ANCHOR, mirror=self.facing > 0 and self.trans >= 1.0)

    # ---------------------------------------------------------- 离家/回家
    def leave_home(self):
        """从任务栏上的座位站起来。"""
        if self.away:
            return
        s = self._scale()
        self.away = True
        self.gx = self.pet.x() + R.CX * s
        self.gy = self._floor()
        self.platform = (None, -1e9, 1e9, self.gy)
        self._plat_offset = None
        self.facing = -1
        self.trans = 0.0
        self._set("standup", 0.55)
        self._want_home_at = time.monotonic() + self._rand_away()
        self._layout()
        self.win.show()
        self.pet.set_home_visible(False)
        self._scan(force=True)

    def _arrive_home(self):
        """在地板上坐下 = 回到任务栏座位（PetWindow 接手）。"""
        s = self._scale()
        self.away = False
        self.state = "home"
        self.pet.go_home_at(self.gx - R.CX * s)
        self.win.hide()
        self.stamina = 1.0
        self._next_leave = time.monotonic() + self._rand_home()

    def go_home_now(self):
        """菜单"回家坐着"：从窗口上下来，走回去坐下。"""
        if not self.away:
            return
        self._want_home_at = 0.0
        if self.state in ("sit",):
            self._set("standup", 0.55)
        elif self.state not in ("drag", "fall", "jump", "standup", "sitdown"):
            self._go_rest(home=True)

    def pick_up_from_home(self, global_pos):
        """在家时被往上拖：直接拎起来。"""
        self.leave_home()
        self.trans = 1.0
        self._start_drag(global_pos, QPointF(0, 0))

    # ---------------------------------------------------------- 状态机
    def _set(self, state, dur=0.0):
        if state == "sitdown":
            self.facing = -1  # 坐姿立绘只有朝左一种：先转回来再坐
        self.state = state
        self.t_state = time.monotonic()
        self.dur = dur

    def _elapsed(self):
        return time.monotonic() - self.t_state

    def tick(self, now, dt):
        fx = self.pet.fx
        if not self.away:
            if (self.activity and now >= self._next_leave and not fx.sleeping
                    and not self.pet.busy() and self.pet._drag_offset is None):
                self.leave_home()
            return
        self._scan()
        st = self.state
        busy = self.pet.busy()
        if busy and st == "walk":  # 面板打开了就站住，别走远
            self._set("idle", 1.0)
            st = "idle"
        if st == "drag":
            self._tick_drag(dt)
        elif st == "fall":
            self._tick_fall(dt)
        elif st == "jump":
            self._tick_jump(dt)
        else:
            self._follow_platform()
            if st == "standup":
                self.trans = min(1.0, self._elapsed() / self.dur)
                if self.trans >= 1.0:
                    self._set("idle", random.uniform(1.0, 2.5))
            elif st == "sitdown":
                self.trans = max(0.0, 1.0 - self._elapsed() / self.dur)
                if self.trans <= 0.0:
                    if self.platform and self.platform[0] is None:
                        self._arrive_home()
                        return
                    self._set("sit", random.uniform(45, 150))
            elif st == "sit":
                self.stamina = min(1.0, self.stamina + dt * 0.02)
                if not fx.sleeping and not busy and self._elapsed() > self.dur:
                    self._set("standup", 0.55)
            elif st == "walk":
                self._tick_walk(dt)
            elif st in ("idle", "look", "stretch"):
                self._tick_idle(now, busy)
        moved = self._layout()
        if moved and self.pet.bubble.isVisible():  # 她走动时气泡跟着头走
            self.pet.bubble._place()
        # 自适应帧率：走/跳/被拎/站起坐下 30fps；站着发呆 15fps；坐着 20fps（尾巴、晃脚够顺）
        fast = st in ("walk", "jump", "fall", "drag", "standup", "sitdown") or             now - self._land_at < 0.3 or self.pet.fx.emote.active()
        interval = 0.0 if fast else (1 / 20 if st == "sit" else 1 / 15)
        if moved or now - self._last_paint >= interval - 0.004:
            self._last_paint = now
            # 同步重绘：move 之后立刻出新画面，和窗口位置在同一帧里交给 DWM（异步 update 会让旧画面在新位置闪一下）
            self.win.repaint()

    # ---- 站着想干嘛
    def _tick_idle(self, now, busy):
        fx = self.pet.fx
        if fx.sleeping and self.trans >= 1.0:   # 困了：原地坐下（窗口边或地板=回家）
            self._set("sitdown", 0.55)
            return
        if busy:
            self._face_cursor()
            return
        if self._elapsed() < self.dur:
            if self.state == "idle" and random.random() < 0.01:
                self._face_cursor()
            return
        tired = self.stamina < 0.28
        want_home = time.monotonic() > self._want_home_at
        if tired or want_home:
            self._go_rest(home=want_home)
            return
        r = random.random()
        if r < 0.5:
            self._start_walk_random()
        elif r < 0.68:
            self._try_jump() or self._start_walk_random()
        elif r < 0.8:
            self.facing = -self.facing
            self._set("look", random.uniform(1.2, 2.4))
        elif r < 0.9:
            self._set("stretch", 1.6)
            self.pet.fx.emote.spawn("note", 1)
        else:
            self._set("idle", random.uniform(2, 5))

    def _go_rest(self, home=False):
        """累了：找地方坐。窗口顶边就地坐；在地板上就走到某处坐下（=回家）。"""
        plat = self.platform
        if plat and plat[0] is not None and not home:
            self._set("sitdown", 0.55)
            if random.random() < 0.5:
                self.pet.show_bubble(random.choice(["……累了，坐会儿。", "歇一下。", "腿酸。"]), 2500)
            return
        if plat and plat[0] is not None:
            # 想回家：从窗口上跳下去
            self._jump_to(self._plats[0], self.gx)
            return
        # 在地板上：走一小段然后坐下
        self.target_x = self._clamp_x(self.gx + random.uniform(-160, 160), self.platform)
        self._then = "sitdown"
        self._set("walk")

    def _clamp_x(self, x, plat):
        s = self._scale()
        margin = 40
        scr = QApplication.primaryScreen().availableGeometry()
        lo = max(plat[1], scr.left()) + margin
        hi = min(plat[2], scr.right()) - margin
        return max(lo, min(hi, x))

    def _start_walk_random(self):
        plat = self.platform
        dist = random.uniform(80, 320) * random.choice((-1, 1))
        self.target_x = self._clamp_x(self.gx + dist, plat)
        if abs(self.target_x - self.gx) < 20:
            self.target_x = self._clamp_x(self.gx - dist, plat)
        self._then = None
        self._set("walk")

    def _tick_walk(self, dt):
        if self.target_x is None:
            self._set("idle", 1.0)
            return
        d = self.target_x - self.gx
        self.facing = 1 if d > 0 else -1
        step = WALK_SPEED_SRC * self._scale() * dt
        self.walk_phase = (self.walk_phase + dt / WALK_PERIOD) % 1.0
        self.stamina = max(0.0, self.stamina - dt * 0.012)
        if abs(d) <= step:
            self.gx = self.target_x
            self.target_x = None
            if getattr(self, "_then", None) == "sitdown":
                self._then = None
                self._set("sitdown", 0.55)
            else:
                self._set("idle", random.uniform(1.5, 4.5))
            return
        self.gx += step * self.facing
        if self._plat_offset is not None:
            self._plat_offset += step * self.facing

    def _face_cursor(self):
        c = QCursor.pos()
        if abs(c.x() - self.gx) > 30:
            self.facing = 1 if c.x() > self.gx else -1

    # ---- 跳
    def _try_jump(self):
        s_h = (R.GROUND_Y - BOX_Y0) * self._scale()
        cands = []
        for p in self._plats[1:] + self._plats[:1]:
            if self.platform and p[0] == self.platform[0]:
                continue  # 同一个平台（包括都在地板上）不算跳
            dy = self.gy - p[3]
            if -760 < dy < 580:  # 猫：往上能跳两个多身高
                x = self._clamp_x(min(max(self.gx, p[1] + 40), p[2] - 40), p)
                if abs(x - self.gx) < 520:
                    cands.append((p, x))
        if not cands:
            return False
        p, x = random.choice(cands)
        self._jump_to(p, x + random.uniform(-40, 40))
        return True

    def _jump_to(self, plat, x):
        x = self._clamp_x(x, plat)
        dx, dy = x - self.gx, plat[3] - self.gy   # dy<0 = 往上跳
        apex = min(self.gy, plat[3]) - 60          # 至少比两端都高 60
        up = self.gy - apex
        t_up = math.sqrt(2 * up / GRAVITY)
        down = plat[3] - apex
        t_down = math.sqrt(2 * max(1.0, down) / GRAVITY)
        T = t_up + t_down
        self.vx = dx / T
        self.vy = -GRAVITY * t_up
        self.facing = 1 if dx > 0 else -1
        self.jump = plat
        self._crouch_until = time.monotonic() + 0.16
        self.stamina = max(0.0, self.stamina - 0.07)
        self._set("jump")

    def _tick_jump(self, dt):
        if time.monotonic() < self._crouch_until:
            return
        self.gx += self.vx * dt
        self.vy += GRAVITY * dt
        self.gy += self.vy * dt
        plat = self.jump
        if self.vy > 0 and self.gy >= plat[3] and plat[1] - 10 <= self.gx <= plat[2] + 10:
            self._land(plat)
        elif self.gy > self._floor() + 5:
            self._land(self._plats[0])

    # ---- 掉落 / 落地
    def _tick_fall(self, dt):
        prev = self.gy
        self.vy += GRAVITY * dt
        self.vx *= 0.995
        self.gx += self.vx * dt
        self.gy += self.vy * dt
        scr = QApplication.primaryScreen().availableGeometry()
        if self.gx < scr.left() + 20 or self.gx > scr.right() - 20:
            self.vx = -self.vx * 0.4
            self.gx = max(scr.left() + 20, min(self.gx, scr.right() - 20))
        for p in sorted(self._plats, key=lambda q: q[3]):
            if prev <= p[3] <= self.gy and p[1] <= self.gx <= p[2]:
                self._land(p, hard=self.vy > 1200)
                return

    def _land(self, plat, hard=False):
        self.gy = plat[3]
        self.vx = self.vy = 0.0
        self.platform = plat
        self._remember_offset()
        self._land_at = time.monotonic()
        self._set("idle", random.uniform(0.8, 2.0))
        if hard:
            self.pet.show_bubble(random.choice(["痛……", "轻点啊！", "……摔死了。"]), 2200)

    # ---- 被拎起
    def _start_drag(self, global_pos, grab_local):
        self._drag = (QPointF(global_pos) - QPointF(self.gx, self.gy))
        self._last_mouse = (QPointF(global_pos), time.monotonic())
        self.swing = self.swing_v = 0.0
        self._set("drag")
        if random.random() < 0.6:
            self.pet.show_bubble(random.choice(["放、放我下来！", "喂！干嘛！", "……哼。"]), 2200)

    def drag_to(self, global_pos):
        if self.state != "drag":
            return
        gp = QPointF(global_pos)
        nx = gp.x() - self._drag.x()
        ny = gp.y() - self._drag.y()
        now = time.monotonic()
        lp, lt = self._last_mouse
        dtm = max(1e-3, now - lt)
        self.vx = 0.7 * self.vx + 0.3 * (nx - self.gx) / dtm
        self.vy = 0.7 * self.vy + 0.3 * (ny - self.gy) / dtm
        self.swing_v -= (nx - self.gx) * 0.35
        self.gx, self.gy = nx, ny
        self._last_mouse = (gp, now)
        if self._layout():
            self.win.repaint()

    def release_drag(self):
        if self.state == "drag":
            self.platform = None
            self.vx = max(-900, min(900, self.vx))
            self.vy = max(-900, min(600, self.vy))
            self._set("fall")

    def _tick_drag(self, dt):
        # 摆角：阻尼弹簧
        self.swing_v += -self.swing * 60 * dt
        self.swing_v *= (1 - 4 * dt)
        self.swing += self.swing_v * dt * 10
        self.swing = max(-40, min(40, self.swing))

    # ---- 平台跟随
    def _remember_offset(self):
        plat = self.platform
        if plat and plat[0]:
            r = _frame_rect(plat[0])
            if r:
                dpr = self.pet.devicePixelRatioF()
                self._plat_offset = self.gx - r[0] / dpr
                return
        self._plat_offset = None

    def _follow_platform(self):
        """站/坐在窗口上：窗口移动就跟着走；窗口没了/被最小化就掉下去。"""
        plat = self.platform
        if not plat or plat[0] is None:
            if plat is not None:
                self.gy = self._floor()
            return
        hwnd = plat[0]
        ok = user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd)
        r = _frame_rect(hwnd) if ok else None
        if not r:
            self._fall_from_here()
            return
        dpr = self.pet.devicePixelRatioF()
        L, T, Rr = r[0] / dpr, r[1] / dpr, r[2] / dpr
        if self._plat_offset is not None:
            self.gx = L + self._plat_offset
        moved = abs(T - self.gy) > 0.5
        self.gy = T
        if moved and self.state == "sit" and random.random() < 0.05:
            self.pet.show_bubble("喂！别晃！", 1800)
        if self.gx < L - 10 or self.gx > Rr + 10:
            self._fall_from_here()

    def _fall_from_here(self):
        self.platform = None
        self._plat_offset = None
        self.trans = 1.0
        self.vx = self.vy = 0.0
        self._set("fall")

    def _scan(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_scan < 0.35:
            return
        self._last_scan = now
        dpr = self.pet.devicePixelRatioF()
        scr = QApplication.primaryScreen().geometry()
        headroom = (R.GROUND_Y - 0) * self._scale() * 0.9
        wins = scan_windows(self._pid)
        self._plats = platforms(wins, dpr, scr, self._floor(), headroom)
        # 当前站的那段顶边如果被别的窗口盖住了，就掉下去
        plat = self.platform
        if plat and plat[0] is not None and self.state not in ("drag", "fall", "jump"):
            if not any(p[0] == plat[0] and p[1] - 12 <= self.gx <= p[2] + 12 for p in self._plats):
                self._fall_from_here()

    # ---------------------------------------------------------- 鼠标
    def on_press(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition()

    def on_move(self, e):
        gp = e.globalPosition()
        if self._press is not None and self.state != "drag":
            if (gp - self._press).manhattanLength() > 6:
                self.trans = 1.0
                self._start_drag(self._press, QPointF(0, 0))
        if self.state == "drag":
            self.drag_to(gp)
            return
        # 悬停：撸头判定（映射回源图坐标）
        s = self._scale()
        lx, ly = e.position().x(), e.position().y()
        sx = (lx - self._ox) / s
        sy = (ly - self._oy) / s
        if self.facing > 0 and self.trans >= 1.0:
            sx = 2 * R.CX - sx
        if self.trans < 1.0:
            sy -= SEAT_DROP * (1.0 - self.trans)
        self.pet.fx.on_hover(sx, sy)

    def on_release(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.state == "drag":
            self.release_drag()
        elif self._press is not None:
            self.pet._toggle_chat()
        self._press = None

    # ---------------------------------------------------------- 绘制
    def paint(self, w: QWidget):
        # 先画进离屏缓冲再一次性贴到窗口：实测直接在高 DPI 透明窗口上画路径要慢 4~5 倍
        dpr = w.devicePixelRatioF()
        size = (round(w.width() * dpr), round(w.height() * dpr))
        buf = self._pm_cache.get("buf")
        if buf is None or (buf.width(), buf.height()) != size:
            buf = QImage(size[0], size[1], QImage.Format_ARGB32_Premultiplied)
            buf.setDevicePixelRatio(dpr)
            self._pm_cache["buf"] = buf
        buf.fill(0)
        p = QPainter(buf)
        self._paint_into(p, w)
        p.end()
        wp = QPainter(w)
        wp.setCompositionMode(QPainter.CompositionMode_Source)
        wp.drawImage(0, 0, buf)
        wp.end()
        fx = self.pet.fx
        em = fx.emote
        a = self.emote_anchor()
        em.move(round(a.x() - em.anchor.x()), round(a.y() - em.anchor.y()))

    def _paint_into(self, p: QPainter, w: QWidget):
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = self._scale()
        now = time.monotonic()
        fx = self.pet.fx
        self.parts.prepare(s, w.devicePixelRatioF())
        p.translate(self._ox, self._oy)
        p.scale(s, s)
        k = self.trans
        if k < 1.0:
            p.save()
            p.setOpacity(1.0 - k)
            # 两种画法同一个平移量 SEAT_DROP×(1-k)：k=0 时坐姿立绘的台沿正好落在落点上，
            # 过程中两者的上半身始终逐像素重合，只有下半身在交替
            p.translate(0, SEAT_DROP * (1.0 - k))
            self._paint_seated(p, now)
            p.restore()
        if k > 0.0:
            p.save()
            p.setOpacity(k)
            p.translate(0, SEAT_DROP * (1.0 - k))
            pose = self._pose(now)
            pose.eyes_closed = fx.eyes_closed() or self.pet.blinking(now)
            R.draw(p, self.parts, pose, self.facing if k >= 1.0 else -1)
            if self.facing < 0 or k < 1.0:
                fx.paint_overlay(p, 1.0)
            else:
                p.save()
                p.translate(2 * R.CX, 0)
                p.scale(-1, 1)
                fx.paint_overlay(p, 1.0)
                p.restore()
            p.restore()

    def _pose(self, now):
        st = self.state
        if st == "walk":
            return R.walk_pose(self.walk_phase)
        if st == "drag":
            return R.dangle_pose(self.swing, now)
        if st in ("fall",):
            return R.air_pose(self.vy)
        if st == "jump":
            if now < getattr(self, "_crouch_until", 0):
                return R.crouch_pose(0.8)
            return R.air_pose(self.vy)
        if st in ("standup", "sitdown"):
            return R.stand_pose(now)  # 上半身必须和立绘重合，不能下蹲
        pose = R.stand_pose(now)
        land = now - getattr(self, "_land_at", -9)
        if land < 0.22:  # 落地缓冲
            k = math.sin(land / 0.22 * math.pi)
            pose = R.crouch_pose(0.45 * k)
        if st == "stretch":
            e = self._elapsed() / max(0.1, self.dur)
            pose.squash = 1.0 + 0.045 * math.sin(min(1.0, e) * math.pi)
            pose.bob = -12 * math.sin(min(1.0, e) * math.pi)
        pose.tail_angle *= self.pet.fx.tail_speed
        return pose

    def _paint_seated(self, p, now):
        """坐姿：直接复用 PetWindow 已经按屏幕缩放好的立绘分层（尾巴/身体/眨眼/晃脚帧）。"""
        pet = self.pet
        s = self._scale()
        p.save()
        p.scale(1 / s, 1 / s)  # 下面按逻辑像素画（pixmap 自带 dpr）
        # 尾巴
        if pet.char.animated and getattr(pet, "_tail_rel", None) is not None:
            tw = pet._tail_win
            p.save()
            ox, oy = pet._tail_rel[0], pet._tail_rel[1] - pet_top_pad()
            px, py = tw._pivot
            p.translate(ox + px, oy + py)
            p.rotate(tw._angle)
            p.translate(-px, -py)
            p.drawPixmap(QPointF(*tw._draw_off), tw._pix)
            p.restore()
        sit_h = round(pet._disp_h * pet.char.sit_ratio)
        body = pet.pix_body if pet.char.animated else pet.pix_full
        dpr = body.devicePixelRatio()
        # 上半身（到臀线），带呼吸
        p.save()
        squash = 1.0 + pet._breath_amp * math.sin(pet._breath_phase)
        p.translate(0, sit_h - sit_h * squash)
        p.scale(1.0, squash)
        p.drawPixmap(QRectF(0, 0, body.width() / dpr, sit_h),
                     body, QRectF(0, 0, body.width(), sit_h * dpr))
        if pet.pix_blink is not None and (pet.blinking(now) or pet.fx.eyes_closed()):
            b = pet.pix_blink
            p.drawPixmap(QRectF(0, 0, b.width() / dpr, sit_h), b, QRectF(0, 0, b.width(), sit_h * dpr))
        pet.fx.paint_overlay(p, s)
        p.restore()
        # 腿（含晃脚帧）
        legs = pet._legs
        if legs._pix is not None:
            p.save()
            p.translate(-pet._legs_pad, sit_h)
            p.drawPixmap(0, 0, legs._pix)
            if legs._patches:
                p.drawPixmap(QPointF(*legs._patch_pos), legs._patches[legs._frame])
            p.restore()
        p.restore()


def pet_top_pad():
    from .pet_window import TOP_PAD
    return TOP_PAD
