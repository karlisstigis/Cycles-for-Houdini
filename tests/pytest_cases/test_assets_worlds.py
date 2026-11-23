# SPDX-License-Identifier: Apache-2.0
"""Blender worlds through the asset importers and menus (blend-import.md: Worlds) against the fake
`hou` scene of test_blend_worlds: import, Replace / Keep existing, link reuse, Reload from
Library, Make Editable, the linked-asset lists; plus the hip-file Move into Project offer."""

import importlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
import test_blend_worlds as tw
from cycles_houdini.assets import importers, menus
from cycles_houdini.blender import blend_library_materials, blend_materials, blend_worlds
from test_blend_worlds import scene  # noqa: F401 - the fake hou scene fixture

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class Cache:
    def __init__(self):
        self.importability = {}

    def set_importability(self, key, state, reason):
        self.importability[key] = (state, reason)


@pytest.fixture
def world(scene, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture
    """The fake scene plus what the importers read: a saved .hip in tmp_path, a .blend, $HIP."""
    hou = sys.modules["hou"]
    (tmp_path / "scene.hip").write_bytes(b"hip")
    blend = tmp_path / "lib" / "sky.blend"
    blend.parent.mkdir()
    blend.write_bytes(b"BLENDER")
    hou.text = SimpleNamespace(expandString=lambda text: text.replace("$HIP", tmp_path.as_posix()))
    hou.hipFile = SimpleNamespace(isNewFile=lambda: False, path=lambda: (tmp_path / "scene.hip").as_posix())
    hou.getenv = lambda name, default=None: default
    hou.isUIAvailable = lambda: False
    hou.objNodeTypeCategory = lambda: "obj"
    hou.vopNodeTypeCategory = lambda: "vop"
    hou.selected = []
    hou.selectedNodes = lambda: list(hou.selected)
    # What scene_paths (Reload after Collect) reads besides the fake's own methods.
    monkeypatch.setattr(tw.HNode, "parent", lambda self: self._parent, raising=False)
    monkeypatch.setattr(tw.Parm, "unexpandedString", lambda self: str(self.value), raising=False)
    monkeypatch.setattr(importers, "source_signature", lambda source: [1, 2, 3, 4])
    from cycles_houdini.assets.records import make_record

    def record(name="Sky"):
        return make_record(name, "WORLD", "sky.blend", source=str(blend))

    def prepared(record_=None, **changes):
        result = tw.prepared(tmp_path, record_, source=blend.as_posix())
        result.update(changes)
        return result

    return SimpleNamespace(scene=scene, hou=hou, blend=blend, record=record, prepared=prepared, tmp=tmp_path)


# ---------------------------------------------------------------- import


def test_worlds_are_importable_in_both_modes():
    record = {"id_type": "WORLD", "name": "Sky"}
    assert importers.valid_modes("WORLD") == ("link", "append") and importers.unsupported_reason(record) == ""
    assert "WORLD" in importers.IMPORTABLE_TYPES
    assert importers.environment_question(["/obj/env1", "/mat/sky"]) == (
        "The scene already has environment lighting (/obj/env1, /mat/sky)."
    )
    assert importers.ENVIRONMENT_EXPLANATION == (
        "Replace: the new one becomes active; existing ones are disabled, not deleted. "
        "Keep existing: the new one is imported disabled."
    )


def test_link_import_builds_once_reuses_without_export_and_reports(world, monkeypatch):
    cache, lines = Cache(), []
    record = world.record()
    [node] = importers.import_asset(
        record, mode="link", prepared=world.prepared(), cache=cache, report=lambda *line: lines.append(line)
    )
    assert node.path() == "/obj/Sky" and node.type_name == "envlight"
    # The import's own undo group holds the world build (nested).
    assert world.scene.undo == ["Import Blender asset Sky", "Import Blender world Sky"]
    info = blend_library_materials.link_info(node)
    assert (info["kind"], info["route"], info["key"]) == ("world", "light", record["ch"]["key"])
    assert node.userData(importers.ASSET_KEY_USER_DATA) == record["ch"]["key"]
    assert cache.importability[record["ch"]["key"]] == ("yes", "")
    assert lines[0] == ("Imported world Sky as the Environment Light /obj/Sky.", False)
    assert any("stays at" in text and not warning for text, warning in lines)
    assert importers.find_linked_world(record) is node
    assert importers.linked_status(record) == {"path": "/obj/Sky", "stale": False}
    # Link again: the shared light is reused; Blender is never asked.
    monkeypatch.setattr(importers, "prepare_world", lambda *a, **k: pytest.fail("exported a reused world"))
    assert importers.existing_environment(record, "link") == []  # the reused light does not count
    assert importers.existing_environment(record, "append") == ["/obj/Sky"]
    assert importers.import_asset(record, mode="link") == [node]
    assert [n.name() for n in world.scene.obj.children()] == ["Sky"]


def test_append_replace_and_keep_existing(world):
    old = world.scene.light("env_old")
    record = world.record()
    assert importers.existing_environment(record, "append") == ["/obj/env_old"]
    [kept] = importers.import_asset(record, mode="append", prepared=world.prepared(), existing_environment="keep")
    assert blend_library_materials.link_info(kept) is None and not any(p.locked for p in kept.parms())
    assert (old.parm("light_enable").value, kept.parm("light_enable").value) == (1, 0)
    [active] = importers.import_asset(record, mode="append", prepared=world.prepared())  # Replace is the default
    assert active.name() == "Sky_1"
    assert [n.parm("light_enable").value for n in (old, kept, active)] == [0, 0, 1]  # disabled, not deleted


def test_missing_hdri_is_a_warning_line(world):
    lines = []
    record = tw.simple_record(image=tw.image_record("D:/Library/hdri/gone.exr", missing=True))
    importers.import_asset(
        world.record(), mode="append", prepared=world.prepared(record), report=lambda *line: lines.append(line)
    )
    assert any("texture not found" in text and warning for text, warning in lines)


def test_content_verdict_marks_the_asset_and_transport_failure_does_not(world, monkeypatch):
    cache, record = Cache(), world.record()

    def verdict(*args, **kwargs):
        raise blend_worlds.BlendWorldError("World 'Sky' cannot be imported: no World Output")

    monkeypatch.setattr(blend_worlds, "build_world", verdict)
    with pytest.raises(blend_worlds.BlendWorldError):
        importers.import_asset(record, mode="append", prepared=world.prepared(), cache=cache)
    assert cache.importability[record["ch"]["key"]] == ("no", "World 'Sky' cannot be imported: no World Output")
    assert importers.is_content_failure(blend_worlds.BlendWorldError("x"))
    assert not importers.is_content_failure(blend_library_materials.LibraryMaterialUnavailable("timeout"))


def test_world_snapshot_and_background_prepare(world, monkeypatch):
    calls = []

    def prepare_world(source, name, **options):
        calls.append((source, name, options))
        return {"name": name}

    monkeypatch.setattr(blend_worlds, "prepare_world", prepare_world)
    snapshot = importers.world_snapshot(world.record())
    assert snapshot["kind"] == "world" and snapshot["asset_root_text"] == "$HIP/assets_blender"
    assert snapshot["root"].replace("\\", "/").endswith("/assets_blender")
    snapshot["executable"] = str(world.blend)  # any existing file stands in for Blender
    prepared = importers.prepare_world(snapshot)
    assert prepared == {"name": "Sky", "asset_root_text": "$HIP/assets_blender", "notice": ""}
    source, name, options = calls[0]
    assert (source, name, options["root"], options["executable"]) == (
        world.blend.as_posix(),
        "Sky",
        snapshot["root"],
        str(world.blend),
    )
    assert importers.prepare_linked(dict(snapshot, kind="world"))["name"] == "Sky"


# ---------------------------------------------------------------- linked worlds: lists, reload, Make Editable


def _linked_world_material(world, monkeypatch):
    def import_world_material(record, root, parent=None, diagnostics=None):
        return parent.createNode("cycles::world_material::1", "Clouds_cdef").path()

    monkeypatch.setattr(blend_materials, "import_world_material", import_world_material)
    record = world.record("Clouds")
    [node] = importers.import_asset(record, mode="link", prepared=world.prepared(tw.material_record()))
    return node


def test_linked_lists_keep_materials_and_worlds_apart(world, monkeypatch):
    light = importers.import_asset(world.record(), mode="link", prepared=world.prepared())[0]
    clouds = _linked_world_material(world, monkeypatch)
    material = world.scene.mat.createNode("cycles::material::1", "steel")
    blend_library_materials.mark_linked(material, {"source": world.blend.as_posix(), "name": "steel"})
    assert importers.all_linked_materials() == [material]  # a linked World Material is a world
    assert importers.all_linked_worlds() == [light, clouds]
    assert importers.all_linked() == [material, light, clouds]
    assert menus.any_linked_in_scene()


def test_reload_dispatches_a_linked_light_and_keeps_its_collected_hdri(world, monkeypatch):
    record = world.record()
    [light] = importers.import_asset(record, mode="link", prepared=world.prepared())
    light.setUserData(importers.ASSET_KEY_USER_DATA, "key")
    # Collect Textures rewrote env_map (locked parameters are unlocked for the write).
    collected = "$HIP/assets_blender/sky/tex/sky_4k.exr"
    light.parm("env_map").locked = False
    light.parm("env_map").set(collected)
    light.parm("env_map").locked = True
    snapshot = importers.reload_snapshot(light.path())
    assert (snapshot["kind"], snapshot["path"], snapshot["name"]) == ("world", "/obj/Sky", "Sky")
    assert snapshot["root"] == (world.tmp / "assets_blender").as_posix()
    assert snapshot["asset_root_text"] == "$HIP/assets_blender"  # still names the recorded root
    newer = tw.simple_record(rotation=90.0)
    newer["simple"]["strength"] = 4.0
    seen = []

    def prepare_world(snapshot, cancelled=None, timeout=300):
        seen.append(snapshot["path"])
        return world.prepared(newer)

    monkeypatch.setattr(importers, "prepare_world", prepare_world)
    monkeypatch.setattr(importers, "prepare_material", lambda *a, **k: pytest.fail("material route for a world"))
    lines = []
    world.scene.undo.clear()
    [new] = importers.rebuild_linked(
        [(light.path(), importers.prepare_linked(snapshot))], lambda text, warning: lines.append(text)
    )
    # All nested in the reload's one undo group (the fake records labels in order of opening).
    assert new is light and world.scene.undo == [
        "Reload linked asset Sky",
        "Reload linked world Sky",
        "Keep collected textures",
    ]
    assert light.parm("light_intensity").value == 4.0 and light.values()["r"] == pytest.approx((0.0, -180.0, 0.0))
    # Reload after a collect: the library HDRI is missing here, so the collected copy is kept, with a warning.
    assert light.parm("env_map").value == collected and light.parm("env_map").locked
    assert any("collected copy is kept" in text for text in lines)
    assert blend_library_materials.link_info(light)["kind"] == "world"
    assert light.userData(importers.ASSET_KEY_USER_DATA) == "key"
    # Menu Reload (no UI: synchronous) goes the same way.
    world.hou.selected = [light]
    assert [node.path() for node in menus.reload_selected(None)] == ["/obj/Sky"]
    assert [node.path() for node in importers.reload_all_linked()] == ["/obj/Sky"]
    assert seen == ["/obj/Sky"] * 3  # the direct reload above, the menu, Reload All


def test_reload_dispatches_a_linked_world_material(world, monkeypatch):
    clouds = _linked_world_material(world, monkeypatch)
    snapshot = importers.reload_snapshot(clouds)
    assert snapshot["kind"] == "world" and snapshot["name"] == "Clouds"
    monkeypatch.setattr(importers, "prepare_world", lambda s, c=None, t=300: world.prepared(tw.material_record()))
    [new] = importers.rebuild_linked([(clouds.path(), importers.prepare_linked(snapshot))])
    assert new is not clouds and new.path() == "/mat/Clouds" and blend_library_materials.link_info(new)
    assert json.loads(new.userData(blend_worlds.WORLD_USER_DATA))["route"] == "material"


def test_direct_reload_keeps_the_hip_form_of_the_asset_root(world, monkeypatch):
    """blend_worlds.reload_linked_world (the synchronous API) gives the rebuild the scene's
    unexpanded asset root, so a packed HDRI stays $HIP/... (live finding, task L)."""
    from cycles_houdini.core import blender_bridge

    [light] = importers.import_asset(world.record(), mode="link", prepared=world.prepared())
    seen = {}
    monkeypatch.setattr(blender_bridge, "require_executable", lambda explicit=None: "/opt/blender")
    monkeypatch.setattr(blend_worlds, "prepare_world", lambda *a, **k: world.prepared())

    def rebuild(node, prepared, diagnostics=None):
        seen.update(prepared)
        return node, []

    monkeypatch.setattr(blend_worlds, "rebuild_linked_world", rebuild)
    blend_worlds.reload_linked_world(light)
    assert seen["asset_root_text"] == "$HIP/assets_blender"
    expand = world.hou.text.expandString
    assert blend_worlds.reload_root_text((world.tmp / "assets_blender").as_posix(), expand) == "$HIP/assets_blender"
    # A root the scene's asset root no longer names keeps absolute paths.
    assert blend_worlds.reload_root_text((world.tmp / "elsewhere").as_posix(), expand) is None


def test_make_editable_on_a_linked_light_and_world_material(world, monkeypatch):
    [light] = importers.import_asset(world.record(), mode="link", prepared=world.prepared())
    clouds = _linked_world_material(world, monkeypatch)
    world.scene.undo.clear()
    importers.append_linked(light.path())
    assert world.scene.undo == ["Make linked asset editable Sky"]
    assert blend_library_materials.link_info(light) is None and not any(p.locked for p in light.parms())
    assert blend_worlds.world_info(light)["route"] == "light"  # still an imported world (Collect)
    world.hou.selected = [clouds]
    assert menus.append_selected(None) == [clouds] and blend_library_materials.link_info(clouds) is None
    assert world.scene.undo[-1] == "Make linked assets editable"
    with pytest.raises(importers.AssetImportError, match="Not a linked Blender asset"):
        importers.append_linked(light)


class Editor:
    def __init__(self, category):
        self._pwd = SimpleNamespace(childTypeCategory=lambda: category)

    def pwd(self):
        return self._pwd


def test_network_menu_items_for_linked_lights_in_obj(world, monkeypatch):
    obj, mat, sop = Editor("obj"), Editor("vop"), Editor("sop")
    assert not menus.network_any_linked({"editor": obj})
    [light] = importers.import_asset(world.record(), mode="link", prepared=world.prepared())
    assert menus.network_any_linked({"editor": obj}) and menus.network_any_linked({"editor": mat})
    assert not menus.network_has_linked({"editor": obj})  # nothing selected
    world.hou.selected = [light]
    assert menus.network_has_linked({"editor": obj}) and menus.has_linked({"node": light})
    assert menus.node_menu({"node": light})
    assert not menus.network_has_linked({"editor": sop}) and not menus.network_any_linked({"editor": sop})
    # A linked (non-world) material selected while /obj is shown: material networks only.
    material = world.scene.mat.createNode("cycles::material::1", "steel")
    blend_library_materials.mark_linked(material, {"source": world.blend.as_posix(), "name": "steel"})
    world.hou.selected = [material]
    assert not menus.network_has_linked({"editor": obj}) and menus.network_has_linked({"editor": mat})


# ---------------------------------------------------------------- leftovers of earlier tasks


def test_layout_file_helpers_are_public():
    from cycles_houdini.blender import blend_assets_layout as layout

    assert layout.file_parts("bark_<UDIM>.exr") == ("bark", "_<UDIM>.exr")
    assert layout.file_parts("smoke.0012.exr") == ("smoke", ".0012.exr")
    assert layout.file_key("Bark_1001.EXR") == layout.file_key("bark_1002.exr")
    assert not hasattr(layout, "_file_parts") and not hasattr(layout, "_file_key")


def test_world_export_errors_name_worlds(tmp_path):
    from cycles_houdini.blender import blend_worker

    with pytest.raises(ValueError, match="^World export request exceeds its bounds$"):
        blend_worker.export_library_worlds(tmp_path / "x.blend", [], tmp_path, "blender")
    with pytest.raises(ValueError, match="^Invalid Blender world name"):
        blend_worker.export_library_worlds(tmp_path / "x.blend", [""], tmp_path, "blender")
    with pytest.raises(ValueError, match="^Material export request exceeds its bounds$"):
        blend_worker.export_library_materials(tmp_path / "x.blend", [], tmp_path, "blender")


def test_build_library_material_has_no_texture_root():
    import inspect

    assert "texture_root" not in inspect.signature(blend_library_materials.build_library_material).parameters


# -------------------------------------------------------- Move into Project offer (blend-import.md: Collect and relink)


@pytest.fixture
def startup(monkeypatch):
    calls = []
    hou = ModuleType("hou")
    hou.ui_available = True
    hou.isUIAvailable = lambda: hou.ui_available
    hou.callbacks = []
    hou.hipFile = SimpleNamespace(eventCallbacks=lambda: tuple(hou.callbacks), addEventCallback=hou.callbacks.append)
    hou.hipFileEventType = SimpleNamespace(AfterSave="after_save", BeforeSave="before_save", AfterLoad="after_load")
    monkeypatch.setitem(sys.modules, "hou", hou)
    from cycles_houdini.assets import move_project

    monkeypatch.setattr(move_project, "maybe_offer_move", lambda: calls.append("offer") or False)
    import cycles_houdini_startup

    return SimpleNamespace(hou=hou, calls=calls, module=cycles_houdini_startup, move_project=move_project)


def test_move_offer_registers_once_and_runs_only_after_save(startup, monkeypatch):
    module, hou = startup.module, startup.hou
    assert module.install_move_offer() is True
    assert module.install_move_offer() is False
    reloaded = importlib.reload(module)  # a reloaded module still sees its earlier callback
    assert reloaded.install_move_offer() is False and len(hou.callbacks) == 1
    callback = hou.callbacks[0]
    callback(hou.hipFileEventType.BeforeSave)
    callback(hou.hipFileEventType.AfterLoad)
    assert startup.calls == []
    callback(hou.hipFileEventType.AfterSave)
    assert startup.calls == ["offer"]

    def broken():
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(startup.move_project, "maybe_offer_move", broken)
    callback(hou.hipFileEventType.AfterSave)  # never raises into Houdini's save


def test_move_offer_needs_a_ui_and_install_ui_registers_it(startup, monkeypatch):
    module, hou = startup.module, startup.hou
    hou.ui_available = False
    assert module.install_move_offer() is False and hou.callbacks == []
    hou.hipFile = SimpleNamespace()  # a broken hou never breaks the UI start-up
    hou.ui_available = True
    assert module.install_move_offer() is False
    hou.hipFile = SimpleNamespace(eventCallbacks=lambda: tuple(hou.callbacks), addEventCallback=hou.callbacks.append)
    installed = []
    for name in ("cycles_houdini.viewer.network_preview", "cycles_houdini.materials.material_previews"):
        stub = ModuleType(name)
        stub.install = lambda name=name: installed.append(name)
        monkeypatch.setitem(sys.modules, name, stub)
    assert module.install_ui() is True
    assert len(installed) == 2 and len(hou.callbacks) == 1


def test_install_ui_hides_the_native_reference_and_never_raises(startup, monkeypatch):
    """The UI start-up hides cycles::blender_reference from the Tab menu through
    blend_file_node.hide_native_reference; a failure there is printed, never raised."""
    from cycles_houdini.blender import blend_file_node

    module = startup.module
    for name in ("cycles_houdini.viewer.network_preview", "cycles_houdini.materials.material_previews"):
        stub = ModuleType(name)
        stub.install = lambda: None
        monkeypatch.setitem(sys.modules, name, stub)
    hidden = []
    monkeypatch.setattr(blend_file_node, "hide_native_reference", lambda: hidden.append(True) or True)
    assert module.install_ui() is True and hidden == [True]
    assert module.hide_native_reference() is True

    def broken():
        raise RuntimeError("no node type")

    monkeypatch.setattr(blend_file_node, "hide_native_reference", broken)
    assert module.hide_native_reference() is False
    assert module.install_ui() is True
