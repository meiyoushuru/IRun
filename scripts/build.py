# SPDX-License-Identifier: MPL-2.0
"""Build the self-contained Windows application and optional Inno Setup installer."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vrun import WINDOWS_FILE_VERSION, __version__
from prepare_ddi import prepare as prepare_ddi
from vrun.offline_maps import OFFLINE_MAP_ASSET, read_map_views


def copy_runtime_notices(target: Path):
    import importlib.metadata as metadata
    license_root = target / "LICENSES"
    license_root.mkdir(parents=True, exist_ok=True)
    from archive_ios17 import distributions
    names = {"PySide6_Essentials", "shiboken6", "geopy", "geographiclib", "winotify", *distributions()}
    for name in sorted(names):
        distribution = metadata.distribution(name)
        for file in distribution.files or []:
            if any(part.lower() in ("licenses", "license", "copying") for part in file.parts) or file.name.lower().startswith(("license", "copying", "notice")):
                path = Path(distribution.locate_file(file))
                if path.is_file():
                    destination = license_root / name / Path(*file.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
    # Wintun retains its separate prebuilt-driver distribution license.
    import pytun_pmd3
    wintun_license = Path(pytun_pmd3.__file__).parent / "wintun" / "LICENSE.txt"
    shutil.copy2(wintun_license, license_root / "Wintun.txt")
    python_license = Path(sys.base_prefix) / "LICENSE_PYTHON.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, license_root / "Python.txt")


def copy_release_material(target: Path):
    shutil.copytree(ROOT / "LICENSES", target / "LICENSES", dirs_exist_ok=True)
    for name in ("LICENSE", "README.md", "CHANGELOG.md", "UPSTREAM.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, target / name)
    copy_runtime_notices(target)
    source_dir = target / "SOURCE"
    source_dir.mkdir(exist_ok=True)
    archive = source_dir / f"IRun-{__version__}-source.zip"
    subprocess.run(["git", "archive", "--format=zip", "--output", str(archive), "HEAD", "--", ".",
                    ":(exclude)routes", ":(exclude)libimobiledevice", ":(exclude)DeveloperDiskImage"],
                   cwd=ROOT, check=True)
    shutil.copytree(ROOT / "third_party" / "sources", source_dir / "third_party", dirs_exist_ok=True)


def find_iscc():
    candidates = [os.environ.get("INNO_ISCC", ""), str(ROOT / "build/inno/ISCC.exe")]
    for prefix in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        for version in (6, 7):
            candidates.append(str(Path(prefix) / f"Inno Setup {version}" / "ISCC.exe"))
    return next((path for path in candidates if path and Path(path).is_file()), None)


def verify_bundle(target: Path):
    internal = target / "_internal"
    manifest = json.loads((ROOT / "third_party/windows-manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        tool = internal / "libimobiledevice" / "win" / entry["name"]
        if tool.suffix == ".exe" and tool.name != "ideviceinfo.exe":
            continue
        if hashlib.sha256(tool.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Unexpected content in bundled {tool.name}")
        if tool.suffix == ".exe":
            subprocess.run([str(tool), "--version"], check=True, timeout=15)

    if (internal / "DeveloperDiskImage").exists():
        raise RuntimeError("The application bundle contains obsolete iOS 15–16 images.")
    pinned = (ROOT / "ios17_helper/ddi-lock.json").read_bytes()
    if (internal / "ios17/_internal/ddi/bundle.json").read_bytes() != pinned:
        raise RuntimeError("The packaged developer images do not match the pinned snapshot.")
    subprocess.run([str(internal / "ios17/IRun-iOS17.exe"), "--check"], check=True, timeout=30)

    with TemporaryDirectory(prefix="IRun-build-check-") as temporary:
        screenshot = Path(temporary) / "startup.png"
        env = os.environ | {"LOCALAPPDATA": temporary}
        subprocess.run([str(target / "IRun.exe"), "--screenshot", str(screenshot)],
                       env=env, check=True, timeout=30)
        if not screenshot.is_file() or not screenshot.stat().st_size:
            raise RuntimeError("The packaged IRun window did not render")


def check_map_pack(path):
    try:
        with zipfile.ZipFile(path) as archive:
            if not read_map_views(archive):
                raise ValueError("missing preset views")
            if not any(re.fullmatch(r"tiles/\d+/\d+/\d+\.png", name) for name in archive.namelist()):
                raise ValueError("missing tile images")
            if corrupt := archive.testzip():
                raise ValueError(f"corrupt ZIP entry: {corrupt}")
    except (OSError, zipfile.BadZipFile, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"Invalid offline map pack: {error}") from error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--offline-map-pack", type=Path)
    parser.add_argument("--edition", help="Label for a custom build, used in its output paths")
    args = parser.parse_args()
    if bool(args.offline_map_pack) != bool(args.edition):
        parser.error("--offline-map-pack and --edition must be provided together")
    if subprocess.check_output(["git", "ls-files", "-z", "--", ".local-editions/"], cwd=ROOT):
        raise SystemExit("Remove local edition files from the public Git index before building.")
    pack = args.offline_map_pack.resolve() if args.offline_map_pack else None
    if pack:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,29}", args.edition):
            parser.error("--edition must contain only ASCII letters, numbers, and hyphens")
        if pack.is_relative_to(ROOT):
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", "--", pack.relative_to(ROOT).as_posix()],
                cwd=ROOT, capture_output=True,
            )
            if ignored.returncode != 0:
                parser.error("Use an ignored local path or a map pack outside the repository")
        check_map_pack(pack)
    os.chdir(ROOT)
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT).returncode:
        raise SystemExit("Commit tracked source changes before building a release.")
    dist = ROOT / "dist"
    work = ROOT / "build"
    release = ROOT / "release"
    suffix = ""
    if pack:
        dist = dist / "editions" / args.edition
        work = work / "editions" / args.edition
        release = release / "editions" / args.edition
        suffix = f"-{args.edition}"
    prepare_ddi(check_latest=args.installer)
    if not args.prepare_only:
        for spec in ("ios17_helper/helper.spec", "IRun.spec"):
            subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
                            "--distpath", str(dist), "--workpath", str(work), spec], check=True)
    target = dist / "IRun"
    if not (target / "IRun.exe").is_file():
        raise SystemExit(f"{target / 'IRun.exe'} is missing")
    helper = dist / "IRun-iOS17"
    if not (helper / "IRun-iOS17.exe").is_file():
        raise SystemExit(f"{helper / 'IRun-iOS17.exe'} is missing")
    offline_asset = target / "_internal" / OFFLINE_MAP_ASSET
    if pack:
        shutil.copy2(pack, offline_asset)
    elif offline_asset.exists() or (offline_asset.parent / "maps").exists():
        raise SystemExit("Unexpected map assets in the public bundle. Rebuild without --prepare-only.")
    shutil.copytree(helper, target / "_internal/ios17", dirs_exist_ok=True)
    verify_bundle(target)
    copy_release_material(target)
    release.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": __version__,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        "python": sys.version,
        "platform": "Windows x64",
        "edition": args.edition or "public",
    }
    (release / "build-info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.installer:
        compiler = find_iscc()
        if not compiler:
            raise SystemExit("Inno Setup 6/7 is required; set INNO_ISCC to ISCC.exe")
        file_version = ".".join(map(str, WINDOWS_FILE_VERSION))
        subprocess.run([compiler, f"/DAppVersion={__version__}", f"/DFileVersion={file_version}",
                        f"/DBundleDir={target}", f"/DReleaseDir={release}", f"/DEditionSuffix={suffix}",
                        str(ROOT / "installer/IRun.iss")], check=True)
    artifacts = sorted(release.glob(f"IRun-{__version__}{suffix}-Setup-x64.exe"))
    (release / "SHA256SUMS.txt").write_text(
        "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts),
        encoding="utf-8",
    )
    print(f"Application: {target / 'IRun.exe'}")
    print(f"Release: {release}")


if __name__ == "__main__":
    main()
