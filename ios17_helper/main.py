# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent JSON-lines service. No GUI imports; stdout is reserved for protocol replies.

Inspired by iOSRealRun-cli-17's RSD -> DVT -> LocationSimulation flow.
Uses the pinned pymobiledevice3 API; the desktop app communicates only over pipes.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
import ctypes
import json
import logging
import math
from pathlib import Path
import re
import ssl
import sys

if __package__ is None and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ios17_helper.images import (ROOT as IMAGE_ROOT, check_bundled_images, prepare_remote_image,
                                 validate_image)

LOG = logging.getLogger("IRun.iOS17")


class SetupHint(RuntimeError):
    pass


def emit(**message):
    if message.get("event") == "status":
        LOG.info("%s", message["message"])
    print(json.dumps(message, ensure_ascii=True), flush=True)


def explain(error):
    if isinstance(error, SetupHint):
        return str(error)
    name = type(error).__name__
    lower = (name + " " + str(error)).lower()
    if "developer" in lower and ("mode" in lower or "enabled" in lower):
        return "请在 iPhone 的“设置 → 隐私与安全性 → 开发者模式”中开启开发者模式，重启并确认。"
    if "locked" in lower or "password" in lower or "passcode" in lower:
        return "请先解锁 iPhone，再点击开始。"
    if "pairingdialog" in lower or "userdenied" in lower or "notpaired" in lower:
        return "请在 iPhone 上确认“信任此电脑”，完成后再次点击开始。"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "iOS 17+ 准备或通信超时，请检查 USB、手机解锁状态和下载镜像所需的网络。"
    if isinstance(error, ImportError):
        return "iOS 17+ 依赖缺失，请重新安装完整的 IRun 安装包。"
    return f"iOS 17+ 设备通信失败（{name}）：{str(error)[:350]}"


def emit_error(identifier, error):
    codes = {key: value for key in ("code", "status_code", "errno", "winerror")
             if isinstance(value := getattr(error, key, None), int)}
    emit(id=identifier, ok=False, error=explain(error), error_type=type(error).__name__,
         detail=str(error)[:4096], codes=codes)


def early_ios17(version):
    parts = tuple(int(value) for value in version.split(".")[:2])
    return parts < (17, 4)


def windows_admin():
    return sys.platform != "win32" or bool(ctypes.windll.shell32.IsUserAnAdmin())


async def mount_image(rsd, variant, directory):
    from pymobiledevice3.exceptions import AlreadyMountedError
    from pymobiledevice3.services.mobile_image_mounter import PersonalizedImageMounter
    from pymobiledevice3.services.cryptexd import CryptexdService

    with suppress(AlreadyMountedError):
        if variant == "Cryptex":
            await CryptexdService(rsd).auto_install_ddi(restore_dir=directory)
        else:
            async with PersonalizedImageMounter(rsd) as mounter:
                await mounter.mount(directory / "Image.dmg", directory / "BuildManifest.plist",
                                    directory / "Image.dmg.trustcache")


async def prepare_developer_image(rsd):
    from pymobiledevice3.services.mobile_image_mounter import PersonalizedImageMounter, uses_cryptex_image
    from pymobiledevice3.services.cryptexd import CryptexdService, DDI_CRYPTEX_IDENTIFIER

    cryptex = uses_cryptex_image(rsd)
    if cryptex:
        if any(image.identifier == DDI_CRYPTEX_IDENTIFIER for image in await CryptexdService(rsd).copy_installed()):
            return
    else:
        async with PersonalizedImageMounter(rsd) as mounter:
            if await mounter.is_image_mounted(PersonalizedImageMounter.IMAGE_TYPE):
                return
    variant = "Cryptex" if cryptex else "Personalized"
    bundled = IMAGE_ROOT / variant
    emit(event="status", message="正在使用预置开发者镜像…")
    try:
        validate_image(bundled, variant)
        await mount_image(rsd, variant, bundled)
        return
    except Exception:
        LOG.warning("Bundled %s developer image could not be used", variant, exc_info=True)

    cache = Path.home() / ".pymobiledevice3" / "IRun-DDI" / variant
    cached = sorted((p for p in cache.glob("*") if p.is_dir() and not p.name.startswith("download-")),
                    key=lambda p: p.stat().st_mtime)
    for directory in cached[-1:]:
        emit(event="status", message="正在尝试已下载的开发者镜像…")
        try:
            validate_image(directory, variant)
            await mount_image(rsd, variant, directory)
            return
        except Exception:
            LOG.warning("Cached %s developer image could not be used", variant, exc_info=True)

    emit(event="status", message="本地镜像未能使用，正在从 GitHub 下载开发者镜像…")
    try:
        directory = await asyncio.to_thread(prepare_remote_image, cache, variant)
    except Exception as error:
        raise SetupHint("预置镜像未能使用，在线镜像也未能下载或校验。"
                        "请检查电脑能否访问 raw.githubusercontent.com，然后重试。") from error
    try:
        await mount_image(rsd, variant, directory)
    except Exception as error:
        raise SetupHint("已下载开发者镜像，但设备未能接受。请检查 USB、开发者模式及苹果签名服务 "
                        f"gs.apple.com 的网络连接，然后重试。详细信息：{str(error)[:190]}") from error


async def open_paired_usb(udid):
    from pymobiledevice3.exceptions import MuxException, PairingError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.pair_records import create_pairing_records_cache_folder, get_local_pairing_record

    async def open_local_record():
        try:
            record = get_local_pairing_record(udid, create_pairing_records_cache_folder())
        except (OSError, ValueError):
            return None
        if not record:
            return None
        try:
            connection = await create_using_usbmux(serial=udid, autopair=False, pair_record=record)
        except (PairingError, ssl.SSLError, KeyError):
            LOG.warning("Cached USB pairing record could not be validated", exc_info=True)
            return None
        if connection.paired and connection.udid == udid:
            return connection, record
        await connection.close()
        return None

    if sys.platform == "win32":
        local = await open_local_record()
        if local is not None:
            return local
    try:
        return await create_using_usbmux(serial=udid), None
    except MuxException as error:
        # In the pinned library the plist result code is only present in the message.
        message = str(error)
        if (sys.platform != "win32"
                or not message.startswith("usbmuxd rejected the SavePairRecord request:")
                or not re.search(r"['\"]Number['\"]:\s*183\b", message)):
            raise
        emit(event="status", message="正在重新验证 Windows 上已有的 USB 配对记录…")
        # Pairing writes the local copy before asking Windows to save its copy.
        # Default lookup may keep picking an older Windows record first.
        local = await open_local_record()
        if local is not None:
            return local
        for delay in (0.25, 0.75):
            await asyncio.sleep(delay)
            connection = await create_using_usbmux(serial=udid, autopair=False)
            if connection.paired and connection.udid == udid:
                return connection, None
            await connection.close()
        raise SetupHint(
            "USB 配对记录未通过验证（SavePairRecord 183）。"
            "请解锁设备，打开 Windows 上的「Apple 设备」或 iTunes，"
            "确认能看到这台设备并在手机上选择「信任此电脑」，再回到 IRun 点击开始。"
            "配对成功后，IRun 才能检查并尝试显示「开发者模式」开关。"
        ) from error


class Session:
    def __init__(self, udid):
        self.udid = udid
        self.stack = AsyncExitStack()
        self.location = None
        self.location_attempted = False

    async def open(self):
        from pymobiledevice3.remote import userspace_tunnel
        from pymobiledevice3.services.amfi import AmfiService
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

        emit(event="status", message="正在验证 USB 配对，请保持设备解锁…")
        lockdown, recovered_record = await open_paired_usb(self.udid)
        self.stack.push_async_callback(lockdown.close)
        if lockdown.udid != self.udid:
            raise RuntimeError("设备标识不匹配，已取消连接。")
        version = lockdown.product_version
        if int(version.split(".")[0]) < 17:
            raise SetupHint(f"当前设备系统为 {version}。IRun 需要 iOS 或 iPadOS 17 及以上。")
        emit(event="status", message="USB 配对已通过，正在检查开发者模式…")
        if not await lockdown.get_developer_mode_status():
            try:
                await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
            except Exception as error:
                LOG.warning("Could not reveal the Developer Mode setting", exc_info=True)
                raise SetupHint(
                    "开发者模式未开启，IRun 未能显示开关。请保持设备解锁并重新连接后重试。"
                    "如果「设置 → 隐私与安全性」里仍无此项，可连接运行 Xcode 的 Mac 发起设备配对，"
                    "再回到设备上开启开发者模式并在重启后确认。"
                ) from error
            raise SetupHint(
                "开发者模式未开启，已请求显示开关。请在设备上打开「设置 → 隐私与安全性 → 开发者模式」，"
                "开启后按提示重启，解锁并确认「打开」，再重新连接 IRun。"
            )
        if early_ios17(version):
            if not windows_admin():
                raise SetupHint("iOS 17.0–17.3.x 需要管理员权限。请退出 IRun，右键快捷方式，选择「以管理员身份运行」。")
            try:
                rsd = await asyncio.wait_for(self._early_tunnel(), 45)
            except Exception as error:
                raise SetupHint("无法建立 iOS 17.0–17.3.x 开发者连接。请检查设备管理器中的 Apple 网络设备驱动，"
                                "确认 Windows 网络适配器已启用 IPv6，重新插拔 USB 后重试。") from error
        else:
            emit(event="status", message="正在建立 iOS 17.4+ USB 开发者隧道…")
            tunnel = userspace_tunnel.UserspaceRsdTunnel(
                serial=self.udid, autopair=False, remotepairing_fallback=False)
            original_connect = userspace_tunnel.create_using_usbmux
            if recovered_record is not None:
                from functools import partial
                # The pinned tunnel opens a second lockdown connection. Keep its lookup
                # on the verified record for this one session in the helper subprocess.
                userspace_tunnel.create_using_usbmux = partial(original_connect, pair_record=recovered_record)
            try:
                rsd = await self.stack.enter_async_context(tunnel)
            finally:
                userspace_tunnel.create_using_usbmux = original_connect
        if rsd.udid != self.udid:
            raise RuntimeError("开发者隧道连接到了其他设备，已停止。")
        await prepare_developer_image(rsd)
        provider = await self.stack.enter_async_context(DvtProvider(rsd))
        self.location = await self.stack.enter_async_context(LocationSimulation(provider))

    async def _early_tunnel(self):
        from pymobiledevice3.remote.common import TunnelProtocol
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.remote.tunnel_service import get_core_device_tunnel_services, start_tunnel

        emit(event="status", message="正在建立 iOS 17.0–17.3 QUIC 隧道，请保持 USB 和 IPv6 网络可用…")
        services = await get_core_device_tunnel_services(udid=self.udid)
        for service in services:
            self.stack.push_async_callback(service.close)
        matching = [service for service in services if service.rsd.udid == self.udid]
        if not matching:
            raise RuntimeError("未发现这台手机的 RemoteXPC 服务。iOS 17.0–17.3 需要 Apple 网络设备驱动、IPv6 和管理员权限；请检查设备管理器及 iTunes/Apple Devices。")
        # The Python >=3.13 default is TCP; early iOS 17 explicitly needs QUIC.
        tunnel = await self.stack.enter_async_context(start_tunnel(matching[0], protocol=TunnelProtocol.QUIC))
        return await self.stack.enter_async_context(RemoteServiceDiscoveryService((tunnel.address, tunnel.port)))

    async def set(self, lat, lng):
        if isinstance(lat, bool) or isinstance(lng, bool):
            raise ValueError("坐标必须是数值。")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            raise ValueError("坐标必须是数值。")
        if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError("坐标超出有效范围。")
        self.location_attempted = True
        await asyncio.wait_for(self.location.set(lat, lng), 8)

    async def clear(self):
        await asyncio.wait_for(self.location.clear(), 8)
        self.location_attempted = False

    async def close(self):
        try:
            if self.location_attempted and self.location is not None:
                await self.clear()
        finally:
            await asyncio.wait_for(self.stack.aclose(), 10)


async def serve(udid):
    session = Session(udid)
    try:
        await asyncio.wait_for(session.open(), 280)
        emit(id=0, ok=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            identifier = None
            try:
                request = json.loads(line)
                identifier = request["id"]
                if request["op"] == "set":
                    await session.set(request["lat"], request["lng"])
                elif request["op"] == "clear":
                    await session.clear()
                else:
                    raise ValueError("未知的定位操作。")
                emit(id=identifier, ok=True)
            except Exception as error:
                LOG.exception("Location operation failed")
                emit_error(identifier, error)
    except Exception as error:
        LOG.exception("Developer service setup failed")
        emit_error(0, error)
        return 1
    finally:
        try:
            await session.close()
        except Exception:
            LOG.exception("Developer service cleanup failed")
    return 0


def check_dependencies():
    from importlib.metadata import version
    from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
    from pymobiledevice3.remote.tunnel_service import get_core_device_tunnel_services, start_tunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
    from pymobiledevice3.services.mobile_image_mounter import auto_mount
    from pymobiledevice3.services.cryptexd import CryptexdService
    from pytun_pmd3 import TunTapDevice
    import pytun_pmd3
    import qh3
    import win32security
    ssl.create_default_context()
    if sys.platform == "win32":
        wintun = Path(pytun_pmd3.__file__).parent / "wintun" / "bin" / "amd64" / "wintun.dll"
        if not wintun.is_file():
            raise RuntimeError("安装包缺少 64 位 Wintun 组件。")
    images = check_bundled_images()["images"]
    emit(ok=True, version=version("pymobiledevice3"), protocol=1,
         images={variant: entry["build"] for variant, entry in images.items()})
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, encoding="utf-8", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    LOG.setLevel(logging.INFO)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        if args.check:
            return check_dependencies()
        if not args.udid:
            parser.error("--udid is required")
        return asyncio.run(serve(args.udid))
    except Exception as error:
        LOG.exception("iOS 17 helper failed")
        emit_error(0, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
