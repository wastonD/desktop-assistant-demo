# -*- coding: utf-8 -*-
"""站立/行走用的 2D 骨骼模型（"纸偶"）：上半身取自立绘，下半身程序绘制。

为什么这样拆：立绘是坐姿（二郎腿），站起来走路需要能动的腿。
- 上半身（头、长发、抱胸的手臂）= body.png 第 0~TORSO_CUT 行，发梢在切口附近淡出——脸和气质与立绘完全一致；
- 毛衣下摆、百褶格子裙、黑丝长腿、脚 = 程序绘制（颜色取自立绘），腿是两段骨骼 + 解析 IK，
  行走时支撑脚钉在地面上不打滑；
- 尾巴复用 tail.png，挂在裙子后面摆。
所有坐标都是"源图像素"（pet.png 705x1113 的坐标系），外面用 painter.scale 缩放到屏幕。
朝向：立绘本身朝左偏；facing=+1（向右）时整体镜像。
"""
import math
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QTransform)

TORSO_CUT = 494            # 上半身切口（此行以下是交叠的大腿，不要）
HAIR_FADE = 26             # 切口以上多少行里，浅色（头发）像素逐渐淡出
CX = 401.0                 # 身体中线 x（切口处毛衣 290~512）
WAIST = QPointF(CX, 600)   # 上半身摆动的支点（腰）
HIP_Y = 640                # 髋关节高度（毛衣盖住胯，腿从裙下出来）
HIP_X = (CX - 24, CX + 24)         # 前/后腿的髋关节 x
THIGH, SHIN = 300, 290     # 骨长：腿约占身高一半（第一版 214/208 太短，头身比像 Q 版）
LEG_W = 1.22               # 腿粗细系数（按上半身宽度配）
STRIDE = 200.0             # 步长（源图像素）
ANKLE_H = 30               # 踝到地面的高度
GROUND_Y = HIP_Y + THIGH + SHIN - 9 + ANKLE_H   # 自然站立（膝微屈）时地面的 y
TAIL_PIVOT_SRC = (595, 690)  # tail.png 里尾巴根部（坐姿立绘坐标）
TAIL_ROOT = QPointF(CX + 60, 640)  # 站姿时尾巴根部位置（裙子后面）

STOCKING = QColor(52, 47, 47)
STOCKING_HI = QColor(118, 106, 103)
STOCKING_RIM = QColor(24, 21, 22)
STOCKING_BAND = QColor(30, 27, 28)
SKIN = QColor(246, 216, 206)
SKIN_SHADE = QColor(226, 176, 165)
SWEATER = QColor(22, 22, 27)
SWEATER_RIB = QColor(12, 12, 15)
SWEATER_HI = QColor(70, 72, 78)
SKIRT = QColor(62, 62, 68)
SKIRT_DARK = QColor(28, 28, 32)
SKIRT_LIGHT = QColor(205, 207, 211)


@dataclass
class Pose:
    """一帧姿态。脚的位置是 IK 目标（源图坐标，y 向下）。"""
    hip: QPointF = field(default_factory=lambda: QPointF(CX, HIP_Y))
    feet: tuple = ((CX - 36, GROUND_Y - ANKLE_H), (CX + 34, GROUND_Y - ANKLE_H))  # (踝 x, 踝 y) 前脚、后脚
    foot_pitch: tuple = (0.0, 0.0)   # 脚掌俯仰（度，正=脚尖朝下）
    lean: float = 0.0                # 上半身前倾角（度，正=向行进方向）
    bob: float = 0.0                 # 上半身额外上下（源图像素）
    squash: float = 1.0              # 上半身纵向伸缩（着地/伸懒腰）
    skirt_sway: float = 0.0          # 裙摆摆动（-1~1）
    tail_angle: float = 0.0          # 尾巴摆角（度）
    eyes_closed: bool = False
    knee_dir: int = -1               # 膝盖朝向（-1=朝左，即立绘面朝的方向）


class RigParts:
    """从角色目录载入并预处理部件（只做一次）。"""

    def __init__(self, body: QImage | None = None, tail: QImage | None = None,
                 blink: QImage | None = None, char=None):
        """离线出图直接给三张图；程序里给 char（Character），用到时才读图、切图，缩好后丢掉原图省内存。"""
        self._char = char
        self.torso = self._cut_torso(body) if body is not None else None
        self.torso_blink = self._cut_torso(blink) if blink is not None else None
        self.tail = tail
        self._pm_key = None
        self.pm = {}

    def prepare(self, s: float, dpr: float):
        """按屏幕缩放预先缩好位图（每帧从原图缩放 + 格式转换太贵）。
        s = 逻辑像素/源图像素，dpr = 设备像素比。"""
        key = (round(s, 5), round(dpr, 3))
        if key == self._pm_key:
            return
        self._pm_key = key
        k = s * dpr
        char = self._char
        if char is not None:  # 从磁盘读原图、切上半身（只在第一次/改体型时发生）
            self.torso = self._cut_torso(char.body)
            self.torso_blink = self._cut_torso(char.blink) if char.has_blink else None
            self.tail = char.tail

        def mk(img):
            if img is None:
                return None
            sc = img.scaled(max(1, round(img.width() * k)), max(1, round(img.height() * k)),
                            Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            pm = QPixmap.fromImage(sc.convertToFormat(QImage.Format_ARGB32_Premultiplied))
            pm.setDevicePixelRatio(dpr)
            return pm
        self.pm = {"torso": mk(self.torso), "blink": mk(self.torso_blink), "tail": mk(self.tail),
                   "s": s, "dpr": dpr}
        if char is not None:  # 原图和切好的上半身都不再需要（~6MB）
            self.torso = self.torso_blink = self.tail = None
            char.release()

    @staticmethod
    def _cut_torso(img: QImage) -> QImage:
        t = img.copy(0, 0, img.width(), TORSO_CUT).convertToFormat(QImage.Format_ARGB32)
        # 发梢淡出：切口附近的浅色像素（头发）alpha 逐行递减，深色毛衣保持不透明
        for y in range(TORSO_CUT - HAIR_FADE, TORSO_CUT):
            k = (TORSO_CUT - y) / HAIR_FADE  # 1 → 0
            for x in range(t.width()):
                c = t.pixelColor(x, y)
                if c.alpha() == 0:
                    continue
                lum = (c.red() + c.green() + c.blue()) / 3
                if lum > 95:
                    c.setAlpha(round(c.alpha() * k * k))
                    t.setPixelColor(x, y, c)
        return t


# ---------------------------------------------------------------- 几何工具
def _ik(hip: QPointF, ankle: QPointF, l1: float, l2: float, knee_dir: int) -> QPointF:
    """两段骨骼解析 IK：返回膝盖位置。knee_dir 决定膝盖向哪边弯。"""
    dx, dy = ankle.x() - hip.x(), ankle.y() - hip.y()
    d = max(1e-3, math.hypot(dx, dy))
    d = min(d, l1 + l2 - 0.01)
    a = (l1 * l1 - l2 * l2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, l1 * l1 - a * a))
    ux, uy = dx / d, dy / d
    mx, my = hip.x() + ux * a, hip.y() + uy * a
    # 垂直方向：(-uy, ux) 或 (uy, -ux)
    px, py = -uy, ux
    if px * knee_dir < 0:
        px, py = -px, -py
    return QPointF(mx + px * h, my + py * h)


def _norm(ax, ay):
    d = math.hypot(ax, ay) or 1.0
    return ax / d, ay / d


def _limb_outline(pts, widths):
    """沿骨骼折线 pts 按宽度 widths 生成左右轮廓点。"""
    left, right = [], []
    n = len(pts)
    for i, (p, w) in enumerate(zip(pts, widths)):
        if i == 0:
            tx, ty = _norm(pts[1][0] - p[0], pts[1][1] - p[1])
        elif i == n - 1:
            tx, ty = _norm(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1])
        else:
            tx, ty = _norm(pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1])
        nx, ny = -ty, tx
        left.append((p[0] + nx * w / 2, p[1] + ny * w / 2))
        right.append((p[0] - nx * w / 2, p[1] - ny * w / 2))
    return left, right


def _smooth_path(points, closed=True):
    """过点的平滑闭合曲线（中点二次贝塞尔）。"""
    path = QPainterPath()
    n = len(points)
    if n < 3:
        return path
    mid = lambda a, b: QPointF((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    path.moveTo(mid(points[-1], points[0]) if closed else QPointF(*points[0]))
    rng = range(n) if closed else range(1, n - 1)
    for i in rng:
        p = points[i]
        q = points[(i + 1) % n]
        path.quadTo(QPointF(*p), mid(p, q))
    if closed:
        path.closeSubpath()
    return path


def _sample_bone(a: QPointF, b: QPointF, n: int):
    return [(a.x() + (b.x() - a.x()) * t / n, a.y() + (b.y() - a.y()) * t / n) for t in range(n + 1)]


# ---------------------------------------------------------------- 绘制
def _draw_leg(p: QPainter, hip: QPointF, ankle: QPointF, pitch: float, knee_dir: int,
              facing_left: bool, back: bool):
    """一条黑丝腿。为了每帧够快：不做路径布尔运算、不用裁剪——
    先用粗描边画出外轮廓，再无描边填充腿和脚（内部接缝被填充盖住）；皮肤段直接单独算多边形。"""
    knee = _ik(hip, ankle, THIGH, SHIN, knee_dir)
    bone = _sample_bone(hip, knee, 4) + _sample_bone(knee, ankle, 4)[1:]
    widths = [w * LEG_W for w in (76, 71, 63, 54, 46, 51, 45, 35, 26)]
    left, right = _limb_outline(bone, widths)
    leg = _smooth_path(left + right[::-1])

    fdir = -1 if facing_left else 1
    ang = math.radians(pitch) * fdir
    ca, sa = math.cos(ang), math.sin(ang)

    def fp(x, y):  # 脚的局部坐标 → 源图坐标（x 朝前）
        x *= fdir
        return (ankle.x() + x * ca - y * sa, ankle.y() + x * sa + y * ca)
    k = 1.2  # 脚按腿同比放大
    foot = _smooth_path([fp(-15 * k, -12 * k), fp(-17 * k, 8 * k), fp(-12 * k, 24 * k), fp(8 * k, 30 * k),
                         fp(40 * k, 30 * k), fp(66 * k, 27 * k), fp(74 * k, 19 * k), fp(66 * k, 9 * k),
                         fp(40 * k, 3 * k), fp(16 * k, -8 * k), fp(8 * k, -16 * k)])

    base = QColor(STOCKING).darker(135) if back else QColor(STOCKING)
    # 1) 外轮廓：粗描边（一半会被下面的填充盖住，只剩外侧一圈）
    p.setPen(QPen(STOCKING_RIM, 4.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(leg)
    p.drawPath(foot)
    # 2) 填充
    p.setPen(Qt.NoPen)
    p.setBrush(base)
    p.drawPath(leg)
    p.drawPath(foot)

    # 3) 大腿上段是皮肤（绝对领域），袜口在大腿 38% 处
    t_top = 0.34
    top_c = QPointF(hip.x() + (knee.x() - hip.x()) * t_top, hip.y() + (knee.y() - hip.y()) * t_top)
    w0 = widths[0]
    w_top = w0 + (widths[4] - w0) * t_top
    sk_bone = _sample_bone(hip, top_c, 2)
    sl, sr = _limb_outline(sk_bone, [w0 - 1, (w0 + w_top) / 2 - 1, w_top - 1])
    skin = QPainterPath()
    skin.moveTo(*sl[0])
    for q in sl[1:]:
        skin.lineTo(*q)
    for q in sr[::-1]:
        skin.lineTo(*q)
    skin.closeSubpath()
    tx, ty = _norm(knee.x() - hip.x(), knee.y() - hip.y())
    nx, ny = -ty, tx
    g = QLinearGradient(top_c + QPointF(nx * w_top / 2, ny * w_top / 2),
                        top_c - QPointF(nx * w_top / 2, ny * w_top / 2))
    sk, sh = QColor(SKIN), QColor(SKIN_SHADE)
    if back:
        sk, sh = sk.darker(115), sh.darker(115)
    g.setColorAt(0.0, sh)
    g.setColorAt(0.35, sk)
    g.setColorAt(0.7, sk)
    g.setColorAt(1.0, sh)
    p.fillPath(skin, g)
    # 袜口加深的一圈
    p.setPen(QPen(STOCKING_BAND, 9, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(top_c + QPointF(nx * w_top / 2 + tx * 4, ny * w_top / 2 + ty * 4),
               top_c - QPointF(nx * w_top / 2 - tx * 4, ny * w_top / 2 - ty * 4))

    # 4) 丝袜高光：沿骨骼偏向受光侧的柔和亮带（从袜口往下）
    light = 1 if nx < 0 else -1
    hi = QPainterPath()
    pts = [QPointF(x - nx * light * w * 0.18, y - ny * light * w * 0.18)
           for (x, y), w in zip(bone, widths)][2:]
    hi.moveTo(pts[0])
    for q in pts[1:]:
        hi.lineTo(q)
    p.setBrush(Qt.NoBrush)
    for width, alpha in ((16, 40), (5, 46)):  # 两道就够（每道宽描边都很贵）
        c = QColor(STOCKING_HI)
        c.setAlpha(alpha // 2 if back else alpha)
        p.setPen(QPen(c, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(hi)


def _draw_skirt(p: QPainter, sway: float, lean: float):
    cx, top, hem = CX, 574.0, 706.0
    tw, hw = 108.0, 150.0
    n = 12
    shift = sway * 12.0
    top_pts = [(cx - tw + 2 * tw * i / n, top) for i in range(n + 1)]
    hem_pts = [(cx - hw + 2 * hw * i / n + shift, hem + (4 if i % 2 else -3)) for i in range(n + 1)]
    path = QPainterPath()
    path.moveTo(*top_pts[0])
    for q in top_pts[1:]:
        path.lineTo(*q)
    path.lineTo(*hem_pts[-1])
    for i in range(n - 1, -1, -1):  # 褶边：折线，不要扇贝形
        path.lineTo(*hem_pts[i])
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(SKIRT)
    p.drawPath(path)
    p.save()
    p.setClipPath(path)
    # 格子：横线 + 沿褶的竖线
    for y, w, c in ((612, 11, SKIRT_DARK), (632, 2, SKIRT_LIGHT), (664, 13, SKIRT_DARK),
                    (685, 2, SKIRT_LIGHT)):
        col = QColor(c)
        col.setAlpha(150 if c is SKIRT_DARK else 150)
        p.setPen(QPen(col, w))
        p.drawLine(QPointF(cx - 200, y), QPointF(cx + 200, y + shift * 0.2))
    for i in range(n + 1):
        a, b = top_pts[i], hem_pts[i]
        col = QColor(SKIRT_DARK if i % 2 else SKIRT_LIGHT)
        col.setAlpha(170 if i % 2 else 110)
        p.setPen(QPen(col, 6 if i % 2 else 2))
        p.drawLine(QPointF(*a), QPointF(*b))
    # 褶的明暗
    for i in range(n):
        if i % 2:
            continue
        poly = QPainterPath()
        poly.moveTo(*top_pts[i])
        poly.lineTo(*top_pts[i + 1])
        poly.lineTo(*hem_pts[i + 1])
        poly.lineTo(*hem_pts[i])
        poly.closeSubpath()
        p.fillPath(poly, QColor(255, 255, 255, 16))
    p.restore()
    p.setPen(QPen(SKIRT_DARK, 2))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def _draw_sweater_hem(p: QPainter):
    """毛衣下半截：接在上半身切口下（切口处宽 290~512），腰部略收、胯部放松外扩，圆弧下摆。
    罗纹对比度压低、加中间高光和两道斜褶，看起来是软的布料而不是一块黑板。"""
    top = TORSO_CUT - 34
    bottom = 604
    L, R_ = CX - 111, CX + 111
    path = QPainterPath()
    path.moveTo(L, top)
    path.lineTo(R_, top)
    path.cubicTo(R_ + 1, 520, R_ - 12, 548, R_ - 8, 566)       # 右侧收腰
    path.cubicTo(R_ - 4, 580, R_ + 8, 590, R_ + 6, bottom - 6)   # 胯部外扩
    path.quadTo(R_ + 2, bottom + 4, R_ - 14, bottom + 3)
    path.quadTo(CX, bottom + 14, L + 14, bottom + 3)             # 圆弧下摆
    path.quadTo(L - 2, bottom + 4, L - 6, bottom - 6)
    path.cubicTo(L - 8, 590, L + 4, 580, L + 8, 566)
    path.cubicTo(L + 12, 548, L - 1, 520, L, top)
    path.closeSubpath()
    g = QLinearGradient(L, 0, R_, 0)                               # 体积：左亮右暗
    g.setColorAt(0.0, QColor(20, 20, 25))
    g.setColorAt(0.35, QColor(40, 40, 47))
    g.setColorAt(0.6, QColor(30, 30, 36))
    g.setColorAt(1.0, QColor(14, 14, 18))
    p.setPen(Qt.NoPen)
    p.fillPath(path, g)
    p.save()
    p.setClipPath(path)
    rib = QColor(SWEATER_RIB)
    rib.setAlpha(120)
    p.setPen(QPen(rib, 1.8))
    x = L + 3
    while x < R_ + 6:
        p.drawLine(QPointF(x, top), QPointF(x + (x - CX) * 0.05, bottom + 12))
        x += 9
    hi = QColor(SWEATER_HI)
    hi.setAlpha(38)
    p.setPen(QPen(hi, 1.0))
    x = L + 7.5
    while x < CX + 40:
        p.drawLine(QPointF(x, top), QPointF(x + (x - CX) * 0.05, bottom + 12))
        x += 9
    # 两道从手臂下方斜下来的软褶
    for x0, x1, a in ((CX - 70, CX - 20, 55), (CX + 40, CX + 75, 45)):
        fold = QPainterPath()
        fold.moveTo(x0, 500)
        fold.quadTo((x0 + x1) / 2 - 10, 548, x1, 596)
        p.setPen(QPen(QColor(0, 0, 0, a), 7, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawPath(fold)
    # 腰部阴影、下摆罗纹收口（跟着圆弧走）
    p.fillRect(QRectF(L - 12, 556, 250, 12), QColor(0, 0, 0, 30))
    cuff = QPainterPath()
    cuff.moveTo(L - 10, bottom - 18)
    cuff.quadTo(CX, bottom - 6, R_ + 10, bottom - 18)
    p.setPen(QPen(QColor(6, 6, 8, 90), 20, Qt.SolidLine, Qt.FlatCap))
    p.drawPath(cuff)
    p.restore()


def draw(p: QPainter, parts: RigParts, pose: Pose, facing: int = -1):
    """在源图坐标系里画一帧。调用方负责把 painter 平移/缩放到位（原点=源图原点）。
    facing=-1 朝左（立绘原样），+1 朝右（以身体中线镜像）。"""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    if facing > 0:
        p.translate(2 * WAIST.x(), 0)
        p.scale(-1, 1)
    facing_left = True  # 镜像后局部坐标里永远"朝左"
    hip = pose.hip
    dx, dy = hip.x() - CX, hip.y() - HIP_Y

    # 尾巴（最底层）
    if parts.tail is not None or parts.pm.get("tail") is not None:
        p.save()
        root = TAIL_ROOT + QPointF(dx, dy)
        p.translate(root)
        p.rotate(pose.tail_angle + 24)
        p.translate(-TAIL_PIVOT_SRC[0], -TAIL_PIVOT_SRC[1])
        _blit(p, parts, "tail", parts.tail)
        p.restore()

    # 后腿、前腿
    hips = [QPointF(HIP_X[0] + dx, hip.y()), QPointF(HIP_X[1] + dx, hip.y())]
    order = (1, 0)  # 先画后腿
    for i in order:
        ax, ay = pose.feet[i]
        _draw_leg(p, hips[i], QPointF(ax, ay), pose.foot_pitch[i], pose.knee_dir,
                  facing_left, back=(i == 1))

    # 裙子、毛衣下摆、上半身（带摆动）
    p.save()
    p.translate(dx, dy)
    _cached(p, parts, ("skirt", round(pose.skirt_sway * 8)), SKIRT_BOX,
            lambda q: _draw_skirt(q, round(pose.skirt_sway * 8) / 8, 0))
    p.translate(WAIST)
    p.rotate(-pose.lean)
    p.scale(1.0, pose.squash)
    p.translate(-WAIST.x(), -WAIST.y() + pose.bob)
    _cached(p, parts, ("hem",), HEM_BOX, _draw_sweater_hem)
    closed = pose.eyes_closed and (parts.torso_blink is not None or parts.pm.get("blink") is not None)
    _blit(p, parts, "blink" if closed else "torso", parts.torso_blink if closed else parts.torso)
    p.restore()
    p.restore()


SKIRT_BOX = QRectF(CX - 190, 570, 380, 150)   # 裙子可能覆盖的范围（源图坐标）
HEM_BOX = QRectF(CX - 135, TORSO_CUT - 40, 270, 150)


def _cached(p: QPainter, parts: "RigParts", key, box: QRectF, painter_fn):
    """把只随少数参数变化的部件（裙子按摆动量化、毛衣下摆）预渲染成位图，之后每帧只贴图。"""
    s = parts.pm.get("s")
    if s is None:  # 没 prepare（离线出图）：直接画
        painter_fn(p)
        return
    pm = parts.pm.get(key)
    if pm is None:
        dpr = parts.pm["dpr"]
        k = s * dpr
        img = QImage(max(1, round(box.width() * k)), max(1, round(box.height() * k)),
                     QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing)
        q.scale(k, k)
        q.translate(-box.x(), -box.y())
        painter_fn(q)
        q.end()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        parts.pm[key] = pm
    p.save()
    p.translate(box.x(), box.y())
    p.scale(1 / s, 1 / s)
    p.drawPixmap(0, 0, pm)
    p.restore()


def _blit(p: QPainter, parts: "RigParts", key: str, img: QImage):
    """画一个部件：有预缩放位图就用（快），否则退回原图（离线出图时）。"""
    pm = parts.pm.get(key)
    if pm is None:
        if img is not None:
            p.drawImage(0, 0, img)
        return
    t = p.worldTransform()
    if abs(t.m12()) < 1e-9 and abs(t.m21()) < 1e-9 and abs(abs(t.m22()) - parts.pm["s"]) < 1e-6:
        # 快路径：没有旋转/伸缩时直接平移贴图（带变换的平滑贴图很贵）；朝右用预先镜像好的位图
        mirrored = t.m11() < 0
        if mirrored:
            mk = key + "_m"
            pm_m = parts.pm.get(mk)
            if pm_m is None:
                pm_m = QPixmap.fromImage(pm.toImage().mirrored(True, False))
                pm_m.setDevicePixelRatio(pm.devicePixelRatio())
                parts.pm[mk] = pm_m
            pm = pm_m
        dpr = pm.devicePixelRatio()
        x, y = t.dx(), t.dy()
        if mirrored:
            x -= pm.width() / dpr
        p.save()
        p.resetTransform()
        p.drawPixmap(QPointF(round(x * dpr) / dpr, round(y * dpr) / dpr), pm)
        p.restore()
        return
    s = parts.pm["s"]
    p.save()
    p.scale(1 / s, 1 / s)
    p.drawPixmap(0, 0, pm)
    p.restore()


# ---------------------------------------------------------------- 动作
def stand_pose(t: float = 0.0) -> Pose:
    """站立待机：轻微重心转移和呼吸。"""
    sway = math.sin(t * 2 * math.pi / 3.6)
    pose = Pose()
    pose.hip = QPointF(CX + sway * 3, HIP_Y + abs(sway) * 1.5)
    pose.feet = ((CX - 30, GROUND_Y - ANKLE_H), (CX + 30, GROUND_Y - ANKLE_H))
    pose.bob = math.sin(t * 2 * math.pi / 4.2) * 1.5
    pose.skirt_sway = sway * 0.2
    pose.tail_angle = 5 * math.sin(t * 2 * math.pi / 3.5)
    return pose


def walk_pose(phase: float, stride: float = STRIDE) -> Pose:
    """行走（朝左，局部坐标）。phase∈[0,1) 一个完整步态周期（两步）。
    支撑相脚钉在地上相对身体向后滑（身体在前进），摆动相脚抬起向前摆。"""
    pose = Pose()
    feet, pitches = [], []
    for k, off in enumerate((0.0, 0.5)):
        ph = (phase + off) % 1.0
        if ph < 0.6:  # 支撑相：脚从前（-stride/2）滑到后（+stride/2）
            u = ph / 0.6
            fx = -stride / 2 + stride * u
            lift = 0.0
            pitch = -6 * (1 - u) + 10 * u * u   # 脚跟着地 → 脚尖蹬地
        else:         # 摆动相：抬脚向前
            u = (ph - 0.6) / 0.4
            e = 0.5 - 0.5 * math.cos(u * math.pi)
            fx = stride / 2 - stride * e
            lift = math.sin(u * math.pi) * 60
            pitch = 18 * math.sin(u * math.pi) - 6 * e
        base_x = CX + (-16 if k == 0 else 16)
        feet.append((base_x + fx, GROUND_Y - ANKLE_H - lift))
        pitches.append(pitch)
    pose.feet = tuple(feet)
    pose.foot_pitch = tuple(pitches)
    s2 = math.cos(phase * 4 * math.pi)  # 每步一次起伏
    pose.hip = QPointF(CX, HIP_Y - 8 + s2 * 6)
    pose.bob = -s2 * 2.5
    pose.lean = 0.0  # 前倾会让上半身贴图走慢路径（旋转），起伏已经够有走路感
    pose.skirt_sway = math.sin(phase * 4 * math.pi) * 0.8
    pose.tail_angle = 6 * math.sin(phase * 2 * math.pi)
    return pose


def crouch_pose(k: float) -> Pose:
    """下蹲（起跳前/落地后），k∈[0,1]。"""
    pose = stand_pose(0)
    drop = 95 * k
    pose.hip = QPointF(CX, HIP_Y + drop)
    pose.feet = ((CX - 46, GROUND_Y - ANKLE_H), (CX + 44, GROUND_Y - ANKLE_H))
    pose.lean = 6 * k
    pose.squash = 1.0 - 0.03 * k
    return pose


def air_pose(vy: float) -> Pose:
    """空中：上升时收腿，下落时腿伸直准备落地。vy 为竖直速度（向下为正）。"""
    pose = Pose()
    tuck = max(0.0, min(1.0, -vy / 600))
    pose.hip = QPointF(CX, HIP_Y)
    pose.feet = ((CX - 34 - 40 * tuck, GROUND_Y - ANKLE_H - 150 * tuck),
                 (CX + 34 + 14 * tuck, GROUND_Y - ANKLE_H - 95 * tuck))
    pose.foot_pitch = (20 * tuck, 25)
    pose.skirt_sway = max(-1.0, min(1.0, vy / 700))
    pose.tail_angle = -10 * tuck + 8
    return pose


def dangle_pose(swing: float, t: float) -> Pose:
    """被拎起来：腿自然下垂，随拖动惯性摆动。swing 为摆角（度）。"""
    pose = Pose()
    a = math.radians(swing)
    kick = math.sin(t * 7) * 12  # 小脚乱蹬
    L = THIGH + SHIN - 6
    for_x = lambda hx, extra: (hx + math.sin(a) * L + extra, HIP_Y + math.cos(a) * L)
    pose.feet = (for_x(HIP_X[0], -8 + kick), for_x(HIP_X[1], 10 - kick))
    pose.foot_pitch = (35, 40)
    pose.skirt_sway = max(-1.0, min(1.0, swing / 25))
    pose.tail_angle = -swing * 0.6 + 10 * math.sin(t * 3)
    pose.lean = -swing * 0.15
    return pose
