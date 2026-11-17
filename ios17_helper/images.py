# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated developer images for the bundled and online installation paths."""
import hashlib
import json
from pathlib import Path
import plistlib
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen

RAW = "https://raw.githubusercontent.com/doronz88/DeveloperDiskImage"
VARIANTS = {
    "Personalized": {
        "PersonalizedDMG": "Image.dmg",
        "LoadableTrustCache": "Image.dmg.trustcache",
    },
    "Cryptex": {
        "Cryptex1,GenericDmg": "Image.dmg",
        "Cryptex1,GenericTrustCache": "Image.dmg.trustcache",
        "Cryptex1,CryptexInfoPlist": "Image.dmg.cryptex_info",
        "Cryptex1,GenericVolume": "Image.dmg.root_hash",
    },
}
MANIFEST = "BuildManifest.plist"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "ddi"


def read_payload(variant, name, ref="main"):
    url = f"{RAW}/{ref}/PersonalizedImages/Xcode_iOS_DDI_{variant}/{name}"
    limit = 64 * 1024 * 1024
    with urlopen(url, timeout=20) as response:
        data = response.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError(f"镜像文件大小异常：{name}")
    return data


def validate_image(directory, variant):
    manifest = plistlib.loads((directory / MANIFEST).read_bytes())
    build = manifest["ProductBuildVersion"]
    if not isinstance(build, str) or not build:
        raise ValueError("镜像构建号无效")
    assets = VARIANTS[variant]
    hashes = {key: hashlib.sha384((directory / name).read_bytes()).digest()
              for key, name in assets.items()}
    for identity in manifest["BuildIdentities"]:
        components = identity["Manifest"]
        if all(components.get(key, {}).get("Digest") == hashes[key]
               and components[key].get("Info", {}).get("Path") == name
               for key, name in assets.items()):
            return build
    raise ValueError(f"{variant} 镜像文件与构建清单不匹配")


def download_image(directory, variant, ref="main"):
    directory.mkdir(parents=True, exist_ok=True)
    for name in (MANIFEST, *VARIANTS[variant].values()):
        (directory / name).write_bytes(read_payload(variant, name, ref))
    return validate_image(directory, variant)


def prepare_remote_image(cache, variant):
    """Publish only complete, verified downloads. Concurrent readers see complete directories."""
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=cache, prefix="download-") as staging:
        source = Path(staging) / "image"
        download_image(source, variant)
        key = hashlib.sha256((source / MANIFEST).read_bytes()).hexdigest()
        destination = cache / key
        try:
            source.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            try:
                validate_image(destination, variant)
            except (OSError, ValueError, KeyError):
                # Use a fresh directory if an earlier cache entry was damaged.
                destination = cache / (key + "-" + Path(staging).name)
                source.rename(destination)
        return destination


def check_bundled_images():
    catalog = json.loads((ROOT / "bundle.json").read_text(encoding="utf-8"))
    for variant, assets in VARIANTS.items():
        entry = catalog["images"][variant]
        directory = ROOT / variant
        if validate_image(directory, variant) != entry["build"]:
            raise ValueError(f"预置 {variant} 镜像构建号不符")
        for name in (MANIFEST, *assets.values()):
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != entry["sha256"][name]:
                raise ValueError(f"预置 {variant} 镜像校验失败：{name}")
    return catalog
