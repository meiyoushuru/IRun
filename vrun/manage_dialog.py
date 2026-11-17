# SPDX-License-Identifier: MPL-2.0
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QPushButton, QVBoxLayout)

from .map_preview import MapRoutePreview
from .route_library import load_saved_route
from .routes import load_route
from .widgets import ChoiceBox


class RouteManagerDialog(QDialog):
    def __init__(self, colors, parent=None):
        super().__init__(parent)
        self.setWindowTitle("路线管理")
        self.setObjectName("routeManager")
        self.resize(720, 475)
        self.entries = []
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索路线名称")
        self.search.setAccessibleName("搜索路线名称")
        filters.addWidget(self.search, 1)
        self.kind_filter = ChoiceBox()
        self.kind_filter.set_popup_colors(colors)
        self.kind_filter.addItem("全部类型", None)
        self.kind_filter.addItem("闭合环线", True)
        self.kind_filter.addItem("非闭合 · 往返", False)
        filters.addWidget(self.kind_filter)
        layout.addLayout(filters)

        content = QHBoxLayout()
        self.routes = QListWidget()
        self.routes.setAccessibleName("已保存路线")
        self.routes.setAlternatingRowColors(True)
        content.addWidget(self.routes, 2)
        details = QVBoxLayout()
        self.preview = MapRoutePreview(parent=self)
        self.preview.colors = colors
        details.addWidget(self.preview, 1)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(self.preview.create_attribution_label(self))
        details.addLayout(footer)
        content.addLayout(details, 3)
        layout.addLayout(content, 1)

        actions = QHBoxLayout()
        self.import_button = self._button(actions, "导入…")
        self.draw_button = self._button(actions, "绘制…")
        self.edit_button = self._button(actions, "编辑…")
        self.rename_button = self._button(actions, "重命名…")
        self.copy_button = self._button(actions, "复制")
        self.export_button = self._button(actions, "导出…")
        self.delete_button = self._button(actions, "删除…")
        actions.addStretch()
        self.use_button = self._button(actions, "使用路线")
        self.use_button.setObjectName("start")
        layout.addLayout(actions)
        self.feedback = QLabel()
        self.feedback.setObjectName("settingsFeedback")
        self.feedback.setTextFormat(Qt.PlainText)
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self.use_button.clicked.connect(self.accept)
        self.routes.itemDoubleClicked.connect(lambda _: self.use_button.click())
        self.routes.currentItemChanged.connect(self._preview)
        self.search.textChanged.connect(self._refilter)
        self.kind_filter.currentIndexChanged.connect(self._refilter)

    @staticmethod
    def _button(layout, text):
        button = QPushButton(text)
        button.setAutoDefault(False)
        layout.addWidget(button)
        return button

    @property
    def current_path(self):
        item = self.routes.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def set_entries(self, entries, selected):
        self.entries = entries
        self._populate(selected)

    def _refilter(self, *_):
        self._populate(self.current_path)

    def _populate(self, selected):
        query = self.search.text().strip().casefold()
        kind = self.kind_filter.currentData()
        self.routes.blockSignals(True)
        self.routes.clear()
        selected_row = 0
        for name, path, closed in self.entries:
            if query not in name.casefold() or kind is not None and kind != closed:
                continue
            item = QListWidgetItem(f"{name}  ·  {'闭合环线' if closed else '非闭合往返'}")
            item.setToolTip(name)
            item.setData(Qt.UserRole, path)
            self.routes.addItem(item)
            if path == selected:
                selected_row = self.routes.count() - 1
        self.routes.setCurrentRow(selected_row)
        self.routes.blockSignals(False)
        self._preview()

    def _preview(self, *_):
        path = self.current_path
        route = None
        summary = "没有符合条件的路线"
        user_route = False
        if path:
            path = Path(path)
            user_route = path.suffix == ".json"
            try:
                route = load_saved_route(path) if user_route else load_route(path)
                summary = f"{route.kind} · {route.distance_summary}"
            except (OSError, ValueError) as error:
                summary = str(error)
        self.preview.set_route(route)
        self.summary.setText(summary)
        self.edit_button.setText("编辑…" if user_route else "编辑副本…")
        for button in (self.edit_button, self.copy_button, self.export_button, self.use_button):
            button.setEnabled(route is not None)
        self.rename_button.setEnabled(user_route)
        self.delete_button.setEnabled(user_route)
