# -*- coding: utf-8 -*-
"""日程视图（AI 简约风）：点宠物弹出的面板左栏用它，桌面常驻的日程卡片也用它。

- 头部：日期 + 完成度细进度条 + 下一项倒计时；
- 周视图：七天一行，选中=黑色圆，今天=紫色字，小灰点=当天有日程；
- 列表：逾期 / 选中那天 / 接下来；圆形勾选、悬停出删除、双击编辑；
- 底部一句话添加（quickadd，边打字边预览），"···"展开完整表单。
"""
from datetime import date, datetime, timedelta

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

from . import agenda, quickadd, status, store, theme

WEEKDAYS = "一二三四五六日"
REPEAT_CHOICES = [("none", "不重复"), ("daily", "每天"), ("workdays", "工作日"), ("weekly", "每周")]
REPEAT_TAG = {"daily": "每天", "weekly": "每周", "workdays": "工作日"}


class _Progress(QWidget):
    """一根 2px 的细进度条（紫→蓝渐变）。"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(3)
        self.frac = 0.0

    def set_frac(self, f):
        if abs(f - self.frac) > 1e-3:
            self.frac = f
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor(theme.LINE_2))
        p.drawRoundedRect(QRectF(0, 0, self.width(), 3), 1.5, 1.5)
        if self.frac > 0:
            w = max(3.0, self.width() * self.frac)
            p.setBrush(theme.ai_gradient(0, 0, w, 0))
            p.drawRoundedRect(QRectF(0, 0, w, 3), 1.5, 1.5)


class WeekStrip(QWidget):
    picked = Signal(object)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        self.monday = date.today() - timedelta(days=date.today().weekday())
        self.selected = date.today()
        self.counts = {}

    def set_data(self, monday, selected, counts):
        self.monday, self.selected, self.counts = monday, selected, counts
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        today = date.today()
        cw = self.width() / 7
        for i in range(7):
            d = self.monday + timedelta(days=i)
            cx = cw * i + cw / 2
            p.setFont(theme.ui_font(11))
            p.setPen(theme.qcolor(theme.TEXT_3))
            p.drawText(QRectF(cx - cw / 2, 0, cw, 16), Qt.AlignCenter, WEEKDAYS[i])
            r = QRectF(cx - 14, 17, 28, 28)
            sel = d == self.selected
            if sel:
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qcolor(theme.ACCENT))
                p.drawEllipse(r)
            p.setFont(theme.ui_font(13, sel or d == today))
            p.setPen(theme.qcolor("#FFFFFF" if sel else (theme.AI_A if d == today else theme.TEXT)))
            p.drawText(r, Qt.AlignCenter, str(d.day))
            n = min(3, self.counts.get(d, 0))
            if n and not sel:
                p.setPen(Qt.NoPen)
                p.setBrush(theme.qcolor(theme.TEXT_3))
                x0 = cx - (n * 3 + (n - 1) * 2) / 2
                for k in range(n):
                    p.drawEllipse(QRectF(x0 + k * 5, 46, 3, 3))

    def mouseReleaseEvent(self, e):
        i = int(e.position().x() // (self.width() / 7))
        if 0 <= i < 7:
            self.picked.emit(self.monday + timedelta(days=i))


class EventRow(QWidget):
    toggled = Signal(int, bool)
    delete = Signal(int)
    edit = Signal(int)

    def __init__(self, e, show_date=False, highlight=False, overdue=False):
        super().__init__()
        self.eid = e["id"]
        self.setObjectName("row")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(f"#row {{ background: transparent; border-radius: 9px; }}"
                           f"#row:hover {{ background: {theme.SURFACE_2}; }}")
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 6, 4, 6)
        h.setSpacing(10)
        self.cb = QCheckBox()
        self.cb.setChecked(bool(e["done"]))
        self.cb.setCursor(Qt.PointingHandCursor)
        self.cb.toggled.connect(lambda c: self.toggled.emit(self.eid, c))
        h.addWidget(self.cb, 0, Qt.AlignTop)
        at = datetime.strptime(e["at"], "%Y-%m-%d %H:%M")
        when = e["at"][11:16]
        if show_date:
            days = (at.date() - date.today()).days
            when = (f"周{WEEKDAYS[at.weekday()]} " if 0 <= days < 7 else f"{at.month}/{at.day} ") + when
        t = QLabel(when)
        t.setFixedWidth(74 if show_date else 38)
        col = theme.WARM if overdue else (theme.AI_A if highlight else theme.TEXT_3)
        t.setStyleSheet(f"color: {col}; font-size: 12px; font-family: 'Segoe UI';"
                        + (" font-weight: bold;" if highlight else ""))
        h.addWidget(t, 0, Qt.AlignTop)
        box = QVBoxLayout()
        box.setSpacing(1)
        self.title = QLabel(e["title"])
        self.title.setWordWrap(True)
        box.addWidget(self.title)
        if e.get("desc"):
            d = QLabel(e["desc"])
            d.setWordWrap(True)
            d.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
            box.addWidget(d)
        h.addLayout(box, 1)
        rep = REPEAT_TAG.get(e.get("repeat", "none"))
        if rep:
            tag = QLabel(f"↻ {rep}")
            tag.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
            h.addWidget(tag, 0, Qt.AlignTop)
        self.del_btn = QPushButton("✕")
        self.del_btn.setObjectName("icon")
        self.del_btn.setFixedSize(20, 20)
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.setToolTip("删除")
        self.del_btn.setStyleSheet("QPushButton { font-size: 10px; }")
        self.del_btn.clicked.connect(lambda: self.delete.emit(self.eid))
        self.del_btn.setVisible(False)
        h.addWidget(self.del_btn, 0, Qt.AlignTop)
        self.setToolTip("双击编辑")
        done = self.cb.isChecked()
        f = self.title.font()
        f.setStrikeOut(done)
        self.title.setFont(f)
        self.title.setStyleSheet(f"color: {theme.TEXT_3 if done else theme.TEXT}; font-size: 13px;")

    def enterEvent(self, _):
        self.del_btn.setVisible(True)

    def leaveEvent(self, _):
        self.del_btn.setVisible(False)

    def mouseDoubleClickEvent(self, _):
        self.edit.emit(self.eid)


class AgendaView(QWidget):
    def __init__(self, compact=False):
        super().__init__()
        self.setStyleSheet(theme.BASE_QSS)
        self._editing = None
        self._db_mtime = 0.0
        self.selected = date.today()
        self.monday = self.selected - timedelta(days=self.selected.weekday())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        # 头部
        top = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        self.kicker = QLabel("今天")
        self.kicker.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
        self.date_label = QLabel()
        self.date_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        tcol.addWidget(self.kicker)
        tcol.addWidget(self.date_label)
        top.addLayout(tcol, 1)
        rcol = QVBoxLayout()
        rcol.setSpacing(2)
        self.count_label = QLabel()
        self.count_label.setAlignment(Qt.AlignRight)
        self.count_label.setStyleSheet(f"color: {theme.TEXT_2}; font-size: 12px;")
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignRight)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px;")
        rcol.addWidget(self.count_label)
        rcol.addWidget(self.status_label)
        top.addLayout(rcol)
        self.header_extra = QHBoxLayout()  # 给外壳放按钮（折叠等）
        top.addLayout(self.header_extra)
        lay.addLayout(top)
        self.progress = _Progress()
        lay.addWidget(self.progress)
        self.next_label = QLabel()
        self.next_label.setStyleSheet(f"color: {theme.TEXT_2}; font-size: 12px;")
        lay.addWidget(self.next_label)

        # 周视图
        wk = QHBoxLayout()
        wk.setSpacing(0)
        prev_btn, next_btn = QPushButton("‹"), QPushButton("›")
        for b, step in ((prev_btn, -7), (next_btn, 7)):
            b.setObjectName("icon")
            b.setFixedSize(18, 36)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, s=step: self._shift_week(s))
        self.week = WeekStrip()
        self.week.picked.connect(self._pick_day)
        wk.addWidget(prev_btn)
        wk.addWidget(self.week, 1)
        wk.addWidget(next_btn)
        self._arrows = (prev_btn, next_btn)
        lay.addLayout(wk)

        # 列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.viewport().setStyleSheet("background: transparent;")
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(host)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(0)
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)

        # 一句话添加
        qa = QHBoxLayout()
        qa.setSpacing(6)
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("添加日程，如：明天下午3点 组会")
        self.quick.setStyleSheet(f"QLineEdit {{ border-radius: 10px; padding: 8px 12px;"
                                 f" background: {theme.SURFACE_2}; border: 1px solid transparent; }}"
                                 f"QLineEdit:focus {{ background: white; border: 1px solid {theme.LINE}; }}")
        self.quick.textChanged.connect(self._preview_quick)
        self.quick.returnPressed.connect(self._submit_quick)
        more = QPushButton("···")
        more.setObjectName("icon")
        more.setFixedSize(30, 30)
        more.setCursor(Qt.PointingHandCursor)
        more.setToolTip("详细添加（重复、提前量、备注）")
        more.clicked.connect(lambda: self._open_form())
        qa.addWidget(self.quick, 1)
        qa.addWidget(more)
        self._more_btn = more
        lay.addLayout(qa)
        self.hint = QLabel()
        self.hint.setStyleSheet(f"color: {theme.TEXT_2}; font-size: 11px; padding-left: 4px;")
        self.hint.hide()
        lay.addWidget(self.hint)

        self._build_form(lay)
        self._today = date.today()
        self.reload()
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_db)
        self._poll.start(4000)
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(30 * 1000)
        self._today = date.today()

    def set_folded(self, folded: bool):
        """折叠：只留日期、完成度那一行。"""
        for w in (self.week, self.scroll, self.quick, self._more_btn, self.next_label,
                  *self._arrows):
            w.setVisible(not folded)
        if folded:
            self.hint.hide()
            self.form.hide()
        else:
            self.reload()

    # ---------- 表单 ----------
    def _build_form(self, lay):
        self.form = QWidget()
        self.form.setObjectName("form")
        self.form.setAttribute(Qt.WA_StyledBackground)
        self.form.setStyleSheet(f"#form {{ background: {theme.SURFACE_3}; border: 1px solid {theme.LINE};"
                                " border-radius: 12px; }")
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(12, 10, 12, 10)
        fl.setSpacing(6)
        self.form_title = QLabel("新日程")
        self.form_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        fl.addWidget(self.form_title)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("做什么")
        fl.addWidget(self.title_edit)
        r1 = QHBoxLayout()
        self.dt_edit = QDateTimeEdit()
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_edit.setCalendarPopup(True)
        self.repeat_box = QComboBox()
        for key, label in REPEAT_CHOICES:
            self.repeat_box.addItem(label, key)
        r1.addWidget(self.dt_edit, 1)
        r1.addWidget(self.repeat_box)
        fl.addLayout(r1)
        r2 = QHBoxLayout()
        self.remind_spin = QSpinBox()
        self.remind_spin.setRange(0, 1440)
        self.remind_spin.setValue(10)
        self.remind_spin.setPrefix("提前 ")
        self.remind_spin.setSuffix(" 分钟")
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("备注")
        r2.addWidget(self.remind_spin)
        r2.addWidget(self.desc_edit, 1)
        fl.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghost")
        cancel.clicked.connect(self._close_form)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.clicked.connect(self._submit_form)
        r3.addWidget(cancel)
        r3.addWidget(ok)
        fl.addLayout(r3)
        self.form.hide()
        lay.addWidget(self.form)

    # ---------- 数据 ----------
    def _poll_db(self):
        if not self.isVisible():
            return
        if store.db_mtime() != self._db_mtime:
            self.reload()
        else:
            self._reload_status()

    def _tick(self):
        if date.today() != self._today:
            self._today = date.today()
            self.selected = self._today
            self.monday = self._today - timedelta(days=self._today.weekday())
            self.reload()
        elif self.isVisible():
            self._reload_header()

    def showEvent(self, e):
        # 只在数据或日期变了时重建列表（每次打开都重建会让弹出慢半拍）
        if store.db_mtime() != self._db_mtime or date.today() != self._today:
            self._today = date.today()
            self.reload()
        else:
            self._reload_header()
        super().showEvent(e)

    def reload(self):
        self._db_mtime = store.db_mtime()
        self._reload_header()
        self._reload_status()
        week = store.events_between(self.monday, self.monday + timedelta(days=6))
        counts = {}
        for e in week:
            d = datetime.strptime(e["at"][:10], "%Y-%m-%d").date()
            counts[d] = counts.get(d, 0) + 1
        self.week.set_data(self.monday, self.selected, counts)
        self._reload_list()

    def _reload_header(self):
        now = datetime.now()
        today = now.date()
        self.date_label.setText(f"{today.month}月{today.day}日 周{WEEKDAYS[today.weekday()]}")
        todays = store.events_on(today)
        done = sum(1 for e in todays if e["done"])
        self.count_label.setText(f"{done}/{len(todays)} 完成" if todays else "今天没安排")
        self.progress.set_frac(done / len(todays) if todays else 0.0)
        nxt = agenda.next_event(now)
        if nxt:
            self.next_label.setText(f"下一项　{nxt['at'][11:16]} {nxt['title'][:14]}　·　"
                                    f"{agenda.countdown(nxt, now)}")
            self.next_label.show()
        else:
            self.next_label.hide()

    def _reload_status(self):
        st = status.read()
        bits = []
        if isinstance(st.get("unread_mail"), int) and st["unread_mail"] > 0:
            bits.append(f"{st['unread_mail']} 封未读")
        if isinstance(st.get("news_ready"), int) and st["news_ready"] > 0:
            bits.append(f"{st['news_ready']} 条资讯")
        self.status_label.setText(" · ".join(bits))
        self.status_label.setVisible(bool(bits))

    def _section(self, text, color=None):
        lb = QLabel(text)
        lb.setStyleSheet(f"color: {color or theme.TEXT_3}; font-size: 11px; padding: 10px 6px 4px;")
        self.list_lay.addWidget(lb)

    def _add_row(self, e, **kw):
        row = EventRow(e, **kw)
        row.toggled.connect(self._on_check)
        row.delete.connect(self._delete)
        row.edit.connect(self._edit)
        self.list_lay.addWidget(row)

    def _reload_list(self):
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        now = datetime.now()
        today = now.date()
        if self.selected == today:
            od = agenda.overdue(now)
            if od:
                self._section(f"逾期 {len(od)}", theme.WARM)
                for e in od:
                    self._add_row(e, show_date=True, overdue=True)
        events = store.events_on(self.selected)
        self._section(agenda.day_label(self.selected, today))
        if not events:
            empty = QLabel("没有安排" if self.selected >= today else "这天没有记录")
            empty.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 12px; padding: 4px 8px 8px;")
            self.list_lay.addWidget(empty)
        nxt = agenda.next_event(now) if self.selected == today else None
        for e in events:
            at = datetime.strptime(e["at"], "%Y-%m-%d %H:%M")
            self._add_row(e, highlight=bool(nxt and nxt["id"] == e["id"]),
                          overdue=not e["done"] and at < now - timedelta(minutes=30))
        start = max(self.selected, today) + timedelta(days=1)
        upcoming = [e for e in store.events_between(start, start + timedelta(days=13))
                    if not e["done"]][:5]
        if upcoming:
            self._section("接下来")
            for e in upcoming:
                self._add_row(e, show_date=True)
        self.list_lay.addStretch(1)

    def _pick_day(self, d):
        self.selected = d
        self.reload()

    def _shift_week(self, days):
        self.monday += timedelta(days=days)
        self.selected += timedelta(days=days)
        self.reload()

    # ---------- 操作 ----------
    def _on_check(self, eid, checked):
        store.set_done(eid, checked)
        self._refresh_wallpaper()
        QTimer.singleShot(220, self.reload)

    def _delete(self, eid):
        store.delete_event(eid)
        self.reload()
        self._refresh_wallpaper()

    def _preview_quick(self, text):
        if not text.strip():
            self.hint.hide()
            return
        r = quickadd.parse(text, base_day=self.selected)
        if r is None:
            self.hint.setText("再具体点，比如：周五 9:30 交周报")
            self.hint.setStyleSheet(f"color: {theme.TEXT_3}; font-size: 11px; padding-left: 4px;")
        else:
            rep = f" · ↻{REPEAT_TAG[r.repeat]}" if r.repeat in REPEAT_TAG else ""
            tm = r.at.strftime("%H:%M") + ("" if r.explicit_time else "（默认）")
            self.hint.setText(f"✦ {agenda.day_label(r.at.date())} {tm} · {r.title}{rep}"
                              f" · 提前{r.remind}分钟　↵")
            self.hint.setStyleSheet(f"color: {theme.AI_A}; font-size: 11px; padding-left: 4px;")
        self.hint.show()

    def _submit_quick(self):
        text = self.quick.text().strip()
        if not text:
            return
        r = quickadd.parse(text, base_day=self.selected)
        if r is None:
            self._preview_quick(text)
            return
        store.add_event(r.title, r.at, r.remind, repeat=r.repeat)
        self.quick.clear()
        self.selected = r.at.date()
        self.monday = self.selected - timedelta(days=self.selected.weekday())
        self.reload()
        self.hint.setText(f"✓ 已添加　{agenda.day_label(r.at.date())} {r.at:%H:%M} {r.title}")
        self.hint.setStyleSheet(f"color: {theme.GREEN}; font-size: 11px; padding-left: 4px;")
        self.hint.show()
        QTimer.singleShot(3000, lambda: self.hint.setVisible(bool(self.quick.text())))
        self._refresh_wallpaper()

    def _open_form(self, e=None):
        self._editing = e["id"] if e else None
        self.form_title.setText("编辑日程" if e else "新日程")
        keys = [k for k, _ in REPEAT_CHOICES]
        if e:
            self.title_edit.setText(e["title"])
            self.desc_edit.setText(e.get("desc", ""))
            self.dt_edit.setDateTime(datetime.strptime(e["at"], "%Y-%m-%d %H:%M"))
            self.remind_spin.setValue(int(e["remind_min"]))
            self.repeat_box.setCurrentIndex(keys.index(e.get("repeat", "none")))
        else:
            base = datetime.combine(self.selected, datetime.now().time()).replace(second=0, microsecond=0)
            base = (base + timedelta(hours=1)).replace(minute=0)
            r = quickadd.parse(self.quick.text(), base_day=self.selected) if self.quick.text().strip() else None
            self.title_edit.setText(r.title if r else self.quick.text().strip())
            self.dt_edit.setDateTime(r.at if r else base)
            self.remind_spin.setValue(r.remind if r else 10)
            self.repeat_box.setCurrentIndex(keys.index(r.repeat if r else "none"))
            self.desc_edit.clear()
        self.form.show()
        self.window().activateWindow()
        self.title_edit.setFocus()

    def _edit(self, eid):
        e = store.get_event(eid)
        if e:
            self._open_form(e)

    def _close_form(self):
        self.form.hide()
        self._editing = None

    def _submit_form(self):
        title = self.title_edit.text().strip()
        if not title:
            self.title_edit.setPlaceholderText("标题不能为空")
            self.title_edit.setFocus()
            return
        at = self.dt_edit.dateTime().toPython()
        repeat = self.repeat_box.currentData()
        if self._editing:
            store.update_event(self._editing, title, at, self.remind_spin.value(),
                               self.desc_edit.text().strip(), repeat)
        else:
            store.add_event(title, at, self.remind_spin.value(), self.desc_edit.text().strip(),
                            repeat=repeat)
        self.quick.clear()
        self._close_form()
        self.selected = at.date()
        self.monday = self.selected - timedelta(days=self.selected.weekday())
        self.reload()
        self._refresh_wallpaper()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.form.isVisible():
            self._close_form()
            return
        super().keyPressEvent(e)

    @staticmethod
    def _refresh_wallpaper():
        from . import wallpaper
        wallpaper.apply_async()
