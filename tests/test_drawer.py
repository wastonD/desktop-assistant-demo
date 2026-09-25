# -*- coding: utf-8 -*-
"""app/drawer/core.py：扫描、分类标签（文件从不移动）、撤销、旧收纳夹原样放回、提权脚本。
全部在临时目录里，绝不碰真实桌面；系统"显示桌面图标"开关一律用假的。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.drawer import core


def _plain_elevate(pairs):
    """测试用：不提权，直接移动（模拟 UAC 同意）。"""
    import shutil
    out = []
    for s, d in pairs:
        Path(d).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        out.append((str(s), str(d), True, ""))
    return out


class _Sandbox(unittest.TestCase):
    """临时桌面 + 临时公共桌面 + 临时旧收纳夹 + 临时记录文件。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.tmp = tmp
        self.desk = tmp / "Desktop"
        self.pub = tmp / "PublicDesktop"
        self.stash = tmp / "stash"
        self.desk.mkdir()
        self.pub.mkdir()
        env = mock.patch.dict(os.environ, {"ASSISTANT_DESKTOP_DIR": str(self.desk),
                                           "ASSISTANT_PUBLIC_DESKTOP_DIR": str(self.pub),
                                           "ASSISTANT_STASH_DIR": str(self.stash)})
        env.start()
        self.addCleanup(env.stop)
        self.elevated = []

        def fake_elevate(pairs):
            self.elevated.append(list(pairs))
            return _plain_elevate(pairs)
        for attr, value in (("MANIFEST_PATH", tmp / "drawer.json"),
                            ("OPS_PATH", tmp / "ops.json"),
                            ("TAGS_PATH", tmp / "tags.json"),
                            ("TAG_OPS_PATH", tmp / "tag_ops.json"),
                            ("CONFIG_PATH", tmp / "drawer_cfg.json"),
                            ("ELEVATE", fake_elevate)):
            p = mock.patch.object(core, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def touch(self, name, text="x", folder=None):
        p = (folder or self.desk) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def names_on_desk(self):
        return sorted(p.name for p in self.desk.iterdir())

    def legacy(self, group, name, origin):
        """造一个旧版收纳过的东西：文件在 <收纳夹>/<分类>/，清单里记着原位置。"""
        p = self.touch(name, folder=self.stash / group)
        m = core._load_manifest()
        m[core._key(p)] = {"origin": str(origin), "at": "2026-09-24 00:09"}
        core._save_manifest(m)
        return p


class DrawerCoreTests(_Sandbox):
    def test_classify(self):
        self.assertEqual(core.classify(Path("a.PDF"), False), "doc")
        self.assertEqual(core.classify(Path("QQ.lnk"), False), "app")
        self.assertEqual(core.classify(Path("x.jpg"), False), "image")
        self.assertEqual(core.classify(Path("x.zip"), False), "archive")
        self.assertEqual(core.classify(Path("x.unknown"), False), "other")
        self.assertEqual(core.classify(Path("dir"), True), "folder")

    def test_display_name_strips_shortcut_suffix(self):
        self.assertEqual(core.display_name(Path("Steam.lnk")), "Steam")
        self.assertEqual(core.display_name(Path("论文.pdf")), "论文.pdf")

    def test_scan_skips_desktop_ini_and_temp(self):
        self.touch("desktop.ini")
        self.touch("~$报告.docx")
        self.touch("报告.docx")
        names = [i.name for i in core.scan()]
        self.assertEqual(names, ["报告.docx"])

    def test_scan_shows_desktop_and_public_in_place(self):
        self.touch("报告.docx")
        (self.desk / "项目").mkdir()
        self.touch("Steam.lnk", folder=self.pub)
        items = {i.name: i for i in core.scan()}
        self.assertEqual(items["报告.docx"].location, "desktop")
        self.assertEqual(items["Steam"].location, "public")
        self.assertEqual(items["项目"].group, "文件夹")
        self.assertEqual(len(core.desktop_items()), 3)
        self.assertEqual(len(core.desktop_items(include_public=False)), 2)

    def test_assign_is_only_a_tag_files_never_move(self):
        a = self.touch("a.txt")
        b = self.touch("Steam.lnk", folder=self.pub)
        self.assertEqual(core.assign([a, b], core.FAVORITE), 2)
        self.assertEqual(self.names_on_desk(), ["a.txt"])                  # 文件原地不动
        self.assertTrue(b.exists())
        self.assertEqual(self.elevated, [])                                # 公共桌面也不用提权
        groups = {i.name: i.group for i in core.scan()}
        self.assertEqual(groups, {"a.txt": core.FAVORITE, "Steam": core.FAVORITE})
        counts = {g["name"]: g["count"] for g in core.group_names()}
        self.assertEqual(counts[core.FAVORITE], 2)
        core.assign([a], None)                                             # 移出分类 → 回到按类型
        self.assertEqual({i.name: i.group for i in core.scan()}["a.txt"], "文档")

    def test_undo_steps_back_through_tag_changes(self):
        a = self.touch("a.txt")
        core.create_group("学习")
        core.assign([a], core.FAVORITE)
        core.assign([a], "学习")
        self.assertEqual(core.last_op()["label"], "放进「学习」1 项")
        core.undo_last()
        self.assertEqual(core.scan()[0].group, core.FAVORITE)
        core.undo_last()
        self.assertEqual(core.scan()[0].group, "文档")
        self.assertIsNone(core.undo_last())
        self.assertTrue(a.exists())

    def test_pin_file_from_elsewhere_keeps_it_in_place(self):
        outside = self.touch("论文.pdf", folder=self.tmp / "Documents")
        core.assign([outside], core.FAVORITE)
        it = [i for i in core.scan() if i.name == "论文.pdf"][0]
        self.assertEqual((it.location, it.group), ("pinned", core.FAVORITE))
        self.assertEqual(self.names_on_desk(), [])                         # 没被搬到桌面
        self.assertTrue(outside.exists())
        outside.unlink()                                                  # 文件没了：不显示，不报错
        self.assertEqual([i.name for i in core.scan()], [])
        outside.write_text("x", encoding="utf-8")
        core.unpin([outside])
        self.assertEqual([i.name for i in core.scan()], [])
        self.assertTrue(outside.exists())

    def test_group_names_order_and_kinds(self):
        self.touch("a.txt")
        core.create_group("游戏")
        core.create_group("学习")
        names = [(g["name"], g["kind"]) for g in core.group_names()]
        self.assertEqual(names, [("常用", "fav"), ("游戏", "custom"), ("学习", "custom"),
                                 ("文档", "type")])

    def test_group_management_is_tag_only_and_undoable(self):
        self.assertIn("名字不合法", core.create_group("a/b"))
        self.assertEqual(core.create_group("学习"), "")
        self.assertEqual(core.create_group("学习"), "已经有这个分类了")
        self.assertEqual(core.create_group("文档"), "已经有这个分类了")
        a = self.touch("a.txt")
        core.assign([a], "学习")
        self.assertEqual(core.rename_group("学习", "课程"), "")
        self.assertEqual(core.scan()[0].group, "课程")
        self.assertEqual(core.rename_group(core.FAVORITE, "x"), "「常用」不能改名")
        self.assertEqual(core.delete_group("课程"), 1)
        self.assertEqual(core.scan()[0].group, "文档")
        self.assertNotIn("课程", core.custom_groups())
        core.undo_last()                                                   # 分类和标签一起回来
        self.assertIn("课程", core.custom_groups())
        self.assertEqual(core.scan()[0].group, "课程")
        self.assertEqual(self.names_on_desk(), ["a.txt"])

    def test_delete_empty_group_is_undoable(self):
        core.create_group("空的")
        core.delete_group("空的")
        self.assertNotIn("空的", core.custom_groups())
        core.undo_last()
        self.assertIn("空的", core.custom_groups())


class LegacyRestoreTests(_Sandbox):
    """旧版（第一~四轮）真的把文件移进了收纳夹：原样放回，常用/自建分类变成标签。"""

    def test_restore_all_back_to_origins_with_tags(self):
        self.legacy("文档", "a.txt", self.desk / "a.txt")
        self.legacy(core.FAVORITE, "QQ.lnk", self.pub / "QQ.lnk")
        self.legacy("学习", "b.png", self.desk / "b.png")
        items = core.legacy_items()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i.location == "stash" for i in items))
        self.assertEqual({g["name"] for g in core.group_names()} & {"文档", "学习"}, set())  # 待放回的不计入分类
        res = core.restore_legacy()
        self.assertEqual(len(res.moved), 3)
        self.assertEqual(self.names_on_desk(), ["a.txt", "b.png"])
        self.assertTrue((self.pub / "QQ.lnk").exists())
        self.assertEqual(len(self.elevated), 1)                           # 公共桌面一次提权
        groups = {i.name: i.group for i in core.scan()}
        self.assertEqual(groups, {"a.txt": "文档", "QQ": core.FAVORITE, "b.png": "学习"})
        self.assertIn("学习", core.custom_groups())
        self.assertFalse(self.stash.exists())                             # 空掉的旧收纳夹删掉
        self.assertEqual(core._load_manifest(), {})
        self.assertEqual(core.legacy_items(), [])

    def test_name_conflict_never_overwrites(self):
        self.touch("a.txt", "新的")
        self.legacy("文档", "a.txt", self.desk / "a.txt")
        res = core.restore_legacy()
        self.assertEqual(res.moved[0][1].name, "a (2).txt")
        self.assertEqual((self.desk / "a.txt").read_text(encoding="utf-8"), "新的")

    def test_missing_origin_folder_goes_to_desktop(self):
        self.legacy("图片", "照片.png", self.tmp / "已删除的文件夹" / "照片.png")
        self.touch("手动放的.png", folder=self.stash / "图片")               # 没有记录的
        core.restore_legacy()
        self.assertEqual(self.names_on_desk(), ["手动放的.png", "照片.png"])

    def test_restore_selected_and_cancel(self):
        a = self.legacy("文档", "a.txt", self.desk / "a.txt")
        self.legacy("文档", "b.txt", self.desk / "b.txt")
        core.restore_legacy([a])
        self.assertEqual(self.names_on_desk(), ["a.txt"])
        res = core.restore_legacy(cancel=lambda: True)
        self.assertTrue(res.cancelled)
        self.assertEqual(len(core.legacy_items()), 1)

    def test_missing_file_reported(self):
        self.legacy("文档", "a.txt", self.desk / "a.txt").unlink()
        self.touch("b.txt", folder=self.stash / "文档")
        (self.stash / "文档" / "b.txt").unlink()
        res = core._run([(self.stash / "文档" / "不存在.txt", self.desk / "x.txt")])
        self.assertEqual(len(res.failed), 1)
        self.assertIn("没动成", core.summary(res, "放回"))

    def test_elevation_script_runs(self):
        """真正跑一遍提权用的 PowerShell 脚本（不提权），确认中文路径、结果回读都正常。"""
        src = self.touch("公共快捷方式.lnk", folder=self.stash / "应用")
        dst = self.desk / "公共快捷方式.lnk"
        with mock.patch.object(core, "ROOT", self.tmp):
            rows = core._elevated_move([(src, dst)], runas=False, timeout_ms=60000)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][2], rows)
        self.assertTrue(dst.exists())


class DrawerCommandTests(_Sandbox):
    """/收纳 指令：整理 = 藏起桌面图标（文件不动），显示 = 显示回来并放回旧收纳夹。"""

    def setUp(self):
        super().setUp()
        from app import desktop_clean
        self.icons = {"hidden": False}
        for name, fn in (("all_icons_hidden", lambda: self.icons["hidden"]),
                         ("set_all_icons_hidden", lambda on: self.icons.update(hidden=bool(on)))):
            p = mock.patch.object(desktop_clean, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_overview_hide_and_show(self):
        from app import commands
        self.touch("a.pdf")
        self.assertIn("桌面上 1 项", commands.handle("/收纳"))
        self.assertIn("藏起来了", commands.handle("/收纳 整理"))
        self.assertTrue(self.icons["hidden"])
        self.assertEqual(self.names_on_desk(), ["a.pdf"])                   # 文件不动
        self.assertIn("显示回来了", commands.handle("/收纳 显示"))
        self.assertFalse(self.icons["hidden"])

    def test_show_also_restores_legacy(self):
        from app import commands
        self.legacy("文档", "a.pdf", self.desk / "a.pdf")
        self.assertIn("旧版收纳夹里还有 1 项", commands.handle("/收纳"))
        self.assertIn("放回原处 1 项", commands.handle("/收纳 放回"))
        self.assertEqual(self.names_on_desk(), ["a.pdf"])

    def test_undo_command(self):
        from app import commands
        a = self.touch("a.pdf")
        core.assign([a], core.FAVORITE)
        self.assertIn("撤销了「放进「常用」1 项」", commands.handle("/收纳 撤销"))
        self.assertIn("没有可以撤销", commands.handle("/收纳 撤销"))

    def test_open_uses_ui_hook(self):
        from app import commands
        called = []
        with mock.patch.dict(commands.UI_HOOKS, {"open_drawer": lambda: called.append(1)}):
            commands.handle("/收纳 打开")
        self.assertEqual(called, [1])


class HotkeyParseTests(unittest.TestCase):
    def test_parse(self):
        from app.hotkeys import parse
        self.assertEqual(parse("Ctrl+Alt+D"), (0x0003, ord("D")))
        self.assertEqual(parse("ctrl + alt + space"), (0x0003, 0x20))
        self.assertEqual(parse("Win+F2"), (0x0008, 0x71))
        self.assertIsNone(parse("Hyper+D"))
        self.assertIsNone(parse(""))


if __name__ == "__main__":
    unittest.main()
