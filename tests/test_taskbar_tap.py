# -*- coding: utf-8 -*-
"""app/taskbar_tap.py 里不碰系统的部分（**绝不注入真实 explorer**）：DLL 暂存、模式校验、日志读取。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import taskbar_tap as tt


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.tmp / "Local"), "TEMP": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    def test_staged_copy_is_content_addressed_and_idempotent(self):
        src = self.tmp / "a.dll"
        src.write_bytes(b"v1")
        d1 = tt.staged_dll(src)
        self.assertTrue(d1.exists())
        self.assertEqual(d1.read_bytes(), b"v1")
        self.assertEqual(tt.staged_dll(src), d1)                    # 同内容不重复复制
        src.write_bytes(b"v2")                                      # 重新编译：换一个文件名（旧的被 explorer 锁着）
        d2 = tt.staged_dll(src)
        self.assertNotEqual(d1, d2)
        self.assertTrue(d1.exists() and d2.exists())
        self.assertTrue(str(d2).startswith(str(self.tmp / "Local" / "DesktopAssistant" / "tap")))

    def test_bad_mode_rejected_before_touching_system(self):
        with mock.patch.object(tt, "available", side_effect=AssertionError("不该走到系统调用")):
            with self.assertRaises(ValueError):
                tt.inject("delete-everything")

    def test_unavailable_returns_message(self):
        with mock.patch.object(tt, "available", return_value=False):
            ok, msg = tt.inject("probe")
        self.assertFalse(ok)
        self.assertIn("22H2", msg)

    def test_read_log_incremental(self):
        log = self.tmp / "assistant_tap.log"
        log.write_bytes("第一行\n".encode("utf-8"))            # DLL 写的是 "\n"，别让 Windows 转成 \r\n
        text, pos = tt.read_log()
        self.assertEqual(text, "第一行\n")
        with open(log, "ab") as f:
            f.write("第二行\n".encode("utf-8"))
        text, _ = tt.read_log(pos)
        self.assertEqual(text, "第二行\n")

    def test_dll_is_built_and_exports(self):
        """仓库里带着编好的 DLL（用户不用编译器）；在本进程加载并取导出——不涉及 explorer。"""
        if os.name != "nt":
            self.skipTest("Windows only")
        self.assertTrue(tt.DLL.exists(), "先运行 tools/build_tap.py")
        import ctypes
        dll = ctypes.WinDLL(str(tt.DLL))
        for name in ("DllGetClassObject", "DllCanUnloadNow", "TapHookProc"):
            self.assertTrue(hasattr(dll, name), name)
        self.assertEqual(dll.DllCanUnloadNow(), 1)                  # S_FALSE：永不卸载


if __name__ == "__main__":
    unittest.main()
