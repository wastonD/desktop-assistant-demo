# -*- coding: utf-8 -*-
"""预合成宠物 v2 的"在家坐姿"整帧（呼吸 × 尾摆 × 晃脚 + 闭眼补丁）。免费、本地、几秒到十几秒。

用法（在 assistant 目录下）：
  .venv\\Scripts\\python tools\\bake_home_frames.py                # 默认 15fps、3.6 秒一圈 = 54 帧
  .venv\\Scripts\\python tools\\bake_home_frames.py --fps 20       # 更顺，72 帧，内存 +33%
  .venv\\Scripts\\python tools\\bake_home_frames.py --out D:\\tmp\\baked --no-manifest

只读 assets/character/（layers/*.png + character.json），输出到 assets/character/anims/baked/（已 gitignore）。
参数和清单不一致时会同步改 assets/character/anims/manifest.json 里 home_idle 的 count/fps/box。
不跑也行：config/pet.json 设 "engine": "v2" 后，程序发现缺帧会在后台自动烘焙一次。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pet2 import bake, frames, layout  # noqa: E402

CHAR = ROOT / "assets" / "character"
ANIMS = CHAR / "anims"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fps", type=int, default=bake.FPS)
    ap.add_argument("--loop", type=float, default=bake.LOOP_S, help="一圈多少秒（要能被晃脚 1.8s 整除）")
    ap.add_argument("--out", type=Path, default=ANIMS / "baked")
    ap.add_argument("--no-manifest", action="store_true", help="不改 manifest.json")
    args = ap.parse_args(argv)

    t0 = time.time()

    def progress(i, n):
        print(f"\r合成 {i}/{n}", end="", flush=True)
    clip = bake.bake_home(CHAR, args.out, args.fps, args.loop, progress)
    print(f"\n完成：{clip['count']} 帧 @ {clip['fps']}fps，画布 {clip['size'][0]}x{clip['size'][1]}，"
          f"用时 {time.time() - t0:.1f}s，输出 {args.out}")

    meta = json.loads((CHAR / "character.json").read_text(encoding="utf-8"))
    sit = float(meta.get("sit_ratio", 0.642))
    src_h = meta.get("source_size", [705, 1113])[1]
    print("显示时的内存估算（任务栏 48 逻辑像素、150% 缩放）：")
    for label, pet_scale in (("迷你", 1.0), ("小", 1.4), ("中", 1.8), ("大", 2.4)):
        s = layout.logical_scale(48, sit, pet_scale, src_h) * 1.5
        w, h = frames.scale_size(tuple(clip["size"]), s)
        mb = frames.estimate_bytes(tuple(clip["size"]), s, clip["count"]) / 2 ** 20
        print(f"  {label}（{pet_scale}）：每帧 {w}x{h}，{clip['count']} 帧共 {mb:.1f} MB")

    if not args.no_manifest and args.out.resolve() == (ANIMS / "baked").resolve():
        mpath = ANIMS / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        if data.get("clips", {}).get("home_idle") != clip:
            data.setdefault("clips", {})["home_idle"] = clip
            mpath.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("已同步 manifest.json 的 home_idle")


if __name__ == "__main__":
    main()
