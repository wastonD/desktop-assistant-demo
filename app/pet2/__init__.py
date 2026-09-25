# -*- coding: utf-8 -*-
"""宠物 v2：整帧动画 + 单窗口原子提交。

和 v1（pet_window.py 三窗口 + roam.py 纸偶）并存，由 config/pet.json 的 "engine" 切换：
  "v1"（缺省）= 旧实现；"v2" = 本包。

模块分工：
- anim.py    动画清单（assets/character/anims/manifest.json）的数据模型 + 帧播放器（纯逻辑）
- frames.py  帧图读取、缩放、预乘 BGRA、按 alpha 命中测试（Pillow，不依赖 Qt）
- bake.py    用现有切层素材离线预合成"在家坐姿"整帧（tools/bake_home_frames.py 调它）
- brain.py   行为状态机（在家/睡觉/摸头/提醒/站起/走/跳/坐窗口边/被拎起/回家，纯逻辑）
- zorder.py  置顶守护：只在真的被压住时才抬一次，事件合并（纯逻辑 + Windows 调用）
- backend.py 显示后端：Windows 上 ctypes 原生分层窗口，UpdateLayeredWindow 带 pptDst
             一次提交位置和内容；其他平台退化为 Qt 窗口（只供开发/测试）
- window.py  PetWindowV2：把以上拼起来，对外接口与 v1 PetWindow 相同（main.py 无感切换）
"""

ENGINES = ("v1", "v2")


def engine_of(cfg: dict) -> str:
    """config/pet.json 的 engine 字段；缺省或写错一律按 v1。"""
    e = str((cfg or {}).get("engine", "v1")).strip().lower()
    return e if e in ENGINES else "v1"
