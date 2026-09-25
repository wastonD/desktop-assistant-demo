# -*- coding: utf-8 -*-
"""任务栏外观：正常 / 全透明 / 模糊 / 亚克力 / 半透明深色。

Win11 22H2 起任务栏是 XAML 画的，外部进程调 SetWindowCompositionAttribute 看不到效果；能改的只有注入
explorer 的工具（TranslucentTB、Windhawk）。本模块**自己不注入任何东西**，路径按顺序：
  A. 联动已安装的 TranslucentTB（开源 GPLv3，微软商店免费）：改它的 settings.json（它会热加载），
     改之前原样备份到 data/ttb_settings.backup.json，切回"正常"或退出助理时还原（可关）。
     没装 → 提示去商店装（不替用户下载）；没运行 → 菜单里给"启动 TranslucentTB"（用户自己点）。
  C. 旧任务栏（Win10 / Win11 21H2，build < 22621）：纯 ctypes 调 SetWindowCompositionAttribute，
     explorer 会改回去，所以定时重设（enforce）。
  B. 内置（第五轮，可选、默认关、实验）：没装 TranslucentTB 时，"全透明"由助理自带的组件实现
     （app/taskbar_tap.py + app/native/assistant_tap.dll，把任务栏背景的 Fill 换成透明画刷）。
     只支持全透明；退出助理时还原；explorer 重启后（任务栏进程号变了）自动重新应用。
  另：系统"透明效果"开关（EnableTransparency）——只在系统半透明和不透明之间切，做不到全透明。
所有系统调用都在 _api / _tap 里，测试整体替换；测试和 CI **绝不在真实系统上执行**。
"""
import threading
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_PATH = ROOT / "data" / "ttb_settings.backup.json"

MODES = ("normal", "clear", "blur", "acrylic", "opaque")
LABELS = {"normal": "正常（系统默认）", "clear": "全透明", "blur": "模糊", "acrylic": "亚克力",
          "opaque": "半透明深色"}
TTB_PFN = "28017CharlesMilette.TranslucentTB_v826wp6bftszj"
TTB_AUMID = TTB_PFN + "!TranslucentTB"
TTB_WINDOW_CLASS = "TTB_WorkerWindow"
TTB_STORE_URI = "ms-windows-store://pdp/?productid=9PF4KZ2VN4W9"
XAML_TASKBAR_BUILD = 22621          # Win11 22H2：任务栏换成 XAML

# 我们的模式 → TranslucentTB 的 (accent, color #RRGGBBAA)
TTB_MODE = {"clear": ("clear", "#00000000"), "blur": ("blur", "#00000000"),
            "acrylic": ("acrylic", "#00000000"), "opaque": ("opaque", "#1C1C1ECC")}
TTB_SECTIONS = ("desktop_appearance", "visible_window_appearance")

# 旧任务栏：ACCENT_STATE 与颜色（ABGR）
ACCENT = {"normal": (0, 0), "opaque": (1, 0xCC1E1C1C), "clear": (2, 0x00000000),
          "blur": (3, 0x00000000), "acrylic": (4, 0x01000000)}


@dataclass
class Status:
    build: int
    xaml_taskbar: bool
    ttb_installed: bool
    ttb_running: bool
    ttb_settings: Path | None
    builtin: bool = False            # 内置组件可用（Win11 新任务栏 + DLL 在）

    @property
    def usable(self) -> bool:
        return (not self.xaml_taskbar) or self.ttb_installed

    def mode_usable(self, mode: str) -> bool:
        if mode == "normal" or self.usable:
            return True
        return mode == "clear" and self.builtin   # 没有 TranslucentTB：只有全透明能用内置实现

    def uses_builtin(self, mode: str) -> bool:
        return self.xaml_taskbar and not self.ttb_installed and mode == "clear" and self.builtin


def strip_json_comments(text: str) -> str:
    """TranslucentTB 的 settings.json 里有整行 // 注释（JSON 标准不允许）：去掉整行注释，字符串里的 // 不动。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def apply_ttb_mode(settings: dict, mode: str) -> dict:
    """返回改好的新字典：只动 desktop/visible_window 两段的 accent 和 color，其余一律原样。"""
    accent, color = TTB_MODE[mode]
    out = json.loads(json.dumps(settings))
    for sec in TTB_SECTIONS:
        cur = out.get(sec)
        if sec == "visible_window_appearance" and not (isinstance(cur, dict) and cur.get("enabled")):
            continue                           # 用户没单独启用"有窗口时"外观：它会沿用桌面外观
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur["accent"], cur["color"] = accent, color
        out[sec] = cur
    return out


# ---------------------------------------------------------------- 系统接口
class _Win32:
    def __init__(self):
        import ctypes
        import ctypes.wintypes as wt
        self.ctypes, self.wt = ctypes, wt
        self.user32 = ctypes.windll.user32

    def build(self) -> int:
        return sys.getwindowsversion().build

    def local_appdata(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))

    def exists(self, p: Path) -> bool:
        return Path(p).exists()

    def read_text(self, p: Path) -> str:
        return Path(p).read_text(encoding="utf-8-sig")

    def write_text_atomic(self, p: Path, text: str) -> None:
        p = Path(p)
        tmp = p.with_name(p.name + ".assistant.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)                     # 同一目录内替换：TranslucentTB 只会看到完整的新文件

    def remove(self, p: Path) -> None:
        try:
            Path(p).unlink()
        except OSError:
            pass

    def window_exists(self, cls: str) -> bool:
        return bool(self.user32.FindWindowW(cls, None))

    def launch(self, target: str) -> None:
        os.startfile(target)

    def tray_windows(self):
        out = []
        h = self.user32.FindWindowW("Shell_TrayWnd", None)
        if h:
            out.append(h)
        h = 0
        while True:
            h = self.user32.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
            if not h:
                break
            out.append(h)
        return out

    def set_accent(self, hwnd, state: int, color: int) -> bool:
        ct = self.ctypes

        class ACCENT_POLICY(ct.Structure):
            _fields_ = [("AccentState", ct.c_int), ("AccentFlags", ct.c_int),
                        ("GradientColor", ct.c_uint), ("AnimationId", ct.c_int)]

        class WINCOMPATTRDATA(ct.Structure):
            _fields_ = [("Attribute", ct.c_int), ("Data", ct.c_void_p), ("SizeOfData", ct.c_size_t)]

        policy = ACCENT_POLICY(state, 2 if state else 0, color, 0)
        data = WINCOMPATTRDATA(19, ct.cast(ct.pointer(policy), ct.c_void_p), ct.sizeof(policy))  # WCA_ACCENT_POLICY
        fn = self.user32.SetWindowCompositionAttribute
        fn.argtypes = [self.wt.HWND, ct.POINTER(WINCOMPATTRDATA)]
        return bool(fn(hwnd, ct.byref(data)))

    def get_system_transparency(self) -> bool:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return bool(winreg.QueryValueEx(k, "EnableTransparency")[0])
        except OSError:
            return True

    def set_system_transparency(self, on: bool) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "EnableTransparency", 0, winreg.REG_DWORD, 1 if on else 0)
        res = self.ctypes.c_size_t()
        self.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "ImmersiveColorSet", 0x0002, 1000,
                                        self.ctypes.byref(res))   # HWND_BROADCAST, WM_SETTINGCHANGE


_api = None
_tap = None            # 内置组件（app.taskbar_tap）；测试里整体替换


def api():
    global _api
    if _api is None:
        _api = _Win32()
    return _api


def tap():
    global _tap
    if _tap is None:
        from . import taskbar_tap
        _tap = taskbar_tap
    return _tap


# ---------------------------------------------------------------- 逻辑
def ttb_settings_path(a=None) -> Path:
    a = a or api()
    return a.local_appdata() / "Packages" / TTB_PFN / "RoamingState" / "settings.json"


def detect(a=None) -> Status:
    a = a or api()
    build = a.build()
    pkg = a.local_appdata() / "Packages" / TTB_PFN
    installed = a.exists(pkg)
    settings = ttb_settings_path(a)
    try:
        builtin = bool(tap().available())
    except Exception:
        builtin = False
    return Status(build, build >= XAML_TASKBAR_BUILD, installed, a.window_exists(TTB_WINDOW_CLASS),
                  settings if a.exists(settings) else None, builtin)


class TaskbarStyle:
    """cfg = pet.json 的字典（会被修改，调用方负责保存）：taskbar_style（模式）、taskbar_style_restore（退出时还原）。"""

    def __init__(self, cfg: dict, backup_path: Path = BACKUP_PATH):
        self.cfg = cfg
        self.backup_path = backup_path

    @property
    def mode(self) -> str:
        m = self.cfg.get("taskbar_style", "normal")
        return m if m in MODES else "normal"

    def status(self) -> Status:
        return detect()

    def apply(self, mode: str) -> tuple:
        """切换外观。返回 (是否成功, 给用户看的一句话)。"""
        if mode not in MODES:
            mode = "normal"
        if mode == "normal":
            self.restore()
            self.cfg["taskbar_style"] = "normal"
            return True, "任务栏外观已恢复正常"
        st = detect()
        if not st.xaml_taskbar:
            ok = self._legacy(mode)
            if ok:
                self.cfg["taskbar_style"] = mode
            return ok, (f"任务栏：{LABELS[mode]}" if ok else "系统拒绝了外观设置")
        if st.uses_builtin(mode):
            self.cfg["taskbar_style"] = mode
            self.cfg["taskbar_builtin"] = True
            self._builtin_async("clear")
            return True, "任务栏：全透明（内置组件，实验功能；不满意随时切回「正常」）"
        if not st.ttb_installed:
            return False, ("只有「全透明」能用内置组件；模糊/亚克力要借助 TranslucentTB（微软商店免费）"
                           if st.builtin else
                           "Win11 新任务栏要借助 TranslucentTB（微软商店免费）才能变透明，装好后再点一次")
        if st.ttb_settings is None:
            return False, "TranslucentTB 还没生成配置文件：先运行它一次"
        ok, msg = self._write_ttb(st.ttb_settings, mode)
        if not ok:
            return False, msg
        self.cfg["taskbar_style"] = mode
        if not st.ttb_running:
            return True, f"已设为{LABELS[mode]}；TranslucentTB 没在运行，启动它之后生效"
        return True, f"任务栏：{LABELS[mode]}"

    def _write_ttb(self, path: Path, mode: str) -> tuple:
        a = api()
        try:
            raw = a.read_text(path)
            data = json.loads(strip_json_comments(raw))
        except (OSError, ValueError) as e:
            return False, f"读不了 TranslucentTB 的配置：{e}"
        if not isinstance(data, dict):
            return False, "TranslucentTB 的配置格式不认识"
        if not a.exists(self.backup_path):      # 只在第一次接管前备份，之后不覆盖（保证还原到用户自己的样子）
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            a.write_text_atomic(self.backup_path, json.dumps({"path": str(path), "text": raw},
                                                             ensure_ascii=False))
        try:
            a.write_text_atomic(path, json.dumps(apply_ttb_mode(data, mode), ensure_ascii=False, indent=4))
        except OSError as e:
            return False, f"写不了 TranslucentTB 的配置：{e}"
        return True, ""

    def _legacy(self, mode: str) -> bool:
        a = api()
        state, color = ACCENT[mode]
        ok = False
        for h in a.tray_windows():
            ok = a.set_accent(h, state, color) or ok
        return ok

    # ---- 内置组件
    def _builtin_async(self, mode: str) -> None:
        """注入要几十到几百毫秒，放后台线程；结果只打日志（DLL 自己也写 %TEMP%\\assistant_tap.log）。"""
        t = tap()
        try:
            self._builtin_pid = t.explorer_pid()
        except Exception:
            self._builtin_pid = 0

        def run():
            try:
                ok, msg = t.inject(mode)
                if not ok:
                    print("内置透明任务栏：", msg)
            except Exception as e:           # 绝不让它影响助理本身
                print("内置透明任务栏出错：", e)
        threading.Thread(target=run, daemon=True, name="taskbar-tap").start()

    def enforce(self) -> None:
        """旧任务栏：explorer 会把外观改回去，定时重设（XAML 任务栏由 TranslucentTB 自己维持）。
        内置组件：explorer 重启过（任务栏进程号变了）就重新应用一次。"""
        if self.mode == "normal":
            return
        if self.cfg.get("taskbar_builtin") and self.mode == "clear":
            try:
                pid = tap().explorer_pid()
            except Exception:
                return
            if pid and pid != getattr(self, "_builtin_pid", pid):
                self._builtin_async("clear")
            return
        if not detect().xaml_taskbar:
            self._legacy(self.mode)

    def restore(self) -> bool:
        """还原：内置组件清掉透明；TranslucentTB 配置写回备份（原样字节）；旧任务栏把 accent 设回默认。"""
        a = api()
        restored = False
        if self.cfg.pop("taskbar_builtin", False):
            try:
                ok, msg = tap().inject("restore")      # 同步：退出时要等它做完
                restored = ok
                if not ok:
                    print("还原内置透明任务栏失败：", msg)
            except Exception as e:
                print("还原内置透明任务栏出错：", e)
        if a.exists(self.backup_path):
            try:
                b = json.loads(a.read_text(self.backup_path))
                a.write_text_atomic(Path(b["path"]), b["text"])
                a.remove(self.backup_path)
                restored = True
            except (OSError, ValueError, KeyError) as e:
                print("还原 TranslucentTB 配置失败：", e)
        if not detect(a).xaml_taskbar and self.mode != "normal":
            self._legacy("normal")
            restored = True
        return restored

    def restore_on_exit(self) -> None:
        """退出助理：默认还原系统原样，但保留用户的选择（下次启动再应用）。"""
        if self.mode != "normal" and self.cfg.get("taskbar_style_restore", True):
            self.restore()

    def launch_ttb(self) -> None:
        """用户在菜单里点了"启动 TranslucentTB"（它会常驻并注入 explorer——那是它自己的机制）。"""
        api().launch(f"shell:AppsFolder\\{TTB_AUMID}")

    def open_store(self) -> None:
        api().launch(TTB_STORE_URI)


def fill_menu(menu, style: TaskbarStyle, notify=None, save=None):
    """把"任务栏外观"子菜单填进 QMenu（托盘和 v2 宠物右键共用）。notify(文字) 显示结果，save() 保存配置。"""
    menu.clear()
    try:
        st = style.status()
    except Exception as e:           # 非 Windows 或接口异常：菜单给说明，不影响其它功能
        a = menu.addAction(f"不可用：{e}")
        a.setEnabled(False)
        return

    def pick(m):
        ok, msg = style.apply(m)
        if save:
            save()
        if notify and msg:
            notify(msg)

    for m in MODES:
        label = LABELS[m] + ("（内置·实验）" if st.uses_builtin(m) else "")
        act = menu.addAction(label)
        act.setCheckable(True)
        act.setChecked(style.mode == m)
        act.setEnabled(st.mode_usable(m))
        act.triggered.connect(lambda _=False, mm=m: pick(mm))
    menu.addSeparator()
    if st.xaml_taskbar:
        if not st.ttb_installed:
            menu.addAction("模糊/亚克力需要 TranslucentTB（微软商店免费）→ 打开商店" if st.builtin
                           else "需要 TranslucentTB（微软商店免费）→ 打开商店", style.open_store)
        elif not st.ttb_running:
            menu.addAction("启动 TranslucentTB", style.launch_ttb)
        else:
            a = menu.addAction("由 TranslucentTB 实现（已在运行）")
            a.setEnabled(False)
    sysm = menu.addAction("系统透明效果（设置里的同名开关，只是半透明）")
    sysm.setCheckable(True)
    try:
        sysm.setChecked(api().get_system_transparency())
        sysm.toggled.connect(lambda on: api().set_system_transparency(on))
    except Exception:
        sysm.setEnabled(False)
    keep = menu.addAction("退出助理时还原任务栏外观")
    keep.setCheckable(True)
    keep.setChecked(bool(style.cfg.get("taskbar_style_restore", True)))

    def set_keep(on):
        style.cfg["taskbar_style_restore"] = bool(on)
        if save:
            save()
    keep.toggled.connect(set_keep)
