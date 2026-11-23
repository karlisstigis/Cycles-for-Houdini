# SPDX-License-Identifier: Apache-2.0
"""Host-side asset worker functions with a fake worker connection, and library material import."""

import json
import os
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from cycles_blender import blend_asset_scan as scan
from cycles_blender import worker_server
from cycles_houdini.blender import blend_library_materials as library_materials
from cycles_houdini.blender import blend_materials, blend_worker
from cycles_houdini.core import blender_bridge

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class _Endpoint:
    def __init__(self):
        self.frames = deque()

    def send_bytes(self, data):
        self.frames.append(bytes(data))

    def recv_bytes(self, limit):
        data = self.frames.popleft()
        assert len(data) <= limit
        return data


class _HostConnection:
    """Host end of a fake pipe; each request runs the real worker-side asset handler."""

    def __init__(self, log):
        self.inbox = _Endpoint()
        self.log = log

    def send_bytes(self, data):
        command = json.loads(bytes(data))
        self.log.append(command)
        worker_server._reply_asset_command(self.inbox, command)

    def recv_bytes(self, limit):
        return self.inbox.recv_bytes(limit)


@pytest.fixture
def fake_worker(monkeypatch):
    state = SimpleNamespace(commands=[], launched=[], closed=0, fail=None)

    class Worker:
        def __init__(self, source, executable, source_config, timeout, cancelled=None):
            state.launched.append((source, executable, source_config, timeout))
            self.connection = _HostConnection(state.commands)
            self.logs = deque(["blender log line"], maxlen=8)

        def _bounded(self, operation, timeout, cancelled=None):
            if state.fail is not None:
                raise state.fail
            return operation()

        def close(self):
            state.closed += 1

    monkeypatch.setattr(blend_worker, "_Worker", Worker)
    return state


def test_assets_many_bounds_never_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(blend_worker, "_Worker", lambda *a, **k: pytest.fail("launched Blender"))
    assert blend_worker.assets_many([], "blender", tmp_path) == []
    with pytest.raises(ValueError, match="bounds"):
        blend_worker.assets_many([tmp_path / f"{i}.blend" for i in range(65)], "blender", tmp_path)
    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="bounds"):
            blend_worker.assets_many([tmp_path / "a.blend"], "blender", tmp_path, timeout=timeout)


def test_assets_many_scans_in_order_and_sanitises(fake_worker, monkeypatch, tmp_path):
    good, missing, changing = tmp_path / "good.blend", tmp_path / "missing.blend", tmp_path / "changing.blend"
    good.write_bytes(b"one")
    changing.write_bytes(b"two")
    thumbs = tmp_path / "thumbs"

    def scan_file(source, thumbnail_dir, signature=None):
        if source == str(changing):
            changing.write_bytes(b"changed during the read")
            os.utime(changing, ns=(1, 1))
        preview = scan.thumbnail_name(source, "MATERIAL", "Metal", signature)
        (Path(thumbnail_dir) / preview).write_bytes(b"png")
        meta = {
            "catalog_id": "",
            "tags": ["metal", 3],
            "author": "a",
            "description": "d",
            "license": "l",
            "copyright": "c",
        }
        return {
            "source": source,
            "signature": signature,
            "assets": [
                {
                    "name": "Metal",
                    "id_type": "MATERIAL",
                    "meta": meta,
                    "catalog_simple_name": "",
                    "object_type": None,
                    "preview": preview,
                    "preview_size": [4, 2],
                },
                {
                    "name": "Escape",
                    "id_type": "OBJECT",
                    "meta": dict(meta),
                    "catalog_simple_name": "x",
                    "object_type": "MESH",
                    "preview": "../outside.png",
                    "preview_size": [4, 2],
                },
                {"name": "", "id_type": "OBJECT", "meta": {}},
                "junk",
            ],
            "warnings": ["one warning"],
        }

    monkeypatch.setattr(scan, "scan_file", scan_file)
    result = blend_worker.assets_many([good, missing, good, changing], Path("blender.exe"), thumbs, timeout=5)
    assert [entry["source"] for entry in result] == [str(good), str(missing), str(changing)]
    assert fake_worker.closed == 1 and len(fake_worker.launched) == 1
    command = fake_worker.commands[0]
    assert command["command"] == "assets_many" and command["thumbnail_dir"] == str(thumbs.resolve())
    first = result[0]
    assert first["signature"] == list(blend_worker._signature(good))
    assert first["warnings"] == ["one warning"]
    metal, escape = first["assets"]
    assert metal["preview"] and metal["preview_size"] == [4, 2] and metal["meta"]["tags"] == ["metal", "3"]
    assert escape["preview"] is None and escape["preview_size"] is None and escape["object_type"] == "MESH"
    assert "not found" in result[1]["error"]
    assert "changed" in result[2]["error"]


def test_assets_many_reports_command_failure_and_closes(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "a.blend"
    source.write_bytes(b"x")
    monkeypatch.setattr(blend_worker, "_MAX_ASSET_SOURCES", 64)

    def broken(command):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(worker_server, "_asset_command", broken)
    with pytest.raises(RuntimeError, match="scanner exploded"):
        blend_worker.assets_many([source], "blender", tmp_path / "t")
    assert fake_worker.closed == 1
    fake_worker.fail = TimeoutError("Blender evaluation exceeded 1.0 seconds")
    with pytest.raises(TimeoutError, match="Blender diagnostics:\nblender log line"):
        blend_worker.assets_many([source], "blender", tmp_path / "t")
    assert fake_worker.closed == 2


def test_worker_classifies_transient_and_invalid_source_at_origin(fake_worker, monkeypatch, tmp_path):
    temporary, corrupt = tmp_path / "temporary.blend", tmp_path / "corrupt.blend"
    temporary.write_bytes(b"BLENDER-v300")
    corrupt.write_bytes(b"definitely not Blender")

    def scan_file(source, *args, **kwargs):
        if source == str(temporary):
            raise PermissionError("sharing violation")
        scan.validate_blend_header(source)

    monkeypatch.setattr(scan, "scan_file", scan_file)
    result = blend_worker.assets_many([temporary, corrupt], "blender", tmp_path / "thumbs")
    assert result[0]["error_kind"] == "transient" and "sharing violation" in result[0]["error"]
    assert result[1]["error_kind"] == "invalid_file" and "Not a Blender file" in result[1]["error"]


@pytest.mark.parametrize("header", [b"BLENDER-v300", b"\x1f\x8bcompressed", b"\x28\xb5\x2f\xfdcompressed"])
def test_asset_header_accepts_blender_encodings(tmp_path, header):
    source = tmp_path / "a.blend"
    source.write_bytes(header)
    scan.validate_blend_header(source)


@pytest.mark.parametrize("previous_success", [False, True])
def test_real_asset_protocol_failure_repairs_provider_without_force(
    fake_worker, monkeypatch, tmp_path, previous_success
):
    from cycles_houdini.assets.cache import IndexCache
    from cycles_houdini.assets.providers import BlenderWorkerProvider

    root = tmp_path / "library"
    root.mkdir()
    source = root / "asset.blend"
    source.write_bytes(b"BLENDER-v300")
    executable = tmp_path / "blender.exe"
    executable.write_bytes(b"")
    now = [10.0]
    broken = [False]

    def scan_file(source, *args, **kwargs):
        if broken[0]:
            raise PermissionError("temporarily locked by writer")
        return {"assets": [{"name": "Sphere", "id_type": "OBJECT", "meta": {}}]}

    monkeypatch.setattr(scan, "scan_file", scan_file)
    provider = BlenderWorkerProvider(
        str(root),
        IndexCache(str(root), root=tmp_path / "cache"),
        executable=executable,
        assets_many=blend_worker.assets_many,
        clock=lambda: now[0],
    )
    before = provider.scan().records if previous_success else []
    if previous_success:
        source.write_bytes(b"BLENDER-v300 edited")
    broken[0] = True
    assert provider.scan().records == before
    assert provider.cache.entry(source)["error_kind"] == "transient"
    broken[0] = False
    now[0] += 5
    repaired = provider.scan()
    assert repaired.scanned == 1 and not repaired.errors and repaired.records[0]["name"] == "Sphere"


def test_malformed_asset_reply_is_rejected(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "a.blend"
    source.write_bytes(b"x")
    monkeypatch.setattr(worker_server, "_asset_command", lambda command: [{"source": "elsewhere", "assets": []}])
    with pytest.raises(ValueError, match="Malformed"):
        blend_worker.assets_many([source], "blender", tmp_path / "t")


def test_reply_keeps_stream_framed_for_unknown_and_oversized_replies(monkeypatch):
    endpoint = _Endpoint()
    worker_server._reply_asset_command(endpoint, {"command": "nonsense"})
    assert json.loads(endpoint.frames.pop())["ok"] is False
    monkeypatch.setattr(worker_server, "_asset_command", lambda command: {"ok": True, "records": ["x" * 4096]})
    monkeypatch.setattr(blend_worker, "_MAX_HEADER", 1024)
    worker_server._reply_asset_command(endpoint, {"command": "export_library_materials"})
    reply = json.loads(endpoint.frames.pop())
    assert reply["ok"] is False and "limit" in reply["error"]


def test_export_bounds_never_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(blend_worker, "_Worker", lambda *a, **k: pytest.fail("launched Blender"))
    source = tmp_path / "a.blend"
    source.write_bytes(b"x")
    for names in ([], [f"m{i}" for i in range(65)], [""], ["a\0b"], [3], ["x" * 1025]):
        with pytest.raises(ValueError):
            blend_worker.export_library_materials(source, names, tmp_path / "out", "blender")
    with pytest.raises(ValueError, match="existing .blend"):
        blend_worker.export_library_materials(tmp_path / "missing.blend", ["m"], tmp_path / "out", "blender")
    other = tmp_path / "a.txt"
    other.write_bytes(b"x")
    with pytest.raises(ValueError, match="existing .blend"):
        blend_worker.export_library_materials(other, ["m"], tmp_path / "out", "blender")
    with pytest.raises(ValueError, match="bounds"):
        blend_worker.export_library_materials(source, ["m"], tmp_path / "out", "blender", timeout=0)


def test_export_isolates_per_material_errors(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "lib.blend"
    source.write_bytes(b"x")
    calls = []

    def export_named_materials(path, names, output_dir):
        calls.append((path, names, output_dir))
        record = {"key": "blender-material-1", "name": "Good", "nodes": [], "outputs": {}}
        return {"records": [record], "errors": {"Bad": "Material output is unconnected: Bad"}}

    monkeypatch.setattr(scan, "export_named_materials", export_named_materials)
    result = blend_worker.export_library_materials(source, ["Good", "Bad", "Lost", "Good"], tmp_path / "out", "blender")
    assert calls == [(str(source), ["Good", "Bad", "Lost"], str((tmp_path / "out").resolve()))]
    assert [r["name"] for r in result["records"]] == ["Good"]
    assert result["errors"]["Bad"] == "Material output is unconnected: Bad"
    assert "no material record" in result["errors"]["Lost"]
    assert fake_worker.closed == 1


def test_export_rejects_foreign_records_and_reports_worker_errors(fake_worker, monkeypatch, tmp_path):
    source = tmp_path / "lib.blend"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        scan,
        "export_named_materials",
        lambda *a: {"records": [{"key": "k", "name": "Other", "nodes": [], "outputs": {}}], "errors": {}},
    )
    with pytest.raises(ValueError, match="Malformed"):
        blend_worker.export_library_materials(source, ["Good"], tmp_path / "out", "blender")

    def stale(*args):
        source.write_bytes(b"changed")
        os.utime(source, ns=(5, 5))
        return {"records": [], "errors": {}}

    monkeypatch.setattr(scan, "export_named_materials", stale)
    with pytest.raises(RuntimeError, match="changed during material export"):
        blend_worker.export_library_materials(source, ["Good"], tmp_path / "out", "blender")


def _completed(stdout, returncode=0, stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_preference_dump_runs_user_startup_and_parses_marker(monkeypatch):
    seen = {}
    monkeypatch.setenv("PYTHONPATH", "/leak")

    def run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        marker = kwargs["env"]["CH_ASSET_PREFS_MARKER"]
        payload = {
            "ok": True,
            "libraries": [
                {"name": "Studio", "path": "D:/Assets", "import_method": "LINK", "enabled": False, "remote": False},
                {"name": "", "path": "E:/Unnamed"},
                {"name": "No path", "path": ""},
                "junk",
            ],
        }
        noise = "Blender 5.3 banner\nRead prefs: userpref.blend\n" + marker[:-3] + " partial\n"
        return _completed((noise + marker + json.dumps(payload) + "\nBlender quit\n").encode())

    monkeypatch.setattr(blend_worker.subprocess, "run", run)
    result = blend_worker.dump_asset_library_preferences("C:/Blender/blender.exe", timeout=10)
    assert result == [
        {"name": "Studio", "path": "D:/Assets", "import_method": "LINK", "enabled": False, "remote": False},
        {"name": "Unnamed", "path": "E:/Unnamed", "import_method": "APPEND", "enabled": True, "remote": False},
    ]
    command = seen["command"]
    assert "--factory-startup" not in command
    assert {"--background", "--disable-autoexec"} <= set(command)
    script = Path(command[command.index("--python") + 1])
    assert script.name == "asset_prefs_main.py" and script.is_file()
    assert seen["kwargs"]["timeout"] == 10 and "PYTHONPATH" not in seen["kwargs"]["env"]


def test_preference_dump_failures(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(blend_worker.subprocess, "run", timeout)
    with pytest.raises(TimeoutError):
        blend_worker.dump_asset_library_preferences("blender", timeout=1)
    monkeypatch.setattr(blend_worker.subprocess, "run", lambda c, **k: _completed(b"no marker", 1, b"crash"))
    with pytest.raises(RuntimeError, match="crash"):
        blend_worker.dump_asset_library_preferences("blender")

    def failed(command, **kwargs):
        marker = kwargs["env"]["CH_ASSET_PREFS_MARKER"]
        return _completed((marker + json.dumps({"ok": False, "error": "no prefs"}) + "\n").encode())

    monkeypatch.setattr(blend_worker.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="no prefs"):
        blend_worker.dump_asset_library_preferences("blender")
    with pytest.raises(ValueError):
        blend_worker.dump_asset_library_preferences("blender", timeout=float("nan"))


# --- blend_library_materials ------------------------------------------------


def test_node_names_and_cache_asset_root(monkeypatch, tmp_path):
    assert library_materials.node_name("Metal - Galvanized") == "Metal_Galvanized"
    assert library_materials.node_name("3D Wood.001") == "m_3D_Wood_001"
    assert library_materials.node_name("!!!") == "blender_material"
    assert library_materials.DEFAULT_ASSET_ROOT == "$HIP/assets_blender"
    cache = ModuleType("cycles_houdini.assets.cache")
    cache.cache_root = lambda: tmp_path / "cache"
    monkeypatch.setitem(sys.modules, "cycles_houdini.assets.cache", cache)
    # An unsaved scene's imports go to <cache>/assets_blender (blend-import.md: Where imports write), not
    # material_textures.
    assert library_materials.cache_asset_root() == (tmp_path / "cache/assets_blender").as_posix()


def _staged_record(staging, payload=b"pixels", name="Metal"):
    """A textures="in_place" record with one image packed in the .blend, staged as textures/abc.png."""
    import hashlib

    (staging / "textures").mkdir(parents=True, exist_ok=True)
    (staging / "textures/abc.png").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    dependency = {"filename": "textures/abc.png", "sha256": digest, "packed": True}
    image = {
        "filename": "textures/abc.png",
        "dependencies": [dependency],
        "is_data": False,
        "tiles": [],
        "packed": True,
    }
    return {
        "key": "blender-material-0123456789abcdef",
        "name": name,
        "nodes": [{"id": "n1", "image": image}],
        "outputs": {},
    }


# Placing packed images once per content and refusing escaping names is covered by
# test_assets_texture_names.py (test_packed_textures_go_to_tex_with_real_names,
# test_reload_of_unchanged_material_reuses_files,
# test_place_packed_textures_refuses_unsafe_or_inconsistent_names).


def test_place_packed_textures_rejects_a_changed_payload(tmp_path):
    staging, blend_dir = tmp_path / "staging", tmp_path / "root" / "metal"
    record = _staged_record(staging)
    record["nodes"][0]["image"]["dependencies"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed"):
        library_materials.place_packed_textures(staging, record, blend_dir)
    assert not any((tmp_path / "root").rglob("*.tmp-*"))
    assert not (blend_dir / "tex" / "abc.png").exists()


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

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def material_host(monkeypatch, tmp_path):
    hip = tmp_path / "project"
    hip.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    source = library / "metal.blend"
    source.write_bytes(b"BLENDER")
    (hip / "scene.hip").write_bytes(b"hip")
    state = SimpleNamespace(
        hip=hip, source=source, nodes={}, names=set(), imports=[], export_errors={}, fail=None, textures=[]
    )

    def expand(value):
        return value.replace("$HIP", state.hip.as_posix())

    def set_name(node):
        def apply(name, unique_name=False):
            assert unique_name
            candidate, index = name, 1
            while candidate in state.names:
                candidate, index = name + str(index), index + 1
            state.names.add(candidate)
            node._path = "/mat/" + candidate

        return apply

    mat = _Node("/mat", "matnet")
    state.nodes["/mat"] = mat
    hou = SimpleNamespace(
        text=SimpleNamespace(expandString=expand),
        node=lambda path: state.nodes.get(path),
        hipFile=SimpleNamespace(isNewFile=lambda: False, path=lambda: (state.hip / "scene.hip").as_posix()),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setattr(blender_bridge, "require_executable", lambda explicit=None: Path("/opt/blender"))

    def export(source, names, output_dir, executable, timeout=300, cancelled=None, textures="copy", **options):
        state.textures.append(textures)
        if state.fail is not None:
            raise state.fail
        record = _staged_record(Path(output_dir), name=names[0])
        return {"records": [record] if not state.export_errors else [], "errors": dict(state.export_errors)}

    def import_materials(records, asset_root, parent=None, diagnostics=None):
        (record,) = records
        state.imports.append((record, Path(asset_root), parent))
        image = record["nodes"][0]["image"]
        assert (Path(asset_root) / image["dependencies"][0]["filename"]).is_file()
        path = "/mat/" + blend_materials._name(record["name"]) + "_" + record["key"][-8:]
        host = _Node(path, "cycles::material_builder::1")
        host.setName = set_name(host)
        texture = _Node(path + "/tex", parms={"filename": _Parm(str(Path(asset_root) / image["filename"]))})
        other = _Node(path + "/noise", "cycles::noise_texture::1", {"filename": _Parm("/elsewhere/x.png")})
        host.children = [texture, other]
        state.nodes[path] = host
        return {record["key"]: path}

    monkeypatch.setattr(blend_worker, "export_library_materials", export)
    monkeypatch.setattr(blend_materials, "import_materials", import_materials)
    return state


def test_import_library_material_keeps_hip_relative_textures(material_host):
    state = material_host
    node = library_materials.import_library_material(state.source, "Metal - Galvanized")
    assert state.textures == ["in_place"]
    record, root, parent = state.imports[0]
    # Packed images go to the material's blend folder: $HIP/assets_blender/<blend>/tex.
    tex = (state.hip / "assets_blender/metal/tex").resolve()
    assert parent is state.nodes["/mat"] and root == tex
    assert node.path() == "/mat/Metal_Galvanized"
    texture, other = node.children
    assert record["nodes"][0]["image"]["filename"] == "abc.png"
    assert texture.parms["filename"].value == "$HIP/assets_blender/metal/tex/abc.png"
    assert other.parms["filename"].value == "/elsewhere/x.png"
    assert (tex / "abc.png").is_file() and not (state.hip / "tex").exists()
    assert node.user_data["cycles_blender_library_material"] == "Metal - Galvanized"
    second = library_materials.import_library_material(state.source, "Metal - Galvanized")
    assert second.path() == "/mat/Metal_Galvanized1"
    assert sorted(p.name for p in tex.iterdir()) == ["abc.png"]  # one file per packed content


def test_import_library_material_absolute_root_stays_absolute(material_host, tmp_path):
    state = material_host
    root = tmp_path / "shared"
    prepared = library_materials.prepare_library_material(state.source, "Metal", root=root, executable="/opt/blender")
    prepared["asset_root_text"] = root.as_posix()  # an absolute asset folder has no $HIP form
    node = library_materials.build_library_material(prepared)
    value = node.children[0].parms["filename"].value
    assert "$" not in value and Path(value).is_relative_to(root.resolve())


def test_import_library_material_reports_specific_failures(material_host, tmp_path):
    state = material_host
    state.export_errors = {"Metal": "Unsupported connected Blender node type: ShaderNodeScript"}
    with pytest.raises(blend_materials.BlendMaterialError, match="ShaderNodeScript"):
        library_materials.import_library_material(state.source, "Metal")
    state.export_errors = {}
    state.fail = RuntimeError("Material export failed in Blender: worker crashed")
    with pytest.raises(blend_materials.BlendMaterialError, match="worker crashed"):
        library_materials.import_library_material(state.source, "Metal")
    state.fail = None
    # The asset root ($HIP/assets_blender) may not lie inside the asset library (blend-import.md: Where imports write)
    # ...
    with pytest.raises(blend_materials.BlendMaterialError, match="outside the asset library"):
        library_materials.import_library_material(state.source, "Metal", library_root=tmp_path)
    # ... nor inside the .blend file's own folder.
    state.hip = state.source.parent
    (state.hip / "scene.hip").write_bytes(b"hip")
    with pytest.raises(blend_materials.BlendMaterialError, match="outside the asset library"):
        library_materials.import_library_material(state.source, "Metal")
    with pytest.raises(library_materials.AssetRootError, match="undefined variable"):
        library_materials.resolve_asset_root("$JOB/assets_blender", state.source)
    assert not state.imports
