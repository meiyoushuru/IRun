# SPDX-License-Identifier: MPL-2.0
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QWidget
import pytest
from vrun import theme
from vrun.app import Window
from vrun.device import Device
from vrun.manage_dialog import RouteManagerDialog
from tests.test_ui import window


def wheel(widget):
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, 120),
                        Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    from PySide6.QtWidgets import QApplication
    QApplication.sendEvent(widget, event)


@pytest.mark.parametrize("accent", ["#000000", "#ffffff", "#ffdd00", "#0078d4"])
def test_system_accent_remains_readable(accent, application):
    palette = theme.colors(accent=accent)
    assert theme.contrast(QColor(palette["ink"]), QColor(palette["surface"])) >= 4.5
    assert theme.contrast(QColor(palette["accent_text"]), QColor(palette["accent"])) >= 4.5


def test_legacy_dark_preference_still_opens_a_light_window(application, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(theme, "windows_accent", lambda: QColor("#008080"))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    settings.setValue("theme", "dark")
    window = Window(settings=settings, poll=False)
    try:
        assert window.palette_colors["background"] == theme.LIGHT_COLORS["background"]
        assert window.palette_colors["accent"] == "#008080"
        assert window.findChild(QWidget, "themeSelector") is None
    finally:
        window.close()
        application.processEvents()


def test_wheel_cannot_change_route_speed_or_target(window):
    window.target.setCurrentIndex(1)
    index, speed, target = window.routes.currentIndex(), window.speed.value(), window.target.currentText()
    for widget in (window.routes, window.routes.view().viewport(), window.speed, window.target,
                   window.target.lineEdit(), window.target.view().viewport()):
        widget.setFocus()
        wheel(widget)
    assert window.routes.currentIndex() == index
    assert window.speed.value() == speed
    assert window.target.currentText() == target


def test_custom_distance_menu_paints_selected_row_and_preserves_custom_input(window, application):
    window.show()
    target = window.target
    target.setEditText("4")
    QTest.mouseClick(target, Qt.LeftButton, pos=QPoint(target.width() - 15, target.height() // 2))
    application.processEvents()
    view = target.view()
    assert view.isVisible()
    selected = view.visualRect(view.model().index(3, 0))
    background = view.viewport().grab().toImage().pixelColor(6, selected.center().y())
    assert background.name() == window.palette_colors["accent"]
    assert view.verticalScrollBar().maximum() > 0
    QTest.keyClick(view, Qt.Key_End)
    QTest.keyClick(view, Qt.Key_Return)
    assert not view.isVisible()
    assert target.currentText() == "20"
    target.setEditText("25")
    target.showPopup()
    assert target.selected_row() == -1
    QTest.keyClick(view, Qt.Key_Escape)
    assert not view.isVisible()
    assert target.currentText() == "25"


def test_route_type_menu_fits_all_three_rows_without_scrolling(window, application):
    window.show()
    manager = RouteManagerDialog(window.palette_colors, window)
    manager.show()
    try:
        choice = manager.kind_filter
        choice.showPopup()
        application.processEvents()
        view = choice.view()
        assert view.isVisible()
        assert choice.count() == 3
        assert view.verticalScrollBar().maximum() == 0
        assert view.viewport().rect().contains(view.visualRect(choice.model().index(2, 0)))
    finally:
        manager.kind_filter.hidePopup()
        manager.close()


def test_compact_window_places_statistics_above_route_and_removes_help(window, application):
    window.show()
    application.processEvents()
    assert window.width() == 850 and window.height() == 700
    assert window.menuWidget() is None
    route_y = window.route_card.mapTo(window, QPoint()).y()
    for item in window.stats_cards:
        assert item.mapTo(window, QPoint()).y() + item.height() <= route_y
    titles = window.findChildren(QLabel, "sectionHeader")
    assert {title.text() for title in titles} == {"运动路线", "跑步设置"}
    assert len({title.mapTo(window, title.rect().center()).y() for title in titles}) == 1


def test_running_progress_does_not_inflate_completed_lap_distance(window, application):
    window._ready(Device("preview", "Test iPhone", "16.7.16"))
    window._on_progress(0.5)
    window.preview.animation.setCurrentTime(220)
    assert window.preview.fraction == pytest.approx(0.5)
    assert window.progress_bar.value() == 500
    assert "50%" in window.lap_progress.text()
    assert window.laps.text() == "0"
    assert window.distance.text() == "0.00"
    window._on_progress(0.02)
    assert window.preview.fraction == 0
    window.preview.animation.setCurrentTime(220)
    assert window.preview.fraction == pytest.approx(0.02)
    window._run_finished()
    assert not window.preview.running


def test_incomplete_speed_does_not_start_device_control(window):
    window._connected(Device("preview", "Test iPhone", "17.6"))
    assert window.start_button.isEnabled()
    window.speed.setText("")
    window.start()
    assert window.worker is None
    assert window.run_state.text() == "· 未就绪"
    assert not window.start_button.isEnabled()
    assert "请输入 0.10–15.00 米/秒" in window.settings_feedback.text()
