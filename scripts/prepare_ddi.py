# SPDX-License-Identifier: GPL-3.0-or-later
"""Prepare the pinned iOS developer-image assets and check upstream before an installer build."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ios17_helper.images import MANIFEST, ROOT, VARIANTS, check_bundled_images, download_image

LOCK = ROOT.parent / "ddi-lock.json"
UPSTREAM = "https://github.com/doronz88/DeveloperDiskImage.git"


def upstream_ref():
    return subprocess.check_output(
        ["git", "ls-remote", UPSTREAM, "refs/heads/main"], text=True, timeout=30
    ).split()[0]


def snapshot(ref, directory):
    images = {}
    for variant, assets in VARIANTS.items():
        folder = directory / variant
        build = download_image(folder, variant, ref)
        images[variant] = {
            "build": build,
            "sha256": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                       for name in (MANIFEST, *assets.values())},
        }
    return {"ref": ref, "images": images}


def prepare(*, check_latest=False, update=False):
    locked = None if update else json.loads(LOCK.read_text(encoding="utf-8"))
    if not check_latest and not update:
        try:
            if check_bundled_images() == locked:
                return
        except (OSError, ValueError, KeyError):
            pass
    ref = upstream_ref() if update or check_latest else locked["ref"]
    with TemporaryDirectory(prefix="IRun-DDI-") as temp:
        staging = Path(temp)
        actual = snapshot(ref, staging)
        if locked and actual["images"] != locked["images"]:
            if check_latest:
                builds = ", ".join(f"{name} {entry['build']}" for name, entry in actual["images"].items())
                raise SystemExit(f"Upstream developer images changed ({builds}). "
                                 "Review and test the new images, then run python scripts/prepare_ddi.py --update.")
            raise SystemExit("Pinned developer images do not match ddi-lock.json.")
        if update:
            locked = actual
        shutil.copytree(staging, ROOT, dirs_exist_ok=True)
        (ROOT / "bundle.json").write_text(json.dumps(locked, indent=2) + "\n", encoding="utf-8")
        if update:
            LOCK.write_text(json.dumps(locked, indent=2) + "\n", encoding="utf-8")
    for variant, entry in check_bundled_images()["images"].items():
        print(f"{variant} developer image: {entry['build']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="compare the pinned files with upstream")
    group.add_argument("--update", action="store_true", help="pin the current upstream image files")
    options = parser.parse_args()
    prepare(check_latest=options.check, update=options.update)
