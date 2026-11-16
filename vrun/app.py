# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
import argparse
import ctypes
import logging
import math
import os
import sys
import time

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QInputDialog, QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
                             QScrollArea, QVBoxLayout, QWidget)

from . import __version__, theme
from .device import Device, DeviceBackend
from .draw_dialog import DrawRouteDialog, choose_gpx_destination
from .import_dialog import ImportRouteDialog
from .manage_dialog import RouteManagerDialog
from .paths import app_dir, data_dir, local_data_root, resource_dir
from .route_library import (MAX_ROUTE_NAME_LENGTH, load_gpx, load_saved_route, read_saved_route,
                            rename_route, route_name_key, save_route, unique_route_name)
from .routes import load_route, pace_text
from .widgets import ChoiceBox, DistanceInput, PaceInput, RoutePreview, SpeedInput
from .worker import ProbeWorker, RunWorker, RestoreWorker

LOG = logging.getLogger(__name__)
FORMER_BUILT_IN_ROUTES = {"hnroute.txt", "zjg-west.txt", "zjg-east.txt"}


def user_settings():
    settings = QSettings("IRun", "IRun")
    if not settings.value("migratedVRun", False, type=bool):
        previous = QSettings("VRun", "VRun")
        for key in previous.allKeys():
            if not settings.contains(key):
                settings.setValue(key, previous.value(key))
        settings.setValue("migratedVRun", True)
    return settings


def label(text="", name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def card():
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(7)
    return widget, layout


class Window(QMainWindow):
    def __init__(self, backend=None, settings=None, poll=True):
        super().__init__()
        self.backend = backend or DeviceBackend()
        self.settings = settings if settings is not None else user_settings()
        self.worker = self.probe_worker = self.route = None
        self._closing = self._allow_close = False
        self._run_started = None
        self._paused_at = None
        self._last_restore_failed = False
        self._last_run_error = self._pending_restore = None
        self._last_restore_device = None
        self._pending_start = self._applying_theme = False
        self.palette_colors = {}
        self.setWindowTitle(f"IRun · 虚拟跑步  {__version__}")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "irun.ico")))
        self.resize(850, 700)
        self.setMinimumSize(760, 600)
        self._create_ui()
        self._load_routes()
        try:
            saved_speed = float(self.settings.value("speed", 3.3))
        except (TypeError, ValueError):
            saved_speed = 3.3
        self._speed_mps = saved_speed if self._valid_speed(saved_speed) else 3.3
        saved_target = str(self.settings.value("target", "2")).lower().replace("公里", "").replace("km", "").strip()
        try:
            self._reminder_m = self._parse_target(saved_target)
        except ValueError:
            saved_target, self._reminder_m = "2", 2000.0
        self.target.setEditText(saved_target)
        self.remind.setChecked(str(self.settings.value("remind", "true")).lower() == "true")
        self.target.setEnabled(self.remind.isChecked())
        self._sync_speed_inputs()
        self.speed_mode.toggled.connect(self._select_speed_entry)
        self.speed.valueChanged.connect(self._speed_changed)
        self.pace.textChanged.connect(self._pace_changed)
        self.target.editTextChanged.connect(self._target_changed)
        self.remind.toggled.connect(self._update_reminder)
        self.theme_choice.currentIndexChanged.connect(self.apply_theme)
        QApplication.styleHints().colorSchemeChanged.connect(self._system_appearance_changed)
        QApplication.instance().paletteChanged.connect(self._system_appearance_changed)
        self.apply_theme()
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(5000)
        self.poll_timer.timeout.connect(self._probe)
        if poll:
            self.poll_timer.start()
            QTimer.singleShot(100, self._probe)

    def _create_ui(self):
        root = QWidget()
        root.setObjectName("root")
        root.setMinimumHeight(640)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        page = QVBoxLayout(root)
        page.setContentsMargins(20, 14, 20, 16)
        page.setSpacing(10)

        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(str(resource_dir() / "assets" / "irun.png")).scaled(
            36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(logo)
        header.addWidget(label("IRun", "brand"))
        header.addWidget(label("虚拟跑步", "muted"))
        header.addStretch()
        self.run_state = label("●  未运行", "runState")
        self.run_state.setProperty("active", False)
        header.addWidget(self.run_state)
        header.addSpacing(12)
        self.theme_choice = ChoiceBox()
        self.theme_choice.setObjectName("themeSelector")
        self.theme_choice.setFixedWidth(126)
        self.theme_choice.setToolTip("外观：跟随 Windows 或手动选择明暗主题")
        for name, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            self.theme_choice.addItem(name, value)
        preference = str(self.settings.value("theme", "system"))
        index = self.theme_choice.findData(preference)
        self.theme_choice.setCurrentIndex(max(0, index))
        header.addWidget(self.theme_choice)
        page.addLayout(header)

        device_card, device_layout = card()
        device_layout.setContentsMargins(14, 9, 14, 9)
        row = QHBoxLayout()
        details = QVBoxLayout()
        details.setSpacing(2)
        self.device_name = label("连接 iPhone 或 iPad", "section")
        self.device_hint = label("通过 USB 连接并解锁手机，确认“信任此电脑”。", "muted")
        self.device_hint.setWordWrap(True)
        details.addWidget(self.device_name)
        details.addWidget(self.device_hint)
        row.addLayout(details, 1)
        self.connection = label("未连接", "connection")
        self.connection.setProperty("connected", False)
        row.addWidget(self.connection)
        device_layout.addLayout(row)
        page.addWidget(device_card)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stats_cards = []
        self.laps, self.lap_title, self.lap_unit = self._stat(stats, "完成圈数", "0", "圈")
        self.distance, _, _ = self._stat(stats, "已运动距离（估算）", "0.00", "公里")
        self.elapsed, _, _ = self._stat(stats, "运动时间", "00:00", "")
        page.addLayout(stats)

        middle = QHBoxLayout()
        middle.setSpacing(10)
        self.route_card, route_layout = card()
        self.route_card.setMinimumHeight(256)
        route_header = QHBoxLayout()
        route_header.addWidget(label("运动路线", "section"))
        route_header.addStretch()
        self.lap_progress = label("本圈 0%", "muted")
        route_header.addWidget(self.lap_progress)
        self.manage_button = QPushButton("管理路线…")
        self.manage_button.clicked.connect(self._manage_routes)
        route_header.addWidget(self.manage_button)
        route_layout.addLayout(route_header)
        self.routes = ChoiceBox()
        self.routes.setObjectName("routeSelector")
        self.routes.setSizeAdjustPolicy(ChoiceBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.routes.setMinimumContentsLength(12)
        self.routes.currentIndexChanged.connect(self._select_route)
        route_layout.addWidget(self.routes)
        route_actions = QHBoxLayout()
        self.import_button = QPushButton("导入路线…")
        self.import_button.clicked.connect(self._import_route)
        route_actions.addWidget(self.import_button)
        self.draw_button = QPushButton("绘制路线…")
        self.draw_button.clicked.connect(self._draw_route)
        route_actions.addWidget(self.draw_button)
        self.export_button = QPushButton("导出 GPX…")
        self.export_button.clicked.connect(self._export_route)
        route_actions.addWidget(self.export_button)
        route_layout.addLayout(route_actions)
        self.preview = RoutePreview()
        route_layout.addWidget(self.preview, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("lapProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        route_layout.addWidget(self.progress_bar)
        self.route_info = label("", "muted")
        self.route_info.setWordWrap(True)
        route_layout.addWidget(self.route_info)
        middle.addWidget(self.route_card, 1)

        options_card, options = card()
        options_card.setMinimumHeight(256)
        options.setSpacing(6)
        options.addWidget(label("跑步设置", "section"))
        speed_modes = QHBoxLayout()
        self.speed_mode = QRadioButton("编辑运动速度")
        self.pace_mode = QRadioButton("编辑运动配速")
        self.speed_mode.setChecked(True)
        speed_modes.addWidget(self.speed_mode, 1)
        speed_modes.addWidget(self.pace_mode, 1)
        options.addLayout(speed_modes)
        speed_row = QHBoxLayout()
        speed_row.setSpacing(8)
        self.speed = SpeedInput()
        self.speed.setObjectName("speedInput")
        self.speed.setAccessibleName("运动速度（米每秒）")
        speed_row.addWidget(self.speed, 1)
        self.pace = PaceInput()
        self.pace.setAccessibleName("运动配速（分:秒每公里）")
        self.pace.setReadOnly(True)
        speed_row.addWidget(self.pace, 1)
        speed_row.addWidget(label("分:秒 / 公里", "muted"))
        options.addLayout(speed_row)
        target_row = QHBoxLayout()
        target_row.addWidget(label("预期提醒距离"))
        self.target = DistanceInput()
        self.target.setObjectName("targetInput")
        self.target.setAccessibleName("预期提醒距离（公里）")
        target_row.addWidget(self.target)
        target_row.addWidget(label("公里", "muted"))
        target_row.addStretch()
        options.addLayout(target_row)
        self.remind = QCheckBox("达到目标后发送 Windows 提醒")
        options.addWidget(self.remind)
        self.settings_feedback = label("", "settingsFeedback")
        self.settings_feedback.setWordWrap(True)
        self.settings_feedback.setMinimumHeight(30)
        options.addWidget(self.settings_feedback)
        options.addStretch()
        middle.addWidget(options_card, 1)
        page.addLayout(middle, 1)

        self.message = label("选择路线与速度，点击开始。运行期间请保持 USB 连接。", "message")
        self.message.setWordWrap(True)
        page.addWidget(self.message)
        footer = QHBoxLayout()
        footer.addWidget(label("停止后会发送恢复正常定位命令。", "muted"), 1)
        self.stop_button = QPushButton("停止并恢复定位")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.start_button = QPushButton("开始")
        self.start_button.setObjectName("start")
        self.start_button.setMinimumWidth(108)
        self.start_button.clicked.connect(self.start)
        footer.addWidget(self.stop_button)
        footer.addWidget(self.pause_button)
        footer.addWidget(self.start_button)
        page.addLayout(footer)

    def _stat(self, row, title, value, unit):
        widget, layout = card()
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(2)
        title_label = label(title, "muted")
        layout.addWidget(title_label)
        values = QHBoxLayout()
        number = label(value, "number")
        values.addWidget(number)
        unit_label = label(unit, "muted")
        values.addWidget(unit_label, 0, Qt.AlignBottom)
        values.addStretch()
        layout.addLayout(values)
        self.stats_cards.append(widget)
        row.addWidget(widget, 1)
        return number, title_label, unit_label

    def _system_appearance_changed(self, *_):
        self.apply_theme()

    def apply_theme(self, *_):
        if self._applying_theme:
            return
        self._applying_theme = True
        try:
            mode = self.theme_choice.currentData() or "system"
            self.palette_colors = theme.colors(mode)
            self.setStyleSheet(theme.stylesheet(self.palette_colors))
            for choice in (self.theme_choice, self.routes, self.target):
                choice.set_popup_colors(self.palette_colors)
            self.speed.unit_color = QColor(self.palette_colors["muted"])
            self.speed.update()
            self.preview.colors = self.palette_colors
            self.preview.update()
            self.settings.setValue("theme", mode)
            theme.titlebar(self, self.palette_colors["dark"])
        finally:
            self._applying_theme = False

    def _connected(self, device):
        self.device_name.setText(device.name)
        self.device_hint.setText(f"{device.system_name} {device.version}  ·  请保持 USB 连接")
        self.connection.setText("已连接")
        self.connection.setProperty("connected", True)
        self._refresh_style(self.connection)

    def _disconnected(self, reason):
        self.connection.setText("未连接")
        self.connection.setProperty("connected", False)
        self._refresh_style(self.connection)
        self.device_name.setText("连接 iPhone 或 iPad")
        self.device_hint.setText(reason)

    @staticmethod
    def _refresh_style(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _ready(self, device):
        self._connected(device)
        if self._closing or isinstance(self.worker, RunWorker) and self.worker.cancel.is_set():
            return
        self._run_started = time.monotonic()
        self.run_state.setText("●  运行中")
        self.run_state.setProperty("active", True)
        self._refresh_style(self.run_state)
        self.preview.set_running(True)
        self.pause_button.setEnabled(isinstance(self.worker, RunWorker))
        self.message.setText("正在运行。估算距离随回放更新。")

    def _toggle_pause(self):
        if not isinstance(self.worker, RunWorker) or self.worker.cancel.is_set():
            return
        self.pause_button.setEnabled(False)
        self.worker.set_paused(self._paused_at is None)

    def _pause_changed(self, paused):
        if not isinstance(self.worker, RunWorker) or self.worker.cancel.is_set():
            return
        now = time.monotonic()
        if paused:
            self._paused_at = now
        elif self._paused_at is not None:
            self._run_started += now - self._paused_at
            self._paused_at = None
        self._tick()
        self.preview.set_running(not paused)
        self.run_state.setText("●  已暂停" if paused else "●  运行中")
        self.run_state.setProperty("active", not paused)
        self._refresh_style(self.run_state)
        self.pause_button.setText("继续" if paused else "暂停")
        self.pause_button.setEnabled(True)
        self.message.setText("已暂停，模拟定位保留在当前位置。" if paused else "已从暂停位置继续运行。")

    def _on_progress(self, fraction):
        fraction = max(0.0, min(1.0, fraction))
        self.preview.set_progress(self.route.geometry_fraction(fraction) if self.route else fraction)
        self.progress_bar.setValue(round(fraction * 1000))
        closed = self.route is None or self.route.closed
        if self._run_started is None:
            prefix = "本圈" if closed else "本次往返"
        elif closed:
            prefix = f"第 {int(self.laps.text()) + 1} 圈"
        else:
            prefix = f"第 {int(self.laps.text()) + 1} 次往返 · {'去程' if fraction < 0.5 else '返程'}"
        self.lap_progress.setText(f"{prefix} · {fraction:.0%}")

    @staticmethod
    def _route_key(path):
        return f"user:{path.stem}" if path.suffix == ".json" else path.name

    @classmethod
    def _route_catalog(cls):
        directory = app_dir() / "routes"
        entries = [(path.stem, path, True) for path in sorted(directory.glob("*.txt"), key=lambda p: p.name.casefold())
                   if path.name.casefold() not in FORMER_BUILT_IN_ROUTES]
        user_entries = {}
        for app_name in ("IRun", "VRun"):
            try:
                for path in (local_data_root() / app_name / "routes").glob("*.json"):
                    key = cls._route_key(path)
                    if key in user_entries:
                        continue
                    try:
                        name, _, closed = read_saved_route(path)
                    except (ValueError, OSError):
                        LOG.warning("Cannot read saved route %s", path, exc_info=True)
                        continue
                    user_entries[key] = (name, path, closed)
            except OSError:
                LOG.warning("Cannot list saved routes for %s", app_name, exc_info=True)
        entries.extend(sorted(user_entries.values(), key=lambda entry: (entry[0].casefold(), entry[1].name)))
        taken = {route_name_key(name) for name, _, _ in entries}
        seen = set()
        catalog = []
        for name, path, closed in entries:
            key = route_name_key(name)
            if key in seen:
                name = unique_route_name(name, taken)
                taken.add(route_name_key(name))
            seen.add(key)
            catalog.append((name, path, closed))
        return catalog

    @classmethod
    def _reserved_route_names(cls, exclude):
        return (name for name, path, _ in cls._route_catalog() if path != exclude)

    def _load_routes(self, preferred=None):
        entries = self._route_catalog()
        if preferred is None:
            preferred = str(self.settings.value("route", ""))
        self.routes.blockSignals(True)
        self.routes.clear()
        self.route_entries = [(name, str(path), closed) for name, path, closed in entries]
        for name, path, closed in entries:
            self.routes.addItem(name, str(path))
            self.routes.setItemData(self.routes.count() - 1, "闭合环线" if closed else "非闭合 · 往返", Qt.ToolTipRole)
        index = next((i for i, (_, path, _) in enumerate(entries) if self._route_key(path) == preferred), 0)
        self.routes.setCurrentIndex(index if entries else -1)
        self.routes.blockSignals(False)
        self._select_route()

    def _routes_available(self):
        return self.worker is None and not self._pending_start and not self._closing

    def _manage_routes(self):
        if not self._routes_available():
            return
        dialog = RouteManagerDialog(self.palette_colors, self)
        dialog.set_entries(self.route_entries, self.routes.currentData())

        def select():
            index = self.routes.findData(dialog.current_path)
            if index >= 0:
                self.routes.setCurrentIndex(index)

        def apply(action, *, use_selection=True):
            if use_selection:
                select()
            prior_message = self.message.text()
            action(dialog)
            dialog.set_entries(self.route_entries, self.routes.currentData())
            message = self.message.text()
            dialog.feedback.setText(message if message != prior_message or message.startswith("路线名称重复") else "")

        actions = (
            (dialog.import_button, self._import_route, False), (dialog.draw_button, self._draw_route, False),
            (dialog.edit_button, self._edit_route, True), (dialog.rename_button, self._rename_route, True),
            (dialog.delete_button, self._delete_route, True), (dialog.copy_button, self._copy_route, True),
            (dialog.export_button, self._export_route, True),
        )
        for button, action, use_selection in actions:
            button.clicked.connect(
                lambda checked=False, action=action, use_selection=use_selection:
                apply(action, use_selection=use_selection)
            )
        try:
            if dialog.exec():
                select()
        finally:
            for button, _, _ in actions:
                button.clicked.disconnect()
            dialog.deleteLater()

    def _import_route(self, parent=None):
        if not self._routes_available():
            return
        parent = parent or self
        filename, _ = QFileDialog.getOpenFileName(parent, "选择 GPX 路线文件", "", "GPX 路线 (*.gpx)")
        if not filename:
            return
        path = Path(filename)
        try:
            dialog = ImportRouteDialog(path, load_gpx(path), self.palette_colors, parent)
        except (ValueError, OSError) as error:
            QMessageBox.warning(parent, "无法打开路线", str(error))
            return
        try:
            if not dialog.exec():
                return
            name = dialog.name.text()
            saved_path = save_route(data_dir() / "routes", name, dialog.route,
                                    reserved_names=self._reserved_route_names)
        except (ValueError, OSError) as error:
            QMessageBox.warning(parent, "无法保存路线", str(error))
            return
        finally:
            dialog.deleteLater()
        self._finish_route_save(saved_path, name, "路线已导入。")

    def _draw_route(self, parent=None, *, edit=False):
        if not self._routes_available() or edit and self.route is None:
            return
        parent = parent or self
        center = self.route.points[0] if self.route is not None else None
        existing = self.route.path if edit and self.route.path.suffix == ".json" else None
        try:
            directory = data_dir()
        except OSError as error:
            QMessageBox.warning(parent, "无法打开路线编辑器", str(error))
            return
        dialog = DrawRouteDialog(directory, center, self.palette_colors, parent,
                                 route=self.route if edit else None,
                                 name=self.routes.currentText() if edit else None, existing=existing,
                                 reserved_names=self._reserved_route_names)
        try:
            if dialog.exec():
                self._finish_route_save(dialog.saved_path, dialog.name.text(), "路线已保存。")
        finally:
            dialog.deleteLater()

    def _edit_route(self, parent):
        self._draw_route(parent, edit=True)

    def _rename_route(self, parent):
        if not self._routes_available() or not self.routes.currentData():
            return
        path = Path(self.routes.currentData())
        if path.suffix != ".json":
            return
        name, confirmed = QInputDialog.getText(parent, "重命名路线", "新名称", text=self.routes.currentText())
        if confirmed:
            try:
                rename_route(path, name, directory=data_dir() / "routes", reserved_names=self._reserved_route_names)
            except (OSError, ValueError) as error:
                QMessageBox.warning(parent, "无法重命名路线", str(error))
                return
            self._finish_route_save(path, name, "路线已重命名。")

    def _copy_route(self, parent):
        if not self._routes_available() or self.route is None:
            return
        try:
            name = self.routes.currentText()[:MAX_ROUTE_NAME_LENGTH - 3] + " 副本"
            path = save_route(data_dir() / "routes", name, self.route,
                              reserved_names=self._reserved_route_names)
        except (OSError, ValueError) as error:
            QMessageBox.warning(parent, "无法复制路线", str(error))
            return
        self._finish_route_save(path, name, "已创建路线副本。")

    def _finish_route_save(self, path, requested_name, message):
        self._load_routes(self._route_key(path))
        saved_name = self.routes.currentText()
        if saved_name != requested_name.strip():
            message = f"路线名称重复，已自动使用「{saved_name}」。"
        self.message.setText(message)

    def _delete_route(self, parent):
        if not self._routes_available() or not self.routes.currentData():
            return
        path = Path(self.routes.currentData())
        if path.suffix != ".json":
            return
        confirmation = QMessageBox(QMessageBox.Warning, "删除路线", f"删除「{self.routes.currentText()}」？此操作无法撤销。",
                                   QMessageBox.Yes | QMessageBox.Cancel, parent)
        confirmation.setTextFormat(Qt.PlainText)
        confirmation.button(QMessageBox.Yes).setText("删除")
        confirmation.button(QMessageBox.Cancel).setText("取消")
        confirmation.setDefaultButton(QMessageBox.Cancel)
        if confirmation.exec() != QMessageBox.Yes:
            return
        try:
            path.unlink()
        except OSError as error:
            QMessageBox.warning(parent, "无法删除路线", str(error))
            return
        self._load_routes("")
        self.message.setText("路线已删除。")

    def _export_route(self, parent=None):
        if self.route is None or not self._routes_available():
            return
        if choose_gpx_destination(parent or self, self.routes.currentText(), self.route.points):
            self.message.setText("GPX 路线已导出。")

    def _select_route(self, *_):
        previous = self.route
        self.route = None
        try:
            path = self.routes.currentData()
            if not path:
                self.message.setText("请绘制路线或导入 GPX 文件。")
                self.route_info.setText("尚无路线")
            else:
                self._show_route(Path(path))
        except (ValueError, OSError, UnicodeError) as error:
            self.message.setText(str(error))
            self.route_info.setText("无法读取路线")
        if self.route != previous:
            self._clear_stats()
        self.preview.set_route(self.route)
        closed = self.route is None or self.route.closed
        self.lap_title.setText("完成圈数" if closed else "完成往返")
        self.lap_unit.setText("圈" if closed else "次")
        self._on_progress(0)
        self.start_button.setEnabled(self.route is not None and self.worker is None and self._pending_restore is None)
        self.export_button.setEnabled(self.route is not None and self.worker is None)

    def _clear_stats(self):
        self.laps.setText("0")
        self.distance.setText("0.00")
        self.elapsed.setText("00:00")

    def _show_route(self, path):
        self.route = load_saved_route(path) if path.suffix == ".json" else load_route(path)
        details = f"{self.route.kind} · {self.route.distance_summary}"
        if path.suffix != ".json":
            details = f"{path.name}    ·    {details}"
        self.route_info.setText(details)
        self.message.setText("选择路线与速度，点击开始。请在运行期间保持 USB 连接。")

    @staticmethod
    def _valid_speed(speed):
        return math.isfinite(speed) and 0.1 <= speed <= 15

    def _sync_speed_inputs(self):
        with QSignalBlocker(self.speed), QSignalBlocker(self.pace):
            self.speed.setValue(self._speed_mps)
            self.pace.setText(pace_text(self._speed_mps).split(" / ")[0])

    def _select_speed_entry(self, edit_speed):
        self._sync_speed_inputs()
        self.speed.setReadOnly(not edit_speed)
        self.pace.setReadOnly(edit_speed)
        self.settings_feedback.setText("可编辑运动速度。" if edit_speed else "可编辑配速，格式为分:秒。")

    def _speed_changed(self, speed):
        if not self.speed_mode.isChecked():
            return
        if not self._valid_speed(speed):
            self.settings_feedback.setText("请输入 0.10–15.00 m/s，当前速度继续生效。")
            return
        text = pace_text(speed)
        with QSignalBlocker(self.pace):
            self.pace.setText(text.split(" / ")[0])
        self._apply_speed(speed, f"速度已更新为 {speed:.2f} m/s，配速约 {text}。")

    def _pace_changed(self, _):
        if not self.pace_mode.isChecked():
            return
        speed = self.pace.speed()
        if not self._valid_speed(speed):
            self.settings_feedback.setText("请输入 1:07–166:40 的配速，当前速度继续生效。")
            return
        with QSignalBlocker(self.speed):
            self.speed.setValue(speed)
        self._apply_speed(speed, f"配速已更新为 {self.pace.text()} / 公里，速度约 {speed:.2f} m/s。")

    def _apply_speed(self, speed, message):
        self._speed_mps = speed
        self.settings.setValue("speed", speed)
        self._apply_run_settings(message)

    def _target_changed(self, text):
        try:
            self._reminder_m = self._parse_target(text)
        except ValueError as error:
            self.settings_feedback.setText(str(error))
            return
        self.settings.setValue("target", text)
        self._apply_run_settings(f"提醒距离已更新为 {self._reminder_m / 1000:g} 公里。")

    def _update_reminder(self, enabled):
        self.target.setEnabled(enabled)
        if enabled:
            try:
                self._parse_target(self.target.currentText())
            except ValueError:
                self.target.setEditText(f"{self._reminder_m / 1000:g}")
        self.settings.setValue("remind", "true" if enabled else "false")
        self._apply_run_settings(
            f"Windows 提醒已开启，达到 {self._reminder_m / 1000:g} 公里时提醒。"
            if enabled else "Windows 提醒已关闭。"
        )

    def _apply_run_settings(self, message):
        self.settings_feedback.setText(message)
        if isinstance(self.worker, RunWorker):
            self.worker.update_settings(self._speed_mps, self._reminder_m if self.remind.isChecked() else None)

    def _probe(self):
        if self._closing or self._pending_start or self.worker is not None or self.probe_worker is not None:
            return
        worker = ProbeWorker(self.backend, self)
        self.probe_worker = worker
        worker.detected.connect(self._connected)
        worker.unavailable.connect(self._disconnected)
        worker.finished.connect(self._probe_finished)
        worker.start()

    def _probe_finished(self):
        worker = self.probe_worker
        self.probe_worker = None
        if worker:
            worker.deleteLater()
        if self._pending_start and not self._closing:
            self._pending_start = False
            self._set_controls(False)
            QTimer.singleShot(0, self.start)
        self._finish_close()

    def _target_m(self):
        if not self.remind.isChecked():
            return None
        return self._parse_target(self.target.currentText())

    @staticmethod
    def _parse_target(text):
        try:
            value = float(text)
            if not math.isfinite(value) or not 0 < value <= 1000:
                raise ValueError
        except ValueError as error:
            raise ValueError("请输入大于 0、且不超过 1000 的提醒公里数，例如 2、3 或 2.5。") from error
        return value * 1000

    def start(self):
        if self.worker is not None or self.route is None or self._closing or self._pending_restore is not None:
            return
        try:
            target = self._target_m()
        except ValueError as error:
            self.message.setText(str(error))
            return
        if self.probe_worker is not None:
            self._pending_start = True
            self.start_button.setEnabled(False)
            self.probe_worker.cancel.set()
            self.message.setText("正在连接手机，请稍候…")
            return
        entry_speed = self.speed.value() if self.speed_mode.isChecked() else self.pace.speed()
        if not self._valid_speed(entry_speed):
            self.settings_feedback.setText("请输入有效的运动速度或配速，再开始模拟。")
            return
        self._last_restore_failed = False
        self._last_run_error = None
        self._clear_stats()
        self._run_started = None
        self._paused_at = None
        self._on_progress(0)
        self._set_controls(True)
        worker = RunWorker(self.backend, self.route, self._speed_mps, target, self)
        self.worker = worker
        worker.ready.connect(self._ready)
        worker.message.connect(self.message.setText)
        worker.progress.connect(self._on_progress)
        worker.distance_changed.connect(self._update_distance)
        worker.pause_changed.connect(self._pause_changed)
        worker.lap.connect(self._lap)
        worker.target_reached.connect(self._notify)
        worker.failed.connect(self._failed)
        worker.restored.connect(self._restored)
        worker.finished.connect(self._run_finished)
        self.message.setText("正在准备设备，请保持手机解锁…")
        worker.start()

    def _set_controls(self, running):
        self.start_button.setEnabled(not running and self.route is not None and self._pending_restore is None)
        can_retry = self._pending_restore is not None or self._last_restore_device is not None
        self.stop_button.setEnabled(running or can_retry)
        self.stop_button.setText("停止并恢复定位" if running or not can_retry else "再次恢复定位")
        self.routes.setEnabled(not running)
        self.manage_button.setEnabled(not running)
        self.import_button.setEnabled(not running)
        self.draw_button.setEnabled(not running)
        self.export_button.setEnabled(not running and self.route is not None)

    def stop(self):
        if self.worker:
            self.stop_button.setEnabled(False)
            self.pause_button.setEnabled(False)
            self.message.setText("正在停止，请等待恢复定位完成…")
            LOG.info("Stop requested")
            self.worker.stop()

        elif (device := self._pending_restore or self._last_restore_device) is not None:
            self._last_run_error = None
            self._set_controls(True)
            self.stop_button.setEnabled(False)
            worker = RestoreWorker(self.backend, device, self)
            self.worker = worker
            worker.restored.connect(self._restored)
            worker.finished.connect(self._run_finished)
            self.message.setText("正在重新尝试恢复正常定位…")
            worker.start()

    def _failed(self, text):
        self._last_run_error = text
        self.message.setText(text)

    def _lap(self, laps, _):
        self.laps.setText(str(laps))

    def _update_distance(self, distance):
        self.distance.setText(f"{distance / 1000:.2f}")

    def _notify(self, distance):
        self.message.setText(f"已达到目标！已运动距离（估算）{distance / 1000:.2f} 公里，将继续运行。")
        try:
            from winotify import Notification
            Notification(app_id="IRun.Desktop", title="IRun · 目标已完成",
                         msg=f"已运动距离（估算）{distance / 1000:.2f} 公里。运动将继续，请手动停止。",
                         icon=str(resource_dir() / "assets" / "irun.png")).show()
        except Exception:
            LOG.exception("Windows notification failed")
            self.message.setText(self.message.text() + " 系统通知未能发送，请检查 Windows 通知设置。")

    def _restored(self, success, message):
        self._last_restore_failed = not success
        self._pending_restore = None if success else getattr(self.worker, "device", None)
        # Do not overwrite the original preparation error with a generic "stopped" message.
        if message != "已停止。":
            self._last_restore_device = getattr(self.worker, "device", None)
            self.message.setText((self._last_run_error + "\n" if self._last_run_error else "") + message)

    def _run_finished(self):
        self._tick()
        self._run_started = None
        self._paused_at = None
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        self.run_state.setText("●  未运行")
        self.run_state.setProperty("active", False)
        self._refresh_style(self.run_state)
        self.preview.set_running(False)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("暂停")
        self._set_controls(False)
        if self._last_restore_failed:
            QMessageBox.warning(self, "请检查手机定位", self.message.text())
        self._finish_close()

    def _tick(self):
        if self._run_started is not None:
            now = self._paused_at if self._paused_at is not None else time.monotonic()
            seconds = max(0, int(now - self._run_started))
            self.elapsed.setText(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600
                                 else f"{seconds // 60:02d}:{seconds % 60:02d}")

    def closeEvent(self, event):
        if self._allow_close:
            self.preview.set_running(False)
            try:
                QApplication.styleHints().colorSchemeChanged.disconnect(self._system_appearance_changed)
                QApplication.instance().paletteChanged.disconnect(self._system_appearance_changed)
            except (RuntimeError, TypeError):
                pass
            if self.route:
                self.settings.setValue("route", self._route_key(self.route.path))
            event.accept()
            return
        event.ignore()
        self._closing = True
        self._pending_start = False
        self.poll_timer.stop()
        if self.probe_worker:
            self.probe_worker.cancel.set()
        if self.worker:
            self.stop()
        self._finish_close()

    def _finish_close(self):
        if self._closing and self.worker is None and self.probe_worker is None:
            self._allow_close = True
            QTimer.singleShot(0, self.close)



def run():
    parser = argparse.ArgumentParser(description="IRun Windows desktop application")
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--theme", choices=("system", "light", "dark"), help=argparse.SUPPRESS)
    parser.add_argument("--running-preview", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.screenshot:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IRun.Desktop")
    logging.basicConfig(filename=data_dir() / "irun.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = QApplication(sys.argv[:1])
    if args.screenshot:
        for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setApplicationName("IRun")
    application.setApplicationVersion(__version__)
    application.setStyle("Fusion")
    if args.screenshot:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(args.screenshot.parent / ("preview-" + (args.theme or "system") + ".ini")), QSettings.IniFormat)
        if args.theme:
            settings.setValue("theme", args.theme)
        window = Window(settings=settings, poll=False)
        if args.running_preview and window.route is not None:
            window._set_controls(True)
            window._ready(Device("preview", "iPhone X · 运行界面预览", "16.7.16"))
            window._run_started = time.monotonic() - 462
            window._tick()
            window._lap(3, window.route.length_m * 3)
            window._on_progress(0.43)
            window._update_distance(window.route.length_m * 3.43)
            window.message.setText("运行界面预览：未连接或控制手机。当前位置与本圈进度会随运动实时更新。")
    else:
        window = Window()
        if args.theme:
            window.theme_choice.setCurrentIndex(window.theme_choice.findData(args.theme))
    window.show()
    if args.screenshot:
        def capture():
            window.grab().save(str(args.screenshot))
            window.close()
        QTimer.singleShot(450, capture)
    return application.exec()
