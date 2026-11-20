# SPDX-License-Identifier: MPL-2.0
from pathlib import Path
from runpy import run_path
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = Path(SPECPATH)
version_info = run_path(str(root / "vrun" / "__init__.py"))
app_version = version_info["__version__"]
version_tuple = version_info["WINDOWS_FILE_VERSION"]
version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, mask=0x3F, flags=0,
                     OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", "IRun"),
            StringStruct("FileVersion", app_version),
            StringStruct("OriginalFilename", "IRun.exe"),
            StringStruct("ProductName", "IRun"),
            StringStruct("ProductVersion", app_version),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)
a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        *[(str(path), "libimobiledevice/win") for path in (root / "libimobiledevice" / "win").iterdir()
          if path.suffix == ".dll" or path.name == "ideviceinfo.exe"],
        *[(str(root / "assets" / name), "assets") for name in (
            "check-ink.svg", "check-white.svg", "chevron.svg", "irun.ico", "irun.png"
        )],
    ],
    hiddenimports=["winotify"],
    hookspath=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "numpy", "pandas", "scipy", "matplotlib",
              "pymobiledevice3", "pmd_pytcp", "pmd_net_addr", "pmd_net_proto",
              "ios17_helper", "IPython", "av"],
    noarchive=False,
)
# Qt on Windows uses the OS-provided ICU. A similarly named DLL on the build
# machine's PATH may export a different ABI and prevent QtCore from loading.
a.binaries = [
    entry for entry in a.binaries
    if entry[0].lower() != "icuuc.dll" and not entry[0].lower().startswith("icudt")
]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="IRun",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(root / "assets" / "irun.ico"), version=version)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="IRun")
