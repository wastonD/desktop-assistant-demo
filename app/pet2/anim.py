# -*- coding: utf-8 -*-
"""动画清单 + 帧播放器（纯逻辑，不依赖 Qt / Windows，便于测试）。

清单文件 assets/character/anims/manifest.json（示例见仓库里那份）：

{
  "version": 1,
  "source_size": [705, 1113],          # 源图坐标系（所有帧同一张画布，锚点才可比）
  "points": {"head_top": [335, 40], "emote_anchor": [440, 70]},
  "clips": {
    "home_idle": {
      "frames": "baked/home_idle/{:03d}.png",   # 相对 anims/ 目录；{} 填帧号
      "count": 72, "fps": 20, "loop": "loop",   # loop | once | pingpong | hold
      "anchor": [352, 714.5],          # 源图坐标：这个点放在世界"落点"上（坐=臀下沿中点，站=脚底中点）
      "seated": true,                  # 坐姿（在家/窗口边）还是站姿
      "next": null,                    # once 播完自动接哪个片段
      "speed": 0,                      # 走路：源图像素/秒（脚不打滑用）
      "events": {"10": "step"},        # 播到某帧时发事件（落地、迈步声……）
      "overlays": {                    # 同一帧上的局部差分（眨眼/睡觉闭眼），按帧号一一对应
        "eyes_closed": {"frames": "baked/home_idle_eyes/{:03d}.png", "box": [x0, y0, x1, y1]}
      }
    }
  }
}

片段缺失时，依赖它的行为自动关闭（例如没有 walk 就不出门），见 brain.py。
"""
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

LOOP_MODES = ("loop", "once", "pingpong", "hold")


class ManifestError(ValueError):
    pass


@dataclass
class Overlay:
    frames: str                     # 路径模板
    box: tuple                      # 源图坐标 (x0, y0, x1, y1)：补丁贴在哪里
    margin: int = 0                 # 补丁四周多存的像素（缩放时给滤波器用，贴之前裁掉）


@dataclass
class Clip:
    name: str
    frames: str
    count: int
    fps: float
    loop: str = "loop"
    anchor: tuple = (0.0, 0.0)
    size: tuple | None = None       # 画布尺寸；None = 清单的 source_size
    seated: bool = False
    next: str | None = None
    speed: float = 0.0
    events: dict = field(default_factory=dict)    # {帧号: 事件名}
    overlays: dict = field(default_factory=dict)  # {名字: Overlay}

    @property
    def duration(self) -> float:
        return self.count / self.fps

    def frame_path(self, root: Path, i: int) -> Path:
        return root / self.frames.format(i)


@dataclass
class Manifest:
    root: Path
    source_size: tuple
    clips: dict
    points: dict = field(default_factory=dict)
    version: int = 1

    def has(self, *names) -> bool:
        return all(n in self.clips for n in names)

    def point(self, name, default=None):
        p = self.points.get(name)
        return tuple(p) if p else default

    def missing_files(self, names=None, overlays=True) -> list:
        """哪些帧文件不存在（没烘焙 / 没生成）。names=None 表示检查全部片段。"""
        out = []
        for name in (names or self.clips):
            c = self.clips.get(name)
            if c is None:
                continue
            for i in range(c.count):
                p = c.frame_path(self.root, i)
                if not p.exists():
                    out.append(p)
                if overlays:
                    for ov in c.overlays.values():
                        q = self.root / ov.frames.format(i)
                        if not q.exists():
                            out.append(q)
        return out


def _pair(v, what):
    try:
        a, b = v
        return float(a), float(b)
    except (TypeError, ValueError):
        raise ManifestError(f"{what} 应该是两个数：{v!r}")


def parse_manifest(data: dict, root: Path) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError("清单必须是 JSON 对象")
    version = int(data.get("version", 1))
    if version != 1:
        raise ManifestError(f"不认识的清单版本 {version}")
    src = tuple(int(v) for v in _pair(data.get("source_size"), "source_size"))
    clips = {}
    for name, c in (data.get("clips") or {}).items():
        if not isinstance(c, dict):
            raise ManifestError(f"片段 {name} 不是对象")
        try:
            count = int(c["count"])
            fps = float(c["fps"])
            frames = str(c["frames"])
        except (KeyError, TypeError, ValueError):
            raise ManifestError(f"片段 {name} 缺 frames/count/fps")
        if count <= 0 or fps <= 0:
            raise ManifestError(f"片段 {name} 的 count/fps 必须为正")
        if "{" not in frames:
            raise ManifestError(f"片段 {name} 的 frames 需要 {{}} 占位帧号")
        loop = str(c.get("loop", "loop"))
        if loop not in LOOP_MODES:
            raise ManifestError(f"片段 {name} 的 loop 只能是 {'/'.join(LOOP_MODES)}")
        events = {}
        for k, v in (c.get("events") or {}).items():
            i = int(k)
            if not 0 <= i < count:
                raise ManifestError(f"片段 {name} 的事件帧 {i} 越界")
            events[i] = str(v)
        overlays = {}
        for oname, o in (c.get("overlays") or {}).items():
            try:
                box = tuple(float(v) for v in o["box"])
                assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
                overlays[oname] = Overlay(str(o["frames"]), box, int(o.get("margin", 0)))
            except (KeyError, TypeError, ValueError, AssertionError):
                raise ManifestError(f"片段 {name} 的叠加 {oname} 需要 frames 和 box[x0,y0,x1,y1]")
        size = c.get("size")
        clips[name] = Clip(
            name=name, frames=frames, count=count, fps=fps, loop=loop,
            anchor=_pair(c.get("anchor", (0, 0)), f"{name}.anchor"),
            size=tuple(int(v) for v in _pair(size, f"{name}.size")) if size else None,
            seated=bool(c.get("seated", False)), next=c.get("next") or None,
            speed=float(c.get("speed", 0.0)), events=events, overlays=overlays)
    for c in clips.values():
        if c.next and c.next not in clips:
            raise ManifestError(f"片段 {c.name} 的 next={c.next} 不存在")
    points = {k: _pair(v, f"points.{k}") for k, v in (data.get("points") or {}).items()}
    return Manifest(root=root, source_size=src, clips=clips, points=points, version=version)


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"读不了清单 {path}：{e}")
    return parse_manifest(data, Path(path).parent)


# ---------------------------------------------------------------- 播放器
def frame_index(clip: Clip, t: float) -> int:
    """片段内时间 t（秒）→ 帧号。"""
    n = clip.count
    raw = max(0, math.floor(t * clip.fps + 1e-9))
    if clip.loop == "hold" or n == 1:
        return 0
    if clip.loop == "loop":
        return raw % n
    if clip.loop == "once":
        return min(raw, n - 1)
    k = raw % (2 * n - 2)            # pingpong：0..n-1..1
    return k if k < n else 2 * n - 2 - k


@dataclass
class FrameRef:
    clip: str
    index: int
    prev_clip: str | None = None    # 交叉淡化期间的上一个片段
    prev_index: int = 0
    mix: float = 1.0                # 当前片段的权重（1 = 不混合）

    def key(self):
        """画面是否变化的判据：帧号一样、没有在淡化 = 不用重新提交。"""
        return (self.clip, self.index, self.prev_clip, self.prev_index,
                round(self.mix, 2) if self.prev_clip else 1.0)


class Player:
    """按时间推进的帧播放器。advance(dt) 返回这一步里发生的事件：
    ("event", 名字) —— 播到带事件的帧；("end", 片段名) —— once 片段播完；("loop", 片段名) —— 循环回到开头。
    速度可随时改（相位累加，改速度不跳帧）。"""

    def __init__(self, manifest: Manifest):
        self.m = manifest
        self.clip: Clip | None = None
        self.t = 0.0
        self.speed = 1.0
        self._blend = None            # (上一片段, 它的时间, 剩余秒, 总秒)
        self.finished = False

    def play(self, name: str, speed: float = 1.0, blend: float = 0.0, restart: bool = True):
        clip = self.m.clips.get(name)
        if clip is None:
            raise KeyError(name)
        if self.clip is not None and self.clip.name == name and not restart:
            self.speed = speed
            return
        if blend > 0 and self.clip is not None:
            self._blend = (self.clip, self.t, blend, blend)
        else:
            self._blend = None
        self.clip, self.t, self.speed, self.finished = clip, 0.0, speed, False

    def advance(self, dt: float) -> list:
        if self.clip is None or dt <= 0:
            return []
        events = []
        if self._blend:
            pc, pt, left, total = self._blend
            left -= dt
            self._blend = (pc, pt + dt * self.speed, left, total) if left > 0 else None
        c = self.clip
        old_raw = math.floor(self.t * c.fps + 1e-9)
        self.t += dt * self.speed
        new_raw = math.floor(self.t * c.fps + 1e-9)
        if c.events and new_raw > old_raw:
            for raw in range(max(old_raw + 1, new_raw - 2 * c.count), new_raw + 1):
                if c.loop == "once" and raw >= c.count:
                    break
                idx = frame_index(c, raw / c.fps)
                if idx in c.events:
                    events.append(("event", c.events[idx]))
        if c.loop == "loop" and new_raw // c.count > old_raw // c.count:
            events.append(("loop", c.name))
        if c.loop == "once" and not self.finished and self.t * c.fps >= c.count:
            self.finished = True
            events.append(("end", c.name))
            if c.next:
                spill = self.t - c.duration
                self.play(c.next, self.speed)
                self.t = min(max(0.0, spill), self.clip.duration)
        return events

    def frame(self) -> FrameRef | None:
        if self.clip is None:
            return None
        ref = FrameRef(self.clip.name, frame_index(self.clip, self.t))
        if self._blend:
            pc, pt, left, total = self._blend
            ref.prev_clip, ref.prev_index = pc.name, frame_index(pc, pt)
            ref.mix = max(0.0, min(1.0, 1.0 - left / total))
        return ref
