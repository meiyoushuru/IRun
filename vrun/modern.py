# SPDX-License-Identifier: MPL-2.0
"""Version-specific adapter to the independent JSON-lines developer-service helper."""
from pathlib import Path
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from .device import END_ACTION, Cancelled, DeviceError
from .diagnostics import error_summary
from .paths import data_dir, resource_dir

LOG = logging.getLogger(__name__)
TRANSPORT_ERRORS = {"BrokenPipeError", "ConnectionAbortedError", "ConnectionResetError",
                    "ConnectionTerminatedError", "IncompleteReadError", "TimeoutError"}


class DeviceConnectionError(DeviceError):
    pass


def helper_command():
    if getattr(sys, "frozen", False):
        executable = resource_dir() / "ios17" / "IRun-iOS17.exe"
        if not executable.is_file():
            raise DeviceError("IRun 的设备连接组件缺失。请重新运行 IRun 安装包。")
        return [str(executable)]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "ios17_helper" / "main.py")]


class ModernBackend:
    def __init__(self):
        self.process = None
        self.udid = None
        self.responses = queue.Queue()
        self.sequence = 0
        self.log_file = None

    def _read(self, process, responses):
        try:
            for line in process.stdout:
                try:
                    responses.put(json.loads(line))
                except (ValueError, TypeError):
                    LOG.warning("Ignored non-protocol helper output")
        finally:
            responses.put(None)

    def _receive(self, identifier, timeout, cancel=None, status=None, restoring=False):
        deadline = time.monotonic() + timeout
        while True:
            if cancel and cancel.is_set():
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceConnectionError("设备响应超时。请保持 iPhone 或 iPad 解锁，重新插拔 USB 线后重试。")
            try:
                response = self.responses.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if response is None:
                code = self.process.poll() if self.process is not None else None
                suffix = f"（退出码 {code}）" if code is not None else ""
                raise DeviceConnectionError("设备连接已中断。请重新连接同一台 iPhone 或 iPad 后重试。" + suffix)
            if not isinstance(response, dict):
                continue
            if response.get("event") == "status":
                if status:
                    status(str(response.get("message", "")))
                continue
            if response.get("id") != identifier:
                continue
            if not response.get("ok"):
                message = str(response.get("error", "设备通信失败。请点击「导出诊断日志」。"))
                if restoring:
                    message = message.replace("「开始」", f"「{END_ACTION}」")
                codes = response.get("codes")
                detail = str(response.get("detail", ""))
                if isinstance(codes, dict):
                    detail += "\n" + " ".join(f"{key} {value}" for key, value in codes.items())
                error_class = DeviceConnectionError if response.get("error_type") in TRANSPORT_ERRORS else DeviceError
                raise error_class(error_summary(message, detail, private_values=(self.udid,)))
            return response

    def open(self, device, cancel=None, status=lambda text: None, *, restoring=False):
        if self.process is not None:
            if self.udid != device.udid:
                raise DeviceError("请连接刚才用于模拟的 iPhone 或 iPad，再恢复定位。")
            if self.process.poll() is None:
                return
            self.close()
        if cancel and cancel.is_set():
            raise Cancelled()
        self.responses = queue.Queue()
        self.udid = device.udid
        self.log_file = (data_dir() / "ios17-helper.log").open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [*helper_command(), "--udid", device.udid], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log_file, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._read, args=(self.process, self.responses), daemon=True).start()
            self._receive(0, 300, cancel, status, restoring=restoring)
        except BaseException:
            self.close()
            raise

    def request(self, operation, **values):
        if self.process is None or self.process.poll() is not None:
            raise DeviceConnectionError("设备连接已中断。请重新连接同一台 iPhone 或 iPad。")
        self.sequence += 1
        try:
            self.process.stdin.write(json.dumps({"id": self.sequence, "op": operation, **values}) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise DeviceConnectionError("设备连接已中断。请检查 USB 线是否插牢。") from error
        # Once a coordinate is sent, wait for its bounded acknowledgement before clearing.
        return self._receive(self.sequence, 15, restoring=operation == "clear")

    def prepare(self, device, cancel, status):
        self.open(device, cancel, status)
        return device

    def set_location(self, device, point, cancel):
        if cancel.is_set():
            raise Cancelled()
        if self.udid != device.udid:
            raise DeviceError("连接的设备已变化，已停止发送定位。请重新连接原来的 iPhone 或 iPad。")
        self.request("set", lat=point.lat, lng=point.lng)

    def reset(self, device):
        try:
            for attempt in range(2):
                started = time.monotonic()
                self.open(device, restoring=True)
                LOG.info("Location reset transport ready after %.2f s", time.monotonic() - started)
                started = time.monotonic()
                try:
                    self.request("clear")
                except DeviceConnectionError:
                    if attempt:
                        raise
                    LOG.warning("Location reset lost its connection; reopening it once", exc_info=True)
                    self.close()
                else:
                    LOG.info("Stop simulation sent by helper after %.2f s", time.monotonic() - started)
                    return
        finally:
            started = time.monotonic()
            self.close()
            LOG.info("Location reset transport closed after %.2f s", time.monotonic() - started)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.wait(timeout=22)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
            finally:
                if process.stdout:
                    process.stdout.close()
        if self.log_file:
            self.log_file.close()
            self.log_file = None
        self.udid = None
