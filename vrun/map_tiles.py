# SPDX-License-Identifier: MPL-2.0
"""Request OpenStreetMap tiles where no bundled tiles are available."""
import logging

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

from . import __version__
from .offline_maps import OfflineMaps

LOG = logging.getLogger(__name__)


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
        self.offline_visible = set()
        self.has_online_tiles = False
        self.pending = {}
        self.failed = set()

    def tile_url(self, key):
        zoom, x, y = key
        return QUrl(f"{self.osm_url}/{zoom}/{x}/{y}.png")

    def show_tiles(self, keys):
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
            self.changed.emit()
        finally:
            reply.deleteLater()
