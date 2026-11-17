# SPDX-License-Identifier: MPL-2.0
import hashlib
import plistlib

import pytest

from ios17_helper import images


@pytest.mark.parametrize("variant", ["Personalized", "Cryptex"])
def test_remote_image_validates_every_file_before_caching(tmp_path, monkeypatch, variant):
    names = images.VARIANTS[variant]
    payloads = {name: name.encode("ascii") for name in names.values()}
    payloads[images.MANIFEST] = plistlib.dumps({
        "ProductBuildVersion": "27Z123",
        "BuildIdentities": [{"Manifest": {
            key: {"Info": {"Path": name}, "Digest": hashlib.sha384(payloads[name]).digest()}
            for key, name in names.items()
        }}],
    })
    monkeypatch.setattr(images, "read_payload", lambda _, name, ref: payloads[name])
    destination = images.prepare_remote_image(tmp_path, variant)
    assert images.validate_image(destination, variant) == "27Z123"
    assert images.prepare_remote_image(tmp_path, variant) == destination

    payloads["Image.dmg"] = b"wrong image"
    with pytest.raises(ValueError, match="与构建清单不匹配"):
        images.prepare_remote_image(tmp_path, variant)
    assert list(tmp_path.iterdir()) == [destination]
    assert images.validate_image(destination, variant) == "27Z123"
