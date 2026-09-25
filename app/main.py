# -*- coding: utf-8 -*-
"""桌面宠物助理入口。运行：.venv/Scripts/python -m app.main（在 assistant 目录下）"""
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import commands, desktop_clean, llm, pet_fx, store, taskbar_style, theme, wallpaper
from .board_window import LazyBoard
from .drawer import core as drawer_core
from .drawer.window import DrawerHandle, DrawerWindow
from .hotkeys import HotkeyManager
from .mail import accounts as mail_accounts
from .news import config as news_config
from .pet_window import create_pet
from .pet_window import save_config as pet_window_save
from .widgets import DesktopLayer

ICON_PATH = Path(__file__).resolve().parents[1] / "assets" / "character" / "pet.png"


def refresh_wallpaper(force=False):
    wallpaper.apply_async(force=force)  # 渲染耗时 2~4 秒，必须走后台线程


def check_reminders(pet, tray):
    now = datetime.now()
    for e in store.due_reminders(now):
        at = datetime.strptime(e["at"], "%Y-%m-%d %H:%M")
        mins = max(0, round((at - now).total_seconds() / 60))
        when = f"还有 {mins} 分钟" if mins > 0 else "现在开始"
        text = f"{e['at'][11:16]} {e['title']}\n{when}"
        eid = e["id"]

        def done(eid=eid):
            store.set_done(eid)
            pet.fx.happy()
            refresh_wallpaper()

        pet.fx.alert()
        pet.show_bubble(text, 30000, title="日程提醒", accent=theme.WARM,
                        actions=[("完成", done),
                                 ("10 分钟后", lambda eid=eid: store.snooze_event(eid, 10))])
        tray.showMessage("日程提醒", text.replace("\n", "，"),
                         QSystemTrayIcon.Information, 10000)
        store.mark_reminded(eid, now)
        refresh_wallpaper()


class _Lazy:
    """邮件/资讯服务的懒加载外壳：没配账户/资讯源时连模块都不导入（imaplib/ssl/urllib 约 8MB 常驻）。"""

    def __init__(self, loader, ready):
        self._loader, self._ready, self._obj = loader, ready, None

    def poll_async(self):
        if not self._ready():
            return False
        if self._obj is None:
            self._obj = self._loader()
        return self._obj.poll_async()


class _MailBridge(QObject):
    """收信在后台线程完成，结果经信号回到界面线程再冒气泡。"""
    polled = Signal(object)


def on_mail_polled(res, pet, tray):
    if not res.new_mail or not mail_accounts.load().get("notify_new", True):
        return
    first = res.new_mail[0]
    sender = first.get("from", "").split("<")[0].strip() or first.get("from", "")
    more = f"\n还有 {len(res.new_mail) - 1} 封新邮件" if len(res.new_mail) > 1 else ""
    text = f"{sender}：{first.get('subject', '')[:30]}{more}"
    pet.show_bubble(text, title="新邮件", accent=theme.SKY)
    tray.showMessage("新邮件", text.replace("\n", "，"), QSystemTrayIcon.Information, 8000)


class _NewsBridge(QObject):
    """抓取在后台线程完成，结果经信号回到界面线程再冒气泡。"""
    polled = Signal(object)


def on_news_polled(res, pet, tray):
    if not res.new_items:
        return
    first = res.new_items[0]
    more = f"\n还有 {len(res.new_items) - 1} 条新资讯" if len(res.new_items) > 1 else ""
    text = f"{first['title'][:30]}{more}"
    pet.show_bubble(text, title="新资讯", accent=theme.LAVENDER)
    tray.showMessage("新资讯", text.replace("\n", "，"), QSystemTrayIcon.Information, 8000)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    # 单实例锁：从快捷方式重复启动时直接退出，避免出现两只宠物
    from PySide6.QtCore import QDir, QLockFile
    lock = QLockFile(QDir.tempPath() + "/desktop_assistant.lock")
    if not lock.tryLock(100):
        print("桌面助理已在运行")
        return
    app._instance_lock = lock  # 保持引用
    theme.apply_app_style(app)
    pet = create_pet()   # config/pet.json 的 engine：v1（缺省）或 v2
    pet.show()
    app.aboutToQuit.connect(pet.cleanup)

    # 桌面挂件层：日程面板 + 收纳把手（普通窗口，点桌面时拉回壁纸之上）
    layer = DesktopLayer()
    board = LazyBoard(layer)  # 默认不常驻（日程已放进点宠物弹出的面板），关着时连窗口都不建
    pet.board = board  # 右键菜单里切换显示
    drawer = DrawerWindow()
    handle = DrawerHandle(drawer)   # 置顶常驻（全屏应用时自动隐藏），不再放桌面挂件层
    handle.show()
    pet.drawer = drawer
    commands.UI_HOOKS["open_drawer"] = drawer.open_drawer

    # 任务栏（正常/自动隐藏/完全隐藏）：启动时应用上次的选择，退出时一定恢复系统原样
    taskbar = desktop_clean.Taskbar(pet.cfg)
    pet.taskbar = taskbar
    if taskbar.mode != "normal":
        taskbar.apply(taskbar.mode)
        QTimer.singleShot(500, pet._relayout_for_taskbar)
    app.aboutToQuit.connect(taskbar.restore_on_exit)
    # 任务栏外观（透明/模糊/亚克力，Win11 新任务栏借助 TranslucentTB）：同样退出时还原
    tb_style = taskbar_style.TaskbarStyle(pet.cfg)
    pet.taskbar_style = tb_style
    if tb_style.mode != "normal":
        QTimer.singleShot(1500, lambda: tb_style.apply(tb_style.mode))
    app.aboutToQuit.connect(tb_style.restore_on_exit)
    tb_timer = QTimer()
    tb_timer.timeout.connect(taskbar.enforce)
    tb_timer.timeout.connect(tb_style.enforce)
    tb_timer.start(2000)
    screen = app.primaryScreen()
    screen.availableGeometryChanged.connect(lambda _g: QTimer.singleShot(300, pet._relayout_for_taskbar))

    hotkeys = HotkeyManager()
    hotkeys.register(drawer_core.load_config().get("hotkey", "Ctrl+Alt+D"), drawer.toggle)
    hotkeys.register(pet.cfg.get("chat_hotkey", "Ctrl+Alt+X"), pet._toggle_chat)
    peek = {"on": False}

    def toggle_peek():
        peek["on"] = not peek["on"]
        taskbar.peek(peek["on"])
    hotkeys.register(pet.cfg.get("taskbar_hotkey", "Ctrl+Alt+T"), toggle_peek)
    app.aboutToQuit.connect(hotkeys.unregister_all)
    app._hotkeys = hotkeys

    tray = QSystemTrayIcon(QIcon(str(ICON_PATH)))
    tray_menu = QMenu()
    mail_bridge = _MailBridge()
    mail_bridge.polled.connect(lambda res: on_mail_polled(res, pet, tray))
    def make_mail():
        from .mail.service import MailService
        return MailService(on_result=mail_bridge.polled.emit)
    mail = _Lazy(make_mail, lambda: bool(mail_accounts.enabled_accounts()))

    def open_mail_accounts():
        from .mail_dialog import MailAccountsDialog
        dlg = MailAccountsDialog(on_changed=mail.poll_async)
        dlg.exec()

    news_bridge = _NewsBridge()
    news_bridge.polled.connect(lambda res: on_news_polled(res, pet, tray))
    def make_news():
        from .news.service import NewsService
        return NewsService(on_result=news_bridge.polled.emit)
    news = _Lazy(make_news, lambda: any(f.get("enabled", True)
                                        for f in news_config.load().get("feeds", [])))

    tray_menu.addAction("日程与聊天", pet._toggle_chat)
    tray_menu.addAction("桌面收纳", drawer.toggle)
    tray_menu.addAction("桌面常驻日程卡片", lambda: board.set_wanted(not board.isVisible()))
    tray_menu.addSeparator()
    clean_menu = tray_menu.addMenu("桌面干净")   # 每次打开时按当前状态重建勾选
    clean_menu.aboutToShow.connect(lambda: pet.fill_clean_menu(clean_menu))
    pet.fill_clean_menu(clean_menu)
    style_menu = tray_menu.addMenu("任务栏外观")
    style_menu.aboutToShow.connect(lambda: taskbar_style.fill_menu(
        style_menu, tb_style, notify=lambda t: pet.show_bubble(t, 5000),
        save=lambda: pet_window_save(pet.cfg)))
    for label, cb in (("收取邮件", mail.poll_async),
                      ("邮箱账户…", open_mail_accounts),
                      ("收取资讯", news.poll_async)):
        tray_menu.addAction(label, cb)
    wp_menu = tray_menu.addMenu("壁纸看板")
    wp_menu.addAction("刷新壁纸", lambda: refresh_wallpaper(True))
    wp_menu.addAction("壁纸设置…", pet._open_settings)
    tray_menu.addSeparator()
    tray_menu.addAction("退出", app.quit)
    theme.style_menu(tray_menu)
    tray.setContextMenu(tray_menu)
    tray.setToolTip("桌面助理 · 小银")
    tray.activated.connect(
        lambda reason: pet._toggle_chat() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    def greet():
        pet.show_bubble(pet_fx.greeting(), 8000)
        pet.fx.happy(1)
    QTimer.singleShot(2500, greet)
    QTimer.singleShot(5000, pet.prebuild_hub)   # 空闲时预建面板，第一次点宠物也秒开
    QTimer.singleShot(6500, drawer.park)        # 抽屉预先画好停在屏幕上沿外面，打开时第一帧就动
    from .widgets import trim_memory
    QTimer.singleShot(20000, lambda: trim_memory(0))  # 启动完成后收一次内存

    QTimer.singleShot(800, refresh_wallpaper)  # 先让宠物出场，再渲染壁纸
    wp_timer = QTimer()
    wp_timer.timeout.connect(refresh_wallpaper)  # 内容没变化时 apply 会自动跳过
    wp_timer.start(5 * 60 * 1000)
    rm_timer = QTimer()
    rm_timer.timeout.connect(lambda: check_reminders(pet, tray))
    rm_timer.start(30 * 1000)
    QTimer.singleShot(3000, lambda: check_reminders(pet, tray))
    mail_timer = QTimer()
    mail_timer.timeout.connect(mail.poll_async)  # 没有账户时直接跳过，0 开销
    mail_timer.start(max(1, int(mail_accounts.load().get("poll_min", 5))) * 60 * 1000)
    QTimer.singleShot(10 * 1000, mail.poll_async)
    news_timer = QTimer()
    news_timer.timeout.connect(news.poll_async)  # 没有配置资讯源时直接跳过，0 开销
    news_timer.start(max(1, int(news_config.load().get("poll_min", 60))) * 60 * 1000)
    QTimer.singleShot(15 * 1000, news.poll_async)
    llm_timer = QTimer()
    llm_timer.timeout.connect(llm.maybe_stop_idle)  # 本地模型空闲自动退出，0 显存/内存常驻
    llm_timer.start(60 * 1000)
    app.aboutToQuit.connect(llm.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
