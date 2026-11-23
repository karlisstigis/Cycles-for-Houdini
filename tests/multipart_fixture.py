# SPDX-License-Identifier: Apache-2.0
"""Independent EXR part/header inspection shared by native and GUI checks."""

from pathlib import Path
import struct
import numpy as np
import OpenImageIO as oiio


def read_parts(path):
    path = Path(path)
    with path.open("rb") as stream:
        magic, flags = struct.unpack("<II", stream.read(8))
    assert magic == 20000630 and flags & 0x1000, (magic, flags)
    image = oiio.ImageInput.open(str(path))
    assert image, oiio.geterror()
    result = {}
    index = 0
    try:
        while image.seek_subimage(index, 0):
            spec = image.spec()
            name = spec.get_string_attribute("oiio:subimagename")
            assert name and name not in result, (index, name)
            result[name] = dict(
                channels=tuple(spec.channelnames),
                formats=tuple(map(str, spec.channelformats))
                if spec.channelformats
                else (str(spec.format),) * spec.nchannels,
                compression=spec.get_string_attribute("compression"),
                dwa=spec.get_float_attribute("openexr:dwaCompressionLevel"),
                metadata={p.name: p.value for p in spec.extra_attribs if p.name.startswith("cryptomatte/")},
                pixels=image.read_image(index, 0, 0, spec.nchannels, oiio.FLOAT),
                window=(spec.x, spec.y, spec.width, spec.height, spec.full_width, spec.full_height),
                pixel_aspect=spec.get_float_attribute("PixelAspectRatio"),
            )
            assert result[name]["pixels"] is not None, oiio.geterror()
            assert result[name]["pixels"].shape == (spec.height, spec.width, spec.nchannels)
            index += 1
    finally:
        image.close()
    return result


def verify_parts(path):
    parts = read_parts(path)
    assert tuple(parts) == ("Beauty", "AO", "CryptoZIP", "CryptoPIZ"), tuple(parts)
    for name, codec, precision in (
        ("Beauty", "dwab", "half"),
        ("AO", "dwaa", "half"),
        ("CryptoZIP", "zip", "float"),
        ("CryptoPIZ", "piz", "float"),
    ):
        part = parts[name]
        assert part["compression"] == codec, (name, part["compression"])
        assert set(part["formats"]) == {precision}, (name, part["formats"])
        if name.startswith("Crypto"):
            assert set(part["channels"]) == {name + f"{i:02}." + c for i in range(3) for c in "RGBA"}
            assert [v for k, v in part["metadata"].items() if k.endswith("/name")] == [name], part["metadata"]
            assert any(k.endswith("/manifest") for k in part["metadata"])
        else:
            assert not part["metadata"], part["metadata"]
    assert set(parts["Beauty"]["channels"]) == set("RGBA")
    assert set(parts["AO"]["channels"]) == {"AO.R", "AO.G", "AO.B"}
    for i in range(3):
        a = parts["CryptoZIP"]
        b = parts["CryptoPIZ"]
        ai = [a["channels"].index(f"CryptoZIP{i:02}." + c) for c in "RGBA"]
        bi = [b["channels"].index(f"CryptoPIZ{i:02}." + c) for c in "RGBA"]
        assert np.array_equal(a["pixels"][:, :, ai].view(np.uint32), b["pixels"][:, :, bi].view(np.uint32))
    return parts
