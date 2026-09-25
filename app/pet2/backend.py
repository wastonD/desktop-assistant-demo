# -*- coding: utf-8 -*-
"""显示后端：把"一帧 + 位置"一次交给系统。

Windows：Win32LayeredWindow —— ctypes 自建 WS_EX_LAYERED 原生窗口，每帧一次
  UpdateLayeredWindow(hwnd, 屏幕DC, &位置, &尺寸, 内存DC, &(0,0), 0, &混合, ULW_ALPHA)。
  位置和内容在同一个调用里生效（调研 b1/b2：Qt 的 move() 会先 MoveWindow、内容稍后才提交，所以会闪）。
  - 不用 setMask/SetWindowRgn：分层窗口按 alpha 命中，alpha=0 的像素鼠标自动穿透；
  - 只移动不换内容：ULW 只传 pptDst（内容由系统保留，不重画）；
  - **所有者 = 任务栏（Shell_TrayWnd）**：被拥有的窗口永远在所有者之上，explorer 每次把任务栏
    重新 HWND_TOPMOST 时，系统在同一次操作里把我们一起带上去——没有"先被盖住、再抬回来"的那一两帧
    （真机实测：普通置顶窗口每次点任务栏被盖 1~2 帧，被拥有的窗口 849 帧 0 帧被盖，开始菜单打开时也在前面）。
    全屏应用时任务栏沉下去，她也跟着沉下去，不会挡游戏/视频。
  - **窗口在独立线程里**：跨线程的所有者关系会把两个线程的输入队列绑在一起（Raymond Chen《Is it legal to have
    a cross-process parent/child or owner/owned window relationship?》），窗口线程一卡，任务栏的鼠标也跟着卡。
    所以窗口线程只做"收帧 → ULW"和"转发鼠标事件"，永远不被 Qt 那边建面板、读文件之类的活阻塞；
    Qt 线程提交帧只是 PostThreadMessage，不等待。
  - explorer 重启会连带销毁被拥有的窗口：窗口线程每 0.5 秒找新任务栏，找到就重建，并补交最后一帧。
其他平台：QtFallbackWindow（只供开发/测试，不保证原子）。

鼠标回调 handler(kind, x, y)：kind ∈ down/move/up/menu/leave/capture_lost/display，x/y = 屏幕物理像素。
Windows 后端的回调由窗口线程经 Qt 的排队信号切回主线程执行。
"""
import sys


class BackendBase:
    """接口说明（也是测试里 FakeBackend 要实现的）。"""
    hwnd = 0

    def set_handler(self, fn):
        self._handler = fn

    def commit(self, data, w: int, h: int, x: int, y: int) -> bool:
        """一次提交：data = 预乘 BGRA（bytes 或可写 memoryview，stride = 4w，行从上到下）。"""
        raise NotImplementedError

    def move(self, x: int, y: int) -> bool:
        raise NotImplementedError

    def show(self):
        raise NotImplementedError

    def hide(self):
        raise NotImplementedError

    def close(self):
        pass


def _emit(self, kind, x, y):
    fn = getattr(self, "_handler", None)
    if fn is not None:
        try:
            fn(kind, x, y)
        except Exception as e:  # 回调里的异常绝不能冒到窗口过程外面
            import traceback
            print("宠物窗口事件处理出错：", e)
            traceback.print_exc()


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt
    import threading

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                    ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 1)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class TRACKMOUSEEVENT(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("dwFlags", wt.DWORD), ("hwndTrack", wt.HWND),
                    ("dwHoverTime", wt.DWORD)]

    # 64 位下句柄/指针参数不声明会被截断（测内存时踩过）：全部写 argtypes/restype
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wt.ATOM
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                       wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                           ctypes.POINTER(wt.SIZE), wt.HDC,
                                           ctypes.POINTER(wt.POINT), wt.COLORREF,
                                           ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
    user32.UpdateLayeredWindow.restype = wt.BOOL
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPVOID]
    user32.LoadCursorW.restype = wt.HANDLE
    user32.SetCapture.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindowAsync.argtypes = [wt.HWND, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindow.argtypes = [wt.HWND]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wt.UINT]
    user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
    user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowW.restype = wt.HWND
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetMessagePos.restype = wt.DWORD
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetCurrentThreadId.restype = wt.DWORD

    WS_POPUP = 0x80000000
    WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = (
        0x00080000, 0x00000080, 0x00000008, 0x08000000)
    ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 0x2, 0x0, 0x1
    WM_DESTROY, WM_SETTINGCHANGE, WM_MOUSEACTIVATE, WM_DISPLAYCHANGE = 0x0002, 0x001A, 0x0021, 0x007E
    WM_TIMER = 0x0113
    WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP = 0x0200, 0x0201, 0x0202, 0x0205
    WM_CAPTURECHANGED, WM_MOUSELEAVE, WM_DPICHANGED = 0x0215, 0x02A3, 0x02E0
    WM_APP_FLUSH, WM_APP_SHOW, WM_APP_QUIT = 0x8001, 0x8002, 0x8003
    MA_NOACTIVATE = 3
    TME_LEAVE = 0x2
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
    IDC_ARROW = 32512
    _CLASS = "AssistantPetV2"

    def _signed_xy(lp):
        return ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value

    def _qt_relay(fn):
        """窗口线程 → Qt 主线程的排队转发器（Qt 没加载时直接调用，给纯脚本用）。"""
        try:
            from PySide6.QtCore import QObject, Qt, Signal
        except ImportError:
            return fn, None

        class _Relay(QObject):
            sig = Signal(str, int, int)

        relay = _Relay()
        relay.sig.connect(fn, Qt.QueuedConnection)
        return relay.sig.emit, relay

    class Win32LayeredWindow(BackendBase):
        def __init__(self, title="小银", owner_taskbar=True):
            self._title = title
            self._owner_taskbar = owner_taskbar
            self._handler = None
            self._emit_to_qt, self._relay = _qt_relay(self._dispatch)
            self.hwnd = 0
            self._pos = (0, 0)            # 窗口线程维护：当前真实位置
            self._size = (0, 0)
            self._tracking = False
            self._captured = False
            self._want_visible = False
            self._closing = False
            self._lock = threading.Lock()
            self._pending = None          # ("commit", data, w, h, x, y) 或 ("move", x, y)：只留最新一个
            self._last = None             # 最后一次提交的整帧，重建窗口后补交
            self._flush_posted = False
            self._proc = WNDPROC(self._wndproc)          # 必须保持引用，否则回调被回收
            self._hinst = kernel32.GetModuleHandleW(None)
            self._cls = f"{_CLASS}_{id(self)}"
            self._ready = threading.Event()
            self._error = None
            self._thread = threading.Thread(target=self._run, daemon=True, name="pet2-window")
            self._thread.start()
            self._ready.wait(5)
            if self._error:
                raise self._error
            if not self._ready.is_set():
                raise OSError("宠物窗口线程没有按时建好窗口")

        # ---- Qt 线程调用：只登记 + 投递，不等待
        def set_handler(self, fn):
            self._handler = fn

        def _dispatch(self, kind, x, y):
            _emit(self, kind, x, y)

        def commit(self, data, w, h, x, y):
            if not isinstance(data, bytes):     # memoryview（QImage.bits()）下一帧会被覆盖：拷一份
                data = bytes(memoryview(data)[:w * h * 4])
            with self._lock:
                self._pending = ("commit", data, w, h, x, y)
            self._post_flush()
            return True

        def move(self, x, y):
            with self._lock:
                p = self._pending
                if p is not None and p[0] == "commit":     # 还没提交的帧：直接改它的位置
                    self._pending = p[:4] + (x, y)
                else:
                    self._pending = ("move", x, y)
            self._post_flush()
            return True

        def _post_flush(self):
            with self._lock:
                if self._flush_posted:
                    return
                self._flush_posted = True
            user32.PostThreadMessageW(self._tid, WM_APP_FLUSH, 0, 0)

        def show(self):
            self._want_visible = True
            user32.PostThreadMessageW(self._tid, WM_APP_SHOW, 1, 0)

        def hide(self):
            self._want_visible = False
            user32.PostThreadMessageW(self._tid, WM_APP_SHOW, 0, 0)

        def is_visible(self):
            return bool(self.hwnd and user32.IsWindowVisible(self.hwnd))

        def close(self):
            if self._closing:
                return
            self._closing = True
            user32.PostThreadMessageW(self._tid, WM_APP_QUIT, 0, 0)
            self._thread.join(2)

        # ---- 窗口线程
        def _run(self):
            try:
                self._tid = kernel32.GetCurrentThreadId()
                wc = WNDCLASSEXW()
                wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
                wc.lpfnWndProc = self._proc
                wc.hInstance = self._hinst
                wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
                wc.lpszClassName = self._cls
                self._wc = wc
                if not user32.RegisterClassExW(ctypes.byref(wc)):
                    raise OSError("RegisterClassExW 失败")
                self._screen_dc = user32.GetDC(None)
                self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
                self._bmp = None
                self._old = None
                self._bits = ctypes.c_void_p()
                self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
                self._create_window()
                # 线程消息队列此刻已存在（GetMessage 之前的 Peek 会建好），之后 Post 不会丢
                msg = wt.MSG()
                user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
                user32.SetTimer(None, 0, 500, None)   # 线程定时器：窗口被连带销毁后找新任务栏重建
            except OSError as e:
                self._error = e
                self._ready.set()
                return
            self._ready.set()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if not msg.hWnd:
                    m = msg.message
                    if m == WM_APP_FLUSH:
                        self._flush()
                        continue
                    if m == WM_APP_SHOW:
                        if self.hwnd:
                            user32.ShowWindow(self.hwnd, 4 if msg.wParam else 0)   # SW_SHOWNOACTIVATE / SW_HIDE
                        continue
                    if m == WM_APP_QUIT:
                        break
                    if m == WM_TIMER:
                        self._maybe_recreate()
                        continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            self._teardown()

        def _create_window(self):
            owner = user32.FindWindowW("Shell_TrayWnd", None) if self._owner_taskbar else None
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                self._cls, self._title, WS_POPUP, 0, 0, 1, 1, owner, None, self._hinst, None)
            if not hwnd:
                raise OSError("CreateWindowExW 失败")
            self.hwnd = hwnd
            self._owner = owner
            self._tracking = False
            self._captured = False

        def _maybe_recreate(self):
            if self._closing:
                return
            if self.hwnd and user32.IsWindow(self.hwnd):
                if not (self._owner_taskbar and not self._owner):
                    return
                if not user32.FindWindowW("Shell_TrayWnd", None):
                    return
                user32.DestroyWindow(self.hwnd)      # 启动时还没有任务栏：等它出现后换成被它拥有
            elif self._owner_taskbar and not user32.FindWindowW("Shell_TrayWnd", None):
                return                               # explorer 还没起来，下次再看
            try:
                self._create_window()
            except OSError as e:
                print("宠物窗口重建失败：", e)
                return
            print("宠物窗口：任务栏重建了，窗口已跟着重建")
            with self._lock:
                if self._pending is None and self._last is not None:
                    self._pending = self._last
            self._size = (0, 0)                      # 新窗口要带尺寸完整提交一次
            self._flush()
            if self._want_visible:
                user32.ShowWindow(self.hwnd, 4)
            self._emit_to_qt("display", 0, 0)       # 任务栏几何可能变了：让 Qt 那边重新布局

        def _ensure_dib(self, w, h):
            if self._bmp and self._dib_size == (w, h):
                return
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h            # 负数 = 行从上到下，和帧字节一致
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bits = ctypes.c_void_p()
            bmp = gdi32.CreateDIBSection(self._mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            if not bmp:
                raise OSError("CreateDIBSection 失败")
            old = gdi32.SelectObject(self._mem_dc, bmp)
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
            else:
                self._old = old
            self._bmp, self._bits, self._dib_size = bmp, bits, (w, h)

        def _flush(self):
            with self._lock:
                p, self._pending = self._pending, None
                self._flush_posted = False
            if p is None or not self.hwnd:
                return
            if p[0] == "move":
                x, y = p[1], p[2]
                if (x, y) == self._pos:
                    return
                pt = wt.POINT(x, y)
                ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pt), None, None, None,
                                                0, None, 0)
                if not ok:  # 个别环境不接受只带位置的 ULW：退回 SetWindowPos（内容由系统保留，不会闪）
                    ok = user32.SetWindowPos(self.hwnd, None, x, y, 0, 0,
                                             SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
                if ok:
                    self._pos = (x, y)
                    if self._last is not None:
                        self._last = self._last[:4] + (x, y)
                return
            _, data, w, h, x, y = p
            try:
                self._ensure_dib(w, h)
            except OSError as e:
                print("宠物窗口：", e)
                return
            ctypes.memmove(self._bits, data, w * h * 4)
            pt, sz, src = wt.POINT(x, y), wt.SIZE(w, h), wt.POINT(0, 0)
            ok = user32.UpdateLayeredWindow(self.hwnd, self._screen_dc, ctypes.byref(pt),
                                            ctypes.byref(sz), self._mem_dc, ctypes.byref(src), 0,
                                            ctypes.byref(self._blend), ULW_ALPHA)
            if ok:
                self._pos, self._size = (x, y), (w, h)
                self._last = p

        def _teardown(self):
            if self.hwnd and user32.IsWindow(self.hwnd):
                user32.DestroyWindow(self.hwnd)
            self.hwnd = 0
            if self._bmp:
                gdi32.SelectObject(self._mem_dc, self._old)
                gdi32.DeleteObject(self._bmp)
                self._bmp = None
            if self._mem_dc:
                gdi32.DeleteDC(self._mem_dc)
                self._mem_dc = None
            if self._screen_dc:
                user32.ReleaseDC(None, self._screen_dc)
                self._screen_dc = None
            user32.UnregisterClassW(self._cls, self._hinst)

        # ---- 窗口过程（窗口线程）
        def _wndproc(self, hwnd, msg, wp, lp):
            try:
                if msg == WM_MOUSEACTIVATE:
                    return MA_NOACTIVATE           # 点宠物不抢当前窗口的焦点
                if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP):
                    # 用消息产生那一刻的屏幕坐标：lParam 是相对窗口的，拖动时窗口一直在动，
                    # "窗口现在的位置 + 当时的相对坐标"会错位并正反馈（真机：拖 300px 最后漂了 -30px）
                    sx, sy = _signed_xy(user32.GetMessagePos())
                    if msg == WM_MOUSEMOVE:
                        if not self._tracking:
                            tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                            self._tracking = bool(user32.TrackMouseEvent(ctypes.byref(tme)))
                        self._emit_to_qt("move", sx, sy)
                    elif msg == WM_LBUTTONDOWN:
                        user32.SetCapture(hwnd)
                        self._captured = True
                        self._emit_to_qt("down", sx, sy)
                    elif msg == WM_LBUTTONUP:
                        self._captured = False
                        user32.ReleaseCapture()
                        self._emit_to_qt("up", sx, sy)
                    else:
                        self._emit_to_qt("menu", sx, sy)
                    return 0
                if msg == WM_MOUSELEAVE:
                    self._tracking = False
                    self._emit_to_qt("leave", 0, 0)
                    return 0
                if msg == WM_CAPTURECHANGED:
                    if self._captured:             # 被别人抢走了捕获（比如弹出了系统对话框）
                        self._captured = False
                        self._emit_to_qt("capture_lost", 0, 0)
                    return 0
                if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED, WM_SETTINGCHANGE):
                    self._emit_to_qt("display", 0, 0)
                if msg == WM_DESTROY and hwnd == self.hwnd:
                    self.hwnd = 0                  # 被任务栏连带销毁（explorer 重启）或自己关：定时器会重建
            except Exception as e:
                print("宠物窗口过程出错：", e)
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    NativeBackend = Win32LayeredWindow
else:
    NativeBackend = None


class QtFallbackWindow(BackendBase):
    """非 Windows 平台的退化实现（Linux 开发机、offscreen 测试）。不保证位置和内容原子提交。"""

    def __init__(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QWidget

        backend = self

        class _W(QWidget):
            def paintEvent(self, _):
                if backend._img is not None:
                    p = QPainter(self)
                    p.setCompositionMode(QPainter.CompositionMode_Source)
                    p.drawImage(self.rect(), backend._img)

            def _ev(self, kind, e):
                g = e.globalPosition()
                dpr = self.devicePixelRatioF()
                _emit(backend, kind, round(g.x() * dpr), round(g.y() * dpr))

            def mousePressEvent(self, e):
                self._ev("down" if e.button() == Qt.LeftButton else "rdown", e)

            def mouseMoveEvent(self, e):
                self._ev("move", e)

            def mouseReleaseEvent(self, e):
                self._ev("up" if e.button() == Qt.LeftButton else "menu", e)

            def leaveEvent(self, _):
                _emit(backend, "leave", 0, 0)

        self._QImage = QImage
        self._img = None
        self._handler = None
        self.w = _W(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.w.setAttribute(Qt.WA_TranslucentBackground)
        self.w.setAttribute(Qt.WA_ShowWithoutActivating)
        self.w.setMouseTracking(True)
        self.hwnd = 0
        self._pos = (0, 0)

    def commit(self, data, w, h, x, y):
        img = self._QImage(bytes(data[:w * h * 4]), w, h, w * 4, self._QImage.Format_ARGB32_Premultiplied)
        self._img = img.copy()
        dpr = self.w.devicePixelRatioF() or 1.0
        self.w.setGeometry(round(x / dpr), round(y / dpr), round(w / dpr), round(h / dpr))
        self._pos = (x, y)
        self.w.update()
        return True

    def move(self, x, y):
        dpr = self.w.devicePixelRatioF() or 1.0
        self.w.move(round(x / dpr), round(y / dpr))
        self._pos = (x, y)
        return True

    def show(self):
        self.w.show()

    def hide(self):
        self.w.hide()

    def is_visible(self):
        return self.w.isVisible()

    def close(self):
        self.w.close()


def create_backend() -> BackendBase:
    if NativeBackend is not None:
        return NativeBackend()
    return QtFallbackWindow()
