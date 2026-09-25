# -*- coding: utf-8 -*-
"""宠物的"小情绪"：睡觉、害羞、提醒、思考，以及头顶飘的小表情。

全部是程序绘制的叠加效果，不改立绘、不动三窗口的切层和接缝：
- 睡觉：用户一段时间没碰键鼠 → 闭眼（沿用眨眼差分层）+ 呼吸变慢 + 尾巴放慢 + 头顶飘 Z；一动鼠标就醒；
- 撸头：鼠标在头上来回蹭几下 → 脸红（脸颊画两团淡粉渐变）+ 冒爱心 + 尾巴摇快 + 一句傲娇台词；
- 提醒：头顶弹出"！"，尾巴摇快；
- 思考：大模型回复期间头顶"…"；
- 问候与碎碎念：启动时按时段问候并报今日概览；之后偶尔（默认 40~80 分钟）说一句。
EmoteWindow 是独立的置顶穿透小窗，贴在头顶，不占鼠标区域。
"""
import ctypes
import math
import random
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from . import theme

# 源图（pet.png 705x1113）里的关键点
HEAD_TOP = (335, 40)          # 头顶（气泡尾巴指向这里）
EMOTE_ANCHOR = (440, 70)      # 表情从这里往上飘（头右上方，猫耳旁）
CHEEKS = ((306, 246), (381, 213))  # 左右脸颊中心
CHEEK_R = (17, 8.5)           # 腮红椭圆半径
CHEEK_TILT = -24              # 脸部倾斜角（度）
HEAD_ZONE_Y = 330             # 源图 y 小于此值算"头部"（撸头判定区）

PET_LINES = ["……别乱摸。", "哼，就、就一下下。", "手很闲吗？", "……再摸要收费了。",
             "喵。……刚才什么都没听到。", "头发会乱的啦。", "……（尾巴出卖了她）"]
WAKE_LINES = ["……嗯？回来了啊。", "才、才没有睡着。", "哈啊——……看什么看。"]
IDLE_LINES = {
    "morning": ["早。……不是特意等你的。", "今天也要好好干活。", "早饭吃了没？"],
    "noon": ["该吃饭了。别让我提醒第二遍。", "中午了，歇一会儿。"],
    "afternoon": ["坐太久了，起来走两步。", "喝点水。……随口说说而已。", "下午容易犯困，撑住。"],
    "evening": ["今天辛苦了……才没有夸你。", "晚饭别凑合。", "今天的事，做完几件了？"],
    "night": ["都几点了，还不睡？", "熬夜会秃的。……我是说你。", "再不睡我先睡了。"],
}
MESSY_LINE = "桌面乱成这样……按 Ctrl+Alt+D，或者拉一下屏幕顶上的把手，我帮你收。"


def _period(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "noon"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def greeting(now: datetime | None = None) -> str:
    now = now or datetime.now()
    head = {"morning": "早。", "noon": "中午好。", "afternoon": "下午好。",
            "evening": "晚上好。", "night": "这么晚还开机？"}[_period(now.hour)]
    try:
        from . import agenda
        return head + agenda.brief(now)
    except Exception:  # 数据库坏了也别影响出场
        return head


def idle_seconds() -> float:
    """距离用户最后一次键鼠输入的秒数（GetLastInputInfo）。"""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError):
        return 0.0


class _Particle:
    __slots__ = ("kind", "x", "y", "vx", "vy", "born", "life", "size", "phase")

    def __init__(self, kind, x, y, vx, vy, life, size):
        self.kind, self.x, self.y, self.vx, self.vy = kind, x, y, vx, vy
        self.born, self.life, self.size = time.monotonic(), life, size
        self.phase = random.uniform(0, math.tau)


class EmoteWindow(QWidget):
    """头顶的小表情层：置顶、完全鼠标穿透。坐标原点在窗口左下角附近的锚点。"""
    W, H = 150, 150

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.W, self.H)
        self.particles: list[_Particle] = []
        self.badge = None          # ("!" | "…" | "?", 出现时刻, 持续秒数；None=常驻)
        self.anchor = QPointF(34, self.H - 22)  # 表情从这里出发

    def active(self):
        return bool(self.particles) or self.badge is not None

    def spawn(self, kind, n=1):
        ax, ay = self.anchor.x(), self.anchor.y()
        for i in range(n):
            if kind == "z":
                self.particles.append(_Particle("z", ax + 4, ay - 6, 14, -26, 3.0, 15 + 3 * i))
            elif kind == "heart":
                self.particles.append(_Particle(
                    "heart", ax + random.uniform(-18, 22), ay + random.uniform(-4, 8),
                    random.uniform(-6, 10), random.uniform(-44, -30), random.uniform(1.4, 1.9),
                    random.uniform(9, 14)))
            elif kind == "note":
                self.particles.append(_Particle("note", ax + random.uniform(-6, 16), ay,
                                                random.uniform(6, 14), -30, 1.8, 15))

    def set_badge(self, text, duration=None):
        self.badge = (text, time.monotonic(), duration) if text else None

    def tick(self):
        now = time.monotonic()
        dt = 0.033
        alive = []
        for p in self.particles:
            if now - p.born < p.life:
                p.x += p.vx * dt + (math.sin((now - p.born) * 5 + p.phase) * 0.35
                                    if p.kind == "heart" else 0)
                p.y += p.vy * dt
                alive.append(p)
        self.particles = alive
        if self.badge and self.badge[2] is not None and now - self.badge[1] > self.badge[2]:
            self.badge = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        for pt in self.particles:
            age = (now - pt.born) / pt.life
            alpha = min(1.0, age * 5) * (1 - max(0.0, (age - 0.55) / 0.45))
            if pt.kind == "z":
                self._draw_text(p, "Z", pt.x, pt.y, pt.size * (0.8 + 0.5 * age), alpha,
                                "#6D6AF2")
            elif pt.kind == "heart":
                self._draw_heart(p, pt.x, pt.y, pt.size * (0.7 + 0.5 * min(1, age * 3)), alpha)
            elif pt.kind == "note":
                self._draw_text(p, "♪", pt.x, pt.y, pt.size, alpha, theme.LAVENDER)
        if self.badge:
            text, born, _dur = self.badge
            age = now - born
            pop = 1.0 + 0.35 * math.exp(-age * 7) * math.sin(age * 22)  # 弹一下
            self._draw_badge(p, text, pop, min(1.0, age * 6), age)

    @staticmethod
    def _draw_text(p, text, x, y, size, alpha, color):
        f = QFont("Segoe UI")
        f.setPixelSize(max(6, round(size)))
        f.setBold(True)
        path = QPainterPath()
        path.addText(QPointF(x, y), f, text)
        p.setPen(QPen(QColor(255, 255, 255, round(230 * alpha)), 3, Qt.SolidLine, Qt.RoundCap,
                      Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor(color, round(255 * alpha)))
        p.drawPath(path)

    @staticmethod
    def _draw_heart(p, x, y, s, alpha):
        path = QPainterPath()
        path.moveTo(x, y + s * 0.35)
        path.cubicTo(x - s * 0.1, y - s * 0.2, x - s * 0.75, y - s * 0.1, x - s * 0.5, y + s * 0.35)
        path.cubicTo(x - s * 0.35, y + s * 0.6, x - s * 0.05, y + s * 0.75, x, y + s * 0.95)
        path.cubicTo(x + s * 0.05, y + s * 0.75, x + s * 0.35, y + s * 0.6, x + s * 0.5, y + s * 0.35)
        path.cubicTo(x + s * 0.75, y - s * 0.1, x + s * 0.1, y - s * 0.2, x, y + s * 0.35)
        p.setPen(QPen(QColor(255, 255, 255, round(220 * alpha)), 2))
        p.setBrush(QColor(255, 122, 150, round(240 * alpha)))
        p.drawPath(path)

    def _draw_badge(self, p, text, scale, alpha, age):
        cx, cy = self.anchor.x() + 10, self.anchor.y() - 30
        r = 15 * scale
        color = {"!": theme.WARM, "?": theme.SKY}.get(text)
        p.setPen(QPen(QColor(255, 255, 255, round(240 * alpha)), 2.5))
        if color is None:  # "…" 思考中：AI 渐变
            g = theme.ai_gradient(cx - r, cy - r, cx + r, cy + r)
            p.setBrush(g)
            p.setOpacity(alpha)
        else:
            p.setBrush(theme.qcolor(color, round(245 * alpha)))
        bubble = QPainterPath()
        bubble.addEllipse(QPointF(cx, cy), r * 1.25, r)
        tail = QPainterPath()
        tail.moveTo(cx - r * 0.55, cy + r * 0.55)
        tail.lineTo(cx - r * 1.15, cy + r * 1.35)
        tail.lineTo(cx - r * 0.05, cy + r * 0.85)
        p.drawPath(bubble.united(tail))
        p.setPen(QColor(255, 255, 255, round(255 * alpha)))
        if text == "…":  # 三个点依次跳
            for i in range(3):
                k = max(0.0, math.sin(age * 6 - i * 0.9))
                p.setBrush(QColor(255, 255, 255, round(255 * alpha)))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(cx - 8 + i * 8, cy - 2.5 * k), 2.3, 2.3)
        else:
            p.setFont(theme.ui_font(round(17 * scale), True))
            p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignCenter, text)


class PetFx:
    """挂在 PetWindow 上的情绪控制器。PetWindow 每帧调 tick()，画身体时调 paint_overlay()。"""

    def __init__(self, pet, cfg: dict):
        self.pet = pet
        self.cfg = cfg
        self.emote = EmoteWindow()
        self.sleeping = False
        self.thinking = False
        self.blush = 0.0            # 0~1，腮红浓度
        self._blush_until = 0.0
        self.tail_speed = 1.0       # 尾巴摆动速度倍率（平滑趋近目标）
        self._tail_boost_until = 0.0
        self._last_z = 0.0
        self._last_idle_check = 0.0
        self._rub = []              # [(时刻, x, 方向)]
        self._last_pet = 0.0
        now = time.monotonic()
        lo, hi = self._chat_interval()
        self._next_chatter = now + random.uniform(lo, hi) * 60

    # ---------- 配置 ----------
    def _chat_interval(self):
        iv = self.cfg.get("chatter_min", [40, 80])
        try:
            return float(iv[0]), float(iv[1])
        except (TypeError, ValueError, IndexError):
            return 40.0, 80.0

    @property
    def sleep_after(self) -> float:
        return float(self.cfg.get("sleep_idle_min", 10)) * 60

    # ---------- 事件 ----------
    def alert(self):
        """提醒弹出时：醒来 + "！" + 摇尾巴。"""
        self.wake(quiet=True)
        self.emote.set_badge("!", 2.2)
        self._tail_boost_until = time.monotonic() + 4

    def set_thinking(self, on: bool):
        self.thinking = on
        if on:
            self.wake(quiet=True)
            self.emote.set_badge("…", None)
        elif self.emote.badge and self.emote.badge[0] == "…":
            self.emote.set_badge(None)

    def happy(self, n=2):
        self.emote.spawn("note", n)
        self._tail_boost_until = time.monotonic() + 3

    def petted(self, say=True):
        now = time.monotonic()
        if now - self._last_pet < 3.5:
            return
        self._last_pet = now
        self.wake(quiet=True)
        self._blush_until = now + 3.5
        self._tail_boost_until = now + 4.5
        self.emote.spawn("heart", 3)
        if say:
            self.pet.show_bubble(random.choice(PET_LINES), 3500)

    def wake(self, quiet=False):
        if not self.sleeping:
            return
        self.sleeping = False
        if not quiet:
            self.emote.set_badge("?", 1.2)
            if random.random() < 0.5:
                self.pet.show_bubble(random.choice(WAKE_LINES), 3000)

    def on_hover(self, src_x: float, src_y: float):
        """鼠标在身体上移动（源图坐标）。在头上来回蹭 = 撸头。"""
        if self.sleeping:
            self.wake()
        if src_y > HEAD_ZONE_Y:
            self._rub.clear()
            return
        now = time.monotonic()
        rub = [r for r in self._rub if now - r[0] < 1.3]
        if rub:
            dx = src_x - rub[-1][1]
            if abs(dx) < 5:
                self._rub = rub
                return
            d = 1 if dx > 0 else -1
            rub.append((now, src_x, d))
        else:
            rub.append((now, src_x, 0))
        turns = sum(1 for a, b in zip(rub, rub[1:]) if a[2] and b[2] and a[2] != b[2])
        self._rub = rub
        if turns >= 4:
            self._rub = []
            self.petted()

    # ---------- 每帧 ----------
    def tick(self, now: float):
        # 睡觉判定每秒一次就够了
        if now - self._last_idle_check > 1.0:
            self._last_idle_check = now
            self._check_sleep(now)
            self._maybe_chatter(now)
        if self.sleeping and now - self._last_z > 1.1:
            self._last_z = now
            self.emote.spawn("z")
        target_blush = 1.0 if now < self._blush_until else 0.0
        self.blush += (target_blush - self.blush) * (0.18 if target_blush else 0.05)
        if abs(self.blush - target_blush) < 0.01:
            self.blush = target_blush
        target_speed = 2.2 if now < self._tail_boost_until else (0.45 if self.sleeping else 1.0)
        self.tail_speed += (target_speed - self.tail_speed) * 0.06
        if self.emote.active():
            self.emote.tick()

    def _check_sleep(self, now):
        after = self.sleep_after
        if after <= 0:
            return
        chat = getattr(self.pet, "_chat", None)
        busy = self.thinking or (chat is not None and chat.isVisible())
        idle = idle_seconds()
        if not self.sleeping and idle >= after and not busy:
            self.sleeping = True
            self._last_z = 0.0
        elif self.sleeping and idle < 2:
            self.wake()

    def _maybe_chatter(self, now):
        if now < self._next_chatter:
            return
        lo, hi = self._chat_interval()
        self._next_chatter = now + random.uniform(lo, hi) * 60
        if not self.cfg.get("chatter", True) or self.sleeping or idle_seconds() > 120:
            return
        if self.pet.bubble.isVisible():
            return
        line = random.choice(IDLE_LINES[_period(datetime.now().hour)])
        try:
            from . import desktop_clean
            from .drawer import core as drawer_core
            if (not desktop_clean.all_icons_hidden() and len(drawer_core.desktop_items()) >= 15
                    and random.random() < 0.35):
                line = MESSY_LINE
        except Exception:
            pass
        self.pet.show_bubble(line, 6000)
        self.emote.spawn("note", 1)

    # ---------- 画在身体上的叠加 ----------
    def eyes_closed(self) -> bool:
        return self.sleeping

    def breath(self):
        """(周期秒, 幅度)：睡着时更慢更深。"""
        return (5.6, 0.009) if self.sleeping else (4.2, 0.006)

    def paint_overlay(self, painter: QPainter, s: float):
        """在身体坐标系（已含呼吸变换）里画腮红。s = 逻辑像素 / 源图像素。"""
        if self.blush <= 0.01:
            return
        painter.save()
        painter.setPen(Qt.NoPen)
        for cx, cy in CHEEKS:
            painter.save()
            painter.translate(cx * s, cy * s)
            painter.rotate(CHEEK_TILT)
            rx, ry = CHEEK_R[0] * s, CHEEK_R[1] * s
            g = QRadialGradient(QPointF(0, 0), rx)
            g.setColorAt(0.0, QColor(255, 110, 130, round(150 * self.blush)))
            g.setColorAt(0.6, QColor(255, 130, 150, round(70 * self.blush)))
            g.setColorAt(1.0, QColor(255, 150, 170, 0))
            painter.setBrush(g)
            painter.scale(1.0, ry / rx)
            painter.drawEllipse(QPointF(0, 0), rx, rx)
            painter.restore()
        painter.restore()
