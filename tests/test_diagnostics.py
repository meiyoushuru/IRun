# SPDX-License-Identifier: MPL-2.0
from PySide6.QtWidgets import QFileDialog, QMessageBox

from tests.test_ui import window
from vrun.device import Device
from vrun.diagnostics import MAX_LOG_BYTES, error_summary, export_report


def test_diagnostic_export_includes_errors_with_sensitive_values_redacted(tmp_path, monkeypatch):
    logs = tmp_path / "local"
    logs.mkdir()
    drivers = tmp_path / "shared" / "IRun"
    drivers.mkdir(parents=True)
    monkeypatch.setenv("PROGRAMDATA", str(drivers.parent))
    monkeypatch.setattr("vrun.diagnostics.apple_usb_status", lambda: "Apple USB 驱动已入库：是")
    monkeypatch.setattr("vrun.diagnostics.apple_usb_device_status", lambda: "Service=appleusb, ConfigManagerErrorCode=28")
    device = Device("00008120-0123456789abcdef", "Personal Device", "27.2.1", "iPhone16,2")
    (logs / "irun.log").write_text(
        f"USB device={device.udid} {device.name}\n"
        "Point(lat=30.12756, lng=120.72563)\n"
        "URL https://example.test/tiles?tk=sample-key&x=3\n"
        r"File C:\Users\SampleUser\AppData\IRun\irun.log"
        "\nSavePairRecord 183\n", encoding="utf-8")
    (logs / "ios17-helper.log").write_text("Traceback: MuxException\n", encoding="utf-8")
    (drivers / "driver-install.log").write_text("Driver check complete\n", encoding="utf-8")
    destination = tmp_path / "diagnostics.txt"
    export_report(destination, logs, device=device, status="已连接",
                  last_error="SavePairRecord 183")

    result = destination.read_text(encoding="utf-8-sig")
    for value in ("iPhone16,2", "27.2.1", "SavePairRecord 183", "MuxException", "Driver check complete",
                  "Apple USB 驱动已入库", "ConfigManagerErrorCode=28"):
        assert value in result
    for value in (device.udid, device.name, "30.12756", "120.72563", "sample-key", "SampleUser"):
        assert value not in result


def test_diagnostic_export_button_saves_current_error_and_log_tail(window, tmp_path, monkeypatch):
    directory = tmp_path / "IRun"
    with (directory / "irun.log").open("wb") as log:
        log.write(b"ignored old log\n" + b"older lines\n" * MAX_LOG_BYTES + b"latest details\n")
    destination = tmp_path / "sent.txt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(destination), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    window._failed("device error")
    window.export_diagnostics.click()

    result = destination.read_text(encoding="utf-8-sig")
    assert "device error" in result
    assert "latest details" in result
    assert "ignored old log" not in result
    assert "仅保留最近的日志" in result
    assert "暂无日志文件" in result


def test_error_summary_keeps_codes_after_the_excerpt():
    detail = "First line\n" + "diagnostic context " * 40 + "\n[E001005] SavePairRecord 183"
    summary = error_summary("设备通信失败。", detail)
    assert "First line" in summary
    assert "E001005" in summary and "SavePairRecord 183" in summary
    assert "diagnostic context" not in summary
