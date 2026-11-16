# SPDX-License-Identifier: MPL-2.0
"""Installer-only Apple component bootstrap. Downloads are signature checked."""
from pathlib import Path
import base64
import ctypes
import hashlib
import logging
import os
import subprocess
import socket
import tempfile
import time
import urllib.request

LOG = logging.getLogger(__name__)
APPLE_URL = "https://www.apple.com/itunes/download/win64"
USB_URL = ("https://catalog.s.download.windowsupdate.com/d/msdownload/update/driver/drvs/2023/10/"
           "5fb262ea-d52d-46a7-9361-f3260ba57a1a_3e8075a4dded0a795131f82285e2f1a06525ebc2.cab")
USB_SHA256 = "8ae3b3001e3733f24ef0178f149c6534f94e57d16e8a0d75647ab979e03151ca"
APPLE_SERVICE = "Apple Mobile Device Service"


def download(url: str, target: Path):
    errors = []
    deadline = time.monotonic() + 300
    for opener in (urllib.request.build_opener(),
                   urllib.request.build_opener(urllib.request.ProxyHandler({}))):
        try:
            with opener.open(url, timeout=45) as response, target.open("wb") as output:
                # urllib validates TLS certificates for both system-proxy and direct requests.
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("下载超时")
                    output.write(chunk)
            return
        except Exception as error:
            errors.append(str(error))
    raise RuntimeError("下载失败，请检查网络：" + "; ".join(errors))


def signature(path: Path, signer: str):
    encoded_path = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    code = ("$ErrorActionPreference='Stop';"
            "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1');"
            f"$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'));"
            "$s=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $p;"
            f"if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*{signer}*')"
            "{exit 1};exit 0")
    encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
    run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout=60)


def extract_service_package(installer: Path, directory: Path) -> Path:
    package = directory / "AppleMobileDeviceSupport64.msi"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    dword = ctypes.c_uint32
    kernel.LoadLibraryExW.argtypes = (ctypes.c_wchar_p, handle, dword)
    kernel.LoadLibraryExW.restype = handle
    kernel.FindResourceW.argtypes = (handle, ctypes.c_wchar_p, handle)
    kernel.FindResourceW.restype = handle
    kernel.SizeofResource.argtypes = (handle, handle)
    kernel.SizeofResource.restype = dword
    kernel.LoadResource.argtypes = (handle, handle)
    kernel.LoadResource.restype = handle
    kernel.LockResource.argtypes = (handle,)
    kernel.LockResource.restype = handle
    kernel.FreeLibrary.argtypes = (handle,)
    kernel.FreeLibrary.restype = ctypes.c_int

    # Map the signed installer as data and never run it or load its code.
    module = kernel.LoadLibraryExW(str(installer), None, 0x22)  # AS_DATAFILE | AS_IMAGE_RESOURCE
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        resource = kernel.FindResourceW(module, "CABINET", 10)  # RT_RCDATA
        if not resource:
            raise RuntimeError("Apple 安装程序缺少驱动 CAB 资源。")
        length = kernel.SizeofResource(module, resource)
        loaded = kernel.LoadResource(module, resource)
        address = kernel.LockResource(loaded) if loaded else None
        if length < 36 or not address or ctypes.string_at(address, 4) != b"MSCF":
            raise RuntimeError("Apple 安装程序内的驱动 CAB 无法读取。")
        cabinet = directory / "AppleSetup.cab"
        with cabinet.open("wb") as output:
            chunk_size = 1024 * 1024
            for offset in range(0, length, chunk_size):
                output.write(ctypes.string_at(address + offset, min(chunk_size, length - offset)))
    finally:
        kernel.FreeLibrary(module)
    run(["expand.exe", f"-F:{package.name}", cabinet, directory])
    if not package.is_file():
        raise RuntimeError("未找到 Apple 移动设备支持安装包。")
    return package


def system_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise RuntimeError("无法定位 Windows 系统目录。")
    return Path(buffer.value)


def run(args, timeout=180, accepted=(0,)):
    directory = system_directory()
    environment = None
    if args[0] == "powershell.exe":
        directory /= "WindowsPowerShell/v1.0"
        # Windows PowerShell must find its own inbox modules, even if launched
        # from a shell whose module path points to a different PowerShell version.
        environment = os.environ.copy()
        environment.pop("PSModulePath", None)
    process = subprocess.run([str(directory / args[0]), *map(str, args[1:])],
                             capture_output=True, timeout=timeout, env=environment,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    LOG.info("Command %s returned %s: %s", Path(str(args[0])).name, process.returncode,
             (process.stdout + process.stderr).decode("oem", errors="replace")[-2000:])
    if process.returncode not in accepted:
        raise RuntimeError(f"{Path(str(args[0])).name} 执行失败（{process.returncode}）。")
    return process.returncode


def service_registered() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{APPLE_SERVICE}"):
            return True
    except FileNotFoundError:
        return False


def usb_driver_staged() -> bool:
    # The driver can already be staged on a PC that has never had a phone plugged in.
    store = system_directory() / "DriverStore" / "FileRepository"
    return any(next(store.glob(f"{name}_amd64_*/{name}"), None) is not None
               for name in ("appleusb.inf", "usbaapl64.inf"))


def start_apple_service():
    run(["sc.exe", "start", APPLE_SERVICE], accepted=(0, 1056))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 27015), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Apple 移动设备服务未能启动 USB 通信，请重试安装。")


def install() -> int:
    reboot = False
    with tempfile.TemporaryDirectory(prefix="IRun-Apple-") as folder:
        temporary = Path(folder)
        if not service_registered():
            installer = temporary / "iTunes64Setup.exe"
            LOG.info("Downloading Apple Mobile Device Support")
            download(APPLE_URL, installer)
            signature(installer, "O=Apple Inc.")
            package = extract_service_package(installer, temporary)
            signature(package, "O=Apple Inc.")
            log_dir = Path(os.environ.get("PROGRAMDATA", temporary)) / "IRun"
            log_dir.mkdir(parents=True, exist_ok=True)
            code = run(["msiexec.exe", "/i", package, "/qn", "/norestart", "/L*v",
                        log_dir / "apple-msi.log"], accepted=(0, 3010))
            reboot = code == 3010
        if not usb_driver_staged():
            cab = temporary / "AppleUSB.cab"
            download(USB_URL, cab)
            if hashlib.sha256(cab.read_bytes()).hexdigest() != USB_SHA256:
                raise RuntimeError("Apple USB 驱动校验失败，安装已停止。")
            drivers = temporary / "drivers"
            drivers.mkdir()
            run(["expand.exe", "-F:*", cab, drivers])
            signature(drivers / "appleusb.cat", "Microsoft Windows Hardware Compatibility Publisher")
            code = run(["pnputil.exe", "/add-driver", drivers / "AppleUsb.inf", "/install"],
                       accepted=(0, 3010))
            reboot = reboot or code == 3010
            if not reboot and not usb_driver_staged():
                raise RuntimeError("Windows 未将 Apple USB 驱动加入驱动库。")
        if not reboot:
            start_apple_service()
    return 3010 if reboot else 0


def install_main() -> int:
    directory = Path(os.environ.get("PROGRAMDATA", tempfile.gettempdir())) / "IRun"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=directory / "driver-install.log", encoding="utf-8", level=logging.INFO)
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("驱动安装需要管理员权限，请重新运行 IRun 安装包。")
        # This process launches Windows tools, which must load their own runtime libraries.
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        return install()
    except Exception as error:
        LOG.exception("Apple component installation failed")
        return 1
