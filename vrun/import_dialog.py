# SPDX-License-Identifier: MPL-2.0
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout

from .route_library import MAX_ROUTE_NAME_LENGTH
from .routes import build_route
from .widgets import ChoiceBox, RoutePreview


class ImportRouteDialog(QDialog):
    def __init__(self, path, segments, colors, parent=None):
        super().__init__(parent)
        self.path = path
        self.segments = segments
        self.route = None
        self.setWindowTitle("导入 GPX 路线")
        self.resize(520, 340)
        layout = QVBoxLayout(self)
        self.segment_choice = ChoiceBox(self)
        self.segment_choice.set_popup_colors(colors)
        self.segment_choice.setSizeAdjustPolicy(ChoiceBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.segment_choice.setMinimumContentsLength(18)
        for segment in segments:
            self.segment_choice.addItem(f"{segment.description} · {segment.name[:MAX_ROUTE_NAME_LENGTH]}")
        if len(segments) > 1:
            layout.addWidget(QLabel("选择要导入的连续路段"))
            layout.addWidget(self.segment_choice)
        else:
            self.segment_choice.hide()
        self.name = QLineEdit()
        self.name.setMaxLength(MAX_ROUTE_NAME_LENGTH)
        self.name.setPlaceholderText("输入路线名称")
        layout.addWidget(QLabel("路线名称"))
        layout.addWidget(self.name)
        self.close_route = QCheckBox("保存为闭合环线")
        layout.addWidget(self.close_route)
        self.preview = RoutePreview()
        self.preview.colors = colors
        layout.addWidget(self.preview, 1)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self._update_save_button)
        self.segment_choice.currentIndexChanged.connect(self._preview_segment)
        self.close_route.toggled.connect(self._preview_route)
        self._preview_segment(0)

    def _preview_segment(self, index):
        segment = self.segments[index]
        self.name.setText(segment.name)
        with QSignalBlocker(self.close_route):
            self.close_route.setChecked(segment.points[0] == segment.points[-1])
        self._preview_route()

    def _preview_route(self):
        segment = self.segments[self.segment_choice.currentIndex()]
        self.route = None
        show_closing = False
        closed = self.close_route.isChecked()
        points = segment.points
        same_endpoints = points[0] == points[-1]
        if not closed and same_endpoints:
            end = len(points) - 1
            while end > 1 and points[0] == points[end - 1]:
                end -= 1
            points = points[:end]
        self.save_button.setText("保存路线")
        try:
            self.route = build_route(self.path, points, closed=closed)
            show_closing = closed and not same_endpoints
            summary = f"{self.route.kind} · {self.route.distance_summary}"
            if show_closing:
                gap_m = self.route.segment_lengths[-1]
                summary += f"。虚线将用 {gap_m:.1f} 米的直线连接终点和起点，请确认。"
                self.save_button.setText("确认补线并保存")
            elif not closed and same_endpoints:
                summary += "。已去掉返回起点的最后一段。"
        except ValueError as error:
            summary = str(error)
        self.summary.setText(summary)
        self.preview.set_route(self.route, show_closing_segment=show_closing)
        self._update_save_button()

    def _update_save_button(self):
        self.save_button.setEnabled(self.route is not None and bool(self.name.text().strip()))
