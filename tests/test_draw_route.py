# SPDX-License-Identifier: MPL-2.0
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from PySide6.QtCore import (QBuffer, QEventLoop, QIODevice, QObject, QPoint, QPointF,
                            QSettings, Qt, QTimer, Signal)
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from vrun.app import Window
from vrun.draw_dialog import DrawRouteDialog
from vrun.map_tiles import MapTiles
from vrun.route_library import export_gpx, load_gpx
from vrun.route_map import project, unproject
from vrun.routes import Point


class UnavailableTiles(QObject):
    changed = Signal()
    attribution = ("https://www.openstreetmap.org/copyright", "© OpenStreetMap contributors")

    def __init__(self, cache_dir, parent):
        super().__init__(parent)
        self.images = {}
        self.pending = {}
        self.failed = set()
        self.denied = set()
        self.limited = set()

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
            dialog.location.setText("31.221, 121.436")
            dialog.location.returnPressed.emit()
            assert (project(Point(31.221, 121.436)) - canvas.center).manhattanLength() < 1e-9
            anchor = QPointF(170, 120)
            start = (canvas.origin + anchor) / canvas.world_size
            canvas.zoom_by(1, anchor)
            assert ((canvas.origin + anchor) / canvas.world_size - start).manhattanLength() < 1e-9
            canvas.request_tiles()
            assert "底图无法加载" in dialog.map_status.text()

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


def test_map_tile_download_and_cache(application, tmp_path, monkeypatch):
    monkeypatch.setattr("vrun.map_tiles._tianditu_key", lambda: "")
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
            tiles.base_url = f"http://127.0.0.1:{server.server_port}"
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


def test_tianditu_reads_local_key_and_requests_both_mercator_layers(application, tmp_path, monkeypatch):
    from PySide6.QtCore import QUrlQuery

    monkeypatch.delenv("TIANDITU_KEY", raising=False)
    monkeypatch.setattr("vrun.map_tiles.app_dir", lambda: tmp_path)
    (tmp_path / ".env").write_text("TIANDITU_KEY=local-test-key\n", encoding="utf-8")
    tiles = MapTiles(tmp_path / "cache")
    assert tiles.layers == ("vec", "cva")
    assert tiles.network.cache() is None
    for layer in tiles.layers:
        url = tiles.tile_url((16, 54628, 26973), layer)
        query = QUrlQuery(url)
        assert url.host() == "t0.tianditu.gov.cn"
        assert url.path() == f"/{layer}_w/wmts"
        assert query.queryItemValue("TILEMATRIXSET") == "w"
        assert query.queryItemValue("TILECOL") == "54628"
        assert query.queryItemValue("TILEROW") == "26973"
        assert query.queryItemValue("tk") == "local-test-key"


def test_tianditu_composites_annotation_over_base_when_label_arrives_first(application, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtGui import QColor
    from PySide6.QtNetwork import QNetworkReply

    monkeypatch.setattr("vrun.map_tiles._tianditu_key", lambda: "local-test-key")
    tiles = MapTiles(tmp_path / "cache")
    key = (16, 54628, 26973)
    for layer in ("cva", "vec"):
        image = QImage(256, 256, QImage.Format_ARGB32)
        image.fill(Qt.transparent if layer == "cva" else Qt.white)
        if layer == "cva":
            image.setPixelColor(50, 50, QColor("red"))
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        assert image.save(buffer, "PNG")
        reply = SimpleNamespace(error=lambda: QNetworkReply.NetworkError.NoError,
                                readAll=buffer.data, deleteLater=lambda: None)
        tiles.pending[(key, layer)] = reply
        tiles._finished((key, layer), reply)
    rendered = tiles.images[key].toImage()
    assert rendered.pixelColor(50, 50) == QColor("red")
    assert rendered.pixelColor(51, 50) == QColor("white")
