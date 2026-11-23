# SPDX-License-Identifier: Apache-2.0
"""Pure parts of the Blender-side asset scan: PNG encoding, pixel order, names."""

import array
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest
from cycles_blender import blend_asset_scan as scan
from support.paths import BLENDER

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def decode_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, idat, header, kinds = 8, b"", None, []
    while position < len(data):
        length, kind = struct.unpack("!I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack("!I", data[position + 8 + length : position + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        kinds.append(kind)
        if kind == b"IHDR":
            header = struct.unpack("!IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    width, height, depth, color, *_ = header
    assert (depth, color) == (8, 6)
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    assert len(raw) == stride * height
    return width, height, b"".join(raw[row * stride + 1 : (row + 1) * stride] for row in range(height))


def test_png_round_trip_preserves_rgba_rows():
    width, height = 3, 2
    rgba = bytes(range(width * height * 4))
    assert decode_png(scan.encode_png(width, height, rgba)) == (width, height, rgba)


@pytest.mark.parametrize("size", [(0, 1), (1, 0), (-1, 2), (1.0, 1)])
def test_png_rejects_invalid_sizes(size):
    with pytest.raises(ValueError):
        scan.encode_png(size[0], size[1], b"")


def test_png_rejects_mismatched_buffer():
    with pytest.raises(ValueError):
        scan.encode_png(2, 2, bytes(15))


def test_flip_rows_turns_blender_bottom_up_into_png_top_down():
    bottom, top = bytes([1, 2, 3, 4] * 2), bytes([5, 6, 7, 8] * 2)
    assert scan.flip_rows(bottom + top, 2, 2) == top + bottom


def test_packed_preview_pixels_decode_to_rgba_byte_order():
    def pack(r, g, b, a):
        value = r | g << 8 | b << 16 | a << 24
        return value - (1 << 32) if value >= 1 << 31 else value

    values = [pack(10, 20, 30, 255), pack(200, 0, 1, 128)]
    assert scan.packed_pixels_to_rgba(values) == bytes([10, 20, 30, 255, 200, 0, 1, 128])
    assert scan.packed_pixels_to_rgba(array.array("i", values)) == bytes([10, 20, 30, 255, 200, 0, 1, 128])


def test_thumbnail_names_are_deterministic_and_input_sensitive(tmp_path):
    source = tmp_path / "lib" / "a.blend"
    base = scan.thumbnail_name(source, "MATERIAL", "Metal", [1, 2, 3, 4])
    assert base == scan.thumbnail_name(str(source), "MATERIAL", "Metal", [1, 2, 3, 4])
    assert len(base) == 44 and base.endswith(".png") and all(c in "0123456789abcdef" for c in base[:40])
    variants = {
        scan.thumbnail_name(tmp_path / "lib" / "b.blend", "MATERIAL", "Metal", [1, 2, 3, 4]),
        scan.thumbnail_name(source, "OBJECT", "Metal", [1, 2, 3, 4]),
        scan.thumbnail_name(source, "MATERIAL", "Metal.001", [1, 2, 3, 4]),
        scan.thumbnail_name(source, "MATERIAL", "Metal", [1, 2, 3, 5]),
    }
    assert base not in variants and len(variants) == 4


def test_signature_matches_worker_stat_tuple(tmp_path):
    from cycles_houdini.blender import blend_worker

    path = tmp_path / "x.blend"
    path.write_bytes(b"BLENDER")
    assert tuple(scan.signature_of(path)) == blend_worker._signature(path)
    assert scan.signature_of(tmp_path / "missing.blend") is None


def test_module_imports_without_blender():
    code = "import sys; import cycles_blender.blend_asset_scan as s; assert 'bpy' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30, cwd=str(BLENDER))


def test_preferences_script_prints_usage_without_marker():
    # hython cannot start isolated (-I drops the environment it needs to find its standard
    # library); the Python it bundles can, as Blender's own Python does.
    python = Path(sys.base_prefix) / "python.exe"
    if Path(sys.executable).stem.lower() != "hython" or not python.is_file():
        python = Path(sys.executable)
    result = subprocess.run(
        [str(python), "-I", str(BLENDER / "asset_prefs_main.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_missing_image_dependency_does_not_block_reference_bounds():
    """A .blend may reference an image from its author's machine; bounds must still be recorded."""
    from cycles_houdini.blender import blend_bounds

    manifest = dict(
        source="D:/Lib/a.blend",
        selector_type="COLLECTION",
        selector="a",
        frame=1.0,
        dependency_versions={
            "D:/Lib/a.blend": [1, 2, 3, 4],
            "D:/Somebody Else/HDRIs/sky_4k.exr": None,
            "D:/Lib/missing_library.blend": None,
        },
        meshes=[],
        instances=[],
        materials=[],
    )
    record = blend_bounds.evaluated_record(manifest, {})
    assert "D:/Somebody Else/HDRIs/sky_4k.exr" not in record["dependency_versions"]
    # A missing non-image dependency (a linked library) is kept so it still invalidates.
    assert record["dependency_versions"]["D:/Lib/missing_library.blend"] is None
