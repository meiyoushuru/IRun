# SPDX-License-Identifier: MPL-2.0
import time
import threading
import pytest
from PySide6.QtCore import QSettings, QPoint
from PySide6.QtGui import QPalette
from vrun import __version__
from vrun.app import Window
from vrun.device import END_ACTION, Device, DeviceError
from vrun.route_library import save_loop

DEVICE = Device("original-device", "iPhone", "27.3.1")


def wait_for(application, condition):
    deadline = time.monotonic() + 3
    while not condition() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    assert condition()


@pytest.fixture
def window(application, monkeypatch, tmp_path, sample_route):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_loop(tmp_path / "IRun" / "routes", "测试路线", sample_route)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    widget = Window(settings=settings, poll=False)
    yield widget
    widget.close()
    application.processEvents()


def test_fresh_install_has_no_routes(application, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("vrun.app.app_dir", lambda: tmp_path)
    former_routes = tmp_path / "routes"
    former_routes.mkdir()
    for filename in ("HNroute.txt", "ZJG-east.txt", "ZJG-west.txt"):
        (former_routes / filename).touch()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    window = Window(settings=settings, poll=False)
    try:
        assert window.windowTitle() == f"IRun · 虚拟跑步  {__version__}"
        assert not window.windowIcon().isNull()
        assert window.route is None and window.routes.count() == 0
        assert not window.start_button.isEnabled()
        assert window.new_button.isEnabled() and window.new_button.text() == "新建…"
        assert window.manage_button.isEnabled() and window.manage_button.text() == "管理…"
        assert not window.stop_button.isEnabled()
        assert (window.pace.minutes.text(), window.pace.seconds.text()) == ("5", "03")
    finally:
        window.close()
        application.processEvents()


def test_previous_library_still_loads_user_route(application, monkeypatch, tmp_path, sample_route):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    saved = save_loop(tmp_path / "VRun" / "routes", "旧版手绘路线", sample_route)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    settings.setValue("route", f"user:{saved.stem}")
    window = Window(settings=settings, poll=False)
    try:
        assert window.routes.currentText() == "旧版手绘路线"
        assert window.route.path == saved
        assert not window.start_button.isEnabled()
        assert window.run_state.text() == "· 未就绪"
    finally:
        window.close()
        application.processEvents()


def test_custom_reminder_and_disabled_reminder(window, application):
    assert [window.target.itemText(i) for i in range(window.target.count())] == list(map(str, range(1, 21)))
    assert window._target_m() == 10000
    window.target.setCurrentText("25")
    assert window._target_m() == 25000
    assert "25 公里" in window.settings_feedback.text()
    for text in ("", "0", "-2", "2.5", "1001"):
        window.target.setCurrentText(text)
        assert not window.target.lineEdit().hasAcceptableInput()
        with pytest.raises(ValueError):
            window._target_m()
    window.remind.setChecked(False)
    assert window._target_m() is None
    assert "提醒已关闭" in window.settings_feedback.text()
    window.remind.setChecked(True)
    assert window._target_m() == 25000
    assert "提醒已开启" in window.settings_feedback.text()
    window.settings.sync()
    reopened = Window(settings=QSettings(window.settings.fileName(), QSettings.IniFormat), poll=False)
    try:
        assert reopened.target.currentText() == "25"
        assert reopened._target_m() == 25000
    finally:
        reopened.close()
        application.processEvents()


def test_speed_and_pace_update_each_other_immediately(window):
    minutes, seconds = window.pace.minutes, window.pace.seconds
    assert not any(field.isReadOnly() for field in (window.speed, minutes, seconds))
    window.speed.setText("3.20")
    assert (minutes.text(), seconds.text()) == ("5", "13")
    assert window._speed_mps == 3.2
    assert "速度已更新" in window.settings_feedback.text()

    minutes.setText("133")
    assert window.settings.value("speed", type=float) == pytest.approx(1000 / (133 * 60 + 13))
    seconds.setText("20")
    assert window.speed.text() == "0.13"
    assert window.settings.value("speed", type=float) == 0.125
    assert "配速已更新" in window.settings_feedback.text()
    seconds.setText("60")
    assert not seconds.hasAcceptableInput()
    assert window.settings.value("speed", type=float) == 0.125


def test_preparation_failure_returns_to_idle(window, application):
    class BrokenBackend:
        def prepare(self, cancel, status):
            raise DeviceError("请先解锁手机")
    window.backend = BrokenBackend()
    window._connected(DEVICE)
    window.start()
    deadline = time.monotonic() + 3
    while window.worker is not None and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    assert window.worker is None
    assert window.start_button.isEnabled()
    assert "解锁手机" in window.message.text()
    assert window.laps.text() == "0"


def test_start_during_probe_is_queued_once(window, application):
    from vrun.device import Cancelled
    entered = threading.Event()
    prepares = []
    class SlowProbeBackend:
        def probe(self, cancel):
            entered.set()
            cancel.wait(2)
            raise Cancelled()
        def prepare(self, cancel, status):
            prepares.append(True)
            raise DeviceError("prepare reached")
    window.backend = SlowProbeBackend()
    window._connected(DEVICE)
    window._probe()
    assert entered.wait(1)
    window.start()
    assert window.run_state.text() == "· 加载中"
    assert not window.start_button.isEnabled()
    deadline = time.monotonic() + 3
    while (not prepares or window.worker is not None or window.probe_worker is not None) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    assert prepares == [True]
    assert window.worker is None
    assert window.start_button.isEnabled()


def test_footer_ready_state_transitions_from_disabled_to_accent(window, application):
    window.show()
    application.processEvents()
    primary = window.start_button
    assert window.run_state.text() == "· 未就绪"
    assert primary.text() == "开始" and not primary.isEnabled()
    assert window.stop_button.text() == END_ACTION and not window.stop_button.isEnabled()
    assert window.run_state.mapTo(window, QPoint()).x() < window.stop_button.mapTo(window, QPoint()).x()
    assert window.run_state.mapTo(window, QPoint()).y() > window.message.mapTo(window, QPoint()).y()

    window._connected(Device("unsupported", "iPhone", "16.7"))
    assert window.run_state.text() == "· 未就绪" and not primary.isEnabled()
    window._connected(DEVICE)
    assert window.run_state.text() == "· 已就绪" and primary.isEnabled()
    assert window.stop_button.isEnabled()
    primary.transition.setCurrentTime(primary.transition.duration() // 2)
    assert primary.styleSheet()
    primary.transition.setCurrentTime(primary.transition.duration())
    assert primary.styleSheet() == ""
    window._disconnected("未连接")
    assert window.run_state.text() == "· 未就绪" and not primary.isEnabled()


def test_two_buttons_start_pause_resume_and_restore(window, application):
    allow_restore = threading.Event()
    resets = []

    class Backend:
        def prepare(self, cancel, status):
            return DEVICE

        def set_location(self, target, point, cancel):
            assert target == DEVICE

        def reset(self, target):
            resets.append(target)
            assert allow_restore.wait(3)

    window.backend = Backend()
    state = window.run_state
    assert state.palette().color(QPalette.ColorRole.WindowText).name() == window.palette_colors["danger"]
    assert window.connection.palette().color(QPalette.ColorRole.WindowText).name() == window.palette_colors["danger"]
    window._connected(DEVICE)
    primary = window.start_button
    assert state.palette().color(QPalette.ColorRole.WindowText).name() == window.palette_colors["text"]
    primary.click()
    assert state.text() == "· 加载中"
    assert state.palette().color(QPalette.ColorRole.WindowText).name() == window.palette_colors["muted"]
    assert not primary.isEnabled() and not window.stop_button.isEnabled()
    wait_for(application, lambda: window.run_state.text() == "· 运行中")
    assert state.palette().color(QPalette.ColorRole.WindowText).name() == "#13754e"
    assert primary.text() == "暂停" and primary.isEnabled()
    assert window.stop_button.isEnabled()
    primary.click()
    assert state.text() == "· 加载中" and not primary.isEnabled()
    wait_for(application, lambda: window.run_state.text() == "· 已暂停")
    assert state.palette().color(QPalette.ColorRole.WindowText).name() == "#165dcc"
    assert primary.text() == "继续" and primary.isEnabled() and window.stop_button.isEnabled()
    primary.click()
    assert state.text() == "· 加载中" and not primary.isEnabled()
    wait_for(application, lambda: window.run_state.text() == "· 运行中")
    primary.click()
    wait_for(application, lambda: window.run_state.text() == "· 已暂停")
    try:
        window.stop_button.click()
        assert window.run_state.text() == "· 恢复中"
        assert state.palette().color(QPalette.ColorRole.WindowText).name() == window.palette_colors["muted"]
        assert not primary.isEnabled() and not window.stop_button.isEnabled()
    finally:
        allow_restore.set()
    wait_for(application, lambda: window.worker is None)
    assert resets == [DEVICE]
    assert window.run_state.text() == "· 已就绪"
    assert primary.text() == "开始" and primary.isEnabled() and window.stop_button.isEnabled()
    assert "地图 App 中确认当前位置" in window.message.text()
    window.stop_button.click()
    assert window.run_state.text() == "· 恢复中" and not window.stop_button.isEnabled()
    wait_for(application, lambda: window.worker is None)
    assert resets == [DEVICE, DEVICE]
    assert window.run_state.text() == "· 已就绪" and window.stop_button.isEnabled()


def test_recovery_failure_can_retry_with_the_same_end_button(window, application, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    error_text = "设备定位服务通信失败。"
    warnings = []

    class Backend:
        def prepare(self, cancel, status):
            return DEVICE

        def set_location(self, target, point, cancel):
            raise DeviceError(error_text)

        def reset(self, target):
            assert target == DEVICE
            attempts.append(target)
            if len(attempts) == 1:
                raise DeviceError(error_text)

    attempts = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, message: warnings.append(message))
    window.backend = Backend()
    window._connected(DEVICE)
    window.start_button.click()
    wait_for(application, lambda: window.worker is None)
    assert attempts == [DEVICE]
    assert window.run_state.text() == "· 未就绪" and not window.start_button.isEnabled()
    assert window.stop_button.isEnabled() and window.stop_button.text() == END_ACTION
    assert END_ACTION in window.message.text()
    assert window.message.text().count(error_text) == 1
    assert len(warnings) == 1 and warnings[0].count(error_text) == 1
    window.stop_button.click()
    wait_for(application, lambda: window.worker is None)
    assert attempts == [DEVICE, DEVICE]
    assert window.run_state.text() == "· 已就绪" and window.stop_button.isEnabled()


def test_manual_recovery_waits_for_existing_device_probe(window, application):
    from vrun.device import Cancelled

    probing = threading.Event()
    calls = []

    class Backend:
        def probe(self, cancel):
            probing.set()
            assert cancel.wait(3)
            calls.append("probe stopped")
            raise Cancelled()

        def reset(self, device):
            calls.append(device)

    window.backend = Backend()
    window._connected(DEVICE)
    window._probe()
    assert probing.wait(1)
    window.stop_button.click()
    assert window.run_state.text() == "· 恢复中" and not window.stop_button.isEnabled()
    wait_for(application, lambda: window.worker is None)
    assert calls == ["probe stopped", DEVICE]
    assert window.run_state.text() == "· 已就绪" and window.stop_button.isEnabled()
