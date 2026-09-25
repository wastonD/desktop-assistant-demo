# -*- coding: utf-8 -*-
"""PetWindowV2：整帧 + 单窗口原子提交的宠物。

对外接口与 v1 PetWindow 相同（main.py / hub.py / bubble.py / chat_panel.py 无感）：
  show、cleanup、head_point、show_bubble、_toggle_chat、prebuild_hub、fill_clean_menu、set_taskbar_mode、
  _relayout_for_taskbar、_open_settings、devicePixelRatioF、busy、blinking；属性 cfg、fx、bubble、board、drawer、taskbar。
小情绪沿用 PetFx（睡觉/撸头/提醒/思考/问候/碎碎念）；腮红和闭眼补丁画进同一帧、同一次提交。
"""
import math
import os
import random
import threading
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QMenu

from .. import pet_window as v1
from .. import theme
from ..bubble import Bubble
from ..pet_fx import PetFx
from . import anim, backend as backend_mod, bake, frames, layout, zorder
from .brain import Brain, World

ROOT = Path(__file__).resolve().parents[2]
CHAR_DIR = ROOT / "assets" / "character"
ANIMS_DIR = CHAR_DIR / "anims"
MANIFEST_PATH = ANIMS_DIR / "manifest.json"
FRAME_BUDGET = 40 * 1024 * 1024
HIT_ALPHA = 24


class _Perf:
    """ASSISTANT_PET_PERF=1 时每 10 秒打印：进程 CPU%、每秒提交次数、提交耗时。"""

    def __init__(self):
        self.on = os.environ.get("ASSISTANT_PET_PERF") == "1"
        self.t0 = time.monotonic()
        self.c0 = time.process_time()
        self.n = 0
        self.total = 0.0
        self.worst = 0.0

    def add(self, sec):
        if not self.on:
            return
        self.n += 1
        self.total += sec
        self.worst = max(self.worst, sec)
        now = time.monotonic()
        if now - self.t0 >= 10:
            cpu = (time.process_time() - self.c0) / (now - self.t0) * 100
            print(f"[pet2] CPU {cpu:.1f}%  提交 {self.n / (now - self.t0):.1f}/s  "
                  f"平均 {self.total / max(1, self.n) * 1000:.2f}ms  最慢 {self.worst * 1000:.2f}ms")
            self.t0, self.c0, self.n, self.total, self.worst = now, time.process_time(), 0, 0.0, 0.0


class PetWindowV2(QObject):
    _loaded = Signal(object)      # 后台线程读好的帧 → 主线程入缓存
    _baked = Signal(object)       # 后台烘焙完成

    # 与 v1 共用的菜单/设置逻辑（这些方法只用到 cfg、taskbar、drawer、fx 等属性）
    fill_clean_menu = v1.PetWindow.fill_clean_menu
    _add_clean_menu = v1.PetWindow._add_clean_menu
    set_taskbar_mode = v1.PetWindow.set_taskbar_mode
    clean_everything = v1.PetWindow.clean_everything
    _open_settings = v1.PetWindow._open_settings
    _refresh_wallpaper = v1.PetWindow._refresh_wallpaper
    _toggle_board = v1.PetWindow._toggle_board
    prebuild_hub = v1.PetWindow.prebuild_hub

    def __init__(self, backend=None, manifest_path: Path = MANIFEST_PATH, auto_bake: bool = True):
        super().__init__()
        self.cfg = v1.load_config()
        self._manifest_path = manifest_path
        self.m = self._load_manifest()
        self.win = backend or backend_mod.create_backend()
        self.win.set_handler(self._on_mouse)
        self.cache = frames.FrameCache(FRAME_BUDGET)
        self.cache.pinned.add("home_idle")
        self.player = anim.Player(self.m)
        if "home_idle" in self.m.clips:
            self.player.play("home_idle")
        # 帧间隔用 perf_counter：Windows 上 time.monotonic() 是 GetTickCount64，精度 15.6ms，
        # 拿它算 dt 帧时间会忽长忽短（抽屉动画曾因此卡顿）；状态逻辑的 now 仍用 monotonic
        self._t_last = time.perf_counter()
        self._menu_open = False
        self._drag_offset = None          # v1 兼容：busy() 里用
        self._chat = None
        self._press_hit = False
        self._hover_inside = False
        self._blink_until = 0.0
        self._next_blink = time.monotonic() + random.uniform(3.0, 7.0)
        self._last_key = None
        self._origin = (0, 0)
        self._frame_size = (1, 1)
        self._boot = None                 # 烘焙完成前临时显示的静态整帧
        self._token = object()
        self._perf = _Perf()
        self._visible = False
        self._fs_hidden = False           # 全屏应用在前台时藏起来（见 zorder.taskbar_dropped）
        self._stage = None
        self.bubble = Bubble(self)
        self.fx = PetFx(self, self.cfg)
        self._loaded.connect(self._on_loaded)
        self._baked.connect(self._on_baked)

        now = time.monotonic()
        clips = [c for c in self.m.clips if not self.m.missing_files([c], overlays=False)]
        self.brain = Brain(clips, 0.0, 0.0, now, activity=self._activity())
        self.brain.clip_lengths = {n: c.duration for n, c in self.m.clips.items()}
        self._layout(first=True)
        self._make_boot_frame()           # 整帧读完之前（约 1 秒）先显示静态立绘，读完无缝换成动画
        if "home_idle" not in clips and auto_bake:
            self._start_bake()
        self._load_clip("home_idle")

        # 被任务栏压住要在同一个显示帧内抬回来：事件一到就查（不等 15fps 的 tick），30ms 内最多抬一次
        self._z = zorder.Coalescer(window=0.0, min_gap=0.03)
        self._z_timer = QTimer(self)
        self._z_timer.setSingleShot(True)
        self._z_timer.setTimerType(Qt.PreciseTimer)
        self._z_timer.timeout.connect(self._guard_z)
        self._hooks = zorder.WinEventHooks(self._on_z_event)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start(layout.fps_interval_ms(self._fps()))

    # ------------------------------------------------------------ 配置 / 清单
    def _activity(self):
        try:
            return int(self.cfg.get("activity", 1))
        except (TypeError, ValueError):
            return 1

    def _load_manifest(self):
        try:
            return anim.load_manifest(self._manifest_path)
        except anim.ManifestError as e:
            print("宠物 v2 清单读取失败，用空清单：", e)
            return anim.Manifest(self._manifest_path.parent, (773, 1121), {})

    def devicePixelRatioF(self):
        scr = QApplication.primaryScreen()
        return scr.devicePixelRatio() if scr else 1.0

    def _fps(self):
        c = self.player.clip or self.m.clips.get("home_idle")
        return c.fps * max(0.5, self.player.speed) if c else 15.0

    # ------------------------------------------------------------ 几何
    def _taskbar_geometry(self):
        return v1.PetWindow._taskbar_geometry(self)

    def _layout(self, first=False):
        taskbar_h, taskbar_top, geo = self._taskbar_geometry()
        self._dpr = self.devicePixelRatioF()
        char_meta = self.m.clips.get("home_idle")
        src_h = 1113
        sit_ratio = 0.642
        try:
            import json
            meta = json.loads((CHAR_DIR / "character.json").read_text(encoding="utf-8"))
            src_h = meta.get("source_size", [705, 1113])[1]
            sit_ratio = float(meta.get("sit_ratio", sit_ratio))
        except (OSError, ValueError):
            pass
        s_log = layout.logical_scale(taskbar_h, sit_ratio, float(self.cfg.get("scale", 1.8)), src_h)
        new_scale = s_log * self._dpr
        changed = abs(new_scale - getattr(self, "_scale", 0.0)) > 1e-9
        self._s_log, self._scale = s_log, new_scale
        self._taskbar_top = taskbar_top
        self._screen = geo
        canvas = char_meta.size if (char_meta and char_meta.size) else self.m.source_size
        self._canvas = tuple(canvas)
        self._frame_size = frames.scale_size(self._canvas, self._scale)
        x = self.cfg.get("x")
        if x is None:
            x = geo.right() - 705 * s_log - int(self.cfg.get("margin_right", 120))
        if first or not self.brain.away:
            self.brain.x, self.brain.y = float(x), float(taskbar_top)
        self.brain.set_world(World(float(taskbar_top), geo.left(), geo.right()))
        if changed and not first:
            self._token = object()
            self.cache.clear()
            self._last_key = None
            self._load_clip("home_idle")
        return changed

    def _anchor_origin(self, clip_name):
        c = self.m.clips.get(clip_name)
        anchor = c.anchor if c else (bake.PAD_L, 0.642 * 1113 + bake.PAD_T)
        return layout.window_origin((self.brain.x, self.brain.y), anchor, self._scale, self._dpr)

    def head_point(self):
        pt = self.m.point("head_top", (335 + bake.PAD_L, 40 + bake.PAD_T))
        x, y = layout.canvas_to_world(pt, self._origin, self._scale, self._dpr)
        return QPointF(x, y)

    def _place_emote(self):
        em = self.fx.emote
        pt = self.m.point("emote_anchor", (440 + bake.PAD_L, 70 + bake.PAD_T))
        x, y = layout.canvas_to_world(pt, self._origin, self._scale, self._dpr)
        em.move(round(x - em.anchor.x()), round(y - em.anchor.y()))

    # ------------------------------------------------------------ 帧加载（后台线程）
    def _make_boot_frame(self):
        """还没烘焙时：先用 pet.png 放到同一张画布上显示（静态），烘焙完无缝换成动画。"""
        try:
            with Image.open(CHAR_DIR / "pet.png") as im:
                im = im.convert("RGBA")
                canvas = Image.new("RGBA", self._canvas, (0, 0, 0, 0))
                canvas.paste(im, (bake.PAD_L, bake.PAD_T))
            self._boot = frames.frame_from_image(canvas, self._frame_size)
        except OSError as e:
            print("读不到 pet.png：", e)

    def _load_clip(self, name):
        clip = self.m.clips.get(name)
        if clip is None or self.cache.get(name) is not None:
            return
        if self.m.missing_files([name], overlays=False):
            return
        token, size, canvas, root = self._token, self._frame_size, self._canvas, self.m.root

        def run():
            try:
                fs = [frames.load_frame(clip.frame_path(root, i), size) for i in range(clip.count)]
                ovs = {}
                for oname, ov in clip.overlays.items():
                    x0, y0 = max(0, ov.box[0] - ov.margin), max(0, ov.box[1] - ov.margin)
                    lst = []
                    for i in range(clip.count):
                        p = root / ov.frames.format(i)
                        if not p.exists():
                            lst = []
                            break
                        with Image.open(p) as im:
                            im.load()
                            lst.append(frames.scale_patch(im, (x0, y0), canvas, size, ov.box))
                    if lst:
                        ovs[oname] = lst
            except Exception as e:
                print(f"宠物 v2 读帧失败（{name}）：", e)
                return
            self._loaded.emit((token, name, fs, ovs))
        threading.Thread(target=run, daemon=True, name=f"pet2-load-{name}").start()

    def _on_loaded(self, payload):
        token, name, fs, ovs = payload
        if token is not self._token:
            return                      # 期间换了体型，作废
        self.cache.put(name, fs)
        for oname, lst in ovs.items():
            self.cache.put(f"{name}:{oname}", lst)
            self.cache.pinned.add(f"{name}:{oname}")
        self._last_key = None
        if name == "home_idle":
            self._boot = None

    def _start_bake(self):
        print("宠物 v2：第一次运行，正在后台烘焙在家动画帧…")

        def run():
            try:
                clip = bake.bake_home(CHAR_DIR, ANIMS_DIR / "baked")
            except Exception as e:
                print("宠物 v2 烘焙失败，保持静态：", e)
                return
            self._baked.emit(clip)
        threading.Thread(target=run, daemon=True, name="pet2-bake").start()

    def _on_baked(self, clip_json):
        self.m = self._load_manifest()
        self.player = anim.Player(self.m)
        if "home_idle" in self.m.clips:
            self.player.play("home_idle")
        self.brain.clips.add("home_idle")
        self._load_clip("home_idle")

    # ------------------------------------------------------------ 每帧
    def show(self):
        self._render(force=True)
        self.win.show()
        self._visible = True
        self.fx.emote.show()
        QTimer.singleShot(0, self._guard_z)

    def _tick(self):
        now = time.monotonic()
        pc = time.perf_counter()
        dt = min(0.2, pc - self._t_last)
        self._t_last = pc
        fx = self.fx
        fx.tick(now)
        was_sleeping = self.brain.sleeping
        self.brain.set_sleeping(fx.sleeping)
        if was_sleeping and not fx.sleeping:
            self.brain.woke(now)
        self.brain.set_busy(self.busy())
        self.brain.happy_until = max(self.brain.happy_until, fx._tail_boost_until)  # 被摸/提醒：播快一点
        self.brain.activity = self._activity()
        self.brain.tick(now, dt)
        self._handle_requests()
        if now >= self._next_blink:
            self._blink_until = now + 0.13
            self._next_blink = now + random.uniform(3.0, 7.0)
        name, speed = self.brain.clip(now)
        if name not in self.m.clips:
            name, speed = "home_idle", speed
        if name in self.m.clips:
            cur = self.player.clip.name if self.player.clip else None
            if cur != name:
                self.player.play(name, speed, blend=0.0)
                self._load_clip(name)
            else:
                self.player.speed = speed
            self.player.advance(dt)
        self._render()
        if self._z.due(now):
            self._guard_z()
        # 头顶表情（爱心/Z/！）的粒子按每次 33ms 推进（pet_fx.EmoteWindow.tick），有表情时按 30fps 跑
        want = 33 if fx.emote.active() else layout.fps_interval_ms(self._fps())
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _handle_requests(self):
        for req in self.brain.take_requests():
            kind = req[0]
            if kind == "bubble":
                self.show_bubble(req[1], 2200)
            elif kind == "home_x":
                self.cfg["x"] = round(req[1])
                v1.save_config(self.cfg)
            elif kind == "click":
                self._toggle_chat()

    def blinking(self, now=None) -> bool:
        return (now or time.monotonic()) < self._blink_until

    def _current(self):
        """(基础帧, 闭眼补丁或 None, 片段名)。"""
        ref = self.player.frame()
        if ref is not None:
            fs = self.cache.get(ref.clip)
            if fs is not None:
                eyes = None
                if self.brain.eyes_closed() or self.fx.eyes_closed() or self.blinking():
                    ov = self.cache.get(f"{ref.clip}:eyes_closed")
                    if ov:
                        eyes = ov[ref.index % len(ov)]
                return fs[ref.index % len(fs)], eyes, ref.clip, ref.key()
        if self._boot is not None:
            return self._boot, None, "home_idle", ("boot",)
        return None, None, None, None

    def _render(self, force=False):
        base, eyes, clip, key = self._current()
        if base is None:
            return
        origin = self._anchor_origin(clip)
        blush = round(self.fx.blush, 2)
        full_key = (key, eyes is not None, blush, origin, base.w, base.h)
        if not force and full_key == self._last_key:
            return
        t0 = time.perf_counter()
        pos_only = (self._last_key is not None and full_key[:3] == self._last_key[:3]
                    and full_key[4:] == self._last_key[4:])
        h = base.h
        if not self.cfg.get("over_taskbar", True):   # 可选：不伸进任务栏
            h = layout.crop_rows_for_taskbar(base.h, origin[1], round(self._taskbar_top * self._dpr))
            pos_only = False
        if pos_only:
            self.win.move(*origin)
        elif h > 0:
            data = self._compose(base, eyes, blush) if (eyes is not None or blush > 0.01) else base.data
            self.win.commit(data, base.w, h, origin[0], origin[1])
        self._origin = origin
        self._last_key = full_key
        self._perf.add(time.perf_counter() - t0)
        self._place_emote()
        if self.bubble.isVisible() and self.brain.away:
            self.bubble._place()

    def _compose(self, base, eyes, blush):
        """需要叠加时：在舞台图上 基础帧 → 闭眼补丁 → 腮红，一次画完（仍是同一次提交）。"""
        st = self._stage
        if st is None or (st.width(), st.height()) != (base.w, base.h):
            st = self._stage = QImage(base.w, base.h, QImage.Format_ARGB32_Premultiplied)
        src = QImage(base.data, base.w, base.h, base.w * 4, QImage.Format_ARGB32_Premultiplied)
        p = QPainter(st)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.drawImage(0, 0, src)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if eyes is not None:
            patch = QImage(eyes.data, eyes.w, eyes.h, eyes.w * 4, QImage.Format_ARGB32_Premultiplied)
            p.setCompositionMode(QPainter.CompositionMode_Source)   # 补丁是整块替换（含同一帧的周边像素）
            p.drawImage(eyes.ox, eyes.oy, patch)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if blush > 0.01:
            p.setRenderHint(QPainter.Antialiasing)
            p.translate(bake.PAD_L * self._scale, bake.PAD_T * self._scale)
            self.fx.paint_overlay(p, self._scale)
        p.end()
        return st.bits()

    # ------------------------------------------------------------ 置顶
    def _on_z_event(self):
        now = time.monotonic()
        self._z.mark(now)
        if self._z.due(now):
            self._guard_z()
        elif not self._z_timer.isActive():
            self._z_timer.start(max(1, round(self._z.next_check_in(now) * 1000)))

    def _guard_z(self):
        now = time.monotonic()
        self._z.ran(now)
        if self._menu_open or not self._visible or not self.win.hwnd:
            return
        fs = zorder.taskbar_dropped()
        if fs != self._fs_hidden:                   # 全屏应用进出：跟着任务栏一起让开/回来
            self._fs_hidden = fs
            if fs:
                self.win.hide()
                self.fx.emote.hide()
            else:
                self._render(force=True)
                self.win.show()
                self.fx.emote.show()
        if fs:
            return
        x, y = self._origin
        rect = (x, y, x + self._frame_size[0], y + self._frame_size[1])
        if zorder.needs_raise(rect, zorder.windows_above(self.win.hwnd), os.getpid()):
            zorder.raise_top(self.win.hwnd)
            zorder.raise_top(int(self.fx.emote.winId()))

    # ------------------------------------------------------------ 鼠标
    def _to_world(self, x, y):
        return x / self._dpr, y / self._dpr

    def _hit(self, x, y):
        base = self._current()[0]
        return base is not None and base.hit(x - self._origin[0], y - self._origin[1], HIT_ALPHA)

    def _on_mouse(self, kind, x, y):
        now = time.monotonic()
        if kind == "display":
            QTimer.singleShot(300, self._relayout_for_taskbar)
            return
        if kind == "leave":
            self._hover_inside = False
            return
        if kind == "menu":
            if self._hit(x, y):
                QTimer.singleShot(0, lambda: self._show_menu(QCursor.pos()))
            return
        wx, wy = self._to_world(x, y)
        if kind == "down":
            self._press_hit = self._hit(x, y)
            if self._press_hit:
                self.brain.press(wx, wy, now)
                self._drag_offset = (wx, wy)
            return
        if kind in ("up", "capture_lost"):
            if self._press_hit:
                self._press_hit = False
                self._drag_offset = None
                self.brain.release(wx, wy, now)
                self._handle_requests()
                self._render()
            return
        if kind == "move":
            if self._press_hit:
                if self.brain.move(wx, wy, now):
                    self._render()          # 只动了位置：ULW 只传 pptDst，不重画
                return
            if not self._hover_inside:
                self._hover_inside = True
                self.fx.wake()
            if self._hit(x, y):
                sx = (x - self._origin[0]) / self._scale - bake.PAD_L
                sy = (y - self._origin[1]) / self._scale - bake.PAD_T
                self.fx.on_hover(sx, sy)

    # ------------------------------------------------------------ 面板 / 菜单
    def busy(self) -> bool:
        chat = self._chat
        return bool(self._menu_open or self._drag_offset is not None
                    or (chat is not None and chat.isVisible()))

    def _toggle_chat(self):
        from ..hub import Hub
        if self._chat is None:
            self._chat = Hub(self)
        opening = not self._chat.isVisible()
        self._chat.toggle()
        if opening:
            self.fx.emote.spawn("note", 1)
            self.fx.wake(quiet=True)

    def show_bubble(self, text, duration_ms=12000, title="", actions=None, accent=None):
        self.bubble.show_text(text, duration_ms, title=title, actions=actions, accent=accent)

    def _show_menu(self, pos):
        menu = QMenu()
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
        menu.addAction("摸摸头", self._pet_head)
        menu.addSeparator()
        size_menu = menu.addMenu("体型")
        cur = float(self.cfg.get("scale", 1.8))
        for label, scale in (("迷你", 1.0), ("小", 1.4), ("中", 1.8), ("大", 2.4)):
            act = size_menu.addAction(("● " if abs(cur - scale) < 0.01 else "　 ") + label)
            act.triggered.connect(lambda _=False, s=scale: self._set_scale(s))
        if self.brain.can_roam:
            if self.brain.away:
                menu.addAction("回家坐着", lambda: self.brain.go_home(time.monotonic()))
            else:
                menu.addAction("出去走走", lambda: self.brain.leave_home(time.monotonic()))
        else:
            na = menu.addAction("出去走走（v2 离家动作素材还没做）")
            na.setEnabled(False)
        self._add_clean_menu(menu)
        style = getattr(self, "taskbar_style", None)
        if style is not None:
            from .. import taskbar_style
            taskbar_style.fill_menu(menu.addMenu("任务栏外观"), style,
                                    notify=lambda t: self.show_bubble(t, 5000),
                                    save=lambda: v1.save_config(self.cfg))
        mood = menu.addMenu("习性")
        act_menu = mood.addMenu("活泼度")
        for lv, label in ((0, "安静：一直坐在任务栏"), (1, "普通：偶尔出去溜达"), (2, "活泼：经常到处跑")):
            a_ = act_menu.addAction(label)
            a_.setCheckable(True)
            a_.setChecked(self._activity() == lv)
            a_.triggered.connect(lambda _=False, v=lv: self._set_cfg("activity", v))
        ch = mood.addAction("偶尔说说话")
        ch.setCheckable(True)
        ch.setChecked(bool(self.cfg.get("chatter", True)))
        ch.toggled.connect(lambda on: self._set_cfg("chatter", on))
        sl = mood.addAction("没人理就睡觉")
        sl.setCheckable(True)
        sl.setChecked(float(self.cfg.get("sleep_idle_min", 10)) > 0)
        sl.toggled.connect(lambda on: self._set_cfg("sleep_idle_min", 10 if on else 0))
        ot = mood.addAction("脚垂在任务栏前（关掉=不与任务栏重叠）")
        ot.setCheckable(True)
        ot.setChecked(bool(self.cfg.get("over_taskbar", True)))
        ot.toggled.connect(lambda on: self._set_cfg("over_taskbar", on))
        eng = mood.addAction("用旧版宠物（v1，重启生效）")
        eng.triggered.connect(lambda: self._set_cfg("engine", "v1"))
        wp = menu.addMenu("壁纸看板")
        wp.addAction("刷新壁纸", self._refresh_wallpaper)
        wp.addAction("壁纸设置…", self._open_settings)
        menu.addSeparator()
        menu.addAction("退出", QApplication.instance().quit)
        theme.style_menu(menu)
        self._menu_open = True
        try:
            menu.exec(pos)
        finally:
            self._menu_open = False

    def _pet_head(self):
        self.fx.petted()
        self.brain.petted(time.monotonic())

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        v1.save_config(self.cfg)
        if key == "sleep_idle_min" and not value:
            self.fx.wake(quiet=True)
        self._last_key = None

    def _set_scale(self, scale):
        self.cfg["scale"] = scale
        v1.save_config(self.cfg)
        self._relayout_for_taskbar()

    def _relayout_for_taskbar(self):
        if self._layout() and (self._boot is not None or self.cache.get("home_idle") is None):
            self._make_boot_frame()
        self._last_key = None
        self._render(force=True)

    def cleanup(self):
        self._timer.stop()
        self._hooks.close()
        self.fx.emote.close()
        self.bubble.close()
        self.win.close()
