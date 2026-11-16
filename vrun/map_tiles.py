# SPDX-License-Identifier: MPL-2.0
"""Load the visible raster tiles for the configured map source."""
import os
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

from . import __version__
from .paths import app_dir


def _tianditu_key() -> str:
    key = os.environ.get("TIANDITU_KEY", "").strip()
    if key or getattr(sys, "frozen", False):
        return key
    try:
        config = (app_dir() / ".env").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    for line in config.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "TIANDITU_KEY":
            return value.strip().strip("\"'")
    return ""


class MapTiles(QObject):
    changed = Signal()
    base_url = "https://tile.openstreetmap.org"

    def __init__(self, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self._key = _tianditu_key()
        if self._key:
            self.attribution = ("https://www.tianditu.gov.cn/", "底图与注记 © 天地图")
            self.layers = ("vec", "cva")
        else:
            self.attribution = ("https://www.openstreetmap.org/copyright", "© OpenStreetMap contributors")
            self.layers = ("osm",)
            cache = QNetworkDiskCache(self.network)
            cache.setCacheDirectory(str(cache_dir))
            cache.setMaximumCacheSize(100 * 1024 * 1024)
            self.network.setCache(cache)
        self.images = {}
        self.pending = {}
        self.failed = set()
        self.denied = set()
        self.limited = set()
        self._tiles = {}

    def tile_url(self, key, layer):
        zoom, x, y = key
        if layer == "osm":
            return QUrl(f"{self.base_url}/{zoom}/{x}/{y}.png")
        url = QUrl(f"https://t0.tianditu.gov.cn/{layer}_w/wmts")
        query = QUrlQuery()
        for name, value in (("SERVICE", "WMTS"), ("REQUEST", "GetTile"), ("VERSION", "1.0.0"),
                            ("LAYER", layer), ("STYLE", "default"), ("TILEMATRIXSET", "w"),
                            ("FORMAT", "tiles"), ("TILEMATRIX", str(zoom)), ("TILECOL", str(x)),
                            ("TILEROW", str(y)), ("tk", self._key)):
            query.addQueryItem(name, value)
        url.setQuery(query)
        return url

    def show_tiles(self, keys):
        visible = {(key, layer) for key in keys for layer in self.layers}
        for layer_key in self.pending.keys() - visible:
            reply = self.pending.pop(layer_key)
            reply.abort()
        for key in self.images.keys() - keys:
            del self.images[key]
        for layer_key in self._tiles.keys() - visible:
            del self._tiles[layer_key]
        self.failed.intersection_update(visible)
        self.denied.intersection_update(visible)
        self.limited.intersection_update(visible)
        for layer_key in sorted(visible - self._tiles.keys() - self.pending.keys() - self.failed):
            key, layer = layer_key
            request = QNetworkRequest(self.tile_url(key, layer))
            request.setRawHeader(b"User-Agent", f"IRun/{__version__} (+https://github.com/meiyoushuru/IRun)".encode("ascii"))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy)
            request.setTransferTimeout(8000)
            reply = self.network.get(request)
            self.pending[layer_key] = reply
            reply.finished.connect(lambda layer_key=layer_key, reply=reply: self._finished(layer_key, reply))
        self.changed.emit()

    def _finished(self, layer_key, reply):
        try:
            if self.pending.get(layer_key) is not reply:
                return
            del self.pending[layer_key]
            image = QPixmap()
            if reply.error() != QNetworkReply.NetworkError.NoError or not image.loadFromData(reply.readAll()):
                self.failed.add(layer_key)
                if self._key:
                    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                    if status in (401, 403):
                        self.denied.add(layer_key)
                    elif status in (418, 429):
                        self.limited.add(layer_key)
            else:
                self.denied.discard(layer_key)
                self.limited.discard(layer_key)
                self._tiles[layer_key] = image
                key, _ = layer_key
                base = self._tiles.get((key, self.layers[0]))
                if base is not None:
                    labels = self._tiles.get((key, "cva"))
                    if labels is not None:
                        base = QPixmap(base)
                        with QPainter(base) as painter:
                            painter.drawPixmap(base.rect(), labels)
                    self.images[key] = base
            self.changed.emit()
        finally:
            reply.deleteLater()
