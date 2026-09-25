# -*- coding: utf-8 -*-
"""内置透明任务栏（可选功能，默认关）：把 app/native/assistant_tap.dll 注入 explorer，把任务栏背景换成透明。

- Win11 22H2+ 的任务栏是 XAML 画的，外部进程改不了它的外观；唯一的路是 XAML 诊断接口
  InitializeXamlDiagnosticsEx（Windows.UI.Xaml.dll 导出）把一个 TAP DLL 加载进 explorer（TranslucentTB、Windhawk 同法）。
  DLL 源码在 native/taskbar_tap/tap.c（从零写），tools/build_tap.py 用 Zig 编译。
- 模式："probe" 只记日志不改、"clear" 透明、"restore" 还原（清掉我们设的本地值 → 回到系统样式）。
  DLL 的日志在 %TEMP%\\assistant_tap.log。
- explorer 会把 DLL 一直留着（直到 explorer 重启），文件被锁：注入前复制到
  %LOCALAPPDATA%\\DesktopAssistant\\tap\\assistant_tap-<哈希>.dll，仓库里的那份可以随时重新编译。
- 风险：DLL 出错会让 explorer 崩溃（任务栏闪一下自己恢复）；系统大更新可能改元素名；
  杀软可能拦截；和 TranslucentTB/Windhawk 同时用会冲突。所以默认关、失败只报错不重试。
不需要管理员权限（同一用户、同一完整性级别）。
"""
import ctypes
import hashlib
import os
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DLL = ROOT / "app" / "native" / "assistant_tap.dll"
CLSID = "5C1F4E6A-3B7D-4C2E-9A61-2D8E7F0B4A93"
E_NOTFOUND = 0x80070490           # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)：这个连接名被占了，换下一个
MODES = ("probe", "clear", "restore")


def log_path() -> Path:
    return Path(os.environ.get("TEMP", str(Path.home()))) / "assistant_tap.log"


def available() -> bool:
    return sys.platform == "win32" and DLL.exists() and sys.getwindowsversion().build >= 22621


def staged_dll(src: Path = DLL) -> Path:
    """复制到用户数据目录、按内容哈希命名（explorer 锁住的是这份，不是仓库里的）。"""
    data = src.read_bytes()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DesktopAssistant" / "tap"
    dst = base / f"assistant_tap-{hashlib.sha256(data).hexdigest()[:12]}.dll"
    if not dst.exists():
        base.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    return dst


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    u = uuid.UUID(s)
    return _GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))


def explorer_pid() -> int:
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    u.FindWindowW.restype = wt.HWND
    tray = u.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return 0
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(tray, ctypes.byref(pid))
    return pid.value


def _ixde():
    import ctypes.wintypes as wt
    k = ctypes.windll.kernel32
    k.LoadLibraryExW.restype = ctypes.c_void_p
    k.LoadLibraryExW.argtypes = [wt.LPCWSTR, wt.HANDLE, wt.DWORD]
    k.GetProcAddress.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    mod = k.LoadLibraryExW("Windows.UI.Xaml.dll", None, 0x00000800)    # LOAD_LIBRARY_SEARCH_SYSTEM32
    if not mod:
        raise OSError("加载不了 Windows.UI.Xaml.dll")
    addr = k.GetProcAddress(mod, b"InitializeXamlDiagnosticsEx")
    if not addr:
        raise OSError("Windows.UI.Xaml.dll 里没有 InitializeXamlDiagnosticsEx")
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, wt.LPCWSTR, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, _GUID, wt.LPCWSTR)
    return proto(addr)


def inject(mode: str, timeout_ms: int = 3000) -> tuple:
    """把 DLL 以某个模式送进 explorer。返回 (请求送达, 说明)。
    从外部直接调 InitializeXamlDiagnosticsEx 会返回成功但 explorer 不加载（实测）→
    和 TranslucentTB 一样：SetWindowsHookEx(WH_CALLWNDPROC) 把 DLL 挂进任务栏线程，发一条请求消息
    （wParam = 模式），DLL 在 explorer 里对自己调 InitializeXamlDiagnosticsEx，然后立刻卸钩子。
    结果写在 DLL 的日志里（read_log）。只在后台线程调用。"""
    import ctypes.wintypes as wt
    if mode not in MODES:
        raise ValueError(mode)
    if not available():
        return False, "这个系统用不了内置透明任务栏（需要 Win11 22H2 及以上）"
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.FindWindowW.restype = wt.HWND
    tray = u.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return False, "找不到任务栏（explorer 没在运行？）"
    tid = u.GetWindowThreadProcessId(tray, None)
    try:
        dll = str(staged_dll())
    except OSError as e:
        return False, f"准备组件失败：{e}"
    k.LoadLibraryW.restype = ctypes.c_void_p
    k.LoadLibraryW.argtypes = [wt.LPCWSTR]
    k.GetProcAddress.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    k.FreeLibrary.argtypes = [ctypes.c_void_p]
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                      ctypes.POINTER(ctypes.c_size_t)]
    mod = k.LoadLibraryW(dll)
    if not mod:
        return False, "加载不了组件 DLL"
    try:
        proc = k.GetProcAddress(mod, b"TapHookProc")
        hook = u.SetWindowsHookExW(4, proc, mod, tid)          # WH_CALLWNDPROC，只挂任务栏那一个线程
        if not hook:
            return False, f"挂钩子失败（{k.GetLastError()}）"
        try:
            msg = u.RegisterWindowMessageW("DesktopAssistant.TaskbarTapRequest")
            res = ctypes.c_size_t()
            ok = u.SendMessageTimeoutW(tray, msg, MODES.index(mode), 0, 0x0002, timeout_ms,   # SMTO_ABORTIFHUNG
                                       ctypes.byref(res))
        finally:
            u.UnhookWindowsHookEx(hook)
    finally:
        k.FreeLibrary(mod)
    if not ok:
        return False, "任务栏没有响应（超时）"
    return True, ""


def read_log(since: int = 0) -> tuple:
    """(新内容, 新的读取位置)：DLL 在 explorer 里写的日志。"""
    p = log_path()
    try:
        with open(p, "rb") as f:
            f.seek(since)
            data = f.read()
        return data.decode("utf-8", "replace"), since + len(data)
    except OSError:
        return "", since
