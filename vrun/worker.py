# SPDX-License-Identifier: MPL-2.0
import logging
import threading
from time import monotonic

from PySide6.QtCore import QThread, Signal

from .device import END_ACTION, Cancelled, DeviceBackend, DeviceError
from .diagnostics import error_summary
from .routes import LapCounter, Route

LOG = logging.getLogger(__name__)
RESET_SENT = "已发送恢复定位指令。设备可能需要几秒刷新位置，请在 iPhone 或 iPad 的地图 App 中确认当前位置。"
SETUP_STOPPED = "已取消启动。准备好后可再次点击「开始」。"


def restore_failure(error):
    message = f"未能恢复定位。请重新连接刚才使用的设备，再点击「{END_ACTION}」。"
    if isinstance(error, DeviceError):
        return f"{message}\n{error}"
    return error_summary(message, error)


class ProbeWorker(QThread):
    detected = Signal(object)
    unavailable = Signal(str)

    def __init__(self, backend: DeviceBackend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.cancel = threading.Event()

    def run(self):
        try:
            self.detected.emit(self.backend.probe(self.cancel))
        except Cancelled:
            pass
        except DeviceError as error:
            self.unavailable.emit(str(error))
        except Exception as error:
            LOG.exception("Unexpected device discovery failure")
            self.unavailable.emit(error_summary(
                "无法读取设备信息。请重新插拔 USB 线。如果仍失败，请点击「导出诊断日志」。", error))


class RunWorker(QThread):
    ready = Signal(object)
    message = Signal(str)
    lap = Signal(int, float)
    progress = Signal(float)
    distance_changed = Signal(float)
    pause_changed = Signal(bool)
    target_reached = Signal(float)
    failed = Signal(str)
    restoring = Signal()
    restored = Signal(bool, str)

    def __init__(self, backend: DeviceBackend, route: Route, speed: float,
                 target_m: float | None, parent=None):
        super().__init__(parent)
        self.backend, self.route = backend, route
        self.counter = LapCounter(route.cycle_length_m, target_m)
        self.cancel = threading.Event()
        self.pause_requested = threading.Event()
        self._settings_lock = threading.Lock()
        self._settings = speed, target_m
        self.device = None

    def update_settings(self, speed: float, target_m: float | None):
        with self._settings_lock:
            self._settings = speed, target_m

    def stop(self):
        self.cancel.set()

    def set_paused(self, paused: bool):
        if paused:
            self.pause_requested.set()
        else:
            self.pause_requested.clear()

    def run(self):
        device = None
        location_attempted = False
        try:
            device = self.backend.prepare(self.cancel, self.message.emit)
            self.device = device
            if self.cancel.is_set():
                return
            location_attempted = True
            initial_started = monotonic()
            self.backend.set_location(device, self.route.points[0], self.cancel)
            origin = monotonic()
            self.ready.emit(device)
            self.progress.emit(0.0)
            self._play(device, origin, origin - initial_started)
        except Cancelled:
            pass
        except Exception as error:
            LOG.exception("Device preparation or route playback failed")
            self.failed.emit(str(error) if isinstance(error, DeviceError) else error_summary(
                "模拟已中断。请重新连接设备。如果仍失败，请点击「导出诊断日志」。", error))
        finally:
            if location_attempted and device is not None:
                self.restoring.emit()
                self.message.emit("正在恢复设备的正常定位，请保持 USB 连接并稍候…")
                LOG.info("Starting location reset after playback")
                try:
                    self.backend.reset(device)
                    self.restored.emit(True, RESET_SENT)
                except Exception as error:
                    LOG.exception("Location reset failed after playback")
                    self.restored.emit(False, restore_failure(error))
            else:
                self.restored.emit(True, SETUP_STOPPED)
            close = getattr(self.backend, "close", None)
            if close:
                close()

    def _play(self, device, origin, initial_latency):
        with self._settings_lock:
            speed, _ = self._settings
        cycle_length = self.route.cycle_length_m
        leg_ratio = 1 if self.route.closed else 0.5
        lap_seconds = cycle_length / speed
        # Short loops still need intermediate coordinates before returning to the start.
        interval = min(0.25, lap_seconds * leg_ratio / 4)
        if initial_latency >= lap_seconds * leg_ratio / 4:
            raise DeviceError("当前设备通信速度无法维持设定的跑步速度，请降低速度或使用更长的路线。")
        latency = min(initial_latency, interval)
        next_update = interval
        last_delivery = origin
        last_target_time = distance_m = time_offset = 0.0

        def wait_until(deadline):
            nonlocal origin, last_delivery
            while not self.cancel.is_set():
                if self.pause_requested.is_set():
                    paused_at = monotonic()
                    self.pause_changed.emit(True)
                    while self.pause_requested.is_set():
                        if self.cancel.wait(0.05):
                            raise Cancelled()
                    paused_for = monotonic() - paused_at
                    origin += paused_for
                    last_delivery += paused_for
                    self.pause_changed.emit(False)
                if self.cancel.wait(max(0, origin + deadline - monotonic())):
                    break
                if not self.pause_requested.is_set():
                    return
            raise Cancelled()

        returning = False
        while not self.cancel.is_set():
            with self._settings_lock:
                new_speed, _ = self._settings
            if new_speed != speed:
                # Keep the last delivered distance fixed when changing the rate.
                speed = new_speed
                time_offset = last_target_time - distance_m / speed
                lap_seconds = cycle_length / speed
                interval = min(0.25, lap_seconds * leg_ratio / 4)
                latency = min(latency, interval)
                next_update = last_target_time + interval
            lap_end = time_offset + (self.counter.laps + 1) * lap_seconds
            turnaround = not self.route.closed and not returning
            boundary_m = self.route.length_m if turnaround else cycle_length
            boundary = lap_end - (lap_seconds / 2 if turnaround else 0)
            target_time = min(next_update, boundary)
            wait_until(target_time - latency)
            sent = monotonic()
            if sent - last_delivery > 2.0:
                raise DeviceError("设备响应过慢，无法维持设定速度，已停止模拟。请检查 USB 连接后重试。")
            # Skip obsolete samples instead of resetting the time origin or replaying a backlog.
            # Estimate arrival time because the native USB command itself can take >250 ms.
            target_time = min(boundary, max(target_time, sent - origin + latency))
            if boundary - target_time <= max(interval, latency):
                # Leave time for the endpoint command; do not send a nearly-complete
                # sample that would keep the device busy past the lap deadline.
                target_time = boundary
                wait_until(boundary - latency)
                sent = monotonic()
            with self._settings_lock:
                new_speed, _ = self._settings
            if new_speed != speed:
                continue
            at_boundary = target_time == boundary
            at_end = at_boundary and not turnaround
            lap_moved = boundary_m if at_boundary else (
                target_time - time_offset - self.counter.laps * lap_seconds) * speed
            point = self.route.cycle_position_at(lap_moved)
            self.backend.set_location(device, point, self.cancel)
            delivered = monotonic()
            command_seconds = delivered - sent
            if delivered - last_delivery > 2.0 or command_seconds >= lap_seconds * leg_ratio / 4:
                raise DeviceError("设备响应过慢，无法维持设定速度，已停止模拟。请检查 USB 连接或降低速度后重试。")
            latency = 0.7 * latency + 0.3 * command_seconds
            last_delivery = delivered
            last_target_time = target_time
            distance_m = self.counter.distance_m + lap_moved
            self.progress.emit(min(1.0, lap_moved / cycle_length))
            self.distance_changed.emit(distance_m)
            if at_boundary:
                # A faster-than-predicted command must not complete a lap ahead of schedule.
                wait_until(boundary)
                returning = turnaround
            with self._settings_lock:
                _, target_m = self._settings
            if target_m != self.counter.target_m:
                self.counter.target_m = target_m
                self.counter.notified = False
            if at_end:
                notify = self.counter.complete_lap()
                self.lap.emit(self.counter.laps, self.counter.distance_m)
            else:
                notify = self.counter.check_target(distance_m)
            if notify:
                self.target_reached.emit(distance_m)
            next_update = target_time + interval


class RestoreWorker(QThread):
    restored = Signal(bool, str)

    def __init__(self, backend, device, parent=None):
        super().__init__(parent)
        self.backend, self.device = backend, device

    def stop(self):
        # Recovery is already bounded by the native-command timeout.
        pass

    def run(self):
        LOG.info("Starting manual location reset")
        try:
            self.backend.reset(self.device)
            self.restored.emit(True, RESET_SENT)
        except Exception as error:
            LOG.exception("Manual location reset failed")
            self.restored.emit(False, restore_failure(error))
