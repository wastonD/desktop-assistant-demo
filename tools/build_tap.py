# -*- coding: utf-8 -*-
"""编译透明任务栏组件 native/taskbar_tap/tap.c → app/native/assistant_tap.dll。

编译器用 Zig（自带 clang 和 mingw 头文件，里面有 xamlom.h；约 97MB，不是 Python 依赖）：
  - 环境变量 ZIG 指向 zig.exe；或
  - 解压到仓库同级的 toolchain/zig-*/zig.exe（从 ziglang.org 下载）。
编好的 DLL 进仓库（几十 KB），最终用户只需要下载助理本身，不需要编译器。
用法：.venv\\Scripts\\python tools\\build_tap.py
"""
import glob
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "native" / "taskbar_tap" / "tap.c"
DEF = SRC.with_suffix(".def")          # 导出 DllGetClassObject / DllCanUnloadNow
OUT = ROOT / "app" / "native" / "assistant_tap.dll"


def find_zig() -> str:
    if os.environ.get("ZIG"):
        return os.environ["ZIG"]
    cands = sorted(glob.glob(str(ROOT.parent / "toolchain" / "zig-*" / "zig.exe")))
    if not cands:
        sys.exit("找不到 zig.exe：设置环境变量 ZIG，或解压到仓库同级的 toolchain/ 目录")
    return cands[-1]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [find_zig(), "cc", "-shared", "-target", "x86_64-windows-gnu", "-O2", "-s",
           "-Wall", "-Wextra", "-Wno-unused-parameter",
           str(SRC), str(DEF), "-o", str(OUT), "-lole32", "-loleaut32", "-luuid"]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    for junk in (OUT.with_suffix(".lib"), OUT.with_suffix(".pdb"), OUT.with_name(DEF.stem + ".lib")):
        junk.unlink(missing_ok=True)
    print(f"已生成 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
