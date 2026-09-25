# -*- coding: utf-8 -*-
"""让桌面"完全干净"：任务栏隐藏、桌面图标隐藏。都是可逆的、只影响当前用户，由用户在菜单里自己开关。

任务栏（三档，config/pet.json 的 taskbar）：
- normal   正常；
- autohide 自动隐藏（系统自带功能，鼠标碰到屏幕底边就出来）；
- hidden   完全隐藏（自动隐藏 + 把任务栏窗口藏起来；按 Win 键开始菜单照常可用；Ctrl+Alt+T 临时呼出）。
  退出助理时一定恢复成开启前的样子（原始的自动隐藏状态记在 taskbar_orig_autohide）。
桌面图标：
- 系统图标（此电脑、回收站、网络、控制面板、用户文件夹）：HKCU\\...\\HideDesktopIcons\\NewStartPanel；
- 全部桌面图标：和桌面右键"查看 → 显示桌面图标"是同一个开关（给桌面窗口发 0x7402 命令）。
所有 Win32 调用集中在 _api 里，测试时整体替换，绝不在测试里改真实系统设置。
"""
import ctypes
import ctypes.wintypes

try:
    import winreg
except ImportError:  # 非 Windows（CI 测试）：真实接口用不了，测试一律替换 _api
    winreg = None

SYSTEM_ICONS = {
    "此电脑": "{20D04FE0-3AEA-1069-A2D8-08002B30309D}",
    "回收站": "{645FF040-5081-101B-9F08-00AA002F954E}",
    "网络": "{F02C1A0D-BE21-4350-88B0-7367FC96EF3C}",
    "控制面板": "{5399E694-6CE5-4D6C-8FCE-1D8870FDCBA0}",
    "用户文件夹": "{59031a47-3f72-44a7-89c5-5595fe6b30ee}",
}
SHELL_TARGETS = {  # 抽屉里"系统"分类点开时用
    "此电脑": "shell:MyComputerFolder",
    "回收站": "shell:RecycleBinFolder",
    "网络": "shell:NetworkPlacesFolder",
    "控制面板": "shell:ControlPanelFolder",
    "用户文件夹": "shell:UsersFilesFolder",
}
HIDE_ICONS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel"
ADVANCED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"

ABM_GETSTATE, ABM_SETSTATE = 0x4, 0xA
ABS_AUTOHIDE, ABS_ALWAYSONTOP = 0x1, 0x2


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("hWnd", ctypes.wintypes.HWND),
                ("uCallbackMessage", ctypes.wintypes.UINT), ("uEdge", ctypes.wintypes.UINT),
                ("rc", ctypes.wintypes.RECT), ("lParam", ctypes.wintypes.LPARAM)]


class _Win32:
    """真实的系统调用。测试里用假对象替换 desktop_clean._api。"""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32

    # --- 任务栏
    def get_autohide(self) -> bool:
        abd = APPBARDATA(ctypes.sizeof(APPBARDATA))
        return bool(self.shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd)) & ABS_AUTOHIDE)

    def set_autohide(self, on: bool) -> None:
        abd = APPBARDATA(ctypes.sizeof(APPBARDATA))
        abd.hWnd = self.user32.FindWindowW("Shell_TrayWnd", None)
        abd.lParam = (ABS_AUTOHIDE if on else 0) | ABS_ALWAYSONTOP
        self.shell32.SHAppBarMessage(ABM_SETSTATE, ctypes.byref(abd))

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

    def show_window(self, hwnd, show: bool) -> None:
        self.user32.ShowWindow(hwnd, 5 if show else 0)  # SW_SHOW / SW_HIDE

    def is_visible(self, hwnd) -> bool:
        return bool(self.user32.IsWindowVisible(hwnd))

    # --- 桌面图标
    def read_dword(self, key, name, default=0):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return int(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return default

    def write_dword(self, key, name, value):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))

    def refresh_desktop(self):
        # 通知资源管理器刷新桌面（等同 F5）
        self.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED

    def toggle_all_icons(self):
        """和"查看 → 显示桌面图标"同一个开关。"""
        progman = self.user32.FindWindowW("Progman", None)
        view = self.user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if not view:  # 换过壁纸引擎/幻灯片时 DefView 挂在某个 WorkerW 下
            h = 0
            while not view:
                h = self.user32.FindWindowExW(None, h, "WorkerW", None)
                if not h:
                    break
                view = self.user32.FindWindowExW(h, None, "SHELLDLL_DefView", None)
        if view:
            self.user32.SendMessageW(view, 0x0111, 0x7402, 0)  # WM_COMMAND


_api = None


def api():
    global _api
    if _api is None:
        _api = _Win32()
    return _api


# ---------------------------------------------------------------- 任务栏
class Taskbar:
    """任务栏三档。cfg 是 pet.json 的字典（会被修改，调用方负责保存）。"""
    MODES = ("normal", "autohide", "hidden")

    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def mode(self) -> str:
        m = self.cfg.get("taskbar", "normal")
        return m if m in self.MODES else "normal"

    def apply(self, mode: str) -> None:
        a = api()
        if mode not in self.MODES:
            mode = "normal"
        if mode != "normal" and "taskbar_orig_autohide" not in self.cfg:
            self.cfg["taskbar_orig_autohide"] = a.get_autohide()  # 记住开启前的样子
        if mode == "normal":
            for h in a.tray_windows():
                a.show_window(h, True)
            orig = self.cfg.pop("taskbar_orig_autohide", False)
            a.set_autohide(bool(orig))
        elif mode == "autohide":
            for h in a.tray_windows():
                a.show_window(h, True)
            a.set_autohide(True)
        else:
            a.set_autohide(True)  # 先自动隐藏，把工作区还给窗口
            for h in a.tray_windows():
                a.show_window(h, False)
        self.cfg["taskbar"] = mode

    def enforce(self) -> None:
        """"完全隐藏"时，资源管理器偶尔会把任务栏重新显示出来（比如按 Win 键之后）：定时再藏回去。"""
        if self.mode == "hidden" and not getattr(self, "_peeking", False):
            a = api()
            for h in a.tray_windows():
                if a.is_visible(h):
                    a.show_window(h, False)

    def peek(self, on: bool) -> None:
        """完全隐藏模式下临时呼出（Ctrl+Alt+T）。"""
        if self.mode != "hidden":
            return
        self._peeking = on
        a = api()
        for h in a.tray_windows():
            a.show_window(h, on)

    def restore_on_exit(self) -> None:
        """退出助理：恢复系统原样，但保留用户的选择（下次启动再应用）。"""
        if self.mode == "normal":
            return
        a = api()
        for h in a.tray_windows():
            a.show_window(h, True)
        a.set_autohide(bool(self.cfg.get("taskbar_orig_autohide", False)))


# ---------------------------------------------------------------- 桌面图标
def system_icons_hidden() -> bool:
    a = api()
    return all(a.read_dword(HIDE_ICONS_KEY, g) == 1 for g in
               (SYSTEM_ICONS["此电脑"], SYSTEM_ICONS["回收站"]))


def set_system_icons_hidden(hidden: bool) -> None:
    a = api()
    for guid in SYSTEM_ICONS.values():
        a.write_dword(HIDE_ICONS_KEY, guid, 1 if hidden else 0)
    a.refresh_desktop()


def all_icons_hidden() -> bool:
    return api().read_dword(ADVANCED_KEY, "HideIcons") == 1


def set_all_icons_hidden(hidden: bool) -> None:
    if all_icons_hidden() != hidden:
        api().toggle_all_icons()
