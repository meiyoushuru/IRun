# SPDX-License-Identifier: MPL-2.0
"""Request OpenStreetMap tiles where no bundled tiles are available."""
import logging
import math

from PySide6.QtCore import QObject, QRectF, QUrl, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

from . import __version__
from .offline_maps import OfflineMaps

LOG = logging.getLogger(__name__)
TILE_SIZE = 256
MAX_TILE_ZOOM = 19


def tile_zoom(zoom):
    return max(0, min(MAX_TILE_ZOOM, round(zoom)))


def visible_tiles(origin, width, height, zoom, world_size):
    count = 2 ** zoom
    tile_size = world_size / count
    left = math.floor(origin.x() / tile_size)
    right = math.ceil((origin.x() + width) / tile_size)
    top = max(0, math.floor(origin.y() / tile_size))
    bottom = min(count, math.ceil((origin.y() + height) / tile_size))
    for x in range(left, right):
        for y in range(top, bottom):
            yield (zoom, x % count, y), QRectF(
                x * tile_size - origin.x(), y * tile_size - origin.y(), tile_size, tile_size)


class MapTiles(QObject):
    changed = Signal()
    osm_url = "https://tile.openstreetmap.org"

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self.network)
        cache.setCacheDirectory(str(cache_dir))
        cache.setMaximumCacheSize(100 * 1024 * 1024)
        self.network.setCache(cache)
        self.offline = OfflineMaps()
        self.images = {}
        self.overlays = {}
        self._previous_images = {}
        self._zoom = None
        self.offline_visible = set()
        self.has_online_tiles = False
        self.pending = {}
        self.failed = set()

    def tile_url(self, key):
        zoom, x, y = key
        return QUrl(f"{self.osm_url}/{zoom}/{x}/{y}.png")

    def show_tiles(self, keys):
        zoom = next(iter(keys))[0] if keys else None
        if zoom is None:
            self._previous_images.clear()
        elif self._zoom != zoom and self.images:
            self._previous_images = self.images.copy()
        self._zoom = zoom
        for key in self.overlays.keys() - keys:
            del self.overlays[key]
        self.overlays.update(self.offline.load_overlays(keys - self.overlays.keys()))
        local = self.offline.load(keys - self.offline_visible)
        self.offline_visible.intersection_update(keys)
        self.offline_visible.update(local)
        self.images.update(local)
        online_keys = keys - self.offline_visible
        self.has_online_tiles = bool(online_keys)
        for key in self.pending.keys() - online_keys:
            self.pending.pop(key).abort()
        for key in self.images.keys() - keys:
            del self.images[key]
        if keys <= self.images.keys():
            self._previous_images.clear()
        self.failed.intersection_update(online_keys)
        for key in sorted(online_keys - self.images.keys() - self.pending.keys() - self.failed):
            request = QNetworkRequest(self.tile_url(key))
            request.setRawHeader(b"User-Agent", f"IRun/{__version__} (+https://github.com/meiyoushuru/IRun)".encode("ascii"))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy)
            request.setTransferTimeout(8000)
            reply = self.network.get(request)
            self.pending[key] = reply
            reply.finished.connect(lambda key=key, reply=reply: self._finished(key, reply))
        self.changed.emit()

    def paint(self, painter, origin, width, height, world_size):
        # A resize can cross a tile zoom boundary before the new tiles arrive.
        # Draw each loaded layer at its geographic position in the new view.
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        for layer in (self._previous_images, self.images, self.overlays):
            for (zoom, x, y), image in layer.items():
                size = world_size / 2 ** zoom
                top = y * size - origin.y()
                if top + size <= 0 or top >= height:
                    continue
                left = x * size - origin.x()
                first = math.floor(-(left + size) / world_size) + 1
                last = math.ceil((width - left) / world_size)
                for wrap in range(first, last):
                    target = QRectF(left + wrap * world_size, top, size, size)
                    painter.drawPixmap(target, image, QRectF(image.rect()))
        painter.restore()

    @property
    def has_basemap(self):
        return bool(self.images or self._previous_images)

    def _finished(self, key, reply):
        try:
            if self.pending.get(key) is not reply:
                return
            del self.pending[key]
            image = QPixmap()
            if reply.error() != QNetworkReply.NetworkError.NoError or not image.loadFromData(reply.readAll()):
                if not self.failed:
                    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                    LOG.warning("OpenStreetMap tile unavailable: HTTP %s, network error %s", status, reply.error())
                self.failed.add(key)
            else:
                self.images[key] = image
                if not self.pending and not self.failed:
                    self._previous_images.clear()
            self.changed.emit()
        finally:
            reply.deleteLater()
