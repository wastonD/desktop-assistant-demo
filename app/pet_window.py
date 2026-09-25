# -*- coding: utf-8 -*-
"""桌面宠物窗口：透明置顶，骑在任务栏上沿，只有人物像素可点击。

三窗口显示：
  主窗口 = 臀线以上（呼吸、眨眼）；TailWindow = 尾巴摆动；LegsWindow = 臀线以下的腿（晃脚见 foot_swing.py）。
角色素材接口见 character.py。
"""
import ctypes
import ctypes.wintypes
import json
import math
import random
import threading
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QImage, QPainter, QPainterPath, QPixmap,
                           QPolygonF, QRegion)
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from . import theme
from .bubble import Bubble
from .character import Character
from .pet_fx import EMOTE_ANCHOR, HEAD_TOP, PetFx

ROOT = Path(__file__).resolve().parents[1]
CHARACTER_DIR = ROOT / "assets" / "character"
CONFIG_PATH = ROOT / "config" / "pet.json"

TOP_PAD = 3  # 呼吸动画向上伸展的余量，避免头顶被窗口边缘裁掉
DEFAULTS = {"scale": 1.8, "margin_right": 120}

HWND_TOP = 0
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def create_pet():
    """按 config/pet.json 的 engine 选实现："v2" = app/pet2（整帧单窗口），缺省 = 本文件的 v1。"""
    from .pet2 import engine_of
    if engine_of(load_config()) == "v2":
        from .pet2.window import PetWindowV2
        return PetWindowV2()
    return PetWindow()


class LegsWindow(QWidget):
    """任务栏以下的腿：独立置顶窗口、完全鼠标穿透，叠在任务栏材质之上。

    历史教训一：做成任务栏子窗口会被 Win11 的 DirectComposition 内容层永远盖住
    （亚克力半透明、时钟文字画在腿上面），传统 HWND z 序对它无效。
    历史教训二：晃脚前四代（切割旋转/重叠分层/条带偏移/纵向伸缩）都有可见瑕疵或不自然。
    现方案（第五代，app/foot_swing.py）：脚掌绕脚踝连续形变，启动时预渲染一个周期的贴片，
    这里只画"挖掉脚掌区的静态腿 + 当前帧贴片"，二者同一物理像素网格、整数对齐。
    """

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._pix = None
        self._patches = []      # 每帧脚掌贴片（QPixmap，已设 dpr）
        self._patch_pos = (0.0, 0.0)  # 逻辑坐标
        self._frame = 0

    def configure(self, pix, patches=None, patch_pos=(0.0, 0.0)):
        self._pix, self._patches, self._patch_pos = pix, patches or [], patch_pos
        self._frame = 0
        self.update()

    def set_frame(self, i):
        if self._patches and i != self._frame:
            self._frame = i % len(self._patches)
            self.update()

    def paintEvent(self, event):
        if self._pix is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pix)
        if self._patches:
            p.drawPixmap(QPointF(*self._patch_pos), self._patches[self._frame])


class TailWindow(QWidget):
    """尾巴的独立穿透窗口：覆盖整个摆动扫过的区域（可跨越臀线），
    压在身体窗口后面，摆到任何角度都不会被主窗口边缘裁切。"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._pix = None
        self._pivot = (0.0, 0.0)
        self._draw_off = (0.0, 0.0)
        self._angle = 0.0

    def configure(self, pix, pivot, draw_off):
        self._pix, self._pivot, self._draw_off = pix, pivot, draw_off
        self.update()

    def set_angle(self, angle):
        if abs(angle - self._angle) >= 0.2:
            self._angle = angle
            self.update()

    def paintEvent(self, event):
        if self._pix is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        px, py = self._pivot
        p.translate(px, py)
        p.rotate(self._angle)
        p.translate(-px, -py)
        p.drawPixmap(QPointF(*self._draw_off), self._pix)


class PetWindow(QWidget):
    _foot_ready = Signal(object)  # 后台线程生成的晃脚帧 → 主线程装配

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.cfg = load_config()
        self.char = Character(CHARACTER_DIR)
        self._drag_offset = None
        self._t0 = time.monotonic()
        self._next_blink = self._t0 + random.uniform(*self.char.blink_interval)
        self._blink_until = 0.0
        self._legs = LegsWindow()
        self._tail_win = TailWindow()
        self._menu_open = False
        self._legs_pad = 0        # 腿部窗口左侧余量（逻辑像素），晃脚帧就绪后才有
        self._foot_cfg = None
        self._foot_token = None
        self._foot_ready.connect(self._on_foot_ready)
        self.bubble = Bubble(self)
        self.fx = PetFx(self, self.cfg)   # 睡觉/害羞/提醒/思考等小情绪（pet_fx.py）
        self._tail_phase = 0.0
        self._breath_phase = 0.0
        self._breath_amp = 0.006
        self._last_frame = self._t0
        self._src_scale = 1.0
        self.setMouseTracking(True)       # 撸头判定需要不按键时的移动事件
        self._layout_on_screen()
        self._tail_win.show()
        self._legs.show()
        self.fx.emote.show()
        from .roam import RoamController
        self.roam = RoamController(self, self.char)  # 离家自由活动（roam.py）
        self._picked = False                          # 在家时被往上拖 = 拎起来
        QTimer.singleShot(0, self._assert_topmost)  # 立即校正三窗口前后顺序

        self._last_assert = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.PreciseTimer)  # 普通精度定时器抖动可达十几毫秒，摆动会一顿一顿
        self._anim_timer.timeout.connect(self._on_anim)
        self._anim_timer.start(33)
        # 任务栏被点击/输入法激活时会把自己抬到最顶层；主窗口已不与任务栏重叠，
        # 这里的压回只为盖住普通窗口，事件 + 轮询兜底即可。
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._assert_topmost)
        self._topmost_timer.start(500)
        proc_type = ctypes.WINFUNCTYPE(
            None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
            ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD)
        self._win_event_proc = proc_type(lambda *args: self._assert_topmost())
        user32 = ctypes.windll.user32
        EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_REORDER = 0x0003, 0x8004
        self._fg_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, self._win_event_proc, 0, 0, 0)
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        tray_pid = ctypes.wintypes.DWORD(0)
        if tray:
            user32.GetWindowThreadProcessId(tray, ctypes.byref(tray_pid))
        self._reorder_hook = user32.SetWinEventHook(
            EVENT_OBJECT_REORDER, EVENT_OBJECT_REORDER, 0, self._win_event_proc,
            tray_pid.value, 0, 0)

    # ---------- 布局 ----------
    def _taskbar_geometry(self):
        """返回 (任务栏高度, 任务栏上沿 y, 屏幕几何)。任务栏不在底部或自动隐藏时按 48px 兜底。"""
        screen = QApplication.primaryScreen()
        geo, avail = screen.geometry(), screen.availableGeometry()
        h = geo.bottom() - avail.bottom()
        if h <= 0:
            h = 48
        return h, geo.bottom() - h + 1, geo

    def _layout_on_screen(self):
        taskbar_h, taskbar_top, geo = self._taskbar_geometry()
        src = self.char.full
        legs_ratio = 1.0 - self.char.sit_ratio
        height = taskbar_h / legs_ratio * float(self.cfg.get("scale", 1.0))
        width = height * src.width() / src.height()
        w, h = round(width), round(height)
        sit = round(h * self.char.sit_ratio)  # 臀线：主窗口到此为止，往下归腿部子窗口
        self._disp_h = h
        self._src_scale = h / src.height()  # 逻辑像素 / 源图像素

        dpr = self.devicePixelRatioF()

        def scale_phys(img):
            pm = QPixmap.fromImage(img.scaled(
                round(w * dpr), round(h * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            pm.setDevicePixelRatio(dpr)
            return pm

        phys_full = src.scaled(round(w * dpr), round(h * dpr),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.pix_full = QPixmap.fromImage(phys_full)
        self.pix_full.setDevicePixelRatio(dpr)
        if self.char.animated:
            self.pix_body = scale_phys(self.char.body)
            self.pix_blink = scale_phys(self.char.blink) if self.char.blink else None
            # 尾巴裁到包围盒，交给独立穿透窗口旋转（见 TailWindow）
            tail_phys = self.char.tail.scaled(
                round(w * dpr), round(h * dpr), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            tail_off = (0.0, 0.0)
            if self.char.tail_bbox:
                sp = tail_phys.height() / src.height()
                bx0, by0, bx1, by1 = self.char.tail_bbox
                rx0 = max(0, math.floor(bx0 * sp) - 2)
                ry0 = max(0, math.floor(by0 * sp) - 2)
                rx1 = min(tail_phys.width(), math.ceil(bx1 * sp) + 2)
                ry1 = min(tail_phys.height(), math.ceil(by1 * sp) + 2)
                tail_phys = tail_phys.copy(rx0, ry0, rx1 - rx0, ry1 - ry0)
                tail_off = (rx0 / dpr, ry0 / dpr)
            self.pix_tail = QPixmap.fromImage(tail_phys)
            self.pix_tail.setDevicePixelRatio(dpr)
            self._tail_off = tail_off
        logical = QPixmap.fromImage(src.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        # 主窗口只到臀线（任务栏上沿），不和任务栏重叠，从机制上避免 z 序闪烁
        self.resize(w, sit + TOP_PAD)
        x = self.cfg.get("x")
        if x is None:
            x = geo.right() - w - int(self.cfg.get("margin_right", 120))
        y = taskbar_top - sit - TOP_PAD
        self.move(int(x), int(y))

        # 鼠标区域：人物不透明像素（尾巴在独立穿透窗口里，不占鼠标区域）
        self.setMask(QRegion(logical.copy(0, 0, w, sit).mask()).translated(0, TOP_PAD))

        # 尾巴窗口：覆盖摆动扫过的全部区域（含摆动越过臀线的部分）
        self._tail_rel = None
        if self.char.animated and self.char.tail_bbox:
            s = h / src.height()
            bx0, by0, bx1, by1 = (v * s for v in self.char.tail_bbox)
            pvx, pvy = self.char.tail_pivot[0] * s, self.char.tail_pivot[1] * s
            amp = math.radians(self.char.tail_amplitude + 1.5)
            xs, ys = [], []
            for ang in (-amp, 0.0, amp):
                ca, sa = math.cos(ang), math.sin(ang)
                for cx, cy in ((bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)):
                    dx, dy = cx - pvx, cy - pvy
                    xs.append(pvx + dx * ca - dy * sa)
                    ys.append(pvy + dx * sa + dy * ca)
            pad = 4
            sx0, sy0 = math.floor(min(xs)) - pad, math.floor(min(ys)) - pad
            sx1, sy1 = math.ceil(max(xs)) + pad, math.ceil(max(ys)) + pad
            self._tail_rel = (sx0, TOP_PAD + sy0)
            self._tail_win.resize(sx1 - sx0, sy1 - sy0)
            self._tail_win.configure(
                self.pix_tail, (pvx - sx0, pvy - sy0),
                (self._tail_off[0] - sx0, self._tail_off[1] - sy0))
        self._sync_side_windows()

        # 臀线以下交给独立的置顶穿透窗口（可延伸到屏幕外，脚被屏幕底边自然裁切）
        sit_phys = round(phys_full.height() * self.char.sit_ratio)
        legs_img = phys_full.copy(0, sit_phys, phys_full.width(),
                                  phys_full.height() - sit_phys).convertToFormat(
            QImage.Format_ARGB32_Premultiplied)
        legs_pix = QPixmap.fromImage(legs_img)
        legs_pix.setDevicePixelRatio(dpr)
        # 先显示静态腿；晃脚帧在后台线程算好后再换上（约 1 秒，不卡出场）
        self._legs_pad = 0
        self._foot_cfg = None
        self._legs.resize(w, h - sit)
        self._legs.configure(legs_pix)
        self._sync_side_windows()
        fc = self.char.meta.get("foot_swing")
        if fc and fc.get("enabled", True):
            self._start_foot_build(legs_img, phys_full.height() / src.height(), dpr, fc,
                                   (w, h - sit))
        self.char.release()  # 显示用位图都生成好了，原图（~12MB）不再常驻

    # ---------- 晃脚帧（后台生成） ----------
    def _start_foot_build(self, legs_img, k, dpr, fc, logical_size):
        from . import foot_swing
        # 左侧余量：脚尖上抬会向左越界。取 4 的整数倍逻辑像素，保证在 125%/150%/175% 缩放下
        # pad×dpr 是整数物理像素，腿部窗口左移后仍与身体窗口逐像素对齐
        pad_logical = 4 * math.ceil(float(fc.get("pad", 40)) * k / dpr / 4)
        pad_phys = round(pad_logical * dpr)
        rgba = legs_img.convertToFormat(QImage.Format_RGBA8888)
        pil = Image.frombuffer("RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()),
                               "raw", "RGBA", rgba.bytesPerLine(), 1).copy()
        sit_src = self.char.size[1] * self.char.sit_ratio
        token = object()
        self._foot_token = token

        def run():
            try:
                result = foot_swing.build(pil, k, sit_src, fc, pad_phys)
            except Exception as e:  # 生成失败就保持静态腿，不影响其它功能
                print("晃脚帧生成失败：", e)
                result = None
            self._foot_ready.emit((token, result, pad_logical, dpr, logical_size, fc))
        threading.Thread(target=run, daemon=True, name="foot-swing").start()

    def _on_foot_ready(self, payload):
        token, result, pad_logical, dpr, (w, legs_h), fc = payload
        if token is not self._foot_token or result is None:
            return  # 期间体型又变了（旧结果作废），或素材里找不到翘腿

        def to_pix(im):
            qimg = QImage(im.tobytes("raw", "RGBA"), im.width, im.height,
                          QImage.Format_RGBA8888).copy()
            pm = QPixmap.fromImage(qimg)
            pm.setDevicePixelRatio(dpr)
            return pm
        patches = [to_pix(p) for p in result.patches]
        pos = (result.patch_pos[0] / dpr, result.patch_pos[1] / dpr)
        self._legs_pad = pad_logical
        self._foot_cfg = fc
        self._legs.resize(w + pad_logical, legs_h)
        self._legs.configure(to_pix(result.static), patches, pos)
        self._sync_side_windows()

    def _sync_side_windows(self):
        _, taskbar_top, _ = self._taskbar_geometry()
        self._legs.move(self.x() - self._legs_pad, taskbar_top)
        if self._tail_rel is not None:
            self._tail_win.move(self.x() + self._tail_rel[0], self.y() + self._tail_rel[1])
        fx = getattr(self, "fx", None)
        roam = getattr(self, "roam", None)
        if fx is not None and not (roam is not None and roam.away):
            s = self._src_scale
            em = fx.emote
            em.move(round(self.x() + EMOTE_ANCHOR[0] * s - em.anchor.x()),
                    round(self.y() + TOP_PAD + EMOTE_ANCHOR[1] * s - em.anchor.y()))

    def _move_group(self, new_x):
        """拖动时一次性移动身体/腿/尾巴/表情四个窗口（DeferWindowPos 原子提交）：
        逐个 move 会有一帧身体到了、腿还没到的撕裂感。"""
        _, taskbar_top, _ = self._taskbar_geometry()
        dpr = self.devicePixelRatioF()
        s = self._src_scale
        em = self.fx.emote
        plan = [(self, new_x, self.y()), (self._legs, new_x - self._legs_pad, taskbar_top)]
        if self._tail_rel is not None:
            plan.append((self._tail_win, new_x + self._tail_rel[0], self.y() + self._tail_rel[1]))
        plan.append((em, round(new_x + EMOTE_ANCHOR[0] * s - em.anchor.x()),
                     round(self.y() + TOP_PAD + EMOTE_ANCHOR[1] * s - em.anchor.y())))
        u = ctypes.windll.user32
        u.BeginDeferWindowPos.restype = ctypes.c_void_p
        u.DeferWindowPos.restype = ctypes.c_void_p
        u.DeferWindowPos.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint]
        u.EndDeferWindowPos.argtypes = [ctypes.c_void_p]
        h = u.BeginDeferWindowPos(len(plan))
        for w, x, y in plan:
            if h:
                h = u.DeferWindowPos(h, int(w.winId()), None, round(x * dpr), round(y * dpr),
                                     0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
        if h:
            u.EndDeferWindowPos(h)
        else:  # 极少数失败时退回逐个移动
            for w, x, y in plan:
                w.move(round(x), round(y))

    def head_point(self):
        """头顶的全局逻辑坐标（气泡尾巴指向这里）。离家时问 RoamController。"""
        roam = getattr(self, "roam", None)
        if roam is not None and roam.away:
            return roam.head_point()
        s = self._src_scale
        return QPointF(self.x() + HEAD_TOP[0] * s, self.y() + TOP_PAD + HEAD_TOP[1] * s)

    # ---------- 置顶 ----------
    def _assert_topmost(self):
        if self._menu_open:  # 弹出菜单期间暂停压顶，否则菜单会被宠物盖住
            return
        # 节流：explorer 的重排事件可能密集触发，频繁 SetWindowPos 会造成动画卡顿
        now = time.monotonic()
        if now - self._last_assert < 0.1:
            return
        self._last_assert = now
        # HWND_TOP：移到自己所在层级（topmost 段）的顶端；已 topmost 时 HWND_TOPMOST 是无操作。
        # 先尾巴再腿再身体：保证尾巴垫底、身体在最上（三窗口重叠处的正确前后关系）
        hwnds = [int(self._tail_win.winId()), int(self._legs.winId()), int(self.winId())]
        roam = getattr(self, "roam", None)
        if roam is not None and roam.win.isVisible():
            hwnds.append(int(roam.win.winId()))
        hwnds.append(int(self.fx.emote.winId()))
        for hwnd in hwnds:
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def cleanup(self):
        self._legs.close()
        self._tail_win.close()
        self.fx.emote.close()
        self.roam.win.close()

    # ---------- 在家 / 离家 ----------
    def set_home_visible(self, on: bool):
        for w in (self, self._tail_win, self._legs):
            w.setVisible(on)
        if on:
            self._assert_topmost()

    def go_home_at(self, x: float):
        """RoamController 在地板上坐下时交还：宠物坐回任务栏的这个位置。"""
        self.move(round(x), self.y())
        self.cfg["x"] = self.x()
        save_config(self.cfg)
        self._sync_side_windows()
        self.set_home_visible(True)

    def blinking(self, now=None) -> bool:
        return (now or time.monotonic()) < self._blink_until

    def busy(self) -> bool:
        """有面板开着、菜单开着或正被拖：宠物别乱跑。"""
        chat = getattr(self, "_chat", None)
        return bool(self._menu_open or self._drag_offset is not None
                    or (chat is not None and chat.isVisible()))

    # ---------- 动画 ----------
    def _on_anim(self):
        now = time.monotonic()
        if self.char.has_blink and now >= self._next_blink:
            self._blink_until = now + self.char.blink_duration
            self._next_blink = now + random.uniform(*self.char.blink_interval)
        self._animate_foot(now)
        self.fx.tick(now)
        dt = min(0.1, now - self._last_frame)
        self._last_frame = now
        if self.char.animated and self._tail_rel is not None:
            # 相位累加（而不是直接 sin(t)）：情绪改变摆速时尾巴不会跳变
            self._tail_phase += dt * 2 * math.pi / self.char.tail_period * self.fx.tail_speed
            self._tail_phase %= 2 * math.pi
            self._tail_win.set_angle(self.char.tail_amplitude * math.sin(self._tail_phase))
        self.roam.tick(now, dt)
        period, amp = self.fx.breath()  # 睡着时更慢更深；相位累加 + 幅度渐变，切换时不跳
        self._breath_phase = (self._breath_phase + dt * 2 * math.pi / period) % (2 * math.pi)
        self._breath_amp += (amp - self._breath_amp) * 0.03
        self.update()

    def _animate_foot(self, now):
        cfg = self._foot_cfg
        if not cfg:
            return
        # 帧本身已按"慢抬-稍停-落下"的节奏生成（foot_swing.ease），这里只按时间取帧
        period = float(cfg.get("period_s", 1.8))
        frames = int(cfg.get("frames", 30))
        phase = ((now - self._t0) / period) % 1.0
        self._legs.set_frame(int(phase * frames))

    def paintEvent(self, event):
        now = time.monotonic()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        # 呼吸：以臀线为锚点做 0.6% 的纵向伸缩，上身微微起伏，腿（子窗口）不动；睡着时更慢更深
        squash = 1.0 + self._breath_amp * math.sin(self._breath_phase)
        h = self.height() - TOP_PAD
        painter.translate(0, TOP_PAD + h - h * squash)
        painter.scale(1.0, squash)

        if not self.char.animated:
            painter.drawPixmap(0, 0, self.pix_full)
            return
        painter.drawPixmap(0, 0, self.pix_body)  # 尾巴在独立窗口（TailWindow）里摆
        if self.pix_blink is not None and (now < self._blink_until or self.fx.eyes_closed()):
            painter.drawPixmap(0, 0, self.pix_blink)
        self.fx.paint_overlay(painter, self._src_scale)

    # ---------- 交互 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self._press_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._picked:
            self.roam.drag_to(event.globalPosition())
            return
        if self._drag_offset is not None:
            gp = event.globalPosition().toPoint()
            if self._press_pos.y() - gp.y() > 36:  # 往上拖 = 把她拎起来（离开任务栏）
                self._picked = True
                self._drag_offset = None
                self.roam.pick_up_from_home(event.globalPosition())
                return
            new_x = (gp - self._drag_offset).x()
            self._move_group(new_x)  # 只允许沿任务栏横向拖动；四个窗口原子移动
        else:
            s = self._src_scale or 1.0
            pos = event.position()
            self.fx.on_hover(pos.x() / s, (pos.y() - TOP_PAD) / s)

    def enterEvent(self, event):
        self.fx.wake()

    def mouseReleaseEvent(self, event):
        if self._picked:
            self._picked = False
            self.roam.release_drag()
            return
        if self._drag_offset is not None:
            self._drag_offset = None
            moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if moved < 5:  # 原地点击 = 打开/收起对话面板
                self._toggle_chat()
            else:
                self.cfg["x"] = self.x()
                save_config(self.cfg)

    def prebuild_hub(self):
        """启动后空闲时先把"日程+对话"面板建好（第一次点宠物就不用等 ~250ms）。"""
        if getattr(self, "_chat", None) is None:
            from .hub import Hub
            self._chat = Hub(self)

    def _toggle_chat(self):
        """点宠物：弹出/收起"日程 + 对话"面板（hub.py）。"""
        from .hub import Hub
        if getattr(self, "_chat", None) is None:
            self._chat = Hub(self)
        opening = not self._chat.isVisible()
        self._chat.toggle()
        if opening:  # 点她一下：先有个小回应，再弹面板
            self.fx.emote.spawn("note", 1)
            self.fx.wake(quiet=True)
            if self.roam.away:
                self.roam._face_cursor()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("日程与聊天　Ctrl+Alt+X", self._toggle_chat)
        drawer = getattr(self, "drawer", None)
        if drawer is not None:
            menu.addAction("桌面收纳　Ctrl+Alt+D", drawer.toggle)
        board = getattr(self, "board", None)
        if board is not None:
            b_act = menu.addAction("桌面常驻日程卡片")
            b_act.setCheckable(True)
            b_act.setChecked(board.wanted)
            b_act.toggled.connect(board.set_wanted)
        menu.addAction("摸摸头", lambda: self.fx.petted())
        menu.addSeparator()
        size_menu = menu.addMenu("体型")
        cur = float(self.cfg.get("scale", 1.8))
        for label, scale in (("迷你", 1.0), ("小", 1.4), ("中", 1.8), ("大", 2.4)):
            act = size_menu.addAction(("● " if abs(cur - scale) < 0.01 else "　 ") + label)
            act.triggered.connect(lambda _=False, s=scale: self._set_scale(s))
        roam = self.roam
        if roam.away:
            menu.addAction("回家坐着", lambda: roam.go_home_now())
        else:
            menu.addAction("出去走走", roam.leave_home)
        self._add_clean_menu(menu)
        mood_menu = menu.addMenu("习性")
        act_menu = mood_menu.addMenu("活泼度")
        cur_act = roam.activity
        for lv, label in ((0, "安静：一直坐在任务栏"), (1, "普通：偶尔出去溜达"), (2, "活泼：经常到处跑")):
            a_ = act_menu.addAction(label)
            a_.setCheckable(True)
            a_.setChecked(cur_act == lv)
            a_.triggered.connect(lambda _=False, v=lv: self._set_cfg("activity", v))
        chat_act = mood_menu.addAction("偶尔说说话")
        chat_act.setCheckable(True)
        chat_act.setChecked(bool(self.cfg.get("chatter", True)))
        chat_act.toggled.connect(lambda on: self._set_cfg("chatter", on))
        sleep_act = mood_menu.addAction("没人理就睡觉")
        sleep_act.setCheckable(True)
        sleep_act.setChecked(float(self.cfg.get("sleep_idle_min", 10)) > 0)
        sleep_act.toggled.connect(lambda on: self._set_cfg("sleep_idle_min", 10 if on else 0))
        wp_menu = menu.addMenu("壁纸看板")
        wp_menu.addAction("刷新壁纸", self._refresh_wallpaper)
        wp_menu.addAction("壁纸设置…", self._open_settings)
        menu.addSeparator()
        menu.addAction("退出", QApplication.instance().quit)
        theme.style_menu(menu)
        self._menu_open = True
        try:
            menu.exec(event.globalPos())
        finally:
            self._menu_open = False

    def _add_clean_menu(self, menu):
        """"桌面干净"：任务栏三档、桌面图标、一键全做。改的是系统外观设置，由用户自己点。"""
        self.fill_clean_menu(menu.addMenu("桌面干净"))

    def fill_clean_menu(self, clean):
        from . import desktop_clean
        clean.clear()
        tb = getattr(self, "taskbar", None)
        if tb is not None:
            for mode, label in (("normal", "任务栏：正常"), ("autohide", "任务栏：自动隐藏"),
                                ("hidden", "任务栏：完全隐藏（Ctrl+Alt+T 临时呼出）")):
                act = clean.addAction(label)
                act.setCheckable(True)
                act.setChecked(tb.mode == mode)
                act.triggered.connect(lambda _=False, m=mode: self.set_taskbar_mode(m))
            clean.addSeparator()
        a1 = clean.addAction("隐藏系统图标（此电脑、回收站…）")
        a1.setCheckable(True)
        a1.setChecked(desktop_clean.system_icons_hidden())
        a1.toggled.connect(desktop_clean.set_system_icons_hidden)
        a2 = clean.addAction("隐藏全部桌面图标")
        a2.setCheckable(True)
        a2.setChecked(desktop_clean.all_icons_hidden())
        a2.toggled.connect(desktop_clean.set_all_icons_hidden)
        clean.addSeparator()
        clean.addAction("一键干净桌面（隐藏桌面图标 + 系统图标 + 任务栏，文件不动）", self.clean_everything)

    def set_taskbar_mode(self, mode):
        tb = getattr(self, "taskbar", None)
        if tb is None:
            return
        tb.apply(mode)
        save_config(self.cfg)
        QTimer.singleShot(400, self._relayout_for_taskbar)

    def _relayout_for_taskbar(self):
        self._layout_on_screen()
        self.roam._layout()

    def clean_everything(self):
        from . import desktop_clean
        desktop_clean.set_system_icons_hidden(True)
        desktop_clean.set_all_icons_hidden(True)   # 只藏图标，文件都留在桌面文件夹里（抽屉从不移动文件）
        self.set_taskbar_mode("hidden")

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        if key == "sleep_idle_min" and not value:
            self.fx.wake(quiet=True)

    def _open_settings(self):
        from .settings_dialog import WallpaperSettings
        if getattr(self, "_settings_dlg", None) is None:
            self._settings_dlg = WallpaperSettings()
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def show_bubble(self, text, duration_ms=12000, title="", actions=None, accent=None):
        self.bubble.show_text(text, duration_ms, title=title, actions=actions, accent=accent)

    def _toggle_board(self):
        board = getattr(self, "board", None)
        if board is not None:
            board.set_wanted(not board.isVisible())

    def _refresh_wallpaper(self):
        from . import wallpaper
        wallpaper.apply_async(force=True)

    def _set_scale(self, scale):
        self.cfg["scale"] = scale
        save_config(self.cfg)
        self._layout_on_screen()
        self.roam._layout()
