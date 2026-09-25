# -*- coding: utf-8 -*-
"""置顶守护：只在真的被压住时才抬一次（v1 是每 500ms 无条件给 5 个窗口 SetWindowPos，还会丢事件）。

- 触发：前台变化（EVENT_SYSTEM_FOREGROUND）、explorer 的重排（EVENT_OBJECT_REORDER）只"打标记"，
  Coalescer 把 16ms 内的多次事件合并成一次检查（尾沿合并，不丢）；另有 1.5 秒低频兜底；
- 检查：沿 GetWindow(GW_HWNDPREV) 往上走 z 序链，上面有"可见、置顶、和我们相交、不是自己进程"的窗口
  （任务栏、别人的置顶窗）才 SetWindowPos(HWND_TOP) 一次；
- 开始菜单打开时任务栏在更高的 band，普通进程怎么抬都没用（调研 c 节），抬了也白抬——needs_raise 仍会
  返回 True，但 Coalescer 的最小间隔（默认 250ms）保证不会疯狂重试。
纯逻辑部分（Coalescer、needs_raise）不依赖 Windows，可测。
"""
import sys

from .layout import rects_intersect


class Coalescer:
    """事件合并器：mark() 只记"有事"；due(now) 到点（合并窗口过了、离上次执行够久）才返回 True。"""

    def __init__(self, window: float = 0.016, min_gap: float = 0.25, fallback: float = 1.5):
        self.window, self.min_gap, self.fallback = window, min_gap, fallback
        self._marked_at = None
        self._last_run = -1e9

    def mark(self, now: float):
        if self._marked_at is None:
            self._marked_at = now

    @property
    def pending(self) -> bool:
        return self._marked_at is not None

    def due(self, now: float) -> bool:
        if self._marked_at is not None:
            return (now - self._marked_at >= self.window and now - self._last_run >= self.min_gap)
        return now - self._last_run >= self.fallback

    def ran(self, now: float):
        self._marked_at = None
        self._last_run = now

    def next_check_in(self, now: float) -> float:
        """距离下一次该检查还有多少秒（给单次定时器用）。"""
        if self._marked_at is not None:
            return max(0.0, max(self._marked_at + self.window, self._last_run + self.min_gap) - now)
        return max(0.0, self._last_run + self.fallback - now)


def needs_raise(my_rect: tuple, above: list, own_pid: int) -> bool:
    """above = 我们上方（z 序更高）的窗口列表 [(rect, visible, topmost, pid)]。
    有可见、置顶、非本进程、和我们相交的窗口压着 → 需要抬。"""
    for rect, visible, topmost, pid in above:
        if visible and topmost and pid != own_pid and rects_intersect(rect, my_rect):
            return True
    return False


# ---------------------------------------------------------------- Windows 实现
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _u = ctypes.windll.user32
    GW_HWNDPREV = 3
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    HWND_TOP = 0
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x0001, 0x0002, 0x0010, 0x4000
    _u.GetWindow.restype = wt.HWND
    _u.GetWindow.argtypes = [wt.HWND, wt.UINT]
    _u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _u.IsWindowVisible.argtypes = [wt.HWND]
    _u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    _u.FindWindowW.restype = wt.HWND
    _u.UnhookWinEvent.argtypes = [wt.HANDLE]
    _u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]

    def windows_above(hwnd, limit: int = 64) -> list:
        out = []
        h = _u.GetWindow(hwnd, GW_HWNDPREV)
        r = wt.RECT()
        pid = wt.DWORD()
        while h and len(out) < limit:
            _u.GetWindowRect(h, ctypes.byref(r))
            _u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            ex = _u.GetWindowLongW(h, GWL_EXSTYLE)
            out.append(((r.left, r.top, r.right, r.bottom), bool(_u.IsWindowVisible(h)),
                        bool(ex & WS_EX_TOPMOST), pid.value))
            h = _u.GetWindow(h, GW_HWNDPREV)
        return out

    def taskbar_dropped() -> bool:
        """前台是全屏应用（游戏/视频/演示）时 explorer 会去掉任务栏的 TOPMOST，退出全屏再加回来
        （真机实测）。宠物窗口虽被任务栏拥有，explorer 这次降级不会带上她，所以要自己跟着藏。"""
        tray = _u.FindWindowW("Shell_TrayWnd", None)
        return bool(tray) and not (_u.GetWindowLongW(tray, GWL_EXSTYLE) & WS_EX_TOPMOST)

    def raise_top(hwnd):
        # 宠物窗口在自己的线程里：异步投递，不等它处理（也就不会被它的线程卡住）
        _u.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    class WinEventHooks:
        """前台变化（全局）+ explorer 的重排：回调里只调 on_event()（打标记），不做任何重活。"""

        def __init__(self, on_event):
            proc_t = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG,
                                        wt.DWORD, wt.DWORD)
            self._proc = proc_t(lambda *a: on_event())
            _u.SetWinEventHook.restype = wt.HANDLE
            self._hooks = [_u.SetWinEventHook(0x0003, 0x0003, 0, self._proc, 0, 0, 0)]
            tray = _u.FindWindowW("Shell_TrayWnd", None)
            if tray:
                pid = wt.DWORD()
                _u.GetWindowThreadProcessId(tray, ctypes.byref(pid))
                self._hooks.append(_u.SetWinEventHook(0x8004, 0x8004, 0, self._proc, pid.value, 0, 0))

        def close(self):
            for h in self._hooks:
                if h:
                    _u.UnhookWinEvent(h)
            self._hooks = []
else:
    def windows_above(hwnd, limit: int = 64) -> list:
        return []

    def taskbar_dropped() -> bool:
        return False

    def raise_top(hwnd):
        pass

    class WinEventHooks:
        def __init__(self, on_event):
            pass

        def close(self):
            pass
