# -*- coding: utf-8 -*-
"""全局快捷键（RegisterHotKey）。被别的程序占用时注册失败，只打印一行，不影响其它功能。"""
import ctypes
import ctypes.wintypes

from PySide6.QtWidgets import QWidget

WM_HOTKEY = 0x0312
MODS = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "`": 0xC0, "backquote": 0xC0,
              "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}


def parse(combo: str):
    """'Ctrl+Alt+D' → (modifiers, vk)；看不懂返回 None。"""
    if not combo:
        return None
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        return None
    return mods, vk


class HotkeyManager(QWidget):
    """隐藏的小窗口，只用来接收 WM_HOTKEY。"""

    def __init__(self):
        super().__init__()
        self._callbacks = {}
        self._next_id = 1
        self.winId()  # 确保有 HWND

    def register(self, combo: str, callback) -> bool:
        spec = parse(combo)
        if spec is None:
            print(f"快捷键格式不对：{combo}")
            return False
        hid = self._next_id
        self._next_id += 1
        ok = ctypes.windll.user32.RegisterHotKey(int(self.winId()), hid,
                                                 spec[0] | MOD_NOREPEAT, spec[1])
        if not ok:
            print(f"快捷键 {combo} 注册失败（可能被别的程序占用）")
            return False
        self._callbacks[hid] = callback
        return True

    def unregister_all(self):
        for hid in list(self._callbacks):
            ctypes.windll.user32.UnregisterHotKey(int(self.winId()), hid)
        self._callbacks.clear()

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam in self._callbacks:
                self._callbacks[msg.wParam]()
                return True, 0
        return False, 0
