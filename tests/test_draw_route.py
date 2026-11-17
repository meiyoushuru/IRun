# SPDX-License-Identifier: MPL-2.0
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from zipfile import ZipFile

import pytest

from PySide6.QtCore import (QBuffer, QCoreApplication, QEvent, QEventLoop, QIODevice, QObject, QPoint, QPointF,
                            QSettings, Qt, QTimer, Signal)
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from vrun.app import Window
from vrun.draw_dialog import DrawRouteDialog, PRESET_SETTING
from vrun.import_dialog import ImportRouteDialog
from vrun.manage_dialog import RouteManagerDialog
from vrun.map_preview import MapRoutePreview
from vrun.map_tiles import MapTiles
from vrun.offline_maps import OFFLINE_MAP_ASSET, OfflineMaps
from vrun.route_library import GpxSegment, export_gpx, load_gpx, save_route
from vrun.route_map import project, unproject
from vrun.routes import Point, build_route


class UnavailableTiles(QObject):
    changed = Signal()
    attribution = ("https://www.openstreetmap.org/copyright", "© OpenStreetMap contributors")

    def __init__(self, cache_dir, parent):
        super().__init__(parent)
        from types import SimpleNamespace
        self.offline = SimpleNamespace(views=[])
        self.offline_visible = set()
        self.has_online_tiles = True
        self.images = {}
        self.pending = {}
        self.failed = set()

    def show_tiles(self, keys):
        self.failed = keys
        self.changed.emit()


def test_export_gpx_round_trips_precise_coordinates_and_name(tmp_path):
    path = tmp_path / "track.gpx"
    points = (Point(0.00000001, 120), Point(30, -119.9999999999))
    export_gpx(path, "路线 <A> & B", points)
    assert b'lat="0.00000001"' in path.read_bytes()
    track, = load_gpx(path)
    assert track.name == "路线 <A> & B"
    assert track.points == points


def test_draw_drag_undo_close_save_and_export(application, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("vrun.draw_dialog.MapTiles", UnavailableTiles)
    destinations = iter([tmp_path / "open", tmp_path / "loop.gpx"])
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(next(destinations)), ""))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    window = Window(settings=settings, poll=False)
    expected = []

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, DrawRouteDialog)
            canvas = dialog.map
            assert canvas.zoom_controls.pos() == QPoint(12, 12)
            assert canvas.scale_bar.y() + canvas.scale_bar.height() + 12 == canvas.height()
            dialog.location.setText("31.221, 121.436")
            dialog.location.returnPressed.emit()
            assert (project(Point(31.221, 121.436)) - canvas.center).manhattanLength() < 1e-9
            original_zoom, original_scale = canvas.zoom, canvas.scale_bar.label
            canvas.zoom_in.click()
            assert canvas.zoom == original_zoom + 1
            assert canvas.scale_bar.label != original_scale
            canvas.zoom_out.click()
            assert canvas.zoom == original_zoom
            assert canvas.scale_bar.label == original_scale
            anchor = QPointF(170, 120)
            start = (canvas.origin + anchor) / canvas.world_size
            canvas.zoom_by(1, anchor)
            assert ((canvas.origin + anchor) / canvas.world_size - start).manhattanLength() < 1e-9
            canvas.request_tiles()
            assert "网络代理" in dialog.map_status.text()

            positions = [QPoint(120, 130), QPoint(220, 135), QPoint(210, 240)]
            for position in positions:
                QTest.mouseClick(canvas, Qt.LeftButton, pos=position)
            assert len(canvas.points) == 3
            origin = canvas.center
            QTest.mousePress(canvas, Qt.LeftButton, pos=QPoint(350, 230))
            QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(380, 250))
            assert canvas.center != origin
            assert len(canvas.points) == 3
            canvas.move_view(origin, canvas.zoom)
            old_point = canvas.points[1]
            QTest.mousePress(canvas, Qt.LeftButton, pos=positions[1])
            QTest.mouseRelease(canvas, Qt.LeftButton, pos=QPoint(245, 155))
            assert canvas.points[1] != old_point
            dialog.undo_button.click()
            assert canvas.points[1] == old_point
            assert dialog.save_button.isEnabled()
            assert not dialog.route.closed
            dialog.redo_button.click()
            assert canvas.points[1] != old_point
            dialog.undo_button.click()
            dialog.name.setText("新绘制的环线")
            dialog.export_button.click()
            track, = load_gpx(tmp_path / "open.gpx")
            assert track.points == tuple(canvas.points)

            QTest.mouseClick(canvas, Qt.LeftButton, pos=positions[0])
            assert canvas.closed
            assert dialog.save_button.isEnabled()
            assert "每圈" in dialog.summary.text()
            expected.append(dialog.route.points)
            dialog.save_button.click()
        finally:
            if dialog.result() != QDialog.Accepted:
                dialog.reject()

    QTimer.singleShot(0, interact)
    window.draw_button.click()
    try:
        assert expected
        assert window.routes.currentText() == "新绘制的环线"
        assert window.route.points == expected[0]
        window.export_button.click()
        track, = load_gpx(tmp_path / "loop.gpx")
        assert track.points == expected[0]
        assert track.points[0] == track.points[-1]
    finally:
        window.close()
        application.processEvents()


def test_map_projection_round_trip():
    point = unproject(project(Point(30.307, 120.083)))
    assert abs(point.lat - 30.307) < 1e-10
    assert abs(point.lng - 120.083) < 1e-10


@pytest.fixture
def bundled_maps(application, tmp_path, monkeypatch):
    views = [
        {"name": f"测试地点 {label}", "lat": 0, "lng": lng, "zoom": 17,
         "bounds": [lng - 0.005, -0.005, lng + 0.005, 0.005],
         "focus_bounds": [lng - 0.005, -0.005, lng + 0.005, 0.005]}
        for label, lng in (("A", 0), ("B", 0.05))
    ]
    image = QImage(256, 256, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    pack = tmp_path / OFFLINE_MAP_ASSET
    pack.parent.mkdir()
    keys = set()
    for view in views:
        for zoom in range(13, 20):
            world = 2 ** zoom
            center = project(Point(view["lat"], view["lng"]))
            x, y = int(center.x() * world), int(center.y() * world)
            keys.update((zoom, x + dx, y + dy) for dx in range(-8, 9) for dy in range(-8, 9))
    with ZipFile(pack, "w") as archive:
        archive.writestr("views.json", json.dumps(views))
        png = bytes(buffer.data())
        for key in sorted(keys):
            archive.writestr(OfflineMaps.tile_name(key), png)
    monkeypatch.setattr("vrun.offline_maps.resource_dir", lambda: tmp_path)


def test_bundled_presets_load_offline_without_tile_requests(application, tmp_path, bundled_maps):
    tiles = MapTiles(tmp_path / "cache")
    assert {view["name"] for view in tiles.offline.views} == {"测试地点 A", "测试地点 B"}
    for view in tiles.offline.views:
        west, south, east, north = view["focus_bounds"]
        for zoom in range(13, 20):
            for corner in (Point(north, west), Point(south, west), Point(north, east), Point(south, east)):
                edge = project(corner)
                corner_key = zoom, int(edge.x() * 2 ** zoom), int(edge.y() * 2 ** zoom)
                assert tiles.offline.tile_name(corner_key) in tiles.offline.names
        position = project(Point(view["lat"], view["lng"]))
        zoom = view["zoom"]
        size = 2 ** zoom
        cx, cy = int(position.x() * size), int(position.y() * size)
        keys = {(zoom, cx + x, cy + y) for x in (-1, 0, 1) for y in (-1, 0, 1)}
        tiles.show_tiles(keys)
        assert tiles.offline_visible == keys
        assert tiles.images.keys() == keys
        assert all(image.width() == 256 for image in tiles.images.values())
        assert not tiles.has_online_tiles
        assert not tiles.pending
    tiles.deleteLater()
    QCoreApplication.sendPostedEvents(tiles, QEvent.DeferredDelete)


def test_main_map_preview_fits_route_and_uses_offline_tiles(application, tmp_path, bundled_maps):
    from vrun import theme

    preview = MapRoutePreview(tmp_path / "cache")
    preview.colors = theme.colors("light")
    preview.resize(380, 200)
    points = (Point(0.001, 0.001), Point(0.002, 0.002))
    preview.set_route(build_route(tmp_path / "demo.gpx", points, closed=False))
    try:
        preview._request_tiles()
        assert preview.tiles.offline_visible
        assert not preview.tiles.has_online_tiles
        assert not preview.tiles.pending
        world, _, origin = preview._view
        for point in points:
            pixel = project(point) * world - origin
            assert 20 < pixel.x() < preview.width() - 20
            assert 20 < pixel.y() < preview.height() - 20
        assert not preview.grab().isNull()
        preview.set_route(None)
        assert not preview.tiles.images
    finally:
        preview.deleteLater()
        QCoreApplication.sendPostedEvents(preview, QEvent.DeferredDelete)


@pytest.mark.parametrize("manage", [True, False])
def test_route_dialog_displays_basemap(application, tmp_path, monkeypatch, bundled_maps, manage):
    from PySide6.QtWidgets import QLabel
    from vrun import theme

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    colors = theme.colors("light")
    path = tmp_path / "preview.gpx"
    points = (Point(0.001, 0.001), Point(0.002, 0.002), Point(0.001, 0.002))
    if manage:
        dialog = RouteManagerDialog(colors)
        saved = save_route(tmp_path / "routes", "示例路线", build_route(path, points, closed=False))
        dialog.set_entries([("示例路线", str(saved), False)], str(saved))
    else:
        dialog = ImportRouteDialog(path, [GpxSegment("示例路线", "轨迹", points)], colors)
        dialog.close_route.setChecked(True)
        assert dialog.preview.show_closing_segment

    try:
        dialog.show()
        application.processEvents()
        preview = dialog.preview
        preview._request_tiles()
        assert preview.tiles.offline_visible
        assert not preview.tiles.pending
        assert preview.grab().toImage().pixelColor(16, 16) == Qt.white
        assert any(widget.isVisible() and "© OpenStreetMap" in widget.text()
                   for widget in dialog.findChildren(QLabel))
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)


def test_closed_route_preserves_the_basemap_inside_the_outline(application, tmp_path):
    from PySide6.QtGui import QColor, QPixmap
    from vrun import theme

    preview = MapRoutePreview(tmp_path / "cache")
    preview.colors = theme.colors("light")
    preview.resize(380, 200)
    preview.set_route(build_route(tmp_path / "ring.gpx", (
        Point(0.001, 0.001), Point(0.002, 0.001),
        Point(0.002, 0.002), Point(0.001, 0.002),
    ), closed=True))
    green = QColor("#5ba949")
    tile = QPixmap(256, 256)
    tile.fill(green)
    preview.tiles.images = {key: tile for key, _ in preview._visible_tiles()}
    try:
        for progress in (0, 0.75, 1):
            preview.set_progress(progress, animate=False)
            rendered = preview.grab().toImage()
            assert rendered.pixelColor(preview.rect().center()) == green
    finally:
        preview.deleteLater()
        QCoreApplication.sendPostedEvents(preview, QEvent.DeferredDelete)


def test_missing_detailed_tile_uses_the_correct_quadrant_of_an_offline_parent(application, tmp_path):
    from PySide6.QtGui import QColor, QPainter

    pack = tmp_path / "offline.zip"
    image = QImage(256, 256, QImage.Format_RGB32)
    image.fill(Qt.white)
    with QPainter(image) as painter:
        painter.fillRect(128, 128, 128, 128, QColor("#5ba949"))
    png = QBuffer()
    png.open(QIODevice.WriteOnly)
    assert image.save(png, "PNG")
    with ZipFile(pack, "w") as archive:
        archive.writestr("views.json", "[]")
        archive.writestr("tiles/15/2/3.png", bytes(png.data()))
    offline = OfflineMaps(pack)
    images = offline.load({(16, 5, 7), (18, 23, 31)})
    assert images.keys() == {(16, 5, 7)}
    assert images[(16, 5, 7)].toImage().pixelColor(128, 128) == QColor("#5ba949")


def test_map_tile_download_and_cache(application, tmp_path):
    image = QImage(256, 256, QImage.Format_RGB32)
    image.fill(Qt.white)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    png = bytes(buffer.data())
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers["User-Agent"]))
            self.send_response(200)
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            tiles = MapTiles(tmp_path / "cache")
            tiles.osm_url = f"http://127.0.0.1:{server.server_port}"
            tiles.network.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
            loop = QEventLoop()
            tiles.changed.connect(lambda: loop.quit() if not tiles.pending else None)
            timeout = QTimer()
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            key = (16, 54628, 26973)
            for _ in range(2):
                timeout.start(3000)
                tiles.show_tiles({key})
                if tiles.pending:
                    loop.exec()
                timeout.stop()
                assert tiles.images[key].width() == 256
                tiles.show_tiles(set())
            assert len(requests) == 1
            assert requests[0][0] == "/16/54628/26973.png"
            assert requests[0][1].startswith("IRun/")
        finally:
            server.shutdown()
            thread.join()


def test_new_route_defaults_to_first_preset_and_restores_the_last(application, tmp_path, monkeypatch, bundled_maps):
    from vrun import theme

    monkeypatch.setattr(MapTiles, "show_tiles", lambda *_: None)
    settings_path = str(tmp_path / "settings.ini")

    def open_draw(width, height):
        settings = QSettings(settings_path, QSettings.IniFormat)
        dialog = DrawRouteDialog(tmp_path, None, theme.colors("light"), settings=settings)
        dialog.resize(width, height)
        dialog.show()
        application.processEvents()
        return dialog

    def assert_preset_fits(dialog):
        west, south, east, north = dialog.preset_choice.currentData()["focus_bounds"]
        for point in (Point(north, west), Point(south, east)):
            pixel = project(point) * dialog.map.world_size - dialog.map.origin
            assert 0 <= pixel.x() <= dialog.map.width()
            assert 0 <= pixel.y() <= dialog.map.height()

    dialog = open_draw(850, 650)
    assert dialog.preset_choice.currentText() == "测试地点 A"
    assert_preset_fits(dialog)
    second = dialog.preset_choice.findText("测试地点 B")
    dialog.preset_choice.setCurrentIndex(second)
    dialog.preset_choice.activated.emit(second)
    assert dialog.settings.value(PRESET_SETTING) == "测试地点 B"
    assert_preset_fits(dialog)
    small_zoom = dialog.map.zoom
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)

    reopened = open_draw(1400, 1300)
    try:
        assert reopened.preset_choice.currentText() == "测试地点 B"
        assert_preset_fits(reopened)
        assert reopened.map.zoom > small_zoom
    finally:
        reopened.close()
        reopened.deleteLater()
        QCoreApplication.sendPostedEvents(reopened, QEvent.DeferredDelete)



def test_public_draw_dialog_has_no_preset_picker(application, tmp_path, monkeypatch):
    from vrun import theme

    monkeypatch.setattr(MapTiles, "show_tiles", lambda *_: None)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    settings.setValue(PRESET_SETTING, "测试地点 A")
    dialog = DrawRouteDialog(tmp_path, None, theme.colors("light"), settings=settings)
    try:
        dialog.show()
        application.processEvents()
        assert dialog.preset_choice is None
        assert not dialog.tiles.offline.names
        assert dialog.map.center == project(Point(0, 0))
    finally:
        dialog.close()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(dialog, QEvent.DeferredDelete)
