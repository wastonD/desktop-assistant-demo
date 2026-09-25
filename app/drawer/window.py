# -*- coding: utf-8 -*-
"""桌面收纳抽屉（AI 简约风）：从屏幕顶部拉下来。**抽屉从不移动桌面上的文件**（第五轮，见 core.py）。

左栏 = 分类：全部 / 常用 / 自建分类 / 按类型；文件拖到分类上就归进去（多选一起拖，只是贴标签），
  右键分类可改名、删除（东西回到按类型的分类，可撤销），底部"＋ 新建分类"。
右边 = 网格（QListView + 自绘 delegate，几百个图标也顺滑）：单击打开，Ctrl/Shift 多选，
  拖出去 = 复制/快捷方式（不会把桌面上的文件拖走），别处的文件拖进来 = 钉进当前分类（只记引用）；右键更多操作。
顶栏：搜索、↶ 撤销（标签改动一步步撤回）、隐藏/显示桌面图标（系统自带开关，桌面干净但文件都在）、··· 菜单。
旧版移进 ~/桌面收纳/ 的东西：状态条提示"放回原处"（后台线程，公共桌面要一次管理员确认）。
打开/收起（第四轮）：真·抽屉——把手是前板上的拉手，随抽屉 1:1 移动、永不消失；拖动跟手、甩动按速度、
点一下由弹簧拉开/推回（物理与状态机在 motion.py，纯逻辑有单测）。
"""
import json
import os
import sys
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QEasingCurve, QFileInfo, QFileSystemWatcher,
                            QMimeData, QModelIndex, QObject, QPoint, QPointF, QPropertyAnimation,
                            QRectF, QSize, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QImageReader, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileIconProvider, QHBoxLayout,
                               QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
                               QProgressBar, QPushButton, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

from .. import desktop_clean, theme
from . import core, motion

SYSTEM_GROUP = "系统"
_SYS_ICON = {"此电脑": QFileIconProvider.Computer, "回收站": QFileIconProvider.Trashcan,
             "网络": QFileIconProvider.Network, "控制面板": QFileIconProvider.Desktop,
             "用户文件夹": QFileIconProvider.Folder}


def system_items():
    """此电脑、回收站等：桌面上隐藏了系统图标之后，从抽屉的"系统"分类里打开。"""
    return [core.Item(Path(t), n, "app", "system", False, 0.0, 0, SYSTEM_GROUP)
            for n, t in desktop_clean.SHELL_TARGETS.items()]

TILE = QSize(100, 108)
ICON = 44
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MIME_ITEMS = "application/x-desktop-drawer-items"


# ---------------------------------------------------------------- 图标
ICON_DIR = core.ROOT / "data" / "icon_cache"


class _IconCache:
    """图标：内存缓存 + 磁盘缓存（data/icon_cache）。
    取系统图标要调用 Shell（会把图标处理 DLL、图像列表加载进本进程，常驻 ~15MB 且释放不掉），
    所以取过一次就存成 PNG，之后启动直接读文件，不再碰 Shell。"""

    def __init__(self):
        self._prov = None
        self._cache = {}

    def _provider(self):
        if self._prov is None:
            self._prov = QFileIconProvider()
        return self._prov

    @staticmethod
    def _disk_path(item, dpr):
        import hashlib
        h = hashlib.md5(f"{item.path}|{item.mtime}|{dpr}".encode("utf-8")).hexdigest()
        return ICON_DIR / f"{h}.png"

    def get(self, item: core.Item, dpr: float) -> QPixmap:
        key = (str(item.path), item.mtime, dpr)
        pm = self._cache.get(key)
        if pm is not None:
            return pm
        disk = self._disk_path(item, dpr)
        if disk is not None and disk.exists():
            pm = QPixmap(str(disk))
            if not pm.isNull():
                pm.setDevicePixelRatio(dpr)
                self._cache[key] = pm
                return pm
        pm = self._make(item, dpr)
        if disk is not None and not pm.isNull():
            try:
                ICON_DIR.mkdir(parents=True, exist_ok=True)
                pm.save(str(disk), "PNG")
            except OSError:
                pass
        self._cache[key] = pm
        return pm

    def _make(self, item: core.Item, dpr: float) -> QPixmap:
        if item.location == "system":
            return self._provider().icon(_SYS_ICON.get(item.name, QFileIconProvider.Folder)).pixmap(
                QSize(ICON, ICON), dpr)
        pm = None
        phys = round(ICON * dpr)
        if item.path.suffix.lower() in IMAGE_EXTS and item.size < 60 * 1024 * 1024:
            reader = QImageReader(str(item.path))
            reader.setAutoTransform(True)
            sz = reader.size()
            if sz.isValid() and sz.width() > 0:
                s = phys / max(sz.width(), sz.height())
                reader.setScaledSize(QSize(max(1, round(sz.width() * s)), max(1, round(sz.height() * s))))
                img = reader.read()
                if not img.isNull():
                    pm = _rounded_thumb(QPixmap.fromImage(img), phys, dpr)
        if pm is None:
            pm = self._provider().icon(QFileInfo(str(item.path))).pixmap(QSize(ICON, ICON), dpr)
            if pm.isNull():
                pm = self._provider().icon(QFileIconProvider.File).pixmap(QSize(ICON, ICON), dpr)
        return pm


def _rounded_thumb(src: QPixmap, phys: int, dpr: float) -> QPixmap:
    out = QPixmap(phys, phys)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, phys, phys), 9 * dpr, 9 * dpr)
    p.setClipPath(path)
    p.fillRect(out.rect(), QColor(theme.SURFACE_2))
    p.drawPixmap(round((phys - src.width()) / 2), round((phys - src.height()) / 2), src)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


# ---------------------------------------------------------------- 模型 / 绘制
class ItemModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.items: list[core.Item] = []
        self.icons = {}

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        if role == Qt.DisplayRole:
            return it.name
        if role == Qt.ToolTipRole:
            where = {"desktop": "在桌面上", "public": "在公共桌面上",
                     "pinned": f"钉在抽屉里 · 文件在 {it.path.parent}",
                     "stash": "旧版收纳夹里 · 右键「放回原处」", "system": "系统位置"}[it.location]
            return f"{it.path.name}\n{where}"
        if role == Qt.UserRole:
            return it
        return None

    def flags(self, index):
        f = super().flags(index)
        if not index.isValid():
            return f | Qt.ItemIsDropEnabled
        if self.items[index.row()].location == "system":
            return f
        return f | Qt.ItemIsDragEnabled

    def mimeTypes(self):
        return ["text/uri-list", MIME_ITEMS]

    def mimeData(self, indexes):
        paths = [str(self.items[i.row()].path) for i in indexes if i.isValid()]
        m = QMimeData()
        m.setUrls([QUrl.fromLocalFile(p) for p in paths])
        m.setData(MIME_ITEMS, json.dumps(paths).encode("utf-8"))
        return m

    def supportedDragActions(self):
        # 不给 Move：拖到资源管理器里默认是"移动"，会把桌面上的文件拖走（抽屉从不改变文件位置）
        return Qt.CopyAction | Qt.LinkAction


class TileDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index):
        return TILE

    def paint(self, p: QPainter, option, index):
        it: core.Item = index.data(Qt.UserRole)
        r = QRectF(option.rect).adjusted(3, 3, -3, -3)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        sel = bool(option.state & QStyle.State_Selected)
        hov = bool(option.state & QStyle.State_MouseOver)
        if sel or hov:
            p.setPen(QPen(QColor("#DDD6FE"), 1) if sel else Qt.NoPen)
            p.setBrush(QColor(theme.AI_SOFT if sel else theme.SURFACE_2))
            p.drawRoundedRect(r, 12, 12)
        pm = self.view.model().icons.get(str(it.path))
        ix = r.center().x() - ICON / 2
        iy = r.y() + 10
        if pm is not None:
            p.drawPixmap(QPointF(ix, iy), pm)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SURFACE_2))
            p.drawRoundedRect(QRectF(ix, iy, ICON, ICON), 10, 10)
        badge = {"pinned": "↗", "stash": "↩"}.get(it.location)   # 钉进来的别处文件 / 旧收纳夹里待放回
        if badge:
            br = QRectF(ix + ICON - 9, iy - 4, 15, 15)
            p.setPen(QPen(QColor("white"), 1.5))
            p.setBrush(theme.ai_gradient(br.left(), br.top(), br.right(), br.bottom()))
            p.drawEllipse(br)
            p.setPen(QColor("white"))
            p.setFont(theme.ui_font(8, True))
            p.drawText(br, Qt.AlignCenter, badge)
        name = it.name
        if not it.is_dir and it.path.suffix and name == it.path.name:
            name = it.path.stem
        p.setFont(theme.ui_font(12))
        fm = QFontMetrics(p.font())
        p.setPen(QColor(theme.TEXT))
        y = iy + ICON + 7
        for ln in _two_lines(name, fm, r.width() - 8):
            p.drawText(QRectF(r.x() + 4, y, r.width() - 8, fm.height()), Qt.AlignHCenter, ln)
            y += fm.height()
        p.restore()


def _two_lines(text, fm: QFontMetrics, width):
    if fm.horizontalAdvance(text) <= width:
        return [text]
    cut = len(text)
    while cut > 1 and fm.horizontalAdvance(text[:cut]) > width:
        cut -= 1
    return [text[:cut], fm.elidedText(text[cut:], Qt.ElideMiddle, int(width))]


class Grid(QListView):
    """文件网格：原生拖出；接收外部文件拖入。"""
    dropped = Signal(list)
    drag_state = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setGridSize(TILE + QSize(4, 4))
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setSpacing(0)
        self.setFrameShape(QListView.NoFrame)
        self.setStyleSheet("QListView { background: transparent; outline: none; }")
        self.viewport().setAttribute(Qt.WA_Hover)

    def startDrag(self, actions):
        self.drag_state.emit(True)
        try:
            super().startDrag(actions)
        finally:
            self.drag_state.emit(False)

    def dragEnterEvent(self, e):
        if e.source() is self:
            e.ignore()
            return
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.LinkAction)
            e.accept()

    def dragMoveEvent(self, e):
        if e.source() is self:
            e.ignore()
            return
        e.setDropAction(Qt.LinkAction)
        e.accept()

    def dropEvent(self, e):
        if e.source() is self:
            return
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.setDropAction(Qt.LinkAction)   # 只记引用：回 Move 的话资源管理器会删掉源文件
            e.accept()
            self.dropped.emit(paths)


class GroupList(QListWidget):
    """左栏分类：可以接收文件拖放。"""
    dropped = Signal(list, str)   # (路径, 分类名)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setFrameShape(QListWidget.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setStyleSheet(
            f"QListWidget {{ background: transparent; outline: none; font-size: 13px; }}"
            f"QListWidget::item {{ height: 32px; border-radius: 8px; padding-left: 8px; color: {theme.TEXT}; }}"
            f"QListWidget::item:hover {{ background: {theme.SURFACE_2}; }}"
            f"QListWidget::item:selected {{ background: {theme.SURFACE_2}; color: {theme.TEXT};"
            " font-weight: bold; }"
            f"QListWidget::item:disabled {{ color: {theme.TEXT_3}; font-size: 11px; height: 26px; }}")

    def _row_at(self, pos):
        it = self.itemAt(pos)
        if it is None or not (it.flags() & Qt.ItemIsEnabled):
            return None
        return it

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.LinkAction)
            e.accept()

    def dragMoveEvent(self, e):
        it = self._row_at(e.position().toPoint())
        if it is not None:
            self.setCurrentItem(it)
            e.setDropAction(Qt.LinkAction)
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        it = self._row_at(e.position().toPoint())
        if it is None:
            return
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.setDropAction(Qt.LinkAction)
            e.accept()
            self.dropped.emit(paths, it.data(Qt.UserRole) or "")


class _Bridge(QObject):
    progress = Signal(int, int, str)
    finished = Signal(str, object, object)  # (动作, MoveResult, 额外信息)


class _FrameTicker(QObject):
    """按显示器的节奏唤醒主线程推进动画（接口同 QTimer：start/stop/isActive/interval/timeout）。
    Qt6 在 Windows 上的定时器底层是 SetTimer，精度 ~15.6ms，设 4ms 也是 16ms 一跳——240Hz 屏上一帧跳 4 格，
    这就是"拉开不丝滑"（真机实测）。这里在后台线程里等 DwmFlush（下一次合成，240Hz ≈ 4.2ms），
    再用排队信号叫醒主线程；DwmFlush 用不了时退回高精度睡眠。只在动画期间有线程，停了就退出。"""
    timeout = Signal()
    _wake = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self._pending = False
        self._ms = 16
        self._wake.connect(self._fire)

    def start(self, ms=16):
        self._ms = ms
        if self._on:
            return
        self._on = True
        threading.Thread(target=self._run, daemon=True, name="drawer-frames").start()

    def stop(self):
        self._on = False

    def isActive(self):
        return self._on

    def interval(self):
        return self._ms

    def _fire(self):
        self._pending = False
        if self._on:
            self.timeout.emit()

    def _run(self):
        flush = None
        if sys.platform == "win32":
            try:
                import ctypes
                flush = ctypes.windll.dwmapi.DwmFlush
            except (AttributeError, OSError):
                flush = None
        while self._on:
            t0 = time.perf_counter()
            if flush is None or flush() != 0 or time.perf_counter() - t0 < 0.0015:
                time.sleep(max(0.0, self._ms / 1000 - (time.perf_counter() - t0)))
            if self._on and not self._pending:   # 主线程还没处理上一帧就不再堆
                self._pending = True
                self._wake.emit()


class Grabber(QWidget):
    """抽屉底部的把手：往上推或点击收起。"""

    def __init__(self, drawer):
        super().__init__()
        self._drawer = drawer
        self._press_y = None
        self.setFixedHeight(16)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("往上推或点击收起")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#D4D4D8"))
        p.drawRoundedRect(QRectF((self.width() - 44) / 2, 6, 44, 4), 2, 2)

    def mousePressEvent(self, e):
        self._press_y = e.globalPosition().y()
        self._drawer.grab(self._press_y)

    def mouseMoveEvent(self, e):
        if self._press_y is not None:
            self._drawer.drag_to(e.globalPosition().y())

    def mouseReleaseEvent(self, e):
        if self._press_y is None:
            return
        self._press_y = None
        self._drawer.release(e.globalPosition().y())


# ---------------------------------------------------------------- 抽屉
class DrawerWindow(theme.CardWindow):
    def __init__(self):
        super().__init__(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
                         margin=22, radius=18, top_flat=True, shadow=28)
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # 停在屏幕外时 show 不抢焦点；打开时再显式激活
        self._parked = False       # 收起后停在屏幕上沿外面（不隐藏）：打开时第一帧就能动，不用重绘
        self._dirty = False        # 动画期间桌面变了：等停下来再刷新
        self._prev_fg = 0          # 打开前的前台窗口：收起后把焦点还给它
        self._icons = _IconCache()
        self._items = []
        self._group = "全部"
        self._busy = False
        self._cancel = False
        self._confirm_until = 0.0
        self._suspend = 0
        self.motion = motion.DrawerMotion()
        self._animating = False
        self._grabbing = False
        self._t_motion = 0.0
        self._mtimer = _FrameTicker(self)
        self._mtimer.timeout.connect(self._step_motion)
        self._pending_icons = []
        self._dpr = 1.0
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.finished.connect(self._on_done)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _: self._rescan.start(300))
        self._rescan = QTimer(self)
        self._rescan.setSingleShot(True)
        self._rescan.timeout.connect(self._refresh_when_still)
        self._icon_timer = QTimer(self)
        self._icon_timer.timeout.connect(self._load_icons)

        self.body.setStyleSheet(theme.BASE_QSS)
        outer = QVBoxLayout(self.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(0)
        outer.addLayout(row, 1)

        # ---- 左栏
        side = QWidget()
        side.setFixedWidth(206)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 20, 10, 10)
        sl.setSpacing(4)
        t = QLabel("桌面收纳")
        t.setStyleSheet("font-size: 17px; font-weight: bold;")
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px; padding-bottom: 10px;")
        sl.addWidget(t)
        sl.addWidget(self.sub)
        self.groups = GroupList()
        self.groups.itemClicked.connect(lambda it: self._set_group(it.data(Qt.UserRole)))
        self.groups.dropped.connect(self._on_group_drop)
        self.groups.setContextMenuPolicy(Qt.CustomContextMenu)
        self.groups.customContextMenuRequested.connect(self._group_menu)
        sl.addWidget(self.groups, 1)
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("分类名，回车创建")
        self.new_edit.returnPressed.connect(self._create_group)
        self.new_edit.hide()
        sl.addWidget(self.new_edit)
        self.new_btn = QPushButton("＋ 新建分类")
        self.new_btn.setObjectName("ghost")
        self.new_btn.setCursor(Qt.PointingHandCursor)
        self.new_btn.setStyleSheet("QPushButton { text-align: left; padding: 7px 10px; }")
        self.new_btn.clicked.connect(self._start_new_group)
        sl.addWidget(self.new_btn)
        row.addWidget(side)
        div = QWidget()
        div.setFixedWidth(1)
        div.setStyleSheet(f"background: {theme.LINE_2};")
        row.addWidget(div)

        # ---- 右边
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(18, 16, 18, 4)
        ml.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(f"QLineEdit {{ background: {theme.SURFACE_2}; border: 1px solid transparent;"
                                  f" border-radius: 10px; padding: 7px 12px; }}"
                                  f"QLineEdit:focus {{ background: white; border: 1px solid {theme.LINE}; }}")
        self.search.textChanged.connect(lambda _: self._apply_filter())
        bar.addWidget(self.search, 1)
        self.undo_btn = QPushButton("↶ 撤销")
        self.undo_btn.setObjectName("ghost")
        self.undo_btn.setCursor(Qt.PointingHandCursor)
        self.undo_btn.clicked.connect(self._undo)
        bar.addWidget(self.undo_btn)
        self.stash_btn = QPushButton("隐藏桌面图标")      # 桌面干净 = 系统自带的"显示桌面图标"开关，文件不动
        self.stash_btn.setObjectName("primary")
        self.stash_btn.setCursor(Qt.PointingHandCursor)
        self.stash_btn.clicked.connect(self._toggle_desktop_icons)
        bar.addWidget(self.stash_btn)
        more = QPushButton("···")
        more.setObjectName("icon")
        more.setFixedSize(32, 32)
        more.setCursor(Qt.PointingHandCursor)
        more.clicked.connect(lambda: self._more_menu(more))
        bar.addWidget(more)
        close = QPushButton("✕")
        close.setObjectName("icon")
        close.setFixedSize(32, 32)
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.close_drawer)
        bar.addWidget(close)
        ml.addLayout(bar)
        self.hint = QLabel("单击打开 · 拖到左侧分类即可归类（文件不会移动）· Ctrl/Shift 多选 · 右键更多")
        self.hint.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
        ml.addWidget(self.hint)

        self.model = ItemModel()
        self.grid = Grid()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(TileDelegate(self.grid))
        self.grid.clicked.connect(self._on_click)
        self.grid.dropped.connect(lambda paths: self._assign(paths, self._drop_group()))
        self.grid.drag_state.connect(self._on_drag_state)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._item_menu)
        ml.addWidget(self.grid, 1)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 13px;")
        self.empty.hide()
        ml.addWidget(self.empty, 1)

        # 进度 / 结果条
        self.status = QWidget()
        self.status.setObjectName("status")
        self.status.setAttribute(Qt.WA_StyledBackground)
        self.status.setStyleSheet(f"#status {{ background: {theme.SURFACE_2}; border-radius: 10px; }}")
        stl = QHBoxLayout(self.status)
        stl.setContentsMargins(12, 6, 6, 6)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: {theme.LINE}; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f" stop:0 {theme.AI_A}, stop:1 {theme.AI_B}); border-radius: 2px; }}")
        self.status_btn = QPushButton()
        self.status_btn.setObjectName("ghost")
        self.status_btn.setCursor(Qt.PointingHandCursor)
        self.status_btn.setStyleSheet(f"QPushButton {{ color: {theme.TEXT}; font-weight: bold; }}")
        self.status_btn.clicked.connect(self._status_action)
        scol = QVBoxLayout()
        scol.setSpacing(4)
        scol.addWidget(self.status_label)
        scol.addWidget(self.progress)
        stl.addLayout(scol, 1)
        stl.addWidget(self.status_btn)
        self.status.hide()
        ml.addWidget(self.status)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.status.hide)
        self._status_kind = ""
        row.addWidget(main, 1)
        outer.addWidget(Grabber(self))
        self._place(hidden=True)

    # ---------------------------------------------------------- 几何与动画
    # 真抽屉（第四轮）：窗口始终不透明，从屏幕上沿后面整块拉出/推回；把手窗口永远贴在卡片底边（前板拉手）。
    # 位置 = motion.pos（0 = 收起：卡片底边正好在屏幕上沿；travel = 完全打开），每帧抽屉和把手一起挪
    # （DeferWindowPos 原子提交；两者内容都不变，只是挪位置，DWM 直接平移位图，不重画）。
    def _geometry(self):
        geo = QApplication.primaryScreen().availableGeometry()
        w = min(1100, round(geo.width() * 0.68))
        h = min(700, round(geo.height() * 0.66))
        return geo, w, h, geo.x() + (geo.width() - w) // 2

    def _card_bottom(self):
        """卡片下沿在窗口里的 y（下面是阴影边距）。"""
        return round(self.card_rect().bottom())

    def _place(self, hidden):
        geo, w, h, x = self._geometry()
        self.resize(w, h)
        self.motion.set_travel(self._open_y() - self._closed_y())
        if hidden:
            self.motion.snap(False)
        self.move(x, self._closed_y() + round(self.motion.pos))

    def _open_y(self):
        return QApplication.primaryScreen().availableGeometry().y()

    def _closed_y(self):
        return QApplication.primaryScreen().geometry().y() - self._card_bottom()

    def handle_y(self):
        """把手顶边的 y：收起时在屏幕上沿，拉出时跟着卡片底边走。"""
        top = QApplication.primaryScreen().geometry().y()
        if not self.is_out():
            return top
        return max(top, self._closed_y() + round(self.motion.pos) + self._card_bottom())

    def is_open(self):
        return self.isVisible() and self.motion.is_open

    def is_out(self):
        """抽屉有一部分拉出来了（停在屏幕外的不算）。"""
        return self.isVisible() and not self.motion.fully_closed

    # ---- 停在屏幕外（第五轮）：收起后不隐藏、不销毁，打开时只挪窗口，第一帧不用等重绘
    def _can_park(self):
        """主屏上面没有别的显示器（否则停在"屏幕外"其实是停在上面那块屏上）。"""
        prim = QApplication.primaryScreen()
        g = prim.geometry()
        above = g.adjusted(0, -self.height() - 40, 0, -g.height())
        return not any(s is not prim and s.geometry().intersects(above) for s in QApplication.screens())

    def park(self):
        """预先建好、画好，停在屏幕上沿外面（启动后空闲时调一次）。"""
        if self.isVisible() or not self._can_park():
            return
        geo, w, h, x = self._geometry()
        self.resize(w, h)
        self.motion.set_travel(self._open_y() - self._closed_y())
        self.motion.snap(False)
        self.move(x, self._closed_y())
        self.refresh()
        self.show()
        self._parked = True

    def _refresh_when_still(self):
        if self._animating or self._grabbing:
            self._dirty = True
        else:
            self.refresh()

    def _frame_ms(self):
        """按屏幕刷新率推进动画（240Hz 屏 = 4ms 一帧；原来固定 16ms，高刷屏上一顿一顿的）。"""
        hz = QApplication.primaryScreen().refreshRate() or 60.0
        return max(3, min(16, round(1000.0 / hz)))

    def toggle(self):
        if self.is_open():
            self.close_drawer()
        else:
            self.open_drawer()

    def _apply_pos(self):
        y = self._closed_y() + round(self.motion.pos)
        h = getattr(self, "handle", None)
        if h is None:
            self.move(self.x(), y)
            return
        hy = y + self._card_bottom()
        if not _defer_move([(self, self.x(), y), (h, h.x(), max(hy, self._screen_top()))]):
            self.move(self.x(), y)
            h.move(h.x(), max(hy, self._screen_top()))

    @staticmethod
    def _screen_top():
        return QApplication.primaryScreen().geometry().y()

    def _run_motion(self):
        self._animating = True
        self._t_motion = time.perf_counter()
        if not self._mtimer.isActive():
            self._mtimer.start(self._frame_ms())

    def _step_motion(self):
        now = time.perf_counter()
        dt = min(0.05, now - self._t_motion)
        self._t_motion = now
        moving = self.motion.tick(dt)
        self._apply_pos()
        if moving or self._grabbing:
            return
        self._mtimer.stop()
        self._animating = False
        if self.motion.fully_closed:
            self._after_close()
        else:
            if getattr(self, "_activate_when_open", False) and self.motion.is_open:
                self._activate_when_open = False
                self.activateWindow()
                self.search.setFocus()
            if self._dirty:
                self._dirty = False
                self.refresh()
            if self._pending_icons:
                self._icon_timer.start(0)

    def _prepare_show(self):
        """没停在屏幕外时（刚启动还没 park / 主屏上面有别的显示器）才走这条慢路。"""
        geo, w, h, x = self._geometry()
        self.resize(w, h)
        self.motion.set_travel(self._open_y() - self._closed_y())
        self.motion.snap(False)
        self.move(x, self._closed_y())
        self.setWindowOpacity(1.0)
        self._animating = True        # 先别加载图标，等滑到位
        self.refresh()
        self.show()
        self._set_handle(False)

    def _remember_foreground(self):
        if sys.platform == "win32" and not self.is_out():
            import ctypes
            u = ctypes.windll.user32
            u.GetForegroundWindow.restype = ctypes.c_void_p
            fg = u.GetForegroundWindow() or 0
            self._prev_fg = 0 if fg == int(self.winId()) else fg

    def open_drawer(self):
        self._remember_foreground()
        if not self.isVisible():
            self._prepare_show()
        else:
            self._set_handle(False)
        self.raise_()
        self.motion.animate_to(True)
        self._run_motion()
        # 激活（拿键盘焦点）放到滑到位之后：激活会让 Qt 把整个大窗口重画一遍（~25ms），放在开头就是第一帧的卡顿
        self._activate_when_open = True

    def close_drawer(self):
        if not self.is_out():
            return
        self.motion.animate_to(False)
        self._run_motion()

    def _after_close(self):
        self.search.clear()
        self._set_handle(True)
        if self._can_park():
            self._parked = True          # 停在屏幕外，内容保持最新（文件夹监视照常），下次打开直接滑
            self._give_back_focus()
            return
        self._parked = False
        self.hide()
        self._watcher.removePaths(self._watcher.directories())
        self._release_memory()
        self.destroy()  # 释放原生窗口和它的绘制缓冲（~7MB），下次 show 会自动重建
        from ..widgets import trim_memory
        trim_memory()

    def _give_back_focus(self):
        """停在屏幕外时窗口还在，焦点会留在它身上（打字进了看不见的搜索框）：还给打开前的窗口。"""
        if sys.platform != "win32":
            return
        import ctypes
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        if (u.GetForegroundWindow() or 0) != int(self.winId()):
            return                       # 用户已经点去别处了
        u.IsWindow.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        target = self._prev_fg if self._prev_fg and u.IsWindow(self._prev_fg) else None
        if target is None:
            u.FindWindowW.restype = ctypes.c_void_p
            target = u.FindWindowW("Progman", None)          # 没有就给桌面
        if target:
            u.SetForegroundWindow(target)

    def _set_handle(self, closed):
        h = getattr(self, "handle", None)
        if h is not None:
            h.set_drawer_open(not closed)

    # ---- 手势（顶端把手 / 抽屉底部把手共用）
    def grab(self, y):
        """按住把手：动画途中也能抓住，从当前位置接着走。"""
        if not self.is_out():
            self._remember_foreground()
        if not self.isVisible():
            self._prepare_show()
        else:
            self._set_handle(False)
        self._grabbing = True
        self._suspend += 1          # 拖动期间别因为失焦自动收起
        self.motion.press(y, time.perf_counter())
        self._run_motion()

    def drag_to(self, y):
        if self._grabbing:
            self.motion.drag(y, time.perf_counter())
            self._apply_pos()

    def release(self, y):
        if not self._grabbing:
            return
        self._grabbing = False
        self._suspend = max(0, self._suspend - 1)
        self.motion.release(y, time.perf_counter())
        if self.motion.is_open:
            self.raise_()
            self._activate_when_open = True      # 滑到位再激活（见 open_drawer）
        self._run_motion()

    # 旧接口（第三轮）：保留给外部调用
    def pull(self, dy):
        if not self._grabbing:
            self.grab(0.0)
        self.drag_to(dy)

    def release_pull(self, dy, velocity=0.0):
        self.release(dy)

    def _release_memory(self):
        """收起后释放图标缓存和列表（再打开时重建，几十毫秒），常驻内存更小。"""
        self.model.set_items([])
        self.model.icons.clear()
        self._icons = _IconCache()
        self._pending_icons = []
        self._items = []

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.new_edit.isVisible():
                self.new_edit.hide()
                self.new_btn.show()
            elif self.search.text():
                self.search.clear()
            else:
                self.close_drawer()
            return
        if e.key() == Qt.Key_Z and e.modifiers() & Qt.ControlModifier:
            self._undo()
            return
        super().keyPressEvent(e)

    def changeEvent(self, e):
        if e.type() == e.Type.ActivationChange and not self.isActiveWindow() and self.is_out():
            QTimer.singleShot(150, self._maybe_autoclose)
        super().changeEvent(e)

    def _maybe_autoclose(self):
        if (not self.isActiveWindow() and not self._suspend and not self._busy
                and QApplication.activePopupWidget() is None
                and QApplication.activeModalWidget() is None):
            self.close_drawer()

    def _on_drag_state(self, dragging):
        self._suspend = max(0, self._suspend + (1 if dragging else -1))

    # ---------------------------------------------------------- 数据
    def refresh(self):
        self._items = core.scan() + system_items()
        n_desk = len(core.desktop_items(self._items))
        legacy = [i for i in self._items if i.location == "stash"]
        hidden = desktop_clean.all_icons_hidden()
        self.sub.setText(f"桌面上 {n_desk} 项\n" + ("桌面图标已隐藏，东西都在这里" if hidden
                                                   else "文件都留在桌面文件夹里"))
        self._reload_groups()
        self._apply_filter()
        self._update_buttons()
        if legacy and not self._busy and not self.status.isVisible():
            self._show_status(f"旧版收纳夹里还有 {len(legacy)} 项（以前移走的），可以原样放回桌面。",
                              "全部放回", sticky=True)
        root = core.stash_dir()
        want = {str(d) for d in (core.desktop_dir(), core.public_desktop_dir())
                if d and Path(d).is_dir()}
        if legacy:
            want |= {str(root)} | {str(i.path.parent) for i in legacy}
        old = set(self._watcher.directories())
        if old - want:
            self._watcher.removePaths(list(old - want))
        if want - old:
            self._watcher.addPaths(list(want - old))

    def _reload_groups(self):
        self.groups.clear()

        def add(label, count, key):
            it = QListWidgetItem(f"{label}　{count}" if count else label)
            it.setData(Qt.UserRole, key)
            self.groups.addItem(it)
            if key == self._group:
                self.groups.setCurrentItem(it)

        def header(text):
            it = QListWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            self.groups.addItem(it)

        add("全部", sum(1 for i in self._items if i.location != "system"), "全部")
        glist = core.group_names(self._items)
        for g in glist:
            if g["kind"] == "fav":
                add("★ 常用", g["count"], g["name"])
        add("⌂ 系统", 0, SYSTEM_GROUP)
        custom = [g for g in glist if g["kind"] == "custom"]
        types = [g for g in glist if g["kind"] == "type"]
        if custom:
            header("我的分类")
            for g in custom:
                add(g["name"], g["count"], g["name"])
        if types:
            header("按类型")
            for g in types:
                add(g["name"], g["count"], g["name"])
        if self._group not in {g["name"] for g in glist} | {"全部", SYSTEM_GROUP}:
            self._group = "全部"
            self.groups.setCurrentRow(0)

    def _apply_filter(self):
        q = self.search.text().strip().lower()
        items = self._items
        if self._group != "全部":
            items = [i for i in items if i.group == self._group and i.location != "stash"]
        else:
            items = [i for i in items if i.location != "system"]
        if q:
            items = [i for i in items if q in i.name.lower()]
        self.model.set_items(items)
        self.empty.setVisible(not items)
        self.grid.setVisible(bool(items))
        if not items:
            self.empty.setText("没有找到" if q else
                               ("把文件拖到这里，或拖到左边的分类上（文件不会移动）"
                                if self._group != "全部" else "桌面上什么都没有～"))
        self._dpr = self.devicePixelRatioF()
        self._pending_icons = [i for i in items if self.model.icons.get(("mt", str(i.path))) != i.mtime]
        self._icon_timer.start(0)

    def _load_icons(self):
        if self._animating:  # 滑动动画期间不动内容，保证每一帧都只是挪窗口
            self._icon_timer.stop()
            return
        deadline = time.perf_counter() + 0.010  # 每批最多 10ms，界面不卡
        changed = False
        while self._pending_icons and time.perf_counter() < deadline:
            it = self._pending_icons.pop(0)
            self.model.icons[str(it.path)] = self._icons.get(it, self._dpr)
            self.model.icons[("mt", str(it.path))] = it.mtime
            changed = True
        if changed:
            self.grid.viewport().update()
        if not self._pending_icons:
            self._icon_timer.stop()

    def _set_group(self, key):
        if not key:
            return
        self._group = key
        self._apply_filter()
        self.grid.verticalScrollBar().setValue(0)

    def _drop_group(self):
        if self._group in ("全部", SYSTEM_GROUP) or self._group in core.TYPE_LABELS:
            return None
        return self._group

    def _update_buttons(self):
        hidden = desktop_clean.all_icons_hidden()
        self.stash_btn.setText("显示桌面图标" if hidden else "隐藏桌面图标")
        self.stash_btn.setToolTip("系统自带的「查看 → 显示桌面图标」开关：图标藏起来，文件都还在桌面文件夹里")
        self.stash_btn.setEnabled(not self._busy)
        op = core.last_op()
        self.undo_btn.setEnabled(op is not None and not self._busy)
        self.undo_btn.setToolTip(f"撤销：{op['label']}　{op['at']}　(Ctrl+Z)" if op else "没有可撤销的操作")

    # ---------------------------------------------------------- 操作
    def _selected_items(self):
        return [self.model.items[i.row()] for i in self.grid.selectionModel().selectedIndexes()]

    def _on_click(self, index):
        if QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return
        it = self.model.items[index.row()]
        try:
            os.startfile(str(it.path))
        except OSError as e:
            self._show_status(f"打不开：{e.strerror or e}", "")
            return
        self.close_drawer()

    def _item_menu(self, pos):
        idx = self.grid.indexAt(pos)
        if not idx.isValid():
            return
        sm = self.grid.selectionModel()
        if not sm.isSelected(idx):
            sm.select(idx, sm.SelectionFlag.ClearAndSelect)
        items = self._selected_items()
        paths = [i.path for i in items]
        one = items[0]
        menu = QMenu(self)
        if any(i.location == "system" for i in items):
            menu.addAction("打开", lambda: self._on_click(idx))
            self._popup(menu, self.grid.viewport().mapToGlobal(pos))
            return
        if len(items) == 1:
            menu.addAction("打开", lambda: self._on_click(idx))
            menu.addAction("在资源管理器中显示",
                           lambda: subprocess.Popen(["explorer", "/select,", str(one.path)]))
        legacy = [i.path for i in items if i.location == "stash"]
        tagged = [i.path for i in items if i.location != "stash"]
        if tagged:
            move = menu.addMenu(f"归到分类（{len(tagged)} 项）" if len(tagged) > 1 else "归到分类")
            for g in core.group_names(self._items):
                if g["kind"] != "type":
                    move.addAction(("★ " if g["kind"] == "fav" else "") + g["name"],
                                   lambda n=g["name"]: self._assign(tagged, n))
            if any(i.group not in core.TYPE_LABELS for i in items if i.location != "stash"):
                move.addAction("移出分类（回到按类型）", lambda: self._assign(tagged, None))
        if legacy:
            menu.addAction("放回原处", lambda: self._restore_legacy(legacy))
        pinned = [i.path for i in items if i.location == "pinned"]
        if pinned:
            menu.addAction("从抽屉拿掉（文件不动）", lambda: self._unpin(pinned))
        menu.addSeparator()
        menu.addAction("复制路径", lambda: QApplication.clipboard().setText(
            "\n".join(str(p) for p in paths)))
        self._popup(menu, self.grid.viewport().mapToGlobal(pos))

    def _group_menu(self, pos):
        it = self.groups.itemAt(pos)
        if it is None or not it.data(Qt.UserRole) or it.data(Qt.UserRole) == "全部":
            return
        name = it.data(Qt.UserRole)
        if name == core.FAVORITE or name in core.TYPE_LABELS or name == SYSTEM_GROUP:
            return
        menu = QMenu(self)
        menu.addAction("重命名", lambda: self._start_rename(name))
        menu.addAction("删除分类（东西回到按类型，文件不动，可撤销）", lambda: self._delete_group(name))
        self._popup(menu, self.groups.viewport().mapToGlobal(pos))

    def _more_menu(self, btn):
        menu = QMenu(self)
        legacy = [i.path for i in self._items if i.location == "stash"]
        if legacy:
            menu.addAction(f"把旧版收纳夹里的 {len(legacy)} 项放回原处", lambda: self._restore_legacy(None))
        icons = menu.addMenu("桌面图标")
        a1 = icons.addAction("隐藏系统图标（此电脑、回收站…，在「系统」里打开）")
        a1.setCheckable(True)
        a1.setChecked(desktop_clean.system_icons_hidden())
        a1.toggled.connect(desktop_clean.set_system_icons_hidden)
        a2 = icons.addAction("隐藏全部桌面图标（东西都在抽屉里）")
        a2.setCheckable(True)
        a2.setChecked(desktop_clean.all_icons_hidden())
        a2.toggled.connect(lambda on: (desktop_clean.set_all_icons_hidden(on), self.refresh()))
        menu.addSeparator()
        menu.addAction("打开桌面文件夹", lambda: self._explore(core.desktop_dir()))
        self._popup(menu, btn.mapToGlobal(QPoint(0, btn.height() + 4)))

    def _popup(self, menu, pos):
        theme.style_menu(menu)
        self._suspend += 1
        try:
            menu.exec(pos)
        finally:
            self._suspend = max(0, self._suspend - 1)

    @staticmethod
    def _explore(path: Path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def _start_new_group(self):
        self.new_btn.hide()
        self.new_edit.clear()
        self.new_edit.setProperty("rename", "")
        self.new_edit.setPlaceholderText("分类名，回车创建")
        self.new_edit.show()
        self.new_edit.setFocus()

    def _start_rename(self, name):
        self.new_btn.hide()
        self.new_edit.setProperty("rename", name)
        self.new_edit.setText(name)
        self.new_edit.setPlaceholderText("新名字，回车确认")
        self.new_edit.show()
        self.new_edit.setFocus()
        self.new_edit.selectAll()

    def _create_group(self):
        name = self.new_edit.text().strip()
        old = self.new_edit.property("rename")
        err = core.rename_group(old, name) if old else core.create_group(name)
        if err:
            self._show_status(err, "")
            return
        self.new_edit.hide()
        self.new_btn.show()
        self._group = name
        self.refresh()
        self._show_status(f"已{'改名为' if old else '新建'}「{name}」。把文件拖到左侧分类即可归类。", "")

    def _on_group_drop(self, paths, group):
        if group == SYSTEM_GROUP:
            return
        if group == "全部" or group in core.TYPE_LABELS:
            group = None  # 回到按类型
        self._assign(paths, group)

    # ---- 标签操作：只改抽屉自己的记录，瞬间完成，文件一个都不动
    def _assign(self, paths, group):
        legacy_root = core.stash_dir()      # 旧收纳夹里的先放回，不给它们贴标签
        paths = [p for p in paths if p and not core._inside(p, legacy_root)]
        if not paths or self._busy:
            return
        n = core.assign(paths, group)
        outside = sum(1 for p in paths if not core.on_desktop(p))
        text = f"放进「{group}」{n} 项" if group else f"{n} 项回到按类型的分类"
        if outside:
            text += f"（其中 {outside} 项在别处，只在抽屉里记了个引用，文件没动）"
        self._show_status(text + "。", "撤销")
        self.refresh()

    def _unpin(self, paths):
        n = core.unpin(paths)
        self._show_status(f"从抽屉拿掉 {n} 项（文件还在原处）。", "撤销")
        self.refresh()

    def _delete_group(self, name):
        n = core.delete_group(name)
        if self._group == name:
            self._group = "全部"
        self._show_status(f"分类「{name}」已删除，{n} 项回到按类型的分类（文件不动）。", "撤销")
        self.refresh()

    def _toggle_desktop_icons(self):
        hide = not desktop_clean.all_icons_hidden()
        desktop_clean.set_all_icons_hidden(hide)
        self._show_status("桌面图标藏起来了：文件都还在桌面文件夹里，从抽屉打开。" if hide
                          else "桌面图标显示回来了。", "")
        self.refresh()

    def hide_desktop_icons(self):
        """「一键干净桌面」用：藏起桌面图标（文件不动）。"""
        desktop_clean.set_all_icons_hidden(True)
        if self.isVisible():
            self.refresh()

    def _undo(self):
        if self._busy:
            return
        op = core.undo_last()
        if op:
            self._show_status(f"已撤销「{op['label']}」。", "")
            self.refresh()

    # ---- 旧版收纳夹：原样放回（真的要移动文件，放后台线程）
    def _restore_legacy(self, paths):
        if self._busy:
            return
        self._busy = True
        self._cancel = False
        self._update_buttons()
        n = len(paths) if paths else sum(1 for i in self._items if i.location == "stash")
        self._show_progress("正在放回…", 0, max(1, n))
        cancel = lambda: self._cancel
        prog = lambda i, n, name: self._bridge.progress.emit(i, n, name)

        def work():
            try:
                res = core.restore_legacy(paths, cancel, prog)
            except Exception as e:  # 不让异常卡住界面
                res = core.MoveResult(failed=[(Path("?"), str(e))])
            self._bridge.finished.emit("restore", res, None)
        threading.Thread(target=work, daemon=True, name="drawer-restore").start()

    def _on_progress(self, i, n, name):
        text = name if name.startswith("等待") else f"正在放回 {min(i + 1, n)}/{n}　{name}"
        self._show_progress(text, i, n)

    def _on_done(self, action, res, extra):
        self._busy = False
        self.status.hide()
        self._show_status(core.summary(res, "放回原处"), "")
        self.refresh()

    def _show_progress(self, text, i, n):
        self._status_timer.stop()
        self.status_label.setText(text)
        self.progress.setRange(0, max(1, n))
        self.progress.setValue(i)
        self.progress.show()
        self._status_kind = "cancel"
        self.status_btn.setText("取消")
        self.status_btn.show()
        self.status.show()

    def _show_status(self, text, action, sticky=False):
        self.status_label.setText(text)
        self.progress.hide()
        self._status_kind = action
        self.status_btn.setText(action)
        self.status_btn.setVisible(bool(action))
        self.status.show()
        if sticky:
            self._status_timer.stop()
        else:
            self._status_timer.start(9000)

    def _status_action(self):
        if self._status_kind == "cancel":
            self._cancel = True
            self.status_label.setText("正在取消…（已经放回的就留在原处了）")
        elif self._status_kind == "撤销":
            self._undo()
        elif self._status_kind == "全部放回":
            self._restore_legacy(None)

    # 整个抽屉也接受外部拖入（落在非网格区域时钉进当前分类）
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.LinkAction)
            e.accept()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.setDropAction(Qt.LinkAction)
            e.accept()
            self._assign(paths, self._drop_group())


def _defer_move(plan):
    """[(widget, x, y)] 逻辑坐标 → 一次 DeferWindowPos 同时挪（Windows）；其它平台返回 False 由调用方逐个 move。"""
    import sys
    if sys.platform != "win32":
        return False
    import ctypes
    import ctypes.wintypes
    u = ctypes.windll.user32
    u.BeginDeferWindowPos.restype = ctypes.c_void_p
    u.DeferWindowPos.restype = ctypes.c_void_p
    u.DeferWindowPos.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u.EndDeferWindowPos.argtypes = [ctypes.c_void_p]
    hd = u.BeginDeferWindowPos(len(plan))
    for w, x, y in plan:
        if not hd:
            return False
        dpr = w.devicePixelRatioF()
        hd = u.DeferWindowPos(hd, int(w.winId()), None, round(x * dpr), round(y * dpr), 0, 0,
                              0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
    return bool(hd) and bool(u.EndDeferWindowPos(hd))


class DrawerHandle(QWidget):
    """抽屉前板上的拉手：永远看得见、抓得住。
    收起时停在屏幕顶端正中（一根细渐变短条，悬停展开"桌面收纳 ⌄"）；拉出时贴着抽屉卡片底边一起走，
    打开后挂在抽屉底边（悬停显示"收起 ⌃"）。往下拖/往上推 = 抽屉 1:1 跟手，甩一下按速度开合，
    点一下 = 弹簧拉开/推回。不抢焦点（WS_EX_NOACTIVATE），所以点它不会触发抽屉的失焦自动收起。
    前台是全屏应用（游戏/视频）且抽屉收着时自动隐藏。"""

    def __init__(self, drawer: DrawerWindow):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._drawer = drawer
        drawer.handle = self
        self._hover = 0.0
        self._target = 0.0
        self._press_y = None
        self._drawer_open = False
        self._fullscreen = False
        self.setFixedSize(150, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("桌面收纳：往下拉，或点一下（Ctrl+Alt+D）")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._step)
        self._place()
        self._fs_timer = QTimer(self)
        self._fs_timer.timeout.connect(self._check_fullscreen)
        self._fs_timer.start(1200)

    def _place(self):
        geo = QApplication.primaryScreen().geometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2, self._drawer.handle_y())

    def set_drawer_open(self, is_open):
        """抽屉出来了/收回去了：把手只换样子，不再隐藏（第三轮"点一下把手就不见了"的根因）。"""
        self._drawer_open = is_open
        self.setToolTip("往上推或点一下收起" if is_open else "桌面收纳：往下拉，或点一下（Ctrl+Alt+D）")
        if not is_open:
            self._place()
        self._apply_visibility()
        self.update()

    def _apply_visibility(self):
        want = not (self._fullscreen and not self._drawer.is_out())
        if want != self.isVisible():
            self.setVisible(want)
        if want and sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.SetWindowPos(int(self.winId()), 0, 0, 0, 0, 0, 0x0013)

    def _check_fullscreen(self):
        """前台窗口铺满整个屏幕（不是最大化，是全屏）且不是桌面 → 隐藏把手，别挡游戏/视频。"""
        if sys.platform != "win32":
            return
        import ctypes
        import ctypes.wintypes
        u = ctypes.windll.user32
        fg = u.GetForegroundWindow()
        fs = False
        if fg:
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(fg, cls, 64)
            if cls.value not in ("Progman", "WorkerW", "Shell_TrayWnd"):
                r = ctypes.wintypes.RECT()
                u.GetWindowRect(fg, ctypes.byref(r))
                geo = QApplication.primaryScreen().geometry()
                dpr = self.devicePixelRatioF()
                fs = (r.left <= 0 and r.top <= 0 and r.right >= round(geo.width() * dpr)
                      and r.bottom >= round(geo.height() * dpr))
        if fs != self._fullscreen:
            self._fullscreen = fs
        self._apply_visibility()

    def _step(self):
        self._hover += (self._target - self._hover) * 0.3
        if abs(self._target - self._hover) < 0.02:
            self._hover = self._target
            self._timer.stop()
        self.update()

    def enterEvent(self, _):
        self._target = 1.0
        self._timer.start(16)

    def leaveEvent(self, _):
        if self._press_y is None:
            self._target = 0.0
            self._timer.start(16)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        attached = self._drawer.is_out()          # 挂在抽屉前板上：白色小耳朵，和卡片连成一体
        k = max(self._hover, 0.45 if attached else 0.0)
        w = 60 + 56 * k
        h = 5 + 19 * k
        r = QRectF((self.width() - w) / 2, 0, w, h)
        if k > 0.02:
            p.setPen(QPen(QColor(theme.LINE), 1))
            # 挂在抽屉上时和卡片一样不透明（半透明会透出后面网页的字，看着发灰）
            p.setBrush(theme.qcolor("#FFFFFF", 255 if attached else round(255 * min(1.0, k * 1.5))))
            path = QPainterPath()
            path.addRoundedRect(r.adjusted(0, -12, 0, 0), 11, 11)
            p.drawPath(path)
        p.setPen(Qt.NoPen)
        bw = 44 - 12 * k
        p.setBrush(theme.ai_gradient((self.width() - bw) / 2, 0, (self.width() + bw) / 2, 0))
        p.drawRoundedRect(QRectF((self.width() - bw) / 2, h - 4.5, bw, 3.5), 1.75, 1.75)
        if self._hover > 0.55:
            p.setPen(theme.qcolor(theme.TEXT, round(255 * (self._hover - 0.55) / 0.45)))
            p.setFont(theme.ui_font(11, True))
            p.drawText(QRectF(r.x(), 1, r.width(), h - 7), Qt.AlignCenter,
                       "收起 ⌃" if self._drawer_open else "桌面收纳 ⌄")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_y = e.globalPosition().y()
            self._drawer.grab(self._press_y)
            self.raise_()

    def mouseMoveEvent(self, e):
        if self._press_y is not None:
            self._drawer.drag_to(e.globalPosition().y())

    def mouseReleaseEvent(self, e):
        if self._press_y is None:
            return
        self._press_y = None
        self._drawer.release(e.globalPosition().y())
        self._target = 0.0
        self._timer.start(16)

    def wheelEvent(self, e):
        if e.angleDelta().y() < 0:
            self._drawer.open_drawer()
        elif e.angleDelta().y() > 0:
            self._drawer.close_drawer()
