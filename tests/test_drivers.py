# SPDX-License-Identifier: MPL-2.0
import tempfile
import pytest
from vrun import drivers


def test_existing_components_are_reused(monkeypatch):
    commands = []
    monkeypatch.setattr(drivers, "service_registered", lambda: True)
    monkeypatch.setattr(drivers, "usb_driver_staged", lambda: True)
    monkeypatch.setattr(drivers, "start_apple_service", lambda: commands.append("start"))
    monkeypatch.setattr(drivers, "download", lambda *a: pytest.fail("Should not download for an existing setup"))
    assert drivers.install() == 0
    assert commands == ["start"]


def test_signature_failure_prevents_msi_execution(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(drivers, "service_registered", lambda: False)
    monkeypatch.setattr(drivers, "download", lambda *a: None)
    monkeypatch.setattr(drivers, "signature", lambda *a: (_ for _ in ()).throw(RuntimeError("untrusted signature")))
    monkeypatch.setattr(drivers, "run", lambda args, **kw: commands.append(args))
    with pytest.raises(RuntimeError, match="untrusted"):
        drivers.install()
    assert not commands


def test_partial_install_only_downloads_missing_driver(monkeypatch, tmp_path):
    packages = []
    commands = []
    staged = iter((False, True))
    monkeypatch.setattr(drivers, "service_registered", lambda: True)
    monkeypatch.setattr(drivers, "usb_driver_staged", lambda: next(staged))
    monkeypatch.setattr(drivers, "USB_SHA256", drivers.hashlib.sha256(b"cab").hexdigest())
    def download(url, path):
        packages.append(url)
        path.write_bytes(b"cab")
    monkeypatch.setattr(drivers, "download", download)
    monkeypatch.setattr(drivers, "signature", lambda *args: None)
    monkeypatch.setattr(drivers, "run", lambda args, **kw: commands.append(args[0]) or 0)
    monkeypatch.setattr(drivers, "start_apple_service", lambda: commands.append("start"))
    assert drivers.install() == 0
    assert packages == [drivers.USB_URL]
    assert commands == ["expand.exe", "pnputil.exe", "start"]


def test_reboot_required_does_not_fail_when_service_cannot_yet_start(monkeypatch, tmp_path):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(drivers, "service_registered", lambda: False)
    monkeypatch.setattr(drivers, "usb_driver_staged", lambda: True)
    monkeypatch.setattr(drivers, "download", lambda *args: None)
    monkeypatch.setattr(drivers, "signature", lambda *args: None)
    monkeypatch.setattr(drivers, "extract_service_package", lambda _, directory: directory / "support.msi")
    monkeypatch.setattr(drivers, "run", lambda *args, **kw: 3010)
    monkeypatch.setattr(drivers, "start_apple_service", lambda: pytest.fail("Reboot is pending"))
    assert drivers.install() == 3010
