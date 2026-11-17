# SPDX-License-Identifier: MPL-2.0
"""Read an optional archive of pre-rendered map tiles and preset views."""
import json
import logging
from zipfile import BadZipFile, ZipFile

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from .paths import resource_dir

LOG = logging.getLogger(__name__)
OSM_ATTRIBUTION = ("https://www.openstreetmap.org/copyright", "© OpenStreetMap contributors")
OFFLINE_MAP_ASSET = "assets/offline-maps.zip"


class OfflineMaps:
    def __init__(self, path=None):
        self.path = path if path is not None else resource_dir() / OFFLINE_MAP_ASSET
        self.views = []
        self.names = set()
        try:
            with ZipFile(self.path) as archive:
                self.views = json.loads(archive.read("views.json"))
                self.names = set(archive.namelist())
        except FileNotFoundError:
            pass
        except (OSError, BadZipFile, KeyError, ValueError):
            LOG.warning("Offline map archive unavailable", exc_info=True)

    @staticmethod
    def tile_name(key):
        z, x, y = key
        return f"tiles/{z}/{x}/{y}.png"

    def load(self, keys):
        matches = []
        for key in keys:
            z, x, y = key
            for shift in range(min(z, 2) + 1):
                name = self.tile_name((z - shift, x >> shift, y >> shift))
                if name in self.names:
                    matches.append((key, name, 2 ** shift))
                    break
        if not matches:
            return {}
        tiles = {}
        try:
            with ZipFile(self.path) as archive:
                decoded = {}
                for key, name, ratio in matches:
                    if name in decoded:
                        image = decoded[name]
                    else:
                        image = QPixmap()
                        image.loadFromData(archive.read(name), "PNG")
                        decoded[name] = image
                    if image.isNull():
                        continue
                    if ratio > 1:
                        _, x, y = key
                        side = image.width() // ratio
                        image = image.copy((x % ratio) * side, (y % ratio) * side, side, side)
                        image = image.scaled(side * ratio, side * ratio, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                    tiles[key] = image
        except (OSError, BadZipFile):
            LOG.warning("Could not read offline map tiles", exc_info=True)
        return tiles
