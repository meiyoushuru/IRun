# SPDX-License-Identifier: MPL-2.0
import time
import pytest
from PySide6.QtCore import QSettings
from vrun import __version__
from vrun.app import Window
from vrun.device import DeviceError
from vrun.route_library import save_loop


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
        assert not window.export_button.isEnabled()
        assert window.draw_button.isEnabled() and window.import_button.isEnabled()
        assert not window.stop_button.isEnabled()
        assert window.pace.text() == "5:03"
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
        assert window.start_button.isEnabled()
    finally:
        window.close()
        application.processEvents()


def test_custom_reminder_and_disabled_reminder(window):
    assert [window.target.itemText(i) for i in range(window.target.count())] == list(map(str, range(1, 21)))
    window.target.setCurrentText("2.5")
    assert window._target_m() == 2500
    assert "2.5 公里" in window.settings_feedback.text()
    window.target.setCurrentText("nan")
    with pytest.raises(ValueError):
        window._target_m()
    window.remind.setChecked(False)
    assert window._target_m() is None
    assert "提醒已关闭" in window.settings_feedback.text()
    window.remind.setChecked(True)
    assert window._target_m() == 2500
    assert "提醒已开启" in window.settings_feedback.text()


def test_speed_and_pace_edit_one_at_a_time(window):
    assert not window.speed.isReadOnly() and window.pace.isReadOnly()
    window.speed.setText("4")
    assert window.pace.text() == "4:10"
    assert "速度已更新" in window.settings_feedback.text()

    window.pace_mode.click()
    assert window.speed.isReadOnly() and not window.pace.isReadOnly()
    window.pace.setText("5:00")
    assert window.speed.text() == "3.33"
    assert window.settings.value("speed", type=float) == pytest.approx(1000 / 300)
    assert "配速已更新" in window.settings_feedback.text()


def test_preparation_failure_returns_to_idle(window, application):
    class BrokenBackend:
        def prepare(self, cancel, status):
            raise DeviceError("请先解锁手机")
    window.backend = BrokenBackend()
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
    import threading
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
    window._probe()
    assert entered.wait(1)
    window.start()
    deadline = time.monotonic() + 3
    while (not prepares or window.worker is not None or window.probe_worker is not None) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    assert prepares == [True]
    assert window.worker is None
    assert window.start_button.isEnabled()


def test_can_resend_stop_command_to_same_device_after_success(window, application):
    from vrun.device import Device

    device = Device("original-device", "iPhone", "27.3.1")
    resets = []

    class Backend:
        def prepare(self, cancel, status):
            return device

        def set_location(self, target, point, cancel):
            cancel.set()

        def reset(self, target):
            resets.append(target)

    def wait_until_idle():
        deadline = time.monotonic() + 3
        while window.worker is not None and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.005)
        assert window.worker is None

    window.backend = Backend()
    window.start()
    wait_until_idle()
    assert resets == [device]
    assert window.stop_button.isEnabled()
    assert window.stop_button.text() == "再次恢复定位"
    window.stop_button.click()
    wait_until_idle()
    assert resets == [device, device]
    assert "请在设备的地图 App 中确认当前位置" in window.message.text()
