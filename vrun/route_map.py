# SPDX-License-Identifier: MPL-2.0
"""Interactive top-down Web Mercator map with editable WGS-84 route points."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QToolButton, QVBoxLayout, QWidget

from .routes import MAX_ROUTE_POINTS, Point


TILE_SIZE = 256
MAX_LATITUDE = 85.0511287798066
EARTH_RADIUS_M = 6378137


def project(point: Point) -> QPointF:
    latitude = math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, point.lat)))
    return QPointF((point.lng + 180) / 360,
                   (1 - math.asinh(math.tan(latitude)) / math.pi) / 2)


def unproject(position: QPointF) -> Point:
    x = position.x() % 1
    y = max(0, min(1, position.y()))
    return Point(math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y)))), x * 360 - 180)


def visible_tiles(origin, width, height, zoom, world_size):
    count = 2 ** zoom
    tile_size = world_size / count
    left = math.floor(origin.x() / tile_size)
    right = math.ceil((origin.x() + width) / tile_size)
    top = max(0, math.floor(origin.y() / tile_size))
    bottom = min(count, math.ceil((origin.y() + height) / tile_size))
    for x in range(left, right):
        for y in range(top, bottom):
            yield (zoom, x % count, y), QRectF(x * tile_size - origin.x(),
                y * tile_size - origin.y(), tile_size, tile_size)


class MapScale(QWidget):
    def __init__(self, colors, parent):
        super().__init__(parent)
        self.colors = colors
        self.pixels = 0.0
        self.label = ""
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(124, 42)

    def set_resolution(self, meters_per_pixel):
        maximum = meters_per_pixel * 100
        magnitude = 10 ** math.floor(math.log10(maximum))
        distance = next(value * magnitude for value in (5, 2, 1) if value * magnitude <= maximum)
        self.pixels = distance / meters_per_pixel
        self.label = f"{distance / 1000:g} 公里" if distance >= 1000 else f"{distance:g} 米"
        self.setAccessibleName(f"地图比例尺 {self.label}")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(self.colors["border"]))
        painter.setBrush(QColor(self.colors["surface"]))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 6, 6)
        painter.setPen(QColor(self.colors["text"]))
        painter.drawText(QRectF(10, 4, self.width() - 18, 22), Qt.AlignLeft | Qt.AlignVCenter, self.label)
        painter.setPen(QPen(QColor(self.colors["text"]), 1.5))
        start = QPointF(11, 32)
        end = start + QPointF(self.pixels, 0)
        painter.drawLine(start, end)
        for mark in (start, end):
            painter.drawLine(mark - QPointF(0, 4), mark + QPointF(0, 2))


class RouteMap(QWidget):
    route_changed = Signal()

    def __init__(self, center, tiles, colors, parent=None):
        super().__init__(parent)
        self.center = project(center)
        self.zoom = 16
        self.tiles = tiles
        self.colors = colors
        self.points = []
        self.closed = False
        self.history = []
        self.redo_history = []
        self.press = None
        self.dragged = False
        self.point_index = None
        self.setMinimumSize(420, 300)
        self.setAccessibleName("路线绘制地图")
        self.tile_timer = QTimer(self)
        self.tile_timer.setSingleShot(True)
        self.tile_timer.setInterval(150)
        self.tile_timer.timeout.connect(self.request_tiles)
        self.tiles.changed.connect(self.update)
        self.zoom_controls = QFrame(self)
        self.zoom_controls.setObjectName("mapZoomControls")
        self.zoom_controls.setStyleSheet(f"""
            QFrame#mapZoomControls {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 6px; }}
            QToolButton {{ background: transparent; color: {colors['text']}; border: none; font-size: 18px; font-weight: 600; }}
            QToolButton:hover {{ background: {colors['field']}; }}
            QToolButton:disabled {{ color: {colors['muted']}; }}
        """)
        zoom_layout = QVBoxLayout(self.zoom_controls)
        zoom_layout.setContentsMargins(1, 1, 1, 1)
        zoom_layout.setSpacing(0)
        self.zoom_in = self._zoom_button(zoom_layout, "+", "放大地图", 1)
        self.zoom_out = self._zoom_button(zoom_layout, "−", "缩小地图", -1)
        self.zoom_controls.adjustSize()
        self.zoom_controls.move(12, 12)
        self.scale_bar = MapScale(colors, self)
        self._refresh_controls()

    def _zoom_button(self, layout, text, label, step):
        button = QToolButton(self.zoom_controls)
        button.setText(text)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.setFixedSize(32, 30)
        button.clicked.connect(lambda: self.zoom_by(step))
        layout.addWidget(button)
        return button

    def _refresh_controls(self):
        self.zoom_in.setEnabled(self.zoom < 19)
        self.zoom_out.setEnabled(self.zoom > 2)
        latitude = unproject(self.center).lat
        resolution = 2 * math.pi * EARTH_RADIUS_M * math.cos(math.radians(latitude)) / self.world_size
        self.scale_bar.set_resolution(resolution)

    @property
    def world_size(self):
        return TILE_SIZE * (2 ** self.zoom)

    @property
    def midpoint(self):
        return QPointF(self.width() / 2, self.height() / 2)

    @property
    def origin(self):
        return self.center * self.world_size - self.midpoint

    def move_view(self, center, zoom):
        self.center = QPointF(center.x() % 1, max(0, min(1, center.y())))
        self.zoom = zoom
        self._refresh_controls()
        self.tile_timer.start()
        self.update()

    def zoom_by(self, steps, anchor=None):
        zoom = max(2, min(19, self.zoom + steps))
        if zoom == self.zoom:
            return
        offset = (anchor if anchor is not None else self.midpoint) - self.midpoint
        self.move_view(self.center + offset / self.world_size - offset / (TILE_SIZE * 2 ** zoom), zoom)

    def fit_route(self):
        if not self.points:
            return
        points = [self.origin + point for point in self._screen_points()]
        self.fit_projected([point / self.world_size for point in points])

    def fit_projected(self, projected, *, padding=64, minimum_zoom=2):
        left, right = min(p.x() for p in projected), max(p.x() for p in projected)
        top, bottom = min(p.y() for p in projected), max(p.y() for p in projected)
        scale = max((right - left) / max(1, self.width() - padding),
                    (bottom - top) / max(1, self.height() - padding))
        zoom = max(minimum_zoom, min(19, math.floor(-math.log2(TILE_SIZE * scale)))) if scale else 16
        self.move_view(QPointF((left + right) / 2, (top + bottom) / 2), zoom)

    def resizeEvent(self, event):
        self.scale_bar.move(12, self.height() - self.scale_bar.height() - 12)
        self.tile_timer.start()
        super().resizeEvent(event)

    def _visible_tiles(self):
        return visible_tiles(self.origin, self.width(), self.height(), self.zoom, self.world_size)

    def request_tiles(self):
        self.tiles.show_tiles({key for key, _ in self._visible_tiles()})

    def _screen_points(self):
        previous = self.center.x()
        origin = self.origin
        result = []
        for point in self.points:
            projected = project(point)
            x = projected.x() + round(previous - projected.x())
            previous = x
            result.append(QPointF(x, projected.y()) * self.world_size - origin)
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.colors["field"]))
        for key, rect in self._visible_tiles():
            tile = self.tiles.images.get(key)
            if tile is not None:
                painter.drawPixmap(rect, tile, QRectF(tile.rect()))
            else:
                painter.setPen(QColor(self.colors["border"]))
                painter.drawRect(rect)
        points = self._screen_points()
        if not points:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        if self.closed:
            path.closeSubpath()
        ink = QColor("#2057d2")
        painter.setBrush(Qt.NoBrush)
        for color, width in ((QColor("white"), 7), (ink, 4)):
            painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
        for index, point in enumerate(points):
            painter.setPen(QPen(ink, 2))
            painter.setBrush(QColor("#94e6b0" if index == 0 else "white"))
            painter.drawEllipse(point, 6 if index == 0 else 4, 6 if index == 0 else 4)

    def remember(self):
        self.history.append((self.points.copy(), self.closed))
        self.redo_history.clear()
        if len(self.history) > 100:
            self.history.pop(0)

    def undo(self):
        if self.history:
            self.redo_history.append((self.points, self.closed))
            self.points, self.closed = self.history.pop()
            self._update_route()

    def redo(self):
        if self.redo_history:
            self.history.append((self.points, self.closed))
            self.points, self.closed = self.redo_history.pop()
            self._update_route()

    def contextMenuEvent(self, event):
        position = QPointF(event.pos())
        points = self._screen_points()
        point_index = next((i for i, p in enumerate(points) if (p - position).manhattanLength() <= 9), None)
        menu = QMenu(self)
        if point_index is not None:
            action = menu.addAction("删除路线点")
            if menu.exec(event.globalPos()) == action:
                self.remember()
                self.points.pop(point_index)
                self._update_route()
            return
        if len(points) < 2 or len(points) >= MAX_ROUTE_POINTS:
            return
        ends = points[1:] + (points[:1] if self.closed else [])
        nearest = 10
        insertion = None
        for index, (start, end) in enumerate(zip(points, ends), 1):
            delta = end - start
            squared = QPointF.dotProduct(delta, delta)
            if squared:
                ratio = max(0, min(1, QPointF.dotProduct(position - start, delta) / squared))
                candidate = start + ratio * delta
                distance = (candidate - position).manhattanLength()
                if distance < nearest:
                    nearest, insertion = distance, (index, candidate)
        if insertion is not None:
            action = menu.addAction("在线段插入路线点")
            if menu.exec(event.globalPos()) == action:
                index, candidate = insertion
                self.remember()
                self.points.insert(index, unproject((self.origin + candidate) / self.world_size))
                self._update_route()

    def toggle_loop(self):
        if self.closed or len(set(self.points)) >= 3:
            self.remember()
            self.closed = not self.closed
            self._update_route()

    def add_point(self, point):
        if self.closed or len(self.points) >= MAX_ROUTE_POINTS:
            return False
        self.remember()
        self.points.append(point)
        self._update_route()
        return True

    def _update_route(self):
        self.update()
        self.route_changed.emit()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta:
            self.zoom_by(1 if delta > 0 else -1, event.position())
        event.accept()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.press = self.last = event.position()
        self.dragged = False
        self.point_index = next((i for i, point in enumerate(self._screen_points())
                                 if (point - self.press).manhattanLength() <= 9), None)

    def mouseMoveEvent(self, event):
        if self.press is not None:
            self._drag_to(event.position())

    def _drag_to(self, position):
        if not self.dragged and (position - self.press).manhattanLength() <= QApplication.startDragDistance():
            return
        if self.point_index is None:
            self.move_view(self.center - (position - self.last) / self.world_size, self.zoom)
        else:
            if not self.dragged:
                self.remember()
            self.points[self.point_index] = unproject((self.origin + position) / self.world_size)
            self._update_route()
        self.dragged = True
        self.last = position

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.press is None:
            return
        position = event.position()
        if position != self.last:
            self._drag_to(position)
        if not self.dragged and not self.closed:
            if self.point_index == 0:
                self.toggle_loop()
            elif self.point_index is None:
                self.add_point(unproject((self.origin + position) / self.world_size))
        self.press = None
