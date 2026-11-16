# SPDX-License-Identifier: MPL-2.0
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
import pytest
from ios17_helper.main import Session, open_paired_usb


@pytest.fixture
def api(monkeypatch):
    import pymobiledevice3.lockdown as lockdown
    import pymobiledevice3.remote.userspace_tunnel as tunnel
    import pymobiledevice3.services.mobile_image_mounter as mounter
    import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt
    import pymobiledevice3.services.dvt.instruments.location_simulation as location
    import pymobiledevice3.services.amfi as amfi

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
    monkeypatch.setattr(mounter, "auto_mount", state.mount)
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
        api.mount.assert_awaited_once_with(api.rsd)
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("already_mounted", [False, True])
def test_ios_27_checks_cryptex_without_legacy_image_probe(api, already_mounted):
    from pymobiledevice3.exceptions import AlreadyMountedError
    api.lockdown.product_version = "27.3.1"
    if already_mounted:
        api.mount.side_effect = AlreadyMountedError("Cryptex is already installed")

    async def scenario():
        session = Session("phone")
        await session.open()
        await session.set(30, 120)
        await session.close()

    asyncio.run(scenario())
    api.legacy_image_probe.assert_not_awaited()
    api.mount.assert_awaited_once_with(api.rsd)
    api.location.clear.assert_awaited_once()


def test_developer_mode_failure_reveals_setting_without_changing_location(api):
    async def scenario():
        api.lockdown.get_developer_mode_status.return_value = False
        session = Session("phone")
        with pytest.raises(RuntimeError, match="Developer mode"):
            await session.open()
        await session.close()
        api.reveal.assert_awaited_once()
        api.location.set.assert_not_awaited()
        assert api.tunnel_args == []
        api.lockdown.close.assert_awaited_once()
    asyncio.run(scenario())


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
