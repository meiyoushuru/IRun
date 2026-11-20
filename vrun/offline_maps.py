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


def read_map_views(archive):
    views = json.loads(archive.read("views.json"))
    if not isinstance(views, list):
        raise ValueError("invalid preset views")
    for view in views:
        if not isinstance(view, dict) or not isinstance(view.get("name"), str) or not view["name"].strip():
            raise ValueError("invalid preset name")
        for bounds in (view.get("bounds"), view.get("focus_bounds", view.get("bounds"))):
            if (not isinstance(bounds, list) or len(bounds) != 4
                    or any(type(coordinate) not in (int, float) for coordinate in bounds)):
                raise ValueError("invalid preset bounds")
            west, south, east, north = bounds
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("invalid preset bounds")
    return views


class OfflineMaps:
    def __init__(self, path=None):
        self.path = path if path is not None else resource_dir() / OFFLINE_MAP_ASSET
        self.views = []
        self.names = set()
        try:
            with ZipFile(self.path) as archive:
                self.views = read_map_views(archive)
                self.names = set(archive.namelist())
        except FileNotFoundError:
            pass
        except (OSError, BadZipFile, KeyError, ValueError):
            LOG.warning("Offline map archive unavailable", exc_info=True)

    @staticmethod
    def tile_name(key, layer="tiles"):
        z, x, y = key
        return f"{layer}/{z}/{x}/{y}.png"

    def load(self, keys):
        return self._load(keys, "tiles", max_parent_shift=2)

    def load_overlays(self, keys):
        return self._load(keys, "overlays")

    def _load(self, keys, layer, *, max_parent_shift=0):
        matches = []
        for key in keys:
            z, x, y = key
            for shift in range(min(z, max_parent_shift) + 1):
                name = self.tile_name((z - shift, x >> shift, y >> shift), layer)
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
                        try:
                            payload = archive.read(name)
                        except KeyError:
                            continue  # The archive may have been replaced since it was indexed.
                        image = QPixmap()
                        image.loadFromData(payload, "PNG")
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
