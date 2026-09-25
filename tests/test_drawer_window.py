# -*- coding: utf-8 -*-
"""抽屉窗口 + 前板把手（offscreen）：把手永不消失、随抽屉 1:1 移动、点击由弹簧拉开/推回。

沙盒：演示桌面/收纳夹/记录文件全部指到临时目录，绝不碰真实桌面。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class DrawerHandleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from app.drawer import core
        tmp = Path(tempfile.mkdtemp())
        (tmp / "Desktop").mkdir()
        env = mock.patch.dict(os.environ, {"ASSISTANT_DESKTOP_DIR": str(tmp / "Desktop"),
                                           "ASSISTANT_PUBLIC_DESKTOP_DIR": str(tmp / "Pub"),
                                           "ASSISTANT_STASH_DIR": str(tmp / "stash")})
        env.start()
        self.addCleanup(env.stop)
        for attr, value in (("MANIFEST_PATH", tmp / "drawer.json"), ("OPS_PATH", tmp / "ops.json"),
                            ("TAGS_PATH", tmp / "tags.json"), ("TAG_OPS_PATH", tmp / "tag_ops.json"),
                            ("CONFIG_PATH", tmp / "cfg.json")):
            p = mock.patch.object(core, attr, value)
            p.start()
            self.addCleanup(p.stop)
        from app import desktop_clean
        self.icons = {"hidden": False}
        for name, fn in (("all_icons_hidden", lambda: self.icons["hidden"]),
                         ("set_all_icons_hidden", lambda on: self.icons.update(hidden=bool(on)))):
            p = mock.patch.object(desktop_clean, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.desk = tmp / "Desktop"
        from app.drawer import window as W
        p = mock.patch.object(W, "ICON_DIR", tmp / "icons")
        p.start()
        self.addCleanup(p.stop)
        self.W = W
        self.drawer = W.DrawerWindow()
        self.handle = W.DrawerHandle(self.drawer)
        self.handle.show()
        self.addCleanup(self.handle.close)
        self.addCleanup(self.drawer.close)

    def settle(self, limit=3.0):
        d = self.drawer
        t_end = time.monotonic() + limit
        while d._mtimer.isActive() and time.monotonic() < t_end:
            d._t_motion -= 1 / 60              # 不等真实时间：每次推进一帧
            d._step_motion()

    def top(self):
        from PySide6.QtWidgets import QApplication
        return QApplication.primaryScreen().geometry().y()

    def test_click_handle_opens_and_handle_stays_attached(self):
        d, h = self.drawer, self.handle
        self.assertEqual(h.y(), self.top())
        d.grab(5.0)
        d.release(6.0)                          # 原地点一下
        self.assertTrue(h.isVisible())          # 第三轮这里把手被藏掉了
        self.settle()
        self.assertTrue(d.is_open())
        self.assertEqual(d.y(), d._open_y())
        self.assertEqual(h.y(), d.y() + d._card_bottom())   # 挂在抽屉底边
        self.assertTrue(h.isVisible())
        d.grab(h.y() + 3.0)                     # 再点一下：推回去
        d.release(h.y() + 3.0)
        self.settle()
        self.assertFalse(d.is_out())
        self.assertEqual(d.y(), d._closed_y())   # 停在屏幕上沿外面（不隐藏，下次打开直接滑）
        self.assertEqual(h.y(), self.top())
        self.assertTrue(h.isVisible())

    def test_drag_follows_one_to_one(self):
        d, h = self.drawer, self.handle
        d.grab(0.0)
        d.drag_to(150.0)
        self.assertEqual(d.y(), d._closed_y() + 150)
        self.assertEqual(h.y(), self.top() + 150)
        d.drag_to(260.0)
        self.assertEqual(h.y() - self.top(), 260)
        d.release(260.0)
        self.settle()
        self.assertIn(d.is_open(), (True, False))   # 松手按位置/速度决定，不卡在半路
        self.assertIn(d.y(), (d._open_y(), d._closed_y()))

    def test_keyboard_toggle_uses_same_motion(self):
        d = self.drawer
        d.open_drawer()
        self.settle()
        self.assertTrue(d.is_open())
        d.toggle()
        self.settle()
        self.assertFalse(d.is_out())

    def test_parked_open_is_instant_and_activates_at_end(self):
        """停在屏幕外：打开不重扫、不重画，第一帧立即动；滑到位才激活（激活会整窗重画）。"""
        d = self.drawer
        d.park()
        self.assertTrue(d.isVisible() and not d.is_out())
        with mock.patch.object(type(d), "refresh") as rf, \
                mock.patch.object(type(d), "activateWindow") as act:
            d.open_drawer()
            rf.assert_not_called()
            act.assert_not_called()
            self.settle()
            act.assert_called_once()
        self.assertTrue(d.is_open())
        self.assertLessEqual(d._frame_ms(), 16)     # 按屏幕刷新率推进（高刷屏 < 16ms）

    def test_autoclose_suspended_while_grabbing(self):
        d = self.drawer
        d.grab(0.0)
        self.assertGreater(d._suspend, 0)
        d.release(0.0)
        self.assertEqual(d._suspend, 0)

    def test_drop_and_icon_toggle_never_move_files(self):
        """拖进分类只贴标签；"隐藏桌面图标"只拨系统开关——文件都留在桌面文件夹里。"""
        from app.drawer import core
        d = self.drawer
        a = self.desk / "a.txt"
        a.write_text("x", encoding="utf-8")
        d.refresh()
        d._on_group_drop([str(a)], core.FAVORITE)
        self.assertTrue(a.exists())
        self.assertEqual([i.group for i in d._items if i.name == "a.txt"], [core.FAVORITE])
        d._toggle_desktop_icons()
        self.assertTrue(self.icons["hidden"])
        self.assertEqual(d.stash_btn.text(), "显示桌面图标")
        self.assertEqual(sorted(p.name for p in self.desk.iterdir()), ["a.txt"])
        d._undo()
        self.assertEqual([i.group for i in d._items if i.name == "a.txt"], ["文档"])

    def test_drag_out_is_copy_or_link_not_move(self):
        from PySide6.QtCore import Qt
        acts = self.drawer.model.supportedDragActions()
        self.assertFalse(acts & Qt.MoveAction)


if __name__ == "__main__":
    unittest.main()
