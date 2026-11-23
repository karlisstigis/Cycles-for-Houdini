# SPDX-License-Identifier: Apache-2.0
"""Library material import with textures in place (blend-import.md: Textures in place): packed images to <blend>/tex.

place_packed_textures shares one file per packed content between materials, gives a
different image of the same name _2, records in-place images untouched in cycles_import.json;
prepare_library_material exports with textures="in_place" into the material's own blend
folder; build_library_material writes $HIP-relative paths for extracted images, keeps
in-place paths absolute and passes material.missing_texture to the caller.
"""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cycles_houdini.blender import blend_assets_layout as layout
from cycles_houdini.blender import blend_library_materials as library
from cycles_houdini.blender import blend_materials, blend_worker
from cycles_houdini.core import blender_bridge

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def packed(staging, name, data, staged=None):
    """A textures="in_place" packed image record whose file is staged at textures/<staged>."""
    staged = staged or name
    _file(Path(staging) / "textures" / staged, data)
    dependency = dict(filename="textures/" + staged, source_path="/gone/" + name, sha256=_sha(data), packed=True)
    return dict(
        filename="textures/" + staged,
        source_path="/gone/" + name,
        tiles=[],
        textures="in_place",
        packed=True,
        missing=False,
        dependencies=[dependency],
    )


def in_place(path, missing=False):
    path = Path(path).as_posix()
    dependency = dict(filename=path, source_path=path, packed=False, missing=missing)
    return dict(filename=path, tiles=[], textures="in_place", packed=False, missing=missing, dependencies=[dependency])


def material(name, *images):
    return {
        "key": "blender-material-" + _sha(name.encode())[:24],
        "name": name,
        "nodes": [{"id": f"n{i}", "name": f"Image {i}", "image": image} for i, image in enumerate(images)],
        "outputs": {},
    }


def stored(blend_dir):
    return json.loads(layout.json_path(blend_dir).read_text())


# ---------------------------------------------------------------- place_packed_textures


def test_one_file_per_packed_content_shared_by_materials(tmp_path):
    blend_dir = tmp_path / "assets_blender" / "props"
    staging = tmp_path / "staging"
    red = material("Red", packed(staging, "wood.png", b"wood"))
    blue = material("Blue", packed(staging, "wood.png", b"wood"))
    tex = library.place_packed_textures(staging, red, blend_dir, source="/library/props.blend")
    library.place_packed_textures(staging, blue, blend_dir, source="/library/props.blend")
    assert sorted(p.name for p in tex.iterdir()) == ["wood.png"]
    for record in (red, blue):
        image = record["nodes"][0]["image"]
        assert image["filename"] == "wood.png" and image["packed"] is True
        assert image["dependencies"][0]["filename"] == "wood.png"
    data = stored(blend_dir)
    assert data["source"]["path"] == "/library/props.blend"
    assert data["images"] == {
        "packed:" + _sha(b"wood"): {
            "original": "/gone/wood.png",
            "packed": True,
            "file": "wood.png",
            "sha256": _sha(b"wood"),
        }
    }


def test_same_name_other_content_gets_a_suffix(tmp_path):
    blend_dir = tmp_path / "props"
    first = material("A", packed(tmp_path / "s1", "wood.png", b"one"))
    second = material("B", packed(tmp_path / "s2", "wood.png", b"two"))
    tex = library.place_packed_textures(tmp_path / "s1", first, blend_dir)
    library.place_packed_textures(tmp_path / "s2", second, blend_dir)
    assert second["nodes"][0]["image"]["filename"] == "wood_2.png"
    assert (tex / "wood.png").read_bytes() == b"one" and (tex / "wood_2.png").read_bytes() == b"two"
    # The same content again lands on its recorded name.
    again = material("C", packed(tmp_path / "s3", "wood.png", b"two"))
    library.place_packed_textures(tmp_path / "s3", again, blend_dir)
    assert again["nodes"][0]["image"]["filename"] == "wood_2.png"
    assert sorted(p.name for p in tex.iterdir()) == ["wood.png", "wood_2.png"]


def test_an_identical_file_already_in_tex_is_reused(tmp_path):
    blend_dir = tmp_path / "props"
    _file(blend_dir / "tex" / "wood.png", b"wood")  # e.g. a collected copy, not recorded as packed
    record = material("A", packed(tmp_path / "s", "wood.png", b"wood"))
    library.place_packed_textures(tmp_path / "s", record, blend_dir)
    assert record["nodes"][0]["image"]["filename"] == "wood.png"
    assert sorted(p.name for p in (blend_dir / "tex").iterdir()) == ["wood.png"]


def test_in_place_images_are_untouched_and_recorded(tmp_path):
    blend_dir = tmp_path / "props"
    bark = _file(tmp_path / "library" / "tex" / "bark.jpg", b"bark")
    gone = tmp_path / "library" / "tex" / "gone.png"
    record = material("A", in_place(bark), in_place(gone, missing=True))
    before = json.dumps(record, sort_keys=True)
    tex = library.place_packed_textures(tmp_path / "staging", record, blend_dir, source="/library/props.blend")
    assert json.dumps(record, sort_keys=True) == before and not tex.exists()
    images = stored(blend_dir)["images"]
    assert images == {
        bark.as_posix(): {"original": bark.as_posix(), "packed": False},
        gone.as_posix(): {"original": gone.as_posix(), "packed": False},
    }
    # A record without images opens no JSON at all.
    assert library.place_packed_textures(tmp_path, material("B"), tmp_path / "none") == tmp_path / "none" / "tex"
    assert not (tmp_path / "none").exists()


def test_copy_mode_records_path_limit_and_foreign_json_are_refused(tmp_path, monkeypatch):
    blend_dir = tmp_path / "props"
    copy_mode = material("A", dict(packed(tmp_path / "s", "wood.png", b"wood")))
    del copy_mode["nodes"][0]["image"]["packed"]
    with pytest.raises(ValueError, match="in_place"):
        library.place_packed_textures(tmp_path / "s", copy_mode, blend_dir)
    monkeypatch.setattr(layout, "MAX_PATH", len(str(blend_dir / "tex")) + 3)
    with pytest.raises(layout.PathTooLongError):
        library.place_packed_textures(
            tmp_path / "s", material("A", packed(tmp_path / "s", "wood.png", b"w")), blend_dir
        )
    assert not (blend_dir / "tex").exists()
    monkeypatch.undo()
    _file(layout.json_path(blend_dir), json.dumps({"schema": 99}).encode())
    with pytest.raises(layout.ForeignStoreError):
        library.place_packed_textures(
            tmp_path / "s", material("A", packed(tmp_path / "s", "wood.png", b"w")), blend_dir
        )
    assert json.loads(layout.json_path(blend_dir).read_text()) == {"schema": 99}


# ---------------------------------------------------------------- prepare / build (fake hou)


class _Parm:
    def __init__(self, value):
        self.value = value

    def unexpandedString(self):
        return self.value

    def set(self, value):
        self.value = value


class _Node:
    def __init__(self, path, type_name="cycles::image_texture::1", parms=None):
        self._path, self._type, self.parms, self.children = path, type_name, parms or {}, []
        self.user_data, self.destroyed = {}, False

    def path(self):
        return self._path

    def type(self):
        return SimpleNamespace(name=lambda: self._type)

    def parm(self, name):
        return self.parms.get(name)

    def allSubChildren(self):
        return list(self.children)

    def setUserData(self, key, value):
        self.user_data[key] = value

    def userData(self, key):
        return self.user_data.get(key)

    def setName(self, name, unique_name=False):
        self._path = "/mat/" + name

    def node(self, name):
        return None

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def host(monkeypatch, tmp_path):
    hip = tmp_path / "project"
    _file(hip / "shot.hip", b"hip")
    library_dir = tmp_path / "library"
    source = _file(library_dir / "metal.blend", b"BLENDER")
    bark = _file(library_dir / "tex" / "bark.jpg", b"bark")
    state = SimpleNamespace(hip=hip, source=source, bark=bark, nodes={}, calls=[], built=[])
    mat = _Node("/mat", "matnet")
    state.nodes["/mat"] = mat
    hou = SimpleNamespace(
        text=SimpleNamespace(expandString=lambda value: value.replace("$HIP", hip.as_posix())),
        node=lambda path: state.nodes.get(path),
        hipFile=SimpleNamespace(isNewFile=lambda: False, path=lambda: (hip / "shot.hip").as_posix()),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setattr(blender_bridge, "require_executable", lambda explicit=None: Path("/opt/blender"))

    def export(source, names, output_dir, executable, timeout=300, cancelled=None, textures="copy", **options):
        state.calls.append(textures)
        record = material(
            names[0],
            packed(output_dir, "metal_diff.png", b"diffuse"),
            in_place(bark),
            in_place(library_dir / "tex" / "gone.png", missing=True),
        )
        return {"records": [record], "errors": {}}

    def import_materials(records, root, parent=None, diagnostics=None):
        (record,) = records
        state.built.append((record, Path(root)))
        path = "/mat/" + record["name"]
        host_node = _Node(path, "cycles::material_builder::1")
        for node in record["nodes"]:
            image = node["image"]
            value = image["filename"] if image["packed"] is False else str(Path(root) / image["filename"])
            host_node.children.append(_Node(path + "/" + node["id"], parms={"filename": _Parm(value)}))
            if image.get("missing") and diagnostics is not None:
                diagnostics.append(dict(code="material.missing_texture", path=image["filename"], message="gone"))
        state.nodes[path] = host_node
        return {record["key"]: path}

    monkeypatch.setattr(blend_worker, "export_library_materials", export)
    monkeypatch.setattr(blend_materials, "import_materials", import_materials)
    return state


def test_import_library_material_places_packed_images_and_keeps_others_in_place(host):
    diagnostics = []
    node = library.import_library_material(host.source, "Metal", diagnostics=diagnostics)
    assert host.calls == ["in_place"]
    record, root = host.built[0]
    blend_dir = host.hip / "assets_blender" / "metal"
    assert root == blend_dir / "tex" and (root / "metal_diff.png").read_bytes() == b"diffuse"
    packed_parm, bark_parm, gone_parm = (child.parms["filename"].value for child in node.children)
    assert packed_parm == "$HIP/assets_blender/metal/tex/metal_diff.png"
    assert bark_parm == host.bark.as_posix()  # untouched: where the .blend points
    assert gone_parm.endswith("/library/tex/gone.png")
    assert [d["code"] for d in diagnostics] == ["material.missing_texture"]
    data = stored(blend_dir)
    assert data["source"]["path"] == host.source.resolve().as_posix()
    # In place under the .blend's own folder: located for Relink (blend-import.md: Import record, Collect and relink).
    assert data["images"][host.bark.as_posix()] == {
        "original": host.bark.as_posix(),
        "packed": False,
        "library_root": host.source.parent.as_posix(),
        "relative_path": "tex/bark.jpg",
    }
    assert not (host.hip / "tex").exists()


def test_in_place_record_keeps_a_collected_copy_and_prefers_the_registered_library(tmp_path):
    """A re-import or Reload re-records an image in place: the copy Collect Textures made
    (file, sha256) stays recorded, and a registered library root wins over the .blend's folder."""
    from cycles_houdini.blender import blend_assets_layout as layout

    original = (tmp_path / "lib" / "props" / "tex" / "wood.png").as_posix()
    blend_dir = tmp_path / "assets_blender" / "props"
    with layout.open_store(blend_dir) as store:
        store.record_image(original, original=original, packed=False, file="wood.png", sha256="a" * 64)
        store.save()
    record = {"nodes": [{"image": {"filename": original, "packed": False}}]}
    library.place_packed_textures(
        tmp_path, record, blend_dir, source=tmp_path / "lib" / "props" / "props.blend", library_root=tmp_path / "lib"
    )
    assert stored(blend_dir)["images"][original] == {
        "original": original,
        "packed": False,
        "file": "wood.png",
        "sha256": "a" * 64,
        "library_root": (tmp_path / "lib").as_posix(),
        "relative_path": "props/tex/wood.png",
    }
    elsewhere = (tmp_path / "other" / "wood.png").as_posix()
    record = {"nodes": [{"image": {"filename": elsewhere, "packed": False}}]}
    library.place_packed_textures(tmp_path, record, blend_dir, source=tmp_path / "lib" / "props" / "props.blend")
    assert stored(blend_dir)["images"][elsewhere] == {"original": elsewhere, "packed": False}


def test_prepare_returns_the_tex_folder_as_the_builder_root(host):
    root = host.hip / "assets_blender"
    prepared = library.prepare_library_material(host.source, "Metal", root=root, executable="/opt/blender")
    assert prepared["root"] == str(root / "metal" / "tex") and prepared["blend_folder"] == str(root / "metal")
    assert (
        prepared["asset_root"] == str(root) and prepared["record"]["nodes"][0]["image"]["filename"] == "metal_diff.png"
    )
