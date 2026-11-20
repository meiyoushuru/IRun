# SPDX-License-Identifier: MPL-2.0
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from vrun.app import Window
from vrun.import_dialog import ImportRouteDialog
from vrun.manage_dialog import RouteManagerDialog
from vrun.route_library import export_gpx, load_gpx, load_saved_route, read_saved_route, save_loop, save_route
from vrun.routes import Point, build_loop, build_route
from tests.test_speed import run_timed


@pytest.fixture
def gpx_file(tmp_path):
    path = tmp_path / "park.gpx"
    path.write_text("""<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
      <trk><name>公园路线</name>
        <trkseg><trkpt lat="31" lon="121"/><trkpt lat="31" lon="121.001"/></trkseg>
        <trkseg><trkpt lat="30" lon="120"/><trkpt lat="30.001" lon="120"/>
          <trkpt lat="30.001" lon="120.001"/></trkseg>
      </trk>
      <rte><rtept lat="32" lon="122"/><rtept lat="32.001" lon="122"/>
        <rtept lat="32" lon="122"/></rte>
    </gpx>""", encoding="utf-8")
    return path


def test_gpx_segments_save_independently_without_coordinate_conversion(gpx_file, tmp_path):
    first, second, closed = load_gpx(gpx_file)
    assert first.points[-1] == Point(31, 121.001)
    assert second.points[0] == Point(30, 120)
    assert closed.points[0] == closed.points[-1]
    route = build_loop(gpx_file, second.points)
    assert route.points == second.points + (second.points[0],)
    assert route.position_at(0) == route.position_at(route.length_m) == Point(30, 120)
    saved = save_loop(tmp_path, second.name, route)
    duplicate = save_loop(tmp_path, second.name, route)
    assert saved != duplicate
    assert read_saved_route(duplicate)[0] == f"{second.name} (2)"
    restored = load_saved_route(saved)
    assert restored.points == route.points
    assert restored.segment_lengths == route.segment_lengths


def test_saved_gpx_replays_one_loop_in_wgs84(application, monkeypatch, gpx_file, tmp_path):
    segment = load_gpx(gpx_file)[1]
    route = build_loop(gpx_file, segment.points)
    saved = load_saved_route(save_loop(tmp_path, segment.name, route))
    backend, completed, errors, _ = run_timed(monkeypatch, saved, 3.3, 0, laps=1)
    assert not errors
    assert backend.deliveries[0][1] == backend.deliveries[-1][1] == Point(30, 120)
    assert completed[0][:2] == (1, route.length_m)
    assert backend.resets == 1


def test_open_route_saves_one_way_and_replays_both_legs(application, monkeypatch, gpx_file, tmp_path):
    segment = load_gpx(gpx_file)[0]
    route = build_route(gpx_file, segment.points, closed=False)
    saved = load_saved_route(save_route(tmp_path, segment.name, route))
    assert not saved.closed
    assert saved.points == segment.points
    backend, completed, errors, _ = run_timed(monkeypatch, saved, 3.3, 0, laps=1)
    assert not errors
    turnaround, = [(t, p) for t, p in backend.deliveries if p == segment.points[-1]]
    assert turnaround[0] == pytest.approx(route.length_m / 3.3)
    assert backend.deliveries[0][1] == backend.deliveries[-1][1] == segment.points[0]
    assert completed[0] == pytest.approx((1, route.length_m * 2, route.length_m * 2 / 3.3))


def test_route_interpolates_across_antimeridian():
    route = build_loop(Path("dateline.gpx"), [Point(0, 179.999), Point(0, -179.999)])
    midpoint = route.position_at(route.segment_lengths[0] / 2)
    assert abs(midpoint.lng) == pytest.approx(180)


def test_invalid_xml_character_in_route_name_cannot_corrupt_gpx(tmp_path, sample_route):
    exported = tmp_path / "route.gpx"
    export_gpx(exported, "路线", sample_route.points)
    with pytest.raises(ValueError, match="路线名称"):
        export_gpx(exported, "路线\x01", sample_route.points)
    assert load_gpx(exported)[0].name == "路线"


@pytest.mark.parametrize("contents, message", [
    ('<gpx version="1.0"/>', "GPX 1.1"),
    ('<gpx', "XML"),
    ('<!DOCTYPE gpx [<!ENTITY test "value">]><gpx/>', "文档类型声明"),
    ('<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk/></gpx>', "连续路线"),
])
def test_invalid_gpx_is_rejected(tmp_path, contents, message):
    path = tmp_path / "invalid.gpx"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_gpx(path)


@pytest.mark.parametrize("save", [False, True])
def test_import_dialog_cancel_or_save_and_restore_selection(application, monkeypatch, tmp_path, gpx_file, save):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(gpx_file), ""))
    settings_path = str(tmp_path / "settings.ini")
    window = Window(settings=QSettings(settings_path, QSettings.IniFormat), poll=False)
    original_count = window.routes.count()
    inspected = []

    def interact():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, ImportRouteDialog)
            dialog.segment_choice.setCurrentIndex(2)
            assert not dialog.preview.show_closing_segment
            dialog.segment_choice.setCurrentIndex(1)
            assert not dialog.route.closed and not dialog.preview.show_closing_segment
            dialog.close_route.setChecked(True)
            assert dialog.preview.show_closing_segment
            assert "确认补线" in dialog.save_button.text()
            dialog.close_route.setChecked(False)
            dialog.name.setText("我的公园路线")
            inspected.append(dialog.route.points)
            if save:
                dialog.save_button.click()
        finally:
            if dialog.result() != QDialog.Accepted:
                dialog.reject()

    def import_from_manager():
        manager = QApplication.activeModalWidget()
        try:
            assert isinstance(manager, RouteManagerDialog)
            QTimer.singleShot(0, interact)
            manager.import_button.click()
        finally:
            manager.reject()

    QTimer.singleShot(0, import_from_manager)
    window.manage_button.click()
    assert inspected
    assert window.routes.count() == original_count + int(save)
    files = list((tmp_path / "IRun" / "routes").glob("*.json"))
    assert len(files) == int(save)
    if save:
        assert window.route.points == inspected[0]
        assert window.route.path == files[0]
        assert not window.route.closed
        assert "往返路线" in window.route_info.text()
        assert window.routes.currentText() == "我的公园路线"
    window.close()
    application.processEvents()
    window.settings.sync()
    reopened = Window(settings=QSettings(settings_path, QSettings.IniFormat), poll=False)
    try:
        if save:
            assert reopened.route.path == files[0]
        else:
            assert reopened.route is None
    finally:
        reopened.close()
        application.processEvents()
