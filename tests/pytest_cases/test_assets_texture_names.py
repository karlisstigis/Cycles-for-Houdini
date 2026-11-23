# SPDX-License-Identifier: Apache-2.0
"""Material textures keep their real file names: the exporter's staging names, and packed images
extracted to the blend folder's flat tex/ (place_packed_textures; blend-import.md: Folder layout, Textures in place)."""

import hashlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from cycles_houdini.blender import blend_library_materials as library_materials
from cycles_blender import blend_material_export as exporter

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

_Images = exporter._Images


class _Image:
    """The parts of bpy.types.Image that _Images.export reads."""

    _pointer = 1000

    def __init__(self, name, filepath="", *, packed=None, tiles=(), file_format="PNG", packed_tiles=()):
        _Image._pointer += 1
        self.pointer = _Image._pointer
        self.name = self.name_full = name
        self.filepath = filepath
        self.library = None
        self.source = "TILED" if tiles else "FILE"
        self.use_multiview = False
        self.tiles = [SimpleNamespace(number=number) for number in tiles]
        self.file_format = file_format
        self.packed_file = SimpleNamespace(data=packed) if packed is not None else None
        self.packed_files = [
            SimpleNamespace(filepath=path, tile_number=number, packed_file=SimpleNamespace(data=data))
            for number, path, data in packed_tiles
        ] or ([SimpleNamespace(filepath=filepath, tile_number=None, packed_file=self.packed_file)] if packed else [])
        self.colorspace_settings = SimpleNamespace(name="sRGB", is_data=False)
        self.alpha_mode = "STRAIGHT"

    def as_pointer(self):
        return self.pointer


@pytest.fixture
def blender(monkeypatch, tmp_path):
    """Fake bpy/OCIO; Blender-relative paths (//) resolve below tmp_path/blend."""
    base = tmp_path / "blend"
    bpy = ModuleType("bpy")
    bpy.path = SimpleNamespace(
        abspath=lambda path, library=None: str(base / path[2:]) if path.startswith("//") else path
    )
    ocio = ModuleType("PyOpenColorIO")
    ocio.GetCurrentConfig = lambda: SimpleNamespace(getColorSpace=lambda name: None)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "PyOpenColorIO", ocio)
    return base


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _record(name, images):
    return {
        "key": "blender-material-" + _sha(name.encode())[:24],
        "name": name,
        "nodes": [{"id": "n" + str(i), "image": image} for i, image in enumerate(images)],
        "outputs": {},
    }


def _builder_check(root, record):
    """What blend_materials._Builder.image verifies before it sets the filename parm."""
    root = Path(root).resolve()
    for node in record["nodes"]:
        image = node["image"]
        assert "\\" not in image["filename"] and ".." not in image["filename"].split("/")
        for dependency in image["dependencies"]:
            path = (root / dependency["filename"]).resolve()
            assert path.is_relative_to(root) and path.is_file()
            assert _sha(path.read_bytes()) == dependency["sha256"]


def test_export_uses_real_file_names(blender, tmp_path):
    _file(blender / "tex/aerial_beach_01_diff_1k.jpg", b"diffuse")
    images = _Images(tmp_path / "out")
    result = images.export(_Image("aerial_beach_01_diff_1k.jpg", "//tex/aerial_beach_01_diff_1k.jpg"))
    assert result["filename"] == "textures/aerial_beach_01_diff_1k.jpg"
    (dependency,) = result["dependencies"]
    assert dependency == {
        "filename": "textures/aerial_beach_01_diff_1k.jpg",
        "source_path": str((blender / "tex/aerial_beach_01_diff_1k.jpg").resolve()),
        "sha256": _sha(b"diffuse"),
        "packed": False,
    }
    assert (tmp_path / "out/textures/aerial_beach_01_diff_1k.jpg").read_bytes() == b"diffuse"
    assert images.dependencies == [dependency]
    # A path without an extension gets the format's; Windows separators are path separators too.
    plain = images.export(_Image("Height", "C:\\textures\\height", packed=b"height", file_format="OPEN_EXR"))
    assert plain["filename"] == "textures/height.exr"
    assert sorted(p.name for p in (tmp_path / "out/textures").iterdir()) == [
        "aerial_beach_01_diff_1k.jpg",
        "height.exr",
    ]


def test_packed_images_are_named_after_path_or_datablock(blender, tmp_path):
    images = _Images(tmp_path / "out")
    no_path = images.export(_Image("Packed color", "", packed=b"red"))
    assert no_path["filename"] == "textures/Packed color.png"
    assert no_path["dependencies"][0]["packed"] is True
    duplicate_name = images.export(_Image("Scan.001", "", packed=b"scan", file_format="JPEG"))
    assert duplicate_name["filename"] == "textures/Scan.001.jpg"
    with_extension = images.export(_Image("logo.png", "", packed=b"logo"))
    assert with_extension["filename"] == "textures/logo.png"
    # A packed image that still has a file path uses its file name (not the missing file).
    with_path = images.export(_Image("Wood", "//gone/wood_basecolor.tga", packed=b"wood", file_format="TARGA"))
    assert with_path["filename"] == "textures/wood_basecolor.tga"
    assert (tmp_path / "out/textures/wood_basecolor.tga").read_bytes() == b"wood"
    with pytest.raises(exporter.BlendMaterialExportError, match="file format"):
        images.export(_Image("Mystery", "", packed=b"?", file_format="FFMPEG"))


def test_name_collisions_in_one_export_are_deterministic(blender, tmp_path):
    _file(blender / "a/diffuse.png", b"first")
    _file(blender / "b/diffuse.png", b"second")
    _file(blender / "c/Diffuse.png", b"third")
    _file(blender / "d/diffuse.png", b"first")

    def run(output):
        images = _Images(output)
        return [
            images.export(_Image(name, path))["filename"]
            for name, path in (
                ("A", "//a/diffuse.png"),
                ("B", "//b/diffuse.png"),
                ("C", "//c/Diffuse.png"),
                ("D", "//d/diffuse.png"),
            )
        ]

    first = run(tmp_path / "one")
    assert first == [
        "textures/diffuse.png",
        "textures/diffuse_2.png",
        "textures/Diffuse_3.png",  # case-insensitive, so it can never clobber on Windows
        "textures/diffuse.png",  # identical bytes share one file
    ]
    assert run(tmp_path / "two") == first
    textures = tmp_path / "one/textures"
    assert (textures / "diffuse.png").read_bytes() == b"first"
    assert (textures / "diffuse_2.png").read_bytes() == b"second"
    assert (textures / "Diffuse_3.png").read_bytes() == b"third"
    assert not any(p.name.startswith(".pending-") for p in textures.iterdir())


def test_exports_sharing_a_staging_folder_never_overwrite(blender, tmp_path):
    # blend_asset_scan exports each requested material with its own _Images, one folder.
    _file(blender / "a/diffuse.png", b"first")
    _file(blender / "b/diffuse.png", b"second")
    first = _Images(tmp_path / "out").export(_Image("A", "//a/diffuse.png"))
    second = _Images(tmp_path / "out").export(_Image("B", "//b/diffuse.png"))
    assert (first["filename"], second["filename"]) == ("textures/diffuse.png", "textures/diffuse_2.png")
    assert (tmp_path / "out/textures/diffuse.png").read_bytes() == b"first"


def test_udim_patterns_keep_names_and_tile_numbers(blender, tmp_path):
    for number in (1001, 1002):
        _file(blender / f"scans_1024/wall_col.{number}.exr", b"tile" + str(number).encode())
        _file(blender / f"other/wall_col.{number}.exr", b"other" + str(number).encode())
    images = _Images(tmp_path / "out")
    token = images.export(_Image("Wall", "//scans_1024/wall_col.<UDIM>.exr", tiles=(1001, 1002)))
    assert token["filename"] == "textures/wall_col.<UDIM>.exr" and token["tiles"] == [1001, 1002]
    assert [d["filename"] for d in token["dependencies"]] == [
        "textures/wall_col.1001.exr",
        "textures/wall_col.1002.exr",
    ]
    assert token["dependencies"][1]["source_path"] == str((blender / "scans_1024/wall_col.1002.exr").resolve())
    # A path naming one tile: the folder's 1024 is not a tile number, the file's 1001 is.
    images = _Images(tmp_path / "out2")
    named = images.export(_Image("Wall", "//scans_1024/wall_col.1001.exr", tiles=(1001, 1002)))
    assert named["filename"] == "textures/wall_col.<UDIM>.exr"
    assert named["dependencies"][1]["source_path"] == str((blender / "scans_1024/wall_col.1002.exr").resolve())
    assert (tmp_path / "out2/textures/wall_col.1002.exr").read_bytes() == b"tile1002"
    # A second, different tiled image of the same name moves all its tiles together.
    other = images.export(_Image("Other", "//other/wall_col.<UDIM>.exr", tiles=(1001, 1002)))
    assert other["filename"] == "textures/wall_col_2.<UDIM>.exr"
    assert [d["filename"] for d in other["dependencies"]] == [
        "textures/wall_col_2.1001.exr",
        "textures/wall_col_2.1002.exr",
    ]
    # Packed tiles of a tiled image without a usable name.
    packed = images.export(
        _Image(
            "Baked",
            "",
            tiles=(1001,),
            packed_tiles=[(1001, "Baked.1001.png", b"baked")],
        )
    )
    assert packed["filename"] == "textures/Baked.<UDIM>.png"
    assert packed["dependencies"][0]["filename"] == "textures/Baked.1001.png"
    renamed = images.export(_Image("Baked.001", "", tiles=(1001,), packed_tiles=[(1001, "", b"again")]))
    assert renamed["filename"] == "textures/Baked.001.<UDIM>.png"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../evil.png", "evil.png"),
        ("C:\\Windows\\con.png", "_con.png"),
        ("drive:name.png", "drive_name.png"),
        ("$HIP/`ls`.png", "_ls_.png"),
        ("..", "texture"),
        ("COM1", "_COM1"),
        ("nul .tar.gz", "_nul .tar.gz"),
        ("tab\there?.jpg", "tab_here_.jpg"),
        (" .hidden.png. ", "hidden.png"),
        ("lpt\u00b9.png", "_lpt\u00b9.png"),
    ],
)
def test_safe_name_blocks_escapes_and_reserved_names(raw, expected):
    assert _Images.safe_name(raw) == expected


def test_safe_name_limits_length_and_keeps_udim_token():
    long = _Images.safe_name("x" * 300 + ".png")
    assert len(long) == _Images.MAX_NAME and long.endswith(".png")
    tiled = _Images.safe_name("y" * 300 + ".<UDIM>.exr", token=True)
    assert len(tiled) <= _Images.MAX_NAME and tiled.endswith("<UDIM>.exr")
    assert _Images.safe_name("a<UDIM>.exr") == "a_UDIM_.exr"
    assert _Images.tagged("wall_col.<UDIM>.exr", "ab12cd34") == "wall_col_ab12cd34.<UDIM>.exr"
    assert _Images.tagged("<UDIM>.exr", "2") == "_2<UDIM>.exr"
    assert _Images.tagged("diffuse.png", "2") == "diffuse_2.png"


def test_exported_names_never_escape_the_staging_folder(blender, tmp_path):
    images = _Images(tmp_path / "out")
    result = images.export(_Image("../../CON", "", packed=b"evil"))
    assert result["filename"] == "textures/_CON.png"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "out/textures/_CON.png"
    ]


def _packed(staging, files, *, tiles=()):
    """Export one material's packed images (textures="in_place") from `files` ({name: bytes})."""
    images = _Images(staging, textures="in_place")
    if tiles:
        name = next(iter(files)).split(".")[0]
        packed = [(number, f"{name}.{number}.exr", data) for number, data in zip(tiles, files.values(), strict=True)]
        return [images.export(_Image(name, "//" + name + ".<UDIM>.exr", tiles=tiles, packed_tiles=packed))]
    return [images.export(_Image(Path(path).name, "//" + path, packed=data)) for path, data in files.items()]


def _place(staging, record, blend_dir, source="/library/beach.blend"):
    return library_materials.place_packed_textures(staging, record, blend_dir, source=source)


def test_packed_textures_go_to_tex_with_real_names(blender, tmp_path):
    blend_dir = tmp_path / "assets_blender" / "beach"
    files = {"tex/aerial_beach_01_diff_1k.jpg": b"diffuse", "tex/aerial_beach_01_rough_1k.jpg": b"rough"}
    record = _record("Beach Sand", _packed(tmp_path / "staging", files))
    tex = _place(tmp_path / "staging", record, blend_dir)
    assert tex == blend_dir / "tex"
    assert [node["image"]["filename"] for node in record["nodes"]] == [
        "aerial_beach_01_diff_1k.jpg",
        "aerial_beach_01_rough_1k.jpg",
    ]
    assert sorted(p.name for p in tex.iterdir()) == ["aerial_beach_01_diff_1k.jpg", "aerial_beach_01_rough_1k.jpg"]
    _builder_check(tex, record)


def test_reload_of_unchanged_material_reuses_files(blender, tmp_path):
    blend_dir = tmp_path / "beach"
    files = {"tex/diffuse.png": b"diffuse"}
    first = _record("Beach Sand", _packed(tmp_path / "s1", files))
    tex = _place(tmp_path / "s1", first, blend_dir)
    before = (tex / "diffuse.png").stat().st_mtime_ns
    reload = _record("Beach Sand", _packed(tmp_path / "s2", files))
    _place(tmp_path / "s2", reload, blend_dir)
    for image in (first["nodes"][0]["image"], reload["nodes"][0]["image"]):
        assert image["filename"] == "diffuse.png"
        assert [(d["filename"], d["sha256"]) for d in image["dependencies"]] == [("diffuse.png", _sha(b"diffuse"))]
    assert [p.name for p in tex.iterdir()] == ["diffuse.png"]
    assert (tex / "diffuse.png").stat().st_mtime_ns == before


def test_changed_packed_texture_gets_a_suffix_and_never_overwrites(blender, tmp_path):
    # Earlier builds replaced the file in place and deleted unused ones (.cycles_textures.json);
    # tex/ is now shared by every material of the .blend, so a changed image gets _2 instead.
    blend_dir = tmp_path / "beach"
    old = _record("Beach Sand", _packed(tmp_path / "s1", {"tex/diffuse.png": b"old", "tex/rough.png": b"r"}))
    tex = _place(tmp_path / "s1", old, blend_dir)
    new = _record("Beach Sand", _packed(tmp_path / "s2", {"tex/diffuse.png": b"new"}))
    _place(tmp_path / "s2", new, blend_dir)
    assert new["nodes"][0]["image"]["filename"] == "diffuse_2.png"
    assert (tex / "diffuse.png").read_bytes() == b"old" and (tex / "diffuse_2.png").read_bytes() == b"new"
    assert sorted(p.name for p in tex.iterdir()) == ["diffuse.png", "diffuse_2.png", "rough.png"]
    _builder_check(tex, new)
    # A material of the same name from another .blend has its own blend folder.
    other = _record("Beach Sand", _packed(tmp_path / "s3", {"tex/diffuse.png": b"other"}))
    other_tex = _place(tmp_path / "s3", other, tmp_path / "beach__other", "/other/beach.blend")
    assert other["nodes"][0]["image"]["filename"] == "diffuse.png"
    assert (other_tex / "diffuse.png").read_bytes() == b"other" and (tex / "diffuse.png").read_bytes() == b"old"


def test_udim_tiles_move_together(blender, tmp_path):
    blend_dir = tmp_path / "wall"
    first = _record("Wall", _packed(tmp_path / "s1", {"wall_col.a": b"a1", "wall_col.b": b"a2"}, tiles=(1001, 1002)))
    tex = _place(tmp_path / "s1", first, blend_dir)
    image = first["nodes"][0]["image"]
    assert image["filename"] == "wall_col.<UDIM>.exr"
    assert [d["filename"] for d in image["dependencies"]] == ["wall_col.1001.exr", "wall_col.1002.exr"]
    same = _record("Wall", _packed(tmp_path / "s2", {"wall_col.a": b"a1", "wall_col.b": b"a2"}, tiles=(1001, 1002)))
    _place(tmp_path / "s2", same, blend_dir)
    assert same["nodes"][0]["image"]["filename"] == "wall_col.<UDIM>.exr"
    # One changed tile is a different image: all its tiles get the suffix together.
    second = _record("Wall", _packed(tmp_path / "s3", {"wall_col.a": b"a1", "wall_col.b": b"b2"}, tiles=(1001, 1002)))
    _place(tmp_path / "s3", second, blend_dir)
    assert second["nodes"][0]["image"]["filename"] == "wall_col_2.<UDIM>.exr"
    assert (tex / "wall_col_2.1002.exr").read_bytes() == b"b2" and (tex / "wall_col.1002.exr").read_bytes() == b"a2"
    _builder_check(tex, second)


def test_place_packed_textures_refuses_unsafe_or_inconsistent_names(tmp_path):
    staging, blend_dir = tmp_path / "staging", tmp_path / "blend"

    def record(pattern, files, tiles=()):
        dependencies = []
        for name in files:
            if os.name == "nt" and ":" in name:
                continue  # Windows cannot create "a:b.png" (a drive-relative path): the name alone is refused
            _file(staging / "textures" / name, b"x")
            dependencies.append({"filename": "textures/" + name, "sha256": _sha(b"x"), "packed": True})
        image = {"filename": "textures/" + pattern, "dependencies": dependencies, "tiles": list(tiles), "packed": True}
        return _record("M", [image])

    for pattern in ("con.png", "$HIP.png", "a:b.png"):
        with pytest.raises(ValueError, match="Unsafe"):
            _place(staging, record(pattern, [pattern]), blend_dir)
    with pytest.raises(ValueError, match="Unsafe"):
        _place(staging, record("wall.1001.exr", ["wall.1001.exr"], (1001,)), blend_dir)
    with pytest.raises(ValueError, match="does not match"):
        _place(staging, record("a.png", ["b.png"]), blend_dir)
    with pytest.raises(ValueError, match="tiles do not match"):
        _place(staging, record("w.<UDIM>.exr", ["w.1001.exr"], (1001, 1002)), blend_dir)
    broken = record("a.png", ["a.png"])
    broken["nodes"][0]["image"]["dependencies"][0]["sha256"] = "../../x"
    with pytest.raises(ValueError, match="sha256"):
        _place(staging, broken, blend_dir)
    escaping = record("a.png", ["a.png"])
    escaping["nodes"][0]["image"]["dependencies"][0]["filename"] = "../outside.png"
    _file(tmp_path / "outside.png", b"x")
    with pytest.raises(ValueError, match="escaping"):
        _place(staging, escaping, blend_dir)
    assert not (blend_dir / "tex").exists() and not (blend_dir / "cycles_import.json").exists()


def test_place_packed_textures_keeps_case_only_differences_apart(tmp_path):
    staging, blend_dir = tmp_path / "staging", tmp_path / "blend"
    _file(blend_dir / "tex/Diffuse.png", b"other")
    _file(staging / "textures/diffuse.png", b"mine")
    image = {
        "filename": "textures/diffuse.png",
        "dependencies": [{"filename": "textures/diffuse.png", "sha256": _sha(b"mine"), "packed": True}],
        "tiles": [],
        "packed": True,
    }
    _place(staging, _record("M", [image]), blend_dir)
    assert image["filename"] == "diffuse_2.png"  # case-insensitive, so it can never clobber on Windows
    assert (blend_dir / "tex/Diffuse.png").read_bytes() == b"other"
