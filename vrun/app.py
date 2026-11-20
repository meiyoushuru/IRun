# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
import argparse
import ctypes
from datetime import datetime
import logging
import math
import os
import sys
import time

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                             QInputDialog, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                             QScrollArea, QVBoxLayout, QWidget)

from . import __version__, theme
from .device import END_ACTION, Device, DeviceBackend
from .diagnostics import export_report
from .draw_dialog import DrawRouteDialog, choose_gpx_destination
from .import_dialog import ImportRouteDialog
from .manage_dialog import RouteManagerDialog
from .map_preview import MapRoutePreview
from .paths import app_dir, data_dir, local_data_root, resource_dir
from .route_library import (MAX_ROUTE_NAME_LENGTH, load_gpx, load_saved_route, read_saved_route,
                            rename_route, route_name_key, save_route, unique_route_name)
from .routes import load_route, pace_text, route_kind
from .widgets import ChoiceBox, DistanceInput, PaceInput, RunButton, SpeedInput
from .worker import SETUP_STOPPED, ProbeWorker, RunWorker, RestoreWorker

LOG = logging.getLogger(__name__)
FORMER_BUILT_IN_ROUTES = {"hnroute.txt", "zjg-west.txt", "zjg-east.txt"}
DEFAULT_TARGET_KM = 10
RUN_STATE_LABELS = {
    "unready": "未就绪",
    "ready": "已就绪",
    "loading": "加载中",
    "running": "运行中",
    "paused": "已暂停",
    "restoring": "恢复中",
}


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
        self._restoring = self._pause_pending = False
        self._last_restore_failed = False
        self._last_run_error = self._pending_restore = None
        self._last_diagnostic_error = None
        self._last_seen_device = None
        self._pending_start = False
        self.palette_colors = theme.colors()
        self.setWindowTitle(f"IRun · 虚拟跑步  {__version__}")
        self.setWindowIcon(QIcon(str(resource_dir() / "assets" / "irun.ico")))
        self.resize(850, 700)
        self.setMinimumSize(760, 600)
        self._create_ui()
        self.setStyleSheet(theme.stylesheet(self.palette_colors))
        self._load_routes()
        try:
            saved_speed = float(self.settings.value("speed", 3.3))
        except (TypeError, ValueError):
            saved_speed = 3.3
        self._speed_mps = saved_speed if self._valid_speed(saved_speed) else 3.3
        saved_target = str(self.settings.value("target", DEFAULT_TARGET_KM))
        saved_target = saved_target.lower().removesuffix("公里").removesuffix("km").strip()
        try:
            kilometers = float(saved_target)
            if not 0 < kilometers <= 1000:
                raise ValueError
        except ValueError:
            kilometers = DEFAULT_TARGET_KM
        target_km = max(1, math.floor(kilometers + 0.5))
        self._reminder_m = target_km * 1000
        self.target.setCurrentText(str(target_km))
        self.remind.setChecked(str(self.settings.value("remind", "true")).lower() == "true")
        self.target.setEnabled(self.remind.isChecked())
        self.speed.setValue(self._speed_mps)
        self.pace.set_speed(self._speed_mps)
        self.speed.valueChanged.connect(self._speed_changed)
        self.pace.valueChanged.connect(self._pace_changed)
        self.target.editTextChanged.connect(self._target_changed)
        self.remind.toggled.connect(self._update_reminder)
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
        header.addStretch()
        page.addLayout(header)

        device_card, device_layout = card()
        device_layout.setContentsMargins(14, 9, 14, 9)
        row = QHBoxLayout()
        details = QVBoxLayout()
        details.setSpacing(2)
        self.device_name = label("连接 iPhone", "section")
        self.device_hint = label("通过 USB 连接设备并解锁。如果设备弹出「信任此电脑」，请点「信任」。", "muted")
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
        route_header.addWidget(label("运动路线", "sectionHeader"))
        route_header.addStretch()
        self.lap_progress = label("本圈 0%", "muted")
        route_header.addWidget(self.lap_progress)
        self.manage_button = QPushButton("管理…")
        self.manage_button.clicked.connect(self._manage_routes)
        route_header.addWidget(self.manage_button)
        self.new_button = QPushButton("新建…")
        self.new_button.clicked.connect(self._draw_route)
        route_header.addWidget(self.new_button)
        route_layout.addLayout(route_header)
        self.routes = ChoiceBox()
        self.routes.setObjectName("routeSelector")
        self.routes.setSizeAdjustPolicy(ChoiceBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.routes.setMinimumContentsLength(12)
        self.routes.currentIndexChanged.connect(self._select_route)
        route_layout.addWidget(self.routes)
        self.preview = MapRoutePreview(parent=self)
        self.preview.colors = self.palette_colors
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
        map_footer = QHBoxLayout()
        map_footer.addWidget(self.route_info, 1)
        map_footer.addWidget(self.preview.create_attribution_label(self))
        route_layout.addLayout(map_footer)
        middle.addWidget(self.route_card, 1)

        options_card, options = card()
        options_card.setMinimumHeight(256)
        options.setSpacing(6)
        options.addWidget(label("跑步设置", "sectionHeader"))
        inputs = QGridLayout()
        inputs.setHorizontalSpacing(10)
        inputs.setColumnStretch(1, 1)
        speed_row = QHBoxLayout()
        speed_row.setSpacing(6)
        self.speed = SpeedInput()
        self.speed.setObjectName("speedInput")
        self.speed.setAccessibleName("运动速度（米每秒）")
        speed_row.addWidget(self.speed)
        speed_row.addWidget(label("米/秒", "muted"))
        speed_row.addStretch()
        inputs.addWidget(label("运动速度"), 0, 0)
        inputs.addLayout(speed_row, 0, 1)
        self.pace = PaceInput()
        inputs.addWidget(label("运动配速"), 1, 0)
        inputs.addWidget(self.pace, 1, 1)
        options.addLayout(inputs)
        options.addSpacing(14)
        self.remind = QCheckBox("达到目标距离后发送Windows提醒（运动仍会继续）")
        options.addWidget(self.remind)
        target_row = QHBoxLayout()
        target_row.addWidget(label("预期提醒距离"))
        self.target = DistanceInput()
        self.target.setObjectName("targetInput")
        self.target.setAccessibleName("预期提醒距离（公里）")
        target_row.addWidget(self.target)
        target_row.addWidget(label("公里", "muted"))
        target_row.addStretch()
        options.addLayout(target_row)
        self.settings_feedback = label("", "settingsFeedback")
        self.settings_feedback.setWordWrap(True)
        self.settings_feedback.setMinimumHeight(30)
        options.addWidget(self.settings_feedback)
        options.addStretch()
        middle.addWidget(options_card, 1)
        page.addLayout(middle, 1)

        self.message = label("连接设备并选择路线后，点击「开始」。", "message")
        self.message.setWordWrap(True)
        status_row = QHBoxLayout()
        status_row.addWidget(self.message, 1)
        self.export_diagnostics = QPushButton("导出诊断日志…")
        self.export_diagnostics.setToolTip("保存当前报错和近期连接日志，便于反馈问题")
        self.export_diagnostics.clicked.connect(self._export_diagnostic_report)
        status_row.addWidget(self.export_diagnostics, 0, Qt.AlignVCenter)
        page.addLayout(status_row)
        footer = QHBoxLayout()
        footer.addWidget(label("运行期间请保持 USB 连接。", "muted"), 1)
        self.run_state = label(f"· {RUN_STATE_LABELS['unready']}", "runState")
        self.run_state.setProperty("state", "unready")
        footer.addWidget(self.run_state)
        footer.addSpacing(6)
        self.stop_button = QPushButton(END_ACTION)
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.start_button = RunButton()
        self.start_button.set_colors(self.palette_colors)
        self.start_button.clicked.connect(self._primary_action)
        footer.addWidget(self.stop_button)
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

    def _connected(self, device):
        self._last_seen_device = device
        self.device_name.setText(device.name)
        hint = device.support_issue or "请保持 USB 连接"
        version = f"{device.system_name} {device.version or '版本未知'}"
        if device.system_name == "iPadOS":
            hint = "IRun 已连接。部分跑步应用可能不支持 iPad。" + (
                " " + device.support_issue if device.support_issue else "")
        self.device_hint.setText(f"{version}  ·  {hint}")
        self.connection.setText("已连接")
        self.connection.setProperty("connected", True)
        self._refresh_style(self.connection)
        self._update_controls()

    def _disconnected(self, reason):
        if reason != self.device_hint.text():
            LOG.info("Device discovery: %s", reason)
        self.connection.setText("未连接")
        self.connection.setProperty("connected", False)
        self._refresh_style(self.connection)
        self.device_name.setText("连接 iPhone")
        self.device_hint.setText(reason)
        self._update_controls()

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
        self.preview.set_running(True)
        self._update_controls()
        self.message.setText("正在模拟路线，请保持 USB 连接。")

    def _primary_action(self):
        if self.worker is None:
            self.start()
            return
        if not isinstance(self.worker, RunWorker) or self.worker.cancel.is_set():
            return
        if self._run_started is None or self._restoring or self._pause_pending:
            return
        self._pause_pending = True
        self._update_controls()
        self.worker.set_paused(self._paused_at is None)

    def _pause_changed(self, paused):
        if not isinstance(self.worker, RunWorker) or self.worker.cancel.is_set() or self._restoring:
            return
        self._pause_pending = False
        now = time.monotonic()
        if paused:
            self._paused_at = now
        elif self._paused_at is not None:
            self._run_started += now - self._paused_at
            self._paused_at = None
        self._tick()
        self.preview.set_running(not paused)
        self._update_controls()
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
            self.routes.setItemData(self.routes.count() - 1, route_kind(closed), Qt.ToolTipRole)
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
            (dialog.import_button, self._import_route, False), (dialog.new_button, self._draw_route, False),
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
                                 settings=self.settings,
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
                self.message.setText("点击「新建…」绘制路线，或在「管理…」中导入 GPX 文件。")
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
        self._update_controls()

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
        self.message.setText("连接设备并选择路线后，点击「开始」。")

    @staticmethod
    def _valid_speed(speed):
        return math.isfinite(speed) and 0.1 <= speed <= 15

    def _speed_changed(self, speed):
        if not self._valid_speed(speed):
            self.settings_feedback.setText("请输入 0.10–15.00 米/秒，当前速度继续生效。")
            self._update_controls()
            return
        self.pace.set_speed(speed)
        self._apply_speed(speed, f"速度已更新为 {speed:.2f} 米/秒，配速约 {pace_text(speed)}。")

    def _pace_changed(self, speed):
        if not self._valid_speed(speed):
            self.settings_feedback.setText("配速请输入 1 分 07 秒至 166 分 40 秒，当前速度继续生效。")
            self._update_controls()
            return
        with QSignalBlocker(self.speed):
            self.speed.setValue(speed)
        self._apply_speed(speed, f"配速已更新为 {pace_text(speed)}，速度约 {self.speed.text()} 米/秒。")

    def _apply_speed(self, speed, message):
        self._speed_mps = speed
        self.settings.setValue("speed", speed)
        self._apply_run_settings(message)

    def _target_changed(self, text):
        try:
            self._reminder_m = self._parse_target(text)
        except ValueError as error:
            self.settings_feedback.setText(str(error))
            self._update_controls()
            return
        self.settings.setValue("target", text)
        self._apply_run_settings(f"提醒距离已更新为 {self._reminder_m / 1000:g} 公里。")

    def _update_reminder(self, enabled):
        self.target.setEnabled(enabled)
        if enabled:
            try:
                self._parse_target(self.target.currentText())
            except ValueError:
                self.target.setCurrentText(f"{self._reminder_m / 1000:g}")
        self.settings.setValue("remind", "true" if enabled else "false")
        self._apply_run_settings(
            f"Windows 提醒已开启，达到 {self._reminder_m / 1000:g} 公里时提醒。"
            if enabled else "Windows 提醒已关闭。"
        )

    def _apply_run_settings(self, message):
        self.settings_feedback.setText(message)
        if isinstance(self.worker, RunWorker):
            self.worker.update_settings(self._speed_mps, self._reminder_m if self.remind.isChecked() else None)
        self._update_controls()

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
            self.start()
            self._update_controls()
        elif isinstance(self.worker, RestoreWorker):
            self.worker.start()
        self._finish_close()

    def _target_m(self):
        if not self.remind.isChecked():
            return None
        return self._parse_target(self.target.currentText())

    @staticmethod
    def _parse_target(text):
        if (len(text) > 4 or not text.isascii() or not text.isdecimal()
                or text.startswith("0") or int(text) > 1000):
            raise ValueError("请输入 1–1000 之间的整数公里数。")
        return int(text) * 1000

    def start(self):
        if not self._can_start():
            return
        if self.probe_worker is not None:
            self._pending_start = True
            self._update_controls()
            self.probe_worker.cancel.set()
            self.message.setText("正在连接设备，请保持 USB 连接并稍候…")
            return
        self._last_restore_failed = False
        self._last_run_error = None
        self._clear_stats()
        self._run_started = None
        self._paused_at = None
        self._on_progress(0)
        worker = RunWorker(self.backend, self.route, self._speed_mps, self._target_m(), self)
        self.worker = worker
        self._update_controls()
        worker.ready.connect(self._ready)
        worker.message.connect(self.message.setText)
        worker.progress.connect(self._on_progress)
        worker.distance_changed.connect(self._update_distance)
        worker.pause_changed.connect(self._pause_changed)
        worker.lap.connect(self._lap)
        worker.target_reached.connect(self._notify)
        worker.failed.connect(self._failed)
        worker.restoring.connect(self._begin_restore)
        worker.restored.connect(self._restored)
        worker.finished.connect(self._run_finished)
        self.message.setText("正在连接设备，请保持 iPhone 或 iPad 解锁并稍候…")
        worker.start()

    def _can_start(self):
        if (self.worker is not None or self._pending_start or self._closing or self.route is None
                or self._pending_restore is not None or not self.connection.property("connected")
                or self._last_seen_device is None or self._last_seen_device.support_issue):
            return False
        if not self._valid_speed(self.speed.value()) or not self._valid_speed(self.pace.speed()):
            return False
        try:
            self._target_m()
        except ValueError:
            return False
        return True

    def _update_controls(self):
        playing = self._run_started is not None and not self._restoring
        paused = self._paused_at is not None
        loading = self._pending_start or self._pause_pending or isinstance(self.worker, RunWorker) and not playing
        ready = not playing and not self._restoring and self._can_start()
        if self._restoring:
            state = "restoring"
        elif loading:
            state = "loading"
        elif playing:
            state = "paused" if paused else "running"
        else:
            state = "ready" if ready else "unready"
        if self.run_state.property("state") != state:
            self.run_state.setText(f"· {RUN_STATE_LABELS[state]}")
            self.run_state.setProperty("state", state)
            self._refresh_style(self.run_state)
        has_playback = playing and isinstance(self.worker, RunWorker) and not self.worker.cancel.is_set()
        action = ("继续" if paused else "暂停") if playing else "开始"
        self.start_button.set_action(action, not self._closing and (ready or has_playback and not self._pause_pending))
        can_retry = self.worker is None and self._pending_restore is not None
        self.stop_button.setEnabled(not self._closing and (ready or has_playback or can_retry))
        editable = self.worker is None and not self._pending_start and not playing and not self._closing
        for control in (self.routes, self.manage_button, self.new_button):
            control.setEnabled(editable)

    def _begin_restore(self):
        if not self._restoring:
            self._tick()
            self._restoring = True
            self.preview.set_running(False)
            self._update_controls()

    def stop(self):
        if self._restoring:
            return
        if isinstance(self.worker, RunWorker):
            if self._run_started is not None:
                self._begin_restore()
            self.message.setText("正在停止，请保持 USB 连接并稍候…")
            LOG.info("Stop requested")
            self.worker.stop()

        elif self.worker is None:
            device = self._pending_restore or (self._last_seen_device if self._can_start() else None)
            if device is None:
                return
            self._last_run_error = None
            worker = RestoreWorker(self.backend, device, self)
            self.worker = worker
            self._begin_restore()
            worker.restored.connect(self._restored)
            worker.finished.connect(self._run_finished)
            self.message.setText("正在尝试恢复正常定位，请保持 USB 连接并稍候…")
            if self.probe_worker is not None:
                self.probe_worker.cancel.set()
            else:
                worker.start()

    def _failed(self, text):
        self._last_run_error = self._last_diagnostic_error = text
        self.message.setText(text)

    def _export_diagnostic_report(self):
        default = f"IRun-{__version__}-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.txt"
        filename, _ = QFileDialog.getSaveFileName(self, "导出 IRun 诊断日志", default, "文本文件 (*.txt)")
        if not filename:
            return
        try:
            export_report(Path(filename), data_dir(),
                          status=f"{self.run_state.text()} · {self.connection.text()}\n"
                                 f"{self.device_hint.text()}\n{self.message.text()}",
                          device=self._last_seen_device, last_error=self._last_diagnostic_error)
        except OSError as error:
            QMessageBox.warning(self, "无法导出诊断日志", str(error))
        else:
            QMessageBox.information(self, "诊断日志已导出", "请查看保存的文本文件，确认后可发给维护者排查。")

    def _lap(self, laps, _):
        self.laps.setText(str(laps))

    def _update_distance(self, distance):
        self.distance.setText(f"{distance / 1000:.2f}")

    def _notify(self, distance):
        ending = f"运动将继续。要结束，请点击「{END_ACTION}」。"
        self.message.setText(f"已达到目标！已运动距离（估算）{distance / 1000:.2f} 公里。{ending}")
        try:
            from winotify import Notification
            Notification(app_id="IRun.Desktop", title="IRun · 目标已完成",
                         msg=f"已运动距离（估算）{distance / 1000:.2f} 公里。{ending}",
                         icon=str(resource_dir() / "assets" / "irun.png")).show()
        except Exception:
            LOG.exception("Windows notification failed")
            self.message.setText(self.message.text() + " 系统通知未能发送，请检查 Windows 通知设置。")

    def _restored(self, success, message):
        self._last_restore_failed = not success
        lines = ((self._last_run_error or "") + "\n" + message).splitlines()
        summary = "\n".join(dict.fromkeys(filter(None, lines)))
        if not success:
            self._last_diagnostic_error = summary
        self._pending_restore = None if success else getattr(self.worker, "device", None)
        if message == SETUP_STOPPED:
            if not self._last_run_error:
                self.message.setText(message)
        else:
            self.message.setText(summary)

    def _run_finished(self):
        self._tick()
        self._run_started = None
        self._paused_at = None
        self._restoring = self._pause_pending = False
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        self.preview.set_running(False)
        self._update_controls()
        if self._last_restore_failed:
            QMessageBox.warning(self, "未能恢复定位", self.message.text())
        self._finish_close()

    def _tick(self):
        if self._run_started is not None and not self._restoring:
            now = self._paused_at if self._paused_at is not None else time.monotonic()
            seconds = max(0, int(now - self._run_started))
            self.elapsed.setText(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600
                                 else f"{seconds // 60:02d}:{seconds % 60:02d}")

    def closeEvent(self, event):
        if self._allow_close:
            self.preview.set_running(False)
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
    parser.add_argument("--running-preview", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.screenshot:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("IRun.Desktop")
    logging.basicConfig(filename=data_dir() / "irun.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    application = QApplication(sys.argv[:1])
    application.styleHints().setColorScheme(Qt.ColorScheme.Light)
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
        settings = QSettings(str(args.screenshot.parent / "preview.ini"), QSettings.IniFormat)
        window = Window(settings=settings, poll=False)
        if args.running_preview and window.route is not None:
            window._ready(Device("preview", "iPhone · 运行界面预览", "17.6"))
            window._run_started = time.monotonic() - 462
            window._tick()
            window._lap(3, window.route.length_m * 3)
            window._on_progress(0.43)
            window._update_distance(window.route.length_m * 3.43)
            window.message.setText("运行界面预览：未连接或控制手机。当前位置与本圈进度会随运动实时更新。")
    else:
        window = Window()
    window.show()
    if args.screenshot:
        def capture():
            window.grab().save(str(args.screenshot))
            window.close()
        QTimer.singleShot(450, capture)
    return application.exec()
