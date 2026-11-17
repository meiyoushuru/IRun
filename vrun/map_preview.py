# SPDX-License-Identifier: MPL-2.0
"""Non-interactive route previews over a fitted OpenStreetMap basemap."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QLabel

from .map_tiles import MapTiles
from .offline_maps import OSM_ATTRIBUTION
from .paths import local_data_root
from .route_map import TILE_SIZE, project, visible_tiles
from .widgets import RoutePreview


class MapRoutePreview(RoutePreview):
    def __init__(self, cache_dir=None, parent=None):
        super().__init__(parent)
        if cache_dir is None:
            cache_dir = local_data_root() / "IRun" / "map-cache"
        self.tiles = MapTiles(cache_dir, self)
        self.tiles.changed.connect(self._tiles_changed)
        self._projected = []
        self._view = None
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.setInterval(150)
        self._tile_timer.timeout.connect(self._request_tiles)

    def create_attribution_label(self, parent):
        url, name = OSM_ATTRIBUTION
        label = QLabel(f'<a href="{url}">© OpenStreetMap</a>', parent)
        label.setObjectName("muted")
        label.setOpenExternalLinks(True)
        label.setToolTip(name)
        label.setVisible(bool(self.tiles.images))
        self.tiles.changed.connect(lambda: label.setVisible(bool(self.tiles.images)))
        return label

    def _tiles_changed(self):
        self.setToolTip(
            "OpenStreetMap 底图加载失败。请检查是否使用了能访问该服务的网络代理。"
            if self.tiles.failed else ""
        )
        self.update()

    def set_route(self, route, **kwargs):
        super().set_route(route, **kwargs)
        self._projected = []
        if route is not None:
            preceding_x = project(route.points[0]).x()
            for point in route.points:
                projected = project(point)
                projected.setX(projected.x() + round(preceding_x - projected.x()))
                self._projected.append(projected)
                preceding_x = projected.x()
        self._fit_view()

    def _fit_view(self):
        if not self._projected:
            self._view = None
            self._tile_timer.stop()
            self.tiles.show_tiles(set())
            return
        left = min(point.x() for point in self._projected)
        right = max(point.x() for point in self._projected)
        top = min(point.y() for point in self._projected)
        bottom = max(point.y() for point in self._projected)
        scale = max((right - left) / max(1, self.width() - 48),
                    (bottom - top) / max(1, self.height() - 44), 1 / (TILE_SIZE * 2 ** 19))
        world = 1 / scale
        zoom = max(0, min(19, round(math.log2(world / TILE_SIZE))))
        origin = QPointF((left + right) * world / 2 - self.width() / 2,
                         (top + bottom) * world / 2 - self.height() / 2)
        self._view = world, zoom, origin
        if self.isVisible():
            self._tile_timer.start()
        self.update()

    def _visible_tiles(self):
        if self._view is not None:
            world, zoom, origin = self._view
            yield from visible_tiles(origin, self.width(), self.height(), zoom, world)

    def _request_tiles(self):
        self.tiles.show_tiles({key for key, _ in self._visible_tiles()})

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_view()

    def hideEvent(self, event):
        self._tile_timer.stop()
        self.tiles.show_tiles(set())
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_view()

    def _mapped_points(self, painter):
        world, _, origin = self._view
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 9, 9)
        painter.setClipPath(clip)
        for key, target in self._visible_tiles():
            image = self.tiles.images.get(key)
            if image is not None:
                painter.drawPixmap(target, image, QRectF(image.rect()))
        return [point * world - origin for point in self._projected]

    def _paint_track(self, painter, full):
        for color, width in ((QColor("#ffffff"), 6), (QColor(self.colors["ink"]), 3)):
            painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(full)
