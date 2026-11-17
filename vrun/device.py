# SPDX-License-Identifier: MPL-2.0
"""Bounded native-tool calls. Every operation after discovery targets the same UDID."""
from dataclasses import dataclass
import logging
from pathlib import Path
import plistlib
import re
import subprocess
import threading
import time

from .diagnostics import error_summary
from .drivers import apple_usb_status
from .paths import resource_dir
from .routes import Point

LOG = logging.getLogger(__name__)
NO_DEVICE = "未找到设备。请检查 USB 连接并解锁设备。"

class DeviceError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Device:
    udid: str
    name: str
    version: str
    product_type: str = ""

    @property
    def system_name(self) -> str:
        return "iPadOS" if self.product_type.startswith("iPad") else "iOS"

    @property
    def support_issue(self) -> str:
        version = re.match(r"^[0-9]+", self.version)
        if not version:
            return "无法确认苹果设备的系统版本，请重新连接设备。"
        elif int(version[0]) < 17:
            return "此系统版本不受支持。IRun 需要 iOS 或 iPadOS 17 及以上。"
        return ""

    def require_supported(self):
        if issue := self.support_issue:
            raise DeviceError(issue)


def friendly_error(text: str) -> str:
    lower = text.lower()
    if "no device" in lower or "no idevice" in lower:
        return NO_DEVICE
    if "passcode" in lower or "password" in lower or "locked" in lower:
        return "请先解锁 iPhone，再点击开始。"
    if "trust" in lower or "pairingdialog" in lower or "userdenied" in lower:
        return "请在 iPhone 上选择“信任此电脑”，完成后再次点击开始。"
    if "developer" in lower:
        return "请检查手机的开发者模式和开发者镜像，然后重试。"
    return "设备通信失败。"


class DeviceBackend:
    def __init__(self, root: Path | None = None):
        self.root = root or resource_dir()
        self.tools = self.root / "libimobiledevice" / "win"
        self._modern = None
        self._next_usb_check = 0.0

    def command(self, tool: str, *args: str, timeout: float = 12,
                cancel: threading.Event | None = None) -> str:
        executable = self.tools / (tool + ".exe")
        if not executable.is_file():
            raise DeviceError("设备工具缺失，请重新安装 IRun。")
        if cancel and cancel.is_set():
            raise Cancelled()
        try:
            process = subprocess.Popen([str(executable), *map(str, args)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise DeviceError(error_summary("无法启动设备工具，请检查 IRun 安装。", str(error))) from error
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise DeviceError("设备响应超时，请检查手机是否解锁及 USB 连接。")
                try:
                    stdout, stderr = process.communicate(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        errors = stderr.decode("utf-8", errors="replace")
        detail = (output + "\n" + errors).strip()
        if process.returncode or re.search(r"(?im)^\s*error:", detail):
            hint = friendly_error(detail)
            if hint == NO_DEVICE:
                raise DeviceError(hint)
            LOG.warning("%s failed (exit %s). stdout=%r stderr=%r", tool, process.returncode,
                        output[:4096], errors[:4096])
            if process.returncode:
                hint += f"（{tool} 退出码 {process.returncode}）"
            raise DeviceError(error_summary(hint, detail))
        return output

    def probe(self, cancel: threading.Event | None = None) -> Device:
        try:
            output = self.command("ideviceinfo", "-s", "-x", timeout=5, cancel=cancel)
        except DeviceError:
            if time.monotonic() >= self._next_usb_check:
                self._next_usb_check = time.monotonic() + 60
                LOG.info("Apple USB check after discovery failure:\n%s", apple_usb_status())
            raise
        try:
            data = plistlib.loads(output[output.index("<?xml"):].encode("utf-8"))
            product_type = data.get("ProductType", "")
            default_name = "iPad" if product_type.startswith("iPad") else "iPhone"
            device = Device(data["UniqueDeviceID"], data.get("DeviceName", default_name),
                            data["ProductVersion"], product_type)
        except (ValueError, KeyError, plistlib.InvalidFileException) as error:
            raise DeviceError("无法读取手机信息，请解锁手机并确认信任此电脑。") from error
        return device

    def prepare(self, cancel: threading.Event, status) -> Device:
        status("正在检查手机，请保持 iPhone 解锁…")
        device = self.probe(cancel)
        device.require_supported()
        return self._modern_backend().prepare(device, cancel, status)

    def set_location(self, device: Device, point: Point, cancel: threading.Event):
        device.require_supported()
        return self._modern_backend().set_location(device, point, cancel)

    def reset(self, device: Device):
        device.require_supported()
        return self._modern_backend().reset(device)

    def _modern_backend(self):
        if self._modern is None:
            from .modern import ModernBackend
            self._modern = ModernBackend()
        return self._modern

    def close(self):
        if self._modern is not None:
            self._modern.close()
