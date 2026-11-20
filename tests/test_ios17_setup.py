# SPDX-License-Identifier: MPL-2.0
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
import pytest
from ios17_helper.main import Session, SetupHint, open_paired_usb


@pytest.fixture
def api(monkeypatch, tmp_path):
    import pymobiledevice3.lockdown as lockdown
    import pymobiledevice3.remote.userspace_tunnel as tunnel
    import pymobiledevice3.services.mobile_image_mounter as mounter
    import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt
    import pymobiledevice3.services.dvt.instruments.location_simulation as location
    import pymobiledevice3.services.amfi as amfi
    import pymobiledevice3.services.cryptexd as cryptex

    state = SimpleNamespace(
        lockdown=SimpleNamespace(udid="phone", product_version="17.6",
                                 close=AsyncMock(), get_developer_mode_status=AsyncMock(return_value=True)),
        rsd=SimpleNamespace(udid="phone"),
        location=SimpleNamespace(set=AsyncMock(), clear=AsyncMock()),
        mounted=True, tunnel_args=[], events=[])
    factory = AsyncMock(side_effect=lambda **kwargs: state.lockdown)
    state.create_lockdown = factory
    monkeypatch.setattr(lockdown, "create_using_usbmux", factory)
    @asynccontextmanager
    async def userspace(**kwargs):
        state.tunnel_args.append(kwargs)
        state.events.append("tunnel-open")
        try:
            yield state.rsd
        finally:
            state.events.append("tunnel-close")
    @asynccontextmanager
    async def mounted(provider):
        assert provider is state.rsd
        yield SimpleNamespace(is_image_mounted=AsyncMock(side_effect=lambda _: state.mounted))
    @asynccontextmanager
    async def provider(rsd):
        assert rsd is state.rsd
        yield "provider"
    @asynccontextmanager
    async def simulation(provider):
        assert provider == "provider"
        yield state.location
    state.mount = AsyncMock()
    state.download = AsyncMock()
    state.base = tmp_path / "bundled"
    state.cryptex_installed = False
    state.reveal = AsyncMock()
    monkeypatch.setattr(tunnel, "UserspaceRsdTunnel", userspace)
    state.legacy_image_probe = AsyncMock()
    @asynccontextmanager
    async def image_probe(provider):
        await state.legacy_image_probe()
        async with mounted(provider) as image:
            yield image
    image_probe.IMAGE_TYPE = "Personalized"
    monkeypatch.setattr(mounter, "PersonalizedImageMounter", image_probe)
    monkeypatch.setattr(mounter, "uses_cryptex_image", lambda _: state.lockdown.product_version.startswith("27."))
    async def installed():
        return [SimpleNamespace(identifier=cryptex.DDI_CRYPTEX_IDENTIFIER)] if state.cryptex_installed else []
    monkeypatch.setattr(cryptex, "CryptexdService", lambda _: SimpleNamespace(copy_installed=installed))
    monkeypatch.setattr("ios17_helper.main.mount_image", state.mount)
    monkeypatch.setattr("ios17_helper.main.validate_image", lambda *args: "27B52")
    monkeypatch.setattr("ios17_helper.main.IMAGE_ROOT", state.base)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("ios17_helper.main.asyncio.to_thread", state.download)
    monkeypatch.setattr(dvt, "DvtProvider", provider)
    monkeypatch.setattr(location, "LocationSimulation", simulation)
    monkeypatch.setattr(amfi, "AmfiService", lambda _: SimpleNamespace(reveal_developer_mode_option_in_ui=state.reveal))
    return state


@pytest.mark.parametrize("version", ["17.4", "18.7", "26.0"])
def test_userspace_setup_pins_device_and_reuses_mounted_image(api, version):
    async def scenario():
        api.lockdown.product_version = version
        session = Session("phone")
        await session.open()
        await session.set(30, 120)
        await session.clear()
        await session.close()
        assert api.tunnel_args == [{"serial": "phone", "autopair": False, "remotepairing_fallback": False}]
        api.mount.assert_not_awaited()
        api.location.set.assert_awaited_once_with(30, 120)
        api.location.clear.assert_awaited_once()
        api.lockdown.close.assert_awaited_once()
        assert api.events == ["tunnel-open", "tunnel-close"]
    asyncio.run(scenario())


@pytest.mark.parametrize("paired", [True, False])
def test_save_pair_record_conflict_only_reuses_a_valid_pairing(api, monkeypatch, paired):
    from pymobiledevice3.exceptions import MuxException
    error = MuxException("usbmuxd rejected the SavePairRecord request: {'MessageType': 'Result', 'Number': 183}")
    api.lockdown.paired = paired
    api.create_lockdown.side_effect = [error, api.lockdown, api.lockdown]
    monkeypatch.setattr("pymobiledevice3.pair_records.get_local_pairing_record", lambda *_: None)
    monkeypatch.setattr("ios17_helper.main.asyncio.sleep", AsyncMock())
    session = Session("phone")
    if paired:
        async def scenario():
            await session.open()
            await session.close()
        asyncio.run(scenario())
        assert api.tunnel_args == [{"serial": "phone", "autopair": False, "remotepairing_fallback": False}]
        api.lockdown.close.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match="SavePairRecord 183"):
            asyncio.run(session.open())
        assert not api.tunnel_args
        assert api.lockdown.close.await_count == 2
    assert api.create_lockdown.await_args_list == [call(serial="phone")] + [
        call(serial="phone", autopair=False)
    ] * (1 if paired else 2)


def test_save_pair_record_conflict_reuses_verified_local_record_for_tunnel(api, monkeypatch):
    from pymobiledevice3.exceptions import MuxException
    from pymobiledevice3.remote import userspace_tunnel

    record = {"HostID": "local-host"}
    api.lockdown.paired = True
    api.create_lockdown.side_effect = [
        MuxException("usbmuxd rejected the SavePairRecord request: {'Number': 183}"),
        api.lockdown,
    ]
    records = iter([None, record])
    monkeypatch.setattr("pymobiledevice3.pair_records.get_local_pairing_record", lambda *_: next(records))
    connect = AsyncMock(return_value=api.lockdown)
    monkeypatch.setattr(userspace_tunnel, "create_using_usbmux", connect)
    original_tunnel = userspace_tunnel.UserspaceRsdTunnel

    @asynccontextmanager
    async def tunnel(**kwargs):
        await userspace_tunnel.create_using_usbmux(serial=kwargs["serial"], autopair=kwargs["autopair"])
        async with original_tunnel(**kwargs) as rsd:
            yield rsd

    monkeypatch.setattr(userspace_tunnel, "UserspaceRsdTunnel", tunnel)

    async def scenario():
        session = Session("phone")
        try:
            await session.open()
            assert userspace_tunnel.create_using_usbmux is connect
        finally:
            await session.close()

    asyncio.run(scenario())
    api.create_lockdown.assert_awaited_with(serial="phone", autopair=False, pair_record=record)
    connect.assert_awaited_once_with(serial="phone", autopair=False, pair_record=record)


def test_different_usbmux_error_is_not_retried(api):
    from pymobiledevice3.exceptions import MuxException
    api.create_lockdown.side_effect = MuxException("usbmuxd rejected the Connect request: {'Number': 183}")
    with pytest.raises(MuxException, match="Connect"):
        asyncio.run(open_paired_usb("phone"))
    api.create_lockdown.assert_awaited_once_with(serial="phone")


def test_unmounted_image_is_prepared_in_tunnel(api):
    async def scenario():
        api.mounted = False
        session = Session("phone")
        await session.open()
        api.mount.assert_awaited_once_with(api.rsd, "Personalized", api.base / "Personalized")
        api.download.assert_not_awaited()
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("already_mounted", [False, True])
def test_ios_27_checks_cryptex_without_legacy_image_probe(api, already_mounted):
    api.lockdown.product_version = "27.3.1"
    api.cryptex_installed = already_mounted

    async def scenario():
        session = Session("phone")
        await session.open()
        await session.set(30, 120)
        await session.close()

    asyncio.run(scenario())
    api.legacy_image_probe.assert_not_awaited()
    if already_mounted:
        api.mount.assert_not_awaited()
    else:
        api.mount.assert_awaited_once_with(api.rsd, "Cryptex", api.base / "Cryptex")
    api.download.assert_not_awaited()
    api.location.clear.assert_awaited_once()


@pytest.mark.parametrize("reveal_failed", [False, True])
def test_developer_mode_failure_reveals_setting_without_changing_location(api, reveal_failed):
    async def scenario():
        api.lockdown.get_developer_mode_status.return_value = False
        if reveal_failed:
            api.reveal.side_effect = RuntimeError("AMFI service unavailable")
        session = Session("phone")
        with pytest.raises(SetupHint, match="请在 iPhone 或 iPad 上打开") as error:
            await session.open()
        assert "开发者模式未开启" in str(error.value)
        assert "已请求显示开关" not in str(error.value)
        assert ("Xcode" in str(error.value)) == reveal_failed
        await session.close()
        api.reveal.assert_awaited_once()
        api.location.set.assert_not_awaited()
        assert api.tunnel_args == []
        api.lockdown.close.assert_awaited_once()
    asyncio.run(scenario())


def test_failed_bundled_image_uses_online_then_cached_build(api, tmp_path, capsys):
    api.mounted = False
    cache = tmp_path / ".pymobiledevice3" / "IRun-DDI" / "Personalized" / "new-build"
    api.download.return_value = cache
    api.mount.side_effect = [ValueError("incompatible"), None, ValueError("incompatible"), None]

    async def scenario():
        for _ in range(2):
            session = Session("phone")
            try:
                await session.open()
            finally:
                await session.close()
            cache.mkdir(parents=True, exist_ok=True)

    asyncio.run(scenario())
    assert api.mount.await_args_list == [
        call(api.rsd, "Personalized", api.base / "Personalized"),
        call(api.rsd, "Personalized", cache),
    ] * 2
    api.download.assert_awaited_once()
    statuses = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
    assert any("通过电脑网络下载开发者镜像" in status for status in statuses)
    assert any("下载完成，正在将开发者镜像加载到 iPhone 或 iPad" in status for status in statuses)
    assert any("电脑上已下载的开发者镜像加载到 iPhone 或 iPad" in status for status in statuses)


@pytest.mark.parametrize("admin, hint", [(False, "以管理员身份运行"), (True, "重新插拔 USB 线")])
def test_early_ios17_failure_has_actionable_hint(api, monkeypatch, admin, hint):
    api.lockdown.product_version = "17.3.1"
    monkeypatch.setattr("ios17_helper.main.windows_admin", lambda: admin)
    early_tunnel = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(Session, "_early_tunnel", early_tunnel)

    async def scenario():
        session = Session("phone")
        try:
            with pytest.raises(SetupHint, match=hint):
                await session.open()
        finally:
            await session.close()

    asyncio.run(scenario())
    assert early_tunnel.await_count == int(admin)
    api.download.assert_not_awaited()


def test_wrong_tunnel_device_is_rejected_before_mount_or_location(api):
    async def scenario():
        api.rsd.udid = "other"
        session = Session("phone")
        with pytest.raises(RuntimeError, match="其他设备"):
            await session.open()
        await session.close()
        api.mount.assert_not_awaited()
        api.location.set.assert_not_awaited()
        assert api.events[-1] == "tunnel-close"
    asyncio.run(scenario())


def test_early_ios17_uses_explicit_quic(monkeypatch):
    import pymobiledevice3.remote.tunnel_service as tunnel
    import pymobiledevice3.remote.remote_service_discovery as discovery
    from pymobiledevice3.remote.common import TunnelProtocol
    events = []
    service = SimpleNamespace(rsd=SimpleNamespace(udid="phone"), close=AsyncMock())
    get_services = AsyncMock(return_value=[service])
    monkeypatch.setattr(tunnel, "get_core_device_tunnel_services", get_services)
    @asynccontextmanager
    async def start(selected, **kwargs):
        assert selected is service
        assert kwargs == {"protocol": TunnelProtocol.QUIC}
        events.append("open")
        try:
            yield SimpleNamespace(address="::1", port=1234)
        finally:
            events.append("close")
    @asynccontextmanager
    async def rsd(address):
        assert address == ("::1", 1234)
        yield "connected-rsd"
    monkeypatch.setattr(tunnel, "start_tunnel", start)
    monkeypatch.setattr(discovery, "RemoteServiceDiscoveryService", rsd)
    async def scenario():
        session = Session("phone")
        assert await session._early_tunnel() == "connected-rsd"
        get_services.assert_awaited_once_with(udid="phone")
        await session.close()
        assert events == ["open", "close"]
        service.close.assert_awaited_once()
    asyncio.run(scenario())
