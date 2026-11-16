# SPDX-License-Identifier: MPL-2.0
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from vrun.app import Window
from vrun.draw_dialog import DrawRouteDialog
from vrun.manage_dialog import RouteManagerDialog
from vrun.route_library import MAX_ROUTE_NAME_LENGTH, load_saved_route, read_saved_route, rename_route, save_route
from vrun.routes import Point, build_route
from tests.test_draw_route import UnavailableTiles


def test_manage_search_edit_rename_copy_and_delete(application, monkeypatch, tmp_path, sample_route):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("vrun.draw_dialog.MapTiles", UnavailableTiles)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Renamed", True))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.Yes)
    directory = tmp_path / "IRun" / "routes"
    save_route(directory, "Loop", sample_route)
    points = (Point(30, 120), Point(30.001, 120), Point(30.001, 120.001))
    saved = save_route(directory, "Path", build_route(tmp_path / "path.gpx", points, closed=False))
    window = Window(settings=QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat), poll=False)
    completed = []

    def edit():
        editor = QApplication.activeModalWidget()
        try:
            assert isinstance(editor, DrawRouteDialog)
            assert tuple(editor.map.points) == points
            editor.loop_button.click()
            editor.save_button.click()
        finally:
            if editor.result() != QDialog.Accepted:
                editor.reject()

    def manage():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, RouteManagerDialog)
            assert dialog.routes.count() == 2
            dialog.kind_filter.setCurrentIndex(2)
            assert dialog.routes.count() == 1
            assert "单程" in dialog.summary.text()
            dialog.search.setText("missing")
            assert not dialog.use_button.isEnabled()
            dialog.search.setText("path")
            dialog.rename_button.click()
            assert read_saved_route(saved)[0] == "Renamed"
            dialog.search.clear()
            dialog.kind_filter.setCurrentIndex(0)
            dialog.search.setText("renamed")
            QTimer.singleShot(0, edit)
            dialog.edit_button.click()
            assert load_saved_route(saved).closed
            dialog.copy_button.click()
            assert dialog.routes.count() == 2
            assert dialog.current_path != str(saved)
            dialog.delete_button.click()
            assert dialog.routes.count() == 1
            completed.append(True)
            dialog.use_button.click()
        finally:
            if dialog.result() != QDialog.Accepted:
                dialog.reject()

    QTimer.singleShot(0, manage)
    try:
        window.manage_button.click()
        assert completed == [True]
        assert window.route.path == saved
        assert window.routes.currentText() == "Renamed"
        assert len(list(directory.glob("*.json"))) == 2
    finally:
        window.close()
        application.processEvents()


def test_route_names_are_unique_on_save_edit_and_rename(tmp_path, sample_route):
    original = save_route(tmp_path, "Park", sample_route)
    save_route(tmp_path, "Park (2)", sample_route)
    duplicate = save_route(tmp_path, "  ＰＡＲＫ  ", sample_route)
    assert read_saved_route(duplicate)[0] == "ＰＡＲＫ (3)"
    assert rename_route(original, "park") == "park"
    assert save_route(tmp_path, "Park", sample_route, existing=duplicate) == duplicate
    assert read_saved_route(duplicate)[0] == "Park (3)"
    long_name = "路" * MAX_ROUTE_NAME_LENGTH
    save_route(tmp_path, long_name, sample_route)
    assert rename_route(duplicate, long_name) == long_name[:-4] + " (2)"


def test_manager_reports_duplicate_across_old_and_current_libraries(
    application, monkeypatch, tmp_path, sample_route
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_route(tmp_path / "VRun" / "routes", "River", sample_route)
    saved = save_route(tmp_path / "IRun" / "routes", "Park", sample_route)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("river", True))
    window = Window(settings=QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat), poll=False)
    confirmed = []

    def manage():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog.current_path == str(saved)
            dialog.rename_button.click()
            assert read_saved_route(saved)[0] == "river (2)"
            assert dialog.feedback.text() == "路线名称重复，已自动使用「river (2)」。"
            confirmed.append(True)
        finally:
            dialog.reject()

    QTimer.singleShot(0, manage)
    try:
        window.manage_button.click()
        assert confirmed == [True]
    finally:
        window.close()
        application.processEvents()
