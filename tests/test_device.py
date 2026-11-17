# SPDX-License-Identifier: MPL-2.0
import plistlib
import threading
import pytest
from vrun.device import DeviceBackend, DeviceError, Cancelled


def probe_output(version="17.6"):
    return plistlib.dumps({"UniqueDeviceID": "test-device", "DeviceName": "Test iPhone",
                           "ProductVersion": version}).decode()


def test_new_ios_is_discovered(monkeypatch):
    backend = DeviceBackend()
    monkeypatch.setattr(backend, "command", lambda *a, **kw: probe_output("17.0"))
    assert backend.probe().version == "17.0"
    assert backend._modern is None


def test_ipad_is_labeled_ipados_and_uses_modern_backend(monkeypatch):
    backend = DeviceBackend()
    data = {"UniqueDeviceID": "test-ipad", "ProductType": "iPad16,3",
            "DeviceName": "平板", "ProductVersion": "27.0.1"}
    monkeypatch.setattr(backend, "command", lambda *a, **kw: plistlib.dumps(data).decode())
    device = backend.probe()
    assert device.system_name == "iPadOS"
    assert device.name == "平板"
    assert device.version == "27.0.1"


@pytest.mark.parametrize("version", ["15.7", "16.7.16"])
def test_unsupported_device_is_reported_without_launching_helper(monkeypatch, version):
    backend = DeviceBackend()
    monkeypatch.setattr(backend, "command", lambda *a, **kw: probe_output(version))
    device = backend.probe()
    assert device.version == version
    assert "不受支持" in device.support_issue
    with pytest.raises(DeviceError, match="需要 iOS 或 iPadOS 17"):
        backend.prepare(threading.Event(), lambda text: None)
    assert backend._modern is None


def test_cancel_before_native_command_does_not_launch():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        DeviceBackend().command("ideviceinfo", cancel=cancel)
