# -*- coding: utf-8 -*-
"""宠物 v2 的行为状态机（纯逻辑：时钟和随机数都从外面给，便于测试）。

状态只决定三件事：播哪个片段（及速度）、落点在哪（世界逻辑坐标）、朝哪边；
要窗口层做的事放进 requests（冒气泡、保存在家位置、打开面板、弹菜单）。

落点 (x, y)：y = 所站/所坐的那个面（任务栏上沿或窗口顶边）的高度；x = 所有片段共用的画布参考线
（清单里每个片段的 anchor 都是"参考线 x, 地面线 y"，素材打包时对齐）。

片段缺了，对应行为自动关闭：
- 出门：stand_up / stand_idle / walk / sit_down 四个都要有，且活泼度 > 0；
- 跳上窗口：jump（可选 fall、land）；
- 被拎起：dangle（没有就只能沿任务栏横向拖）；
- 在家的小反应：home_petted / home_alert / home_wake，没有就只用叠加效果（腮红、表情），不切片段。
"""
import math
import random
from dataclasses import dataclass, field

ROAM_CLIPS = ("stand_up", "stand_idle", "walk", "sit_down")
GRAVITY = 2400.0            # 逻辑像素/秒²
PICK_UP_DY = 36             # 在家时往上拖这么多像素 = 拎起来
CLICK_SLOP = 5              # 按下到松开移动不超过这么多 = 点击
ACTIVITY = {0: None, 1: ((150, 420), (240, 600)), 2: ((50, 160), (360, 900))}
SLEEP_SPEED = 0.75          # 睡着时在家循环放慢（呼吸变慢）
HAPPY_SPEED = 1.15          # 被摸/提醒时略快（整帧方案没法只加速尾巴）


@dataclass
class World:
    floor_y: float
    left: float
    right: float
    platforms: list = field(default_factory=list)   # [(id, x0, x1, y)]，不含地板


class Brain:
    def __init__(self, clips, home_x: float, floor_y: float, now: float = 0.0,
                 activity: int = 1, rng: random.Random | None = None):
        self.clips = set(clips)
        self.rng = rng or random.Random()
        self.activity = activity
        self.world = World(floor_y, -1e9, 1e9)
        self.state = "home"
        self.x, self.y = float(home_x), float(floor_y)
        self.facing = -1
        self.vx = self.vy = 0.0
        self.platform = None             # 当前所在平台 (id, x0, x1, y)；None = 地板
        self.target_x = None
        self.then = None
        self.t_state = now
        self.dur = 0.0
        self.stamina = 1.0
        self.sleeping = False
        self.busy = False
        self.happy_until = 0.0
        self.react = None                # (片段, 结束时刻)
        self.requests = []
        self._press = None               # (x, y, 时刻, 按下时的落点)
        self._drag_off = None
        self._last_move = None
        self.next_leave = now + self._rand_home()
        self.want_home_at = 0.0

    # ------------------------------------------------------------ 能力
    @property
    def can_roam(self) -> bool:
        return bool(self.activity) and all(c in self.clips for c in ROAM_CLIPS)

    @property
    def can_jump(self) -> bool:
        return self.can_roam and "jump" in self.clips

    @property
    def can_drag(self) -> bool:
        return self.can_roam and "dangle" in self.clips

    @property
    def away(self) -> bool:
        return self.state not in ("home", "slide")

    @property
    def seated(self) -> bool:
        return self.state in ("home", "slide", "ledge")

    def _rand_home(self):
        a = ACTIVITY.get(self.activity)
        return self.rng.uniform(*a[0]) if a else 1e9

    def _rand_away(self):
        a = ACTIVITY.get(self.activity) or ACTIVITY[1]
        return self.rng.uniform(*a[1])

    def _set(self, state, now, dur=0.0):
        self.state, self.t_state, self.dur = state, now, dur

    # ------------------------------------------------------------ 输出
    def clip(self, now: float) -> tuple:
        """(片段名, 播放速度)。"""
        st = self.state
        if st in ("home", "slide", "ledge"):
            if self.react and now < self.react[1] and self.react[0] in self.clips:
                return self.react[0], 1.0
            if self.sleeping:
                return ("home_sleep", 1.0) if "home_sleep" in self.clips else ("home_idle", SLEEP_SPEED)
            return "home_idle", (HAPPY_SPEED if now < self.happy_until else 1.0)
        return {"standup": ("stand_up", 1.0), "stand": ("stand_idle", 1.0), "walk": ("walk", 1.0),
                "sitdown": ("sit_down", 1.0), "drag": ("dangle", 1.0),
                "jump": ("jump", 1.0), "fall": ("fall" if "fall" in self.clips else "jump", 1.0),
                "land": ("land" if "land" in self.clips else "stand_idle", 1.0)}.get(st, ("home_idle", 1.0))

    def eyes_closed(self) -> bool:
        return self.sleeping and "home_sleep" not in self.clips and self.seated

    # ------------------------------------------------------------ 外部事件
    def set_world(self, world: World):
        self.world = world
        if self.platform is None and self.state in ("home", "slide", "stand", "walk", "standup",
                                                     "sitdown", "land"):
            self.y = world.floor_y

    def set_sleeping(self, on: bool):
        self.sleeping = bool(on)

    def set_busy(self, on: bool):
        self.busy = bool(on)

    def petted(self, now):
        self.happy_until = now + 4.5
        if "home_petted" in self.clips and self.seated:
            self.react = ("home_petted", now + 1.2)

    def alert(self, now):
        self.happy_until = now + 4.0
        if "home_alert" in self.clips and self.seated:
            self.react = ("home_alert", now + 1.2)

    def woke(self, now):
        if "home_wake" in self.clips and self.seated:
            self.react = ("home_wake", now + 1.0)

    # ------------------------------------------------------------ 鼠标（世界逻辑坐标）
    def press(self, x, y, now):
        self._press = (x, y, now, (self.x, self.y))
        self._last_move = (x, y, now)

    def move(self, x, y, now):
        """按住拖动中。返回 True 表示位置变了（窗口要跟着动）。"""
        if self._press is None:
            return False
        px, py, _, (ox, oy) = self._press
        lx, ly, lt = self._last_move
        dtm = max(1e-3, now - lt)
        self._last_move = (x, y, now)
        if self.state == "drag":
            nx, ny = x - self._drag_off[0], y - self._drag_off[1]
            self.vx = 0.7 * self.vx + 0.3 * (nx - self.x) / dtm
            self.vy = 0.7 * self.vy + 0.3 * (ny - self.y) / dtm
            self.x, self.y = nx, ny
            return True
        if self.state in ("home", "slide"):
            if self.can_drag and py - y > PICK_UP_DY:
                self._start_drag(x, y, now)
                return True
            if abs(x - px) > CLICK_SLOP or self.state == "slide":
                self.state = "slide"
                self.x = ox + (x - px)       # 只沿任务栏横向
                return True
            return False
        if abs(x - px) + abs(y - py) > CLICK_SLOP and self.can_drag:
            self._start_drag(x, y, now)
            return True
        return False

    def release(self, x, y, now):
        if self._press is None:
            return
        px, py, _, _ = self._press
        self._press = None
        if self.state == "drag":
            self.platform = None
            self.vx = max(-900.0, min(900.0, self.vx))
            self.vy = max(-900.0, min(600.0, self.vy))
            self._set("fall", now)
            return
        if self.state == "slide":
            self.state = "home"
            self.requests.append(("home_x", self.x))
            return
        if abs(x - px) + abs(y - py) <= CLICK_SLOP:
            self.requests.append(("click",))

    def _start_drag(self, x, y, now):
        self._drag_off = (x - self.x, y - self.y)
        self.platform = None
        self.vx = self.vy = 0.0
        self._set("drag", now)
        if self.rng.random() < 0.6:
            self.requests.append(("bubble", self.rng.choice(["放、放我下来！", "喂！干嘛！", "……哼。"])))

    # ------------------------------------------------------------ 出门 / 回家
    def leave_home(self, now) -> bool:
        if self.away or not self.can_roam:
            return False
        self.platform = None
        self.facing = -1
        self._set("standup", now, self._clip_len("stand_up"))
        self.want_home_at = now + self._rand_away()
        return True

    def go_home(self, now):
        if not self.away:
            return
        self.want_home_at = 0.0
        if self.state == "ledge":
            self._set("standup", now, self._clip_len("stand_up"))
        elif self.state in ("stand", "walk"):
            self._go_rest(now, home=True)

    def _clip_len(self, name):
        return getattr(self, "clip_lengths", {}).get(name, 0.6)

    # ------------------------------------------------------------ 每帧
    def tick(self, now: float, dt: float):
        if self.react and now >= self.react[1]:
            self.react = None
        st = self.state
        if st == "home":
            if (self.can_roam and now >= self.next_leave and not self.sleeping and not self.busy
                    and self._press is None):
                self.leave_home(now)
            return
        if st == "slide":
            return
        if st == "drag":
            return
        if st in ("fall", "jump"):
            self._tick_air(now, dt)
            return
        self._follow_floor()
        if st == "standup" and now - self.t_state >= self.dur:
            self._set("stand", now, self.rng.uniform(1.0, 2.5))
        elif st == "sitdown" and now - self.t_state >= self.dur:
            if self.platform is None:
                self._arrive_home(now)
            else:
                self._set("ledge", now, self.rng.uniform(45, 150))
        elif st == "ledge":
            self.stamina = min(1.0, self.stamina + dt * 0.02)
            if not self.sleeping and not self.busy and now - self.t_state > self.dur:
                self._set("standup", now, self._clip_len("stand_up"))
        elif st == "land" and now - self.t_state >= self.dur:
            self._set("stand", now, self.rng.uniform(0.8, 2.0))
        elif st == "walk":
            self._tick_walk(now, dt)
        elif st == "stand":
            self._tick_stand(now)

    def _follow_floor(self):
        if self.platform is None:
            self.y = self.world.floor_y

    def _arrive_home(self, now):
        self._set("home", now)
        self.stamina = 1.0
        self.next_leave = now + self._rand_home()
        self.requests.append(("home_x", self.x))

    def _tick_stand(self, now):
        if self.sleeping:
            self._set("sitdown", now, self._clip_len("sit_down"))
            return
        if self.busy or now - self.t_state < self.dur:
            return
        if self.stamina < 0.28 or now > self.want_home_at:
            self._go_rest(now, home=now > self.want_home_at)
            return
        r = self.rng.random()
        if r < 0.55:
            self._walk_to(self.x + self.rng.uniform(80, 320) * self.rng.choice((-1, 1)), now)
        elif r < 0.75 and self.can_jump and self._try_jump(now):
            pass
        else:
            self._set("stand", now, self.rng.uniform(2, 5))

    def _clamp(self, x, plat):
        lo, hi = self.world.left + 40, self.world.right - 40
        if plat is not None:
            lo, hi = max(lo, plat[1] + 20), min(hi, plat[2] - 20)
        return max(lo, min(hi, x))

    def _walk_to(self, x, now, then=None):
        self.target_x = self._clamp(x, self.platform)
        self.then = then
        self._set("walk", now)

    def _go_rest(self, now, home=False):
        if self.platform is not None and not home:
            self._set("sitdown", now, self._clip_len("sit_down"))
            return
        if self.platform is not None and self.can_jump:   # 想回家：从窗口上跳下去
            self._jump_to(None, self.x, now)
            return
        self._walk_to(self.x + self.rng.uniform(-160, 160), now, then="sitdown")

    def walk_speed(self) -> float:
        return getattr(self, "walk_speed_px", 120.0)

    def _tick_walk(self, now, dt):
        if self.target_x is None:
            self._set("stand", now, 1.0)
            return
        d = self.target_x - self.x
        self.facing = 1 if d > 0 else -1
        step = self.walk_speed() * dt
        self.stamina = max(0.0, self.stamina - dt * 0.012)
        if abs(d) <= step:
            self.x, self.target_x = self.target_x, None
            if self.then == "sitdown":
                self.then = None
                self._set("sitdown", now, self._clip_len("sit_down"))
            else:
                self._set("stand", now, self.rng.uniform(1.5, 4.5))
            return
        self.x += step * self.facing

    # ---- 跳 / 落
    def _try_jump(self, now) -> bool:
        cands = []
        for p in self.world.platforms:
            if self.platform is not None and p[0] == self.platform[0]:
                continue
            dy = self.y - p[3]
            if -760 < dy < 580:
                x = self._clamp(min(max(self.x, p[1] + 40), p[2] - 40), p)
                if abs(x - self.x) < 520:
                    cands.append((p, x))
        if self.platform is not None:
            cands.append((None, self.x))
        if not cands:
            return False
        p, x = self.rng.choice(cands)
        self._jump_to(p, x, now)
        return True

    def _jump_to(self, plat, x, now):
        ty = plat[3] if plat is not None else self.world.floor_y
        x = self._clamp(x, plat)
        apex = min(self.y, ty) - 60
        t_up = math.sqrt(2 * (self.y - apex) / GRAVITY)
        t_down = math.sqrt(2 * max(1.0, ty - apex) / GRAVITY)
        self.vx = (x - self.x) / (t_up + t_down)
        self.vy = -GRAVITY * t_up
        self.facing = 1 if x > self.x else -1
        self._jump_target = plat
        self.stamina = max(0.0, self.stamina - 0.07)
        self._set("jump", now)

    def _tick_air(self, now, dt):
        prev = self.y
        self.vy += GRAVITY * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        if self.x < self.world.left + 20 or self.x > self.world.right - 20:
            self.vx = -self.vx * 0.4
            self.x = max(self.world.left + 20, min(self.x, self.world.right - 20))
        if self.vy <= 0:
            return
        plats = self.world.platforms
        if self.state == "jump" and getattr(self, "_jump_target", None) is not None:
            plats = [self._jump_target]
        for p in sorted(plats, key=lambda q: q[3]):
            if prev <= p[3] <= self.y and p[1] - 10 <= self.x <= p[2] + 10:
                self._land(p, now)
                return
        if self.y >= self.world.floor_y:
            self._land(None, now)

    def _land(self, plat, now):
        hard = self.vy > 1200
        self.y = plat[3] if plat is not None else self.world.floor_y
        self.vx = self.vy = 0.0
        self.platform = plat
        self._set("land", now, self._clip_len("land") if "land" in self.clips else 0.2)
        if hard:
            self.requests.append(("bubble", self.rng.choice(["痛……", "轻点啊！", "……摔死了。"])))

    def platform_moved(self, plat_id, x0, x1, y):
        """窗口层每帧告诉我当前平台的新位置（窗口被拖了）；平台没了传 None。"""
        if self.platform is None or self.platform[0] != plat_id:
            return
        if y is None:
            self.platform = None
            self.vx = self.vy = 0.0
            self._set("fall", self.t_state)
            return
        dx = x0 - self.platform[1]
        self.platform = (plat_id, x0, x1, y)
        self.x += dx
        self.y = y
        if not x0 - 10 <= self.x <= x1 + 10:
            self.platform = None
            self._set("fall", self.t_state)

    def take_requests(self) -> list:
        out, self.requests = self.requests, []
        return out
