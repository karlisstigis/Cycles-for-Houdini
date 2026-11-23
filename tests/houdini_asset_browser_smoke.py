# SPDX-License-Identifier: Apache-2.0
"""Houdini 22 smoke test for the asset browser importers (run in hython or the Python Shell).

Not runnable without Houdini. Inputs (environment):

  CYCLES_HOUDINI_ASSET_SMOKE_BLEND       absolute path of a library .blend file (required)
  CYCLES_HOUDINI_ASSET_SMOKE_OBJECT      name of a marked mesh object in it (optional)
  CYCLES_HOUDINI_ASSET_SMOKE_COLLECTION  name of a marked collection in it (optional)
  CYCLES_HOUDINI_ASSET_SMOKE_MATERIAL    name of a marked material in it (optional; needs Blender)

Checks node types, parameters, the material assignment (synchronous and the
panel's snapshot/prepare/build split), and that each import is exactly one
undo step. Also constructs and disposes the panel when
a UI is available.
"""

import os
import tempfile
from pathlib import Path

import hou
from cycles_houdini.assets import importers
from cycles_houdini.assets.records import make_record

blend = Path(os.environ["CYCLES_HOUDINI_ASSET_SMOKE_BLEND"]).resolve()
assert blend.is_file(), blend
library_root = blend.parent
os.environ.setdefault(
    "CYCLES_HOUDINI_ASSET_BROWSER_SETTINGS_FILE", str(Path(tempfile.mkdtemp()) / "asset_browser_settings.json")
)


def asset(name, id_type, object_type=None):
    return make_record(name, id_type, blend.name, source=str(blend), object_type=object_type)


def undo_removes(path):
    hou.undos.performUndo()
    assert hou.node(path) is None, "one undo step must remove " + path
    hou.undos.performRedo()
    assert hou.node(path) is not None, "redo must restore " + path


checked = []
for kind, env in (
    ("OBJECT", "CYCLES_HOUDINI_ASSET_SMOKE_OBJECT"),
    ("COLLECTION", "CYCLES_HOUDINI_ASSET_SMOKE_COLLECTION"),
):
    name = os.environ.get(env)
    if not name:
        continue
    record = asset(name, kind, "MESH" if kind == "OBJECT" else None)
    try:
        importers.import_asset(record, mode="append")
        raise AssertionError("append must be rejected for " + kind)
    except importers.AssetImportError:
        pass
    nodes = importers.import_asset(record, mode="link")
    geo = nodes[0]
    assert geo.type().name() == "geo" and geo.parent().path() == "/obj", geo.path()
    reference = geo.node("blender_reference")
    assert reference is not None and reference.type().name() == importers.REFERENCE_SOP, reference
    assert reference.evalParm("file") == str(blend)
    assert reference.evalParm("multiple_references") == 1 and reference.evalParm("references") == 1
    assert reference.evalParm("ref_enabled1") == 1
    assert reference.evalParm("ref_target_kind1") == kind.lower()
    assert reference.evalParm("ref_target1") == importers.target_token(name)
    # A legacy ref_name would make the node skip name resolution (no bounds or hull).
    assert reference.evalParm("ref_name1") == "", reference.evalParm("ref_name1")
    assert reference.isDisplayFlagSet() and geo.userData(importers.ASSET_KEY_USER_DATA) == record["ch"]["key"]
    undo_removes(geo.path())
    checked.append(kind)

material_name = os.environ.get("CYCLES_HOUDINI_ASSET_SMOKE_MATERIAL")
if material_name:
    record = asset(material_name, "MATERIAL")
    [material] = importers.import_asset(record, mode="append", library_root=str(library_root))
    assert material.parent().path() == "/mat", material.path()
    assert material.userData(importers.ASSET_KEY_USER_DATA) == record["ch"]["key"]
    undo_removes(material.path())
    target = hou.node("/obj").createNode("geo", "asset_smoke_target", run_init_scripts=False)
    assigned, objects = importers.assign_material(record, [target], library_root=str(library_root))
    assert assigned.path() == material.path(), "an imported material is reused, not re-imported"
    assert target.evalParm("shop_materialpath") == material.path()
    hou.undos.performUndo()
    assert target.evalParm("shop_materialpath") == "", "assignment is one undo step"
    checked.append("MATERIAL")
    # The panel's split path: host snapshot, background-safe prepare, host build in one undo group.
    snapshot = importers.material_snapshot(record)
    prepared = importers.prepare_material(snapshot)
    [split] = importers.import_asset(record, mode="append", prepared=prepared)
    assert split.path() != material.path() and split.parent().path() == "/mat"
    undo_removes(split.path())
    if hou.hipFile.isNewFile():
        assert snapshot["notice"], "an untitled scene must fall back to the shared cache"
    checked.append("MATERIAL (split)")

    # Link -> reuse -> reload -> append (make local).
    from cycles_houdini.blender.blend_library_materials import LINK_USER_DATA, link_info

    [linked] = importers.import_asset(record, mode="link", library_root=str(library_root))
    assert link_info(linked)["name"] == material_name
    assert all(parm.isLocked() for parm in linked.parms()), "linked parameters are locked (greyed out)"
    assert all(parm.isLocked() for child in linked.allSubChildren() for parm in child.parms())
    assert linked.comment().startswith("Linked from "), linked.comment()
    assert importers.import_asset(record, mode="link") == [linked], "Link reuses the one shared node"
    assert importers.assign_material(record, [target], mode="link")[0] == linked
    linked_path = linked.path()
    linked.setPosition(hou.Vector2(4, -3))
    [reloaded] = importers.rebuild_linked([(linked, importers.prepare_material(importers.reload_snapshot(linked)))])
    assert reloaded.path() == linked_path and tuple(reloaded.position()) == (4, -3)
    assert target.evalParm("shop_materialpath") == linked_path, "assignments survive a reload"
    assert all(parm.isLocked() for parm in reloaded.parms()), "locks are re-applied after reload"
    hou.undos.performUndo()
    assert hou.node(linked_path) is not None, "undoing a reload restores the previous node"
    hou.undos.performRedo()
    reloaded = hou.node(linked_path)
    importers.append_linked(reloaded)
    assert reloaded.path() == linked_path and not reloaded.userData(LINK_USER_DATA)
    assert not any(parm.isLocked() for parm in reloaded.parms()) and reloaded.comment() == ""
    hou.undos.performUndo()
    assert link_info(hou.node(linked_path)) is not None, "make-local is one undo step"
    checked.append("MATERIAL (link/reload/append)")

try:
    importers.import_asset(asset("Sky", "WORLD"))
    raise AssertionError("worlds must be rejected")
except importers.AssetImportError as error:
    assert "cannot be imported" in str(error)

if hou.isUIAvailable():
    from cycles_houdini.assets.panel import AssetBrowserPanel

    panel = AssetBrowserPanel()
    panel.dispose()
    checked.append("panel")

print("asset browser smoke OK:", ", ".join(checked) or "rejections only")
