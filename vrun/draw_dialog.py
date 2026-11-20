# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QVBoxLayout)

from .map_tiles import MapTiles
from .offline_maps import OSM_ATTRIBUTION
from .route_library import MAX_ROUTE_NAME_LENGTH, export_gpx, save_route
from .route_map import MAX_LATITUDE, RouteMap, project
from .routes import MAX_ROUTE_POINTS, Point, build_route, parse_point
from .widgets import ChoiceBox, MapDialog

PRESET_SETTING = "maps/lastPreset"


def choose_gpx_destination(parent, name, points):
    filename, _ = QFileDialog.getSaveFileName(parent, "导出 GPX 路线", "", "GPX 路线 (*.gpx)")
    if not filename:
        return False
    path = Path(filename)
    if path.suffix.lower() != ".gpx":
        path = path.with_name(path.name + ".gpx")
        if path.exists():
            answer = QMessageBox.question(parent, "替换文件", f"已存在 {path.name}，是否替换？")
            if answer != QMessageBox.Yes:
                return False
    try:
        export_gpx(path, name, points)
    except (OSError, ValueError) as error:
        QMessageBox.warning(parent, "无法导出路线", str(error))
        return False
    return True


class DrawRouteDialog(MapDialog):
    def __init__(self, directory, center, colors, parent=None, *, settings, route=None, name=None, existing=None,
                 reserved_names=None):
        super().__init__(parent, 1180, 820)
        self.directory = directory
        self.settings = settings
        self._initial_view_pending = True
        self._editing = route is not None
        self.saved_path = None
        self.existing = existing
        self.reserved_names = reserved_names
        self.route = None
        self.setObjectName("drawRouteDialog")
        self.setWindowTitle("编辑路线" if self._editing else "新建路线")
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.location = QLineEdit()
        self.location.setPlaceholderText("纬度, 经度（例如 30.000, 120.000）")
        self.location.setAccessibleName("地图定位坐标")
        self.location.returnPressed.connect(self._jump)
        toolbar.addWidget(self.location, 1)
        self._button(toolbar, "定位", self._jump)
        self.add_coordinate = self._button(toolbar, "添加坐标", self._add_coordinate)
        self.tiles = MapTiles(directory / "map-cache", self)
        self.map = RouteMap(center if center is not None else Point(0, 0), self.tiles, colors)
        if center is None:
            self.map.move_view(self.map.center, 4)
        self._button(toolbar, "适应缩放", self._fit_content)
        self.preset_choice = None
        if self.tiles.offline.views:
            self.preset_choice = ChoiceBox(self)
            self.preset_choice.set_popup_colors(colors)
            self.preset_choice.setAccessibleName("定位预设地点")
            if self._editing:
                self.preset_choice.addItem("预设地点…")
            for view in self.tiles.offline.views:
                self.preset_choice.addItem(view["name"], view)
            if not self._editing:
                previous = settings.value(PRESET_SETTING, settings.value("maps/lastCampus", "", type=str), type=str)
                self.preset_choice.setCurrentIndex(max(0, self.preset_choice.findText(previous)))
            self.preset_choice.activated.connect(self._jump_to_preset)
            toolbar.addWidget(self.preset_choice)
        layout.addLayout(toolbar)

        hint = QLabel("点击加点，拖动圆点调整位置。右键点击圆点可删除，右键点击线段可插入点。拖动空白处移动地图。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(self.map, 1)

        map_info = QHBoxLayout()
        self.map_status = QLabel()
        self.map_status.setWordWrap(True)
        map_info.addWidget(self.map_status, 1)
        self.retry_button = self._button(map_info, "重试底图", self._retry_tiles)
        policy = self.retry_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.retry_button.setSizePolicy(policy)
        self.attribution = QLabel()
        attribution_url, attribution_name = OSM_ATTRIBUTION
        self.attribution.setText(f'<a href="{attribution_url}" style="color: {colors["ink"]}">{attribution_name}</a>')
        self.attribution.setOpenExternalLinks(True)
        map_info.addWidget(self.attribution)
        layout.addLayout(map_info)

        edit = QHBoxLayout()
        self.name = QLineEdit(name if name is not None else "新建路线")
        self.name.setAccessibleName("路线名称")
        self.name.setPlaceholderText("路线名称")
        self.name.setMaxLength(MAX_ROUTE_NAME_LENGTH)
        edit.addWidget(self.name, 1)
        self.undo_button = self._button(edit, "撤销", self.map.undo)
        self.redo_button = self._button(edit, "重做", self.map.redo)
        self.loop_button = self._button(edit, "闭合路线", self.map.toggle_loop)
        layout.addLayout(edit)

        actions = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        actions.addWidget(self.summary, 1)
        self.export_button = self._button(actions, "导出 GPX…", self._export)
        self.save_button = self._button(actions, "保存修改" if existing is not None else "保存到路线库", self._save)
        self.save_button.setObjectName("saveDrawnRoute")
        self._button(actions, "关闭", self.reject)
        layout.addLayout(actions)

        self.tiles.changed.connect(self._update_map_status)
        self.map.route_changed.connect(self._update_route)
        self.name.textChanged.connect(self._update_actions)
        if route is not None:
            self.map.points = list(route.points[:-1] if route.closed else route.points)
            self.map.closed = route.closed
        self._update_route()
        self._update_map_status()

    @staticmethod
    def _button(layout, text, action):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def _jump(self):
        point = self._read_coordinate()
        if point is not None:
            self.map.move_view(project(point), 17)

    def _jump_to_preset(self, index):
        view = self.preset_choice.itemData(index)
        if view:
            self.settings.setValue(PRESET_SETTING, view["name"])
            self.settings.sync()
            self._fit_preset(view)

    def _fit_preset(self, view):
        west, south, east, north = view.get("focus_bounds", view["bounds"])
        self.map.fit_projected((project(Point(north, west)), project(Point(south, east))), padding=16)

    def _fit_content(self):
        if self.map.points:
            self.map.fit_route()
        elif self.preset_choice is not None and (view := self.preset_choice.currentData()):
            self._fit_preset(view)

    def _add_coordinate(self):
        point = self._read_coordinate()
        if point is not None and self.map.add_point(point):
            self.map.move_view(project(point), max(self.map.zoom, 17))
            self.location.clear()
            self.location.setFocus()

    def _read_coordinate(self):
        try:
            lat, lng = re.split(r"[,，\s]+", self.location.text().strip())
            point = parse_point(lat, lng)
            if abs(point.lat) > MAX_LATITUDE:
                raise ValueError
        except ValueError:
            QMessageBox.information(self, "请输入地图坐标",
                                    "请输入十进制的纬度和经度。地图支持约 ±85.05° 的纬度，经度须在 ±180° 内。")
            return None
        return point

    def showEvent(self, event):
        super().showEvent(event)
        if self._initial_view_pending:
            self._initial_view_pending = False
            QTimer.singleShot(0, self, self._fit_content)

    def _retry_tiles(self):
        self.tiles.failed.clear()
        self.map.request_tiles()

    def _update_map_status(self):
        self.retry_button.setVisible(bool(self.tiles.failed))
        if self.tiles.failed:
            text = ("无法加载 OpenStreetMap 底图。请检查网络或代理，然后点击「重试底图」。"
                    "也可以继续绘制和保存路线。")
        elif self.tiles.offline_visible and not self.tiles.has_online_tiles:
            text = "正在显示离线底图，无需联网。"
        elif self.tiles.pending:
            text = "正在加载 OpenStreetMap 底图，请稍候…"
        else:
            text = ""
        self.map_status.setText(text)

    def _update_route(self):
        self.route = None
        self.undo_button.setEnabled(bool(self.map.history))
        self.redo_button.setEnabled(bool(self.map.redo_history))
        distinct = len(set(self.map.points))
        self.add_coordinate.setEnabled(not self.map.closed and len(self.map.points) < MAX_ROUTE_POINTS)
        self.loop_button.setEnabled(self.map.closed or distinct >= 3)
        self.loop_button.setText("改为往返" if self.map.closed else "闭合路线")
        summary = f"{len(self.map.points)} 个点"
        if distinct >= 2:
            try:
                self.route = build_route(Path("drawn.gpx"), self.map.points, closed=self.map.closed)
            except ValueError as error:
                summary += f" · {error}"
            else:
                summary += f" · {self.route.kind} · {self.route.distance_summary}"
        self.summary.setText(summary)
        self._update_actions()

    def _update_actions(self):
        ready = self.route is not None and bool(self.name.text().strip())
        self.export_button.setEnabled(ready)
        self.save_button.setEnabled(ready)

    def _export(self):
        if not self.export_button.isEnabled():
            return
        if choose_gpx_destination(self, self.name.text(), self.route.points):
            self.summary.setText(self.summary.text().removesuffix(" · GPX 已导出") + " · GPX 已导出")

    def _save(self):
        if not self.save_button.isEnabled():
            return
        try:
            self.saved_path = save_route(self.directory / "routes", self.name.text(), self.route,
                                         existing=self.existing, reserved_names=self.reserved_names)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法保存路线", str(error))
            return
        self.accept()
