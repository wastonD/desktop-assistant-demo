# -*- coding: utf-8 -*-
"""app/taskbar_style.py：TranslucentTB 联动、旧任务栏 accent、备份/还原（全部假接口 + 临时目录，绝不碰真实系统）。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import taskbar_style as ts

TTB_TEXT = """{
    // 这是 TranslucentTB 自带的注释
    "$schema": "https://TranslucentTB.github.io/settings.schema.json",
    "desktop_appearance": {"accent": "normal", "color": "#00000000", "show_peek": true},
    "visible_window_appearance": {"enabled": false, "accent": "acrylic", "color": "#00000000"},
    "maximised_window_appearance": {"enabled": true, "accent": "opaque", "color": "#000000FF"},
    "hide_tray": false
}
"""


class FakeApi:
    def __init__(self, root: Path, build=26200, installed=True, running=True, settings=True):
        self.root = root
        self._build = build
        self.files = {}
        self.running = running
        self.launched = []
        self.accents = []
        self.pkg = root / "Local" / "Packages" / ts.TTB_PFN
        if installed:
            self.files[str(self.pkg)] = None
            if settings:
                self.files[str(self.pkg / "RoamingState" / "settings.json")] = TTB_TEXT

    def build(self):
        return self._build

    def local_appdata(self):
        return self.root / "Local"

    def exists(self, p):
        return str(p) in self.files

    def read_text(self, p):
        return self.files[str(p)]

    def write_text_atomic(self, p, text):
        self.files[str(p)] = text

    def remove(self, p):
        self.files.pop(str(p), None)

    def window_exists(self, cls):
        return self.running and cls == ts.TTB_WINDOW_CLASS

    def launch(self, target):
        self.launched.append(target)

    def tray_windows(self):
        return [11, 12]

    def set_accent(self, hwnd, state, color):
        self.accents.append((hwnd, state, color))
        return True


class FakeTap:
    """内置组件的假接口：记录注入请求。**测试绝不能碰真实 explorer**（真的 available() 在本机是 True）。"""

    def __init__(self, available=False):
        self._available = available
        self.calls = []
        self.pid = 4242

    def available(self):
        return self._available

    def explorer_pid(self):
        return self.pid

    def inject(self, mode):
        self.calls.append(mode)
        return True, ""


class _Base(unittest.TestCase):
    def use(self, builtin=False, **kw):
        self.tmp = Path(tempfile.mkdtemp())
        self.api = FakeApi(self.tmp, **kw)
        self.tap = FakeTap(builtin)
        for name, value in (("_api", self.api), ("_tap", self.tap)):
            p = mock.patch.object(ts, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = {}
        self.style = ts.TaskbarStyle(self.cfg, backup_path=self.tmp / "backup.json")
        self.settings = str(self.api.pkg / "RoamingState" / "settings.json")


_module_patch = None


def setUpModule():
    """整个模块默认用假组件：哪条测试忘了 use() 也不会注入真实 explorer。"""
    global _module_patch
    _module_patch = mock.patch.object(ts, "_tap", FakeTap(False))
    _module_patch.start()


def tearDownModule():
    _module_patch.stop()


class BuiltinTapTests(_Base):
    """没装 TranslucentTB 时，"全透明"由内置组件实现（假组件，只记录请求）。"""

    def wait_calls(self, n):
        import time
        end = time.monotonic() + 3
        while len(self.tap.calls) < n and time.monotonic() < end:
            time.sleep(0.01)
        return self.tap.calls

    def test_clear_uses_builtin_and_restore(self):
        self.use(builtin=True, installed=False, running=False)
        st = ts.detect()
        self.assertTrue(st.mode_usable("clear"))
        self.assertFalse(st.mode_usable("blur"))
        ok, msg = self.style.apply("clear")
        self.assertTrue(ok, msg)
        self.assertIn("内置", msg)
        self.assertEqual(self.wait_calls(1), ["clear"])
        self.assertTrue(self.cfg["taskbar_builtin"])
        self.assertEqual(self.api.files.get(self.settings), None)          # 没碰 TranslucentTB 的东西
        ok, _ = self.style.apply("normal")
        self.assertTrue(ok)
        self.assertEqual(self.tap.calls, ["clear", "restore"])
        self.assertNotIn("taskbar_builtin", self.cfg)

    def test_blur_still_needs_ttb(self):
        self.use(builtin=True, installed=False, running=False)
        ok, msg = self.style.apply("blur")
        self.assertFalse(ok)
        self.assertIn("TranslucentTB", msg)
        self.assertEqual(self.tap.calls, [])

    def test_ttb_installed_wins(self):
        self.use(builtin=True)
        ok, _ = self.style.apply("clear")
        self.assertTrue(ok)
        self.assertEqual(self.tap.calls, [])
        self.assertNotIn("taskbar_builtin", self.cfg)

    def test_reapply_after_explorer_restart_and_restore_on_exit(self):
        self.use(builtin=True, installed=False, running=False)
        self.style.apply("clear")
        self.wait_calls(1)
        self.style.enforce()                                   # explorer 没变：不重复注入
        self.assertEqual(self.tap.calls, ["clear"])
        self.tap.pid = 9999                                    # explorer 重启了
        self.style.enforce()
        self.assertEqual(self.wait_calls(2), ["clear", "clear"])
        self.style.restore_on_exit()
        self.assertEqual(self.tap.calls[-1], "restore")
        self.assertEqual(self.cfg["taskbar_style"], "clear")  # 下次启动还按用户的选择

    def test_menu_labels(self):
        from PySide6.QtWidgets import QApplication, QMenu
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication.instance() or QApplication([])
        self.use(builtin=True, installed=False, running=False)
        m = QMenu()
        ts.fill_menu(m, self.style)
        acts = {a.text(): a.isEnabled() for a in m.actions() if a.text()}
        self.assertTrue(acts["全透明（内置·实验）"])
        self.assertFalse(acts["模糊"])


class PureTests(unittest.TestCase):
    def test_strip_comments_keeps_urls(self):
        data = json.loads(ts.strip_json_comments(TTB_TEXT))
        self.assertEqual(data["$schema"], "https://TranslucentTB.github.io/settings.schema.json")

    def test_apply_mode_touches_only_needed_fields(self):
        data = json.loads(ts.strip_json_comments(TTB_TEXT))
        out = ts.apply_ttb_mode(data, "clear")
        self.assertEqual(out["desktop_appearance"], {"accent": "clear", "color": "#00000000",
                                                     "show_peek": True})
        self.assertEqual(out["visible_window_appearance"], data["visible_window_appearance"])  # 没启用就不动
        self.assertEqual(out["maximised_window_appearance"], data["maximised_window_appearance"])
        self.assertEqual(data["desktop_appearance"]["accent"], "normal")   # 原字典不被改
        data["visible_window_appearance"]["enabled"] = True
        out = ts.apply_ttb_mode(data, "acrylic")
        self.assertEqual(out["visible_window_appearance"]["accent"], "acrylic")

    def test_modes_complete(self):
        for m in ts.MODES:
            self.assertIn(m, ts.LABELS)
            self.assertIn(m, ts.ACCENT)
            if m != "normal":
                self.assertIn(m, ts.TTB_MODE)


class TranslucentTBTests(_Base):
    def test_detect(self):
        self.use()
        st = ts.detect(self.api)
        self.assertTrue(st.xaml_taskbar and st.ttb_installed and st.ttb_running and st.usable)
        self.assertEqual(str(st.ttb_settings), self.settings)

    def test_apply_backup_and_restore_exact(self):
        self.use()
        ok, msg = self.style.apply("clear")
        self.assertTrue(ok, msg)
        self.assertEqual(self.cfg["taskbar_style"], "clear")
        written = json.loads(self.api.files[self.settings])
        self.assertEqual(written["desktop_appearance"]["accent"], "clear")
        self.assertFalse(written["hide_tray"])
        ok, _ = self.style.apply("blur")                       # 再换一次：备份不能被覆盖
        backup = json.loads(self.api.files[str(self.tmp / "backup.json")])
        self.assertEqual(backup["text"], TTB_TEXT)
        self.style.apply("normal")
        self.assertEqual(self.api.files[self.settings], TTB_TEXT)   # 原样字节（含注释）
        self.assertNotIn(str(self.tmp / "backup.json"), self.api.files)
        self.assertEqual(self.cfg["taskbar_style"], "normal")
        self.assertEqual(self.api.accents, [])                 # XAML 任务栏上绝不调 SWCA

    def test_restore_on_exit_keeps_choice(self):
        self.use()
        self.style.apply("acrylic")
        self.style.restore_on_exit()
        self.assertEqual(self.api.files[self.settings], TTB_TEXT)
        self.assertEqual(self.cfg["taskbar_style"], "acrylic")
        self.cfg["taskbar_style_restore"] = False               # 用户选"退出时不还原"
        self.style.apply("acrylic")
        self.style.restore_on_exit()
        self.assertIn('"acrylic"', self.api.files[self.settings])

    def test_not_installed_touches_nothing(self):
        self.use(installed=False)
        before = dict(self.api.files)
        ok, msg = self.style.apply("clear")
        self.assertFalse(ok)
        self.assertIn("TranslucentTB", msg)
        self.assertEqual(self.api.files, before)
        self.assertNotIn("taskbar_style", self.cfg)

    def test_no_settings_file_yet(self):
        self.use(settings=False)
        ok, msg = self.style.apply("clear")
        self.assertFalse(ok)
        self.assertIn("先运行它一次", msg)

    def test_not_running_still_writes_and_says_so(self):
        self.use(running=False)
        ok, msg = self.style.apply("clear")
        self.assertTrue(ok)
        self.assertIn("没在运行", msg)
        self.assertEqual(self.api.launched, [])                 # 不擅自启动
        self.style.launch_ttb()
        self.assertEqual(self.api.launched, [f"shell:AppsFolder\\{ts.TTB_AUMID}"])

    def test_bad_json(self):
        self.use()
        self.api.files[self.settings] = "{ not json"
        ok, msg = self.style.apply("clear")
        self.assertFalse(ok)
        self.assertIn("读不了", msg)


class LegacyTaskbarTests(_Base):
    def test_accent_apply_enforce_restore(self):
        self.use(build=19045, installed=False)
        ok, _ = self.style.apply("clear")
        self.assertTrue(ok)
        self.assertEqual(self.api.accents, [(11, 2, 0), (12, 2, 0)])
        self.style.enforce()                                    # explorer 改回去了：重设
        self.assertEqual(len(self.api.accents), 4)
        self.style.apply("normal")
        self.assertEqual(self.api.accents[-2:], [(11, 0, 0), (12, 0, 0)])

    def test_enforce_noop_on_xaml(self):
        self.use()
        self.cfg["taskbar_style"] = "clear"
        self.style.enforce()
        self.assertEqual(self.api.accents, [])


class MenuTests(_Base):
    def test_fill_menu(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMenu
        app = QApplication.instance() or QApplication([])
        self.use(installed=False)
        msgs = []
        m = QMenu()
        ts.fill_menu(m, self.style, notify=msgs.append)
        texts = [a.text() for a in m.actions()]
        self.assertIn("全透明", texts)
        self.assertTrue(any("微软商店" in t for t in texts))
        clear = next(a for a in m.actions() if a.text() == "全透明")
        self.assertFalse(clear.isEnabled())                     # 没装 TTB：透明选项置灰
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
