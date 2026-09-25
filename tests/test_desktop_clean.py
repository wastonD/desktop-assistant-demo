# -*- coding: utf-8 -*-
"""app/desktop_clean.py：任务栏三档与桌面图标开关（全部用假的系统接口，绝不改真实设置）。"""
import unittest
from unittest import mock

from app import desktop_clean as dc


class FakeApi:
    def __init__(self, autohide=False):
        self.autohide = autohide
        self.visible = {1: True, 2: True}
        self.reg = {}
        self.toggles = 0
        self.refreshed = 0

    def get_autohide(self):
        return self.autohide

    def set_autohide(self, on):
        self.autohide = on

    def tray_windows(self):
        return [1, 2]

    def show_window(self, h, show):
        self.visible[h] = show

    def is_visible(self, h):
        return self.visible[h]

    def read_dword(self, key, name, default=0):
        return self.reg.get((key, name), default)

    def write_dword(self, key, name, value):
        self.reg[(key, name)] = value

    def refresh_desktop(self):
        self.refreshed += 1

    def toggle_all_icons(self):
        self.toggles += 1
        cur = self.reg.get((dc.ADVANCED_KEY, "HideIcons"), 0)
        self.reg[(dc.ADVANCED_KEY, "HideIcons")] = 0 if cur else 1


class TaskbarTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi(autohide=False)
        p = mock.patch.object(dc, "_api", self.api)
        p.start()
        self.addCleanup(p.stop)

    def test_hidden_then_normal_restores_original(self):
        cfg = {}
        tb = dc.Taskbar(cfg)
        tb.apply("hidden")
        self.assertTrue(self.api.autohide)
        self.assertEqual(self.api.visible, {1: False, 2: False})
        self.assertEqual(cfg["taskbar"], "hidden")
        tb.apply("normal")
        self.assertFalse(self.api.autohide)          # 回到开启前（原来没自动隐藏）
        self.assertEqual(self.api.visible, {1: True, 2: True})
        self.assertNotIn("taskbar_orig_autohide", cfg)

    def test_user_originally_had_autohide(self):
        self.api.autohide = True
        tb = dc.Taskbar({})
        tb.apply("hidden")
        tb.apply("normal")
        self.assertTrue(self.api.autohide)

    def test_restore_on_exit_keeps_choice(self):
        cfg = {}
        tb = dc.Taskbar(cfg)
        tb.apply("hidden")
        tb.restore_on_exit()
        self.assertEqual(self.api.visible, {1: True, 2: True})
        self.assertFalse(self.api.autohide)
        self.assertEqual(cfg["taskbar"], "hidden")   # 下次启动还会隐藏

    def test_enforce_and_peek(self):
        tb = dc.Taskbar({})
        tb.apply("hidden")
        self.api.visible[1] = True                   # 资源管理器自己又显示了
        tb.enforce()
        self.assertFalse(self.api.visible[1])
        tb.peek(True)
        tb.enforce()                                 # 临时呼出期间不强制隐藏
        self.assertTrue(self.api.visible[1])
        tb.peek(False)
        self.assertFalse(self.api.visible[1])

    def test_autohide_mode(self):
        tb = dc.Taskbar({})
        tb.apply("autohide")
        self.assertTrue(self.api.autohide)
        self.assertEqual(self.api.visible, {1: True, 2: True})


class DesktopIconTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        p = mock.patch.object(dc, "_api", self.api)
        p.start()
        self.addCleanup(p.stop)

    def test_system_icons(self):
        self.assertFalse(dc.system_icons_hidden())
        dc.set_system_icons_hidden(True)
        self.assertTrue(dc.system_icons_hidden())
        self.assertEqual(self.api.refreshed, 1)
        dc.set_system_icons_hidden(False)
        self.assertFalse(dc.system_icons_hidden())

    def test_all_icons_toggle_is_idempotent(self):
        dc.set_all_icons_hidden(True)
        dc.set_all_icons_hidden(True)
        self.assertEqual(self.api.toggles, 1)
        self.assertTrue(dc.all_icons_hidden())
        dc.set_all_icons_hidden(False)
        self.assertFalse(dc.all_icons_hidden())


if __name__ == "__main__":
    unittest.main()
