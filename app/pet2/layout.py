# -*- coding: utf-8 -*-
"""几何换算（纯函数）：体型 → 缩放比、世界落点 → 窗口物理坐标。

约定（与 v1 保持一致，切换引擎时宠物位置不变）：
- 源图高度 src_h（立绘 1113）按 v1 的公式缩放：显示高度(逻辑) = 任务栏高 / (1 - sit_ratio) × 体型；
- 世界坐标 = 逻辑像素；窗口位置和帧尺寸 = 物理像素（逻辑 × dpr）；
- 片段的 anchor（画布坐标）放在世界落点上：在家时落点 = (config 的 x, 任务栏上沿)。
"""
import math


def logical_scale(taskbar_h: float, sit_ratio: float, pet_scale: float, src_h: float) -> float:
    """逻辑像素 / 源图像素。"""
    return taskbar_h / (1.0 - sit_ratio) * pet_scale / src_h


def window_origin(anchor_world: tuple, anchor_canvas: tuple, scale_phys: float, dpr: float) -> tuple:
    """落点（逻辑坐标）+ 片段锚点（画布坐标）→ 窗口左上角（物理像素，整数）。"""
    return (round(anchor_world[0] * dpr - anchor_canvas[0] * scale_phys),
            round(anchor_world[1] * dpr - anchor_canvas[1] * scale_phys))


def canvas_to_world(pt: tuple, origin_phys: tuple, scale_phys: float, dpr: float) -> tuple:
    """画布坐标的点（如头顶）→ 当前窗口位置下的世界逻辑坐标。"""
    return ((origin_phys[0] + pt[0] * scale_phys) / dpr, (origin_phys[1] + pt[1] * scale_phys) / dpr)


def world_to_frame(pt_phys: tuple, origin_phys: tuple) -> tuple:
    return pt_phys[0] - origin_phys[0], pt_phys[1] - origin_phys[1]


def crop_rows_for_taskbar(frame_h: int, origin_y_phys: int, taskbar_top_phys: int) -> int:
    """over_taskbar=False 时：帧只保留任务栏上沿以上的行数。"""
    return max(0, min(frame_h, taskbar_top_phys - origin_y_phys))


def rects_intersect(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def fps_interval_ms(fps: float) -> int:
    return max(15, math.floor(1000.0 / max(1.0, fps)))
