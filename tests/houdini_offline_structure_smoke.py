# SPDX-License-Identifier: Apache-2.0
"""Native archive retention for a healthy root with a vanished child on load."""

import argparse
import hashlib
import json
from pathlib import Path

import hou
import cycles_houdini as ch
import cycles_houdini_offline as protection

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
hou.hipFile.clear(suppress_save_prompt=True)
material = ch.create_material(name="structural_loss", shader="emission")
# The default this test was written for (new nodes now take Blender's).
material.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
hou.hipFile.save(str(args.output / "intact.hiplc"), save_to_recent_files=False)
identity = material.userData(protection.MARKER)
intact = hou.node("/obj").dataBlock(protection.BLOCK)
material.node("surface_shader").destroy()
# Preserve the prior block to simulate a root surviving a missing-child load.
protection._busy = True
try:
    hou.hipFile.save(str(args.output / "damaged.hiplc"), save_to_recent_files=False)
finally:
    protection._busy = False
hou.hipFile.load(str(args.output / "damaged.hiplc"), suppress_save_prompt=True)
assert identity in protection._unresolved
assert "changed graph structure" in protection.inspect()[0]["status"]
hou.hipFile.save(str(args.output / "damaged-resaved.hiplc"), save_to_recent_files=False)
assert hou.node("/obj").dataBlock(protection.BLOCK) == intact
assert hou.node("/mat/structural_loss/surface_shader") is None
review = hou.node("/obj").createNode("matnet", "retained_intact_copy")
copy = protection.recover_copy(identity, review)
assert copy.node("surface_shader").type().name() == "cycles::emission::1"
# The artist may deliberately accept the structural deletion after review.
protection.accept_current(identity)
hou.hipFile.save(str(args.output / "accepted-deletion.hiplc"), save_to_recent_files=False)
assert hou.node("/obj").dataBlock(protection.BLOCK) != intact
hou.hipFile.load(str(args.output / "accepted-deletion.hiplc"), suppress_save_prompt=True)
assert identity not in protection._unresolved
assert "healthy graph structure" in protection.inspect()[0]["status"]
# New edits following a healthy load still refresh the archive normally.
material = hou.node("/mat/structural_loss")
shader = material.createNode("cycles::emission::1", "deliberate_new_shader")
# The default this test was written for (new nodes now take Blender's).
shader.setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
material.node("surface_output").setInput(0, shader)
hou.hipFile.save(str(args.output / "healthy-edited.hiplc"), save_to_recent_files=False)
records, _ = protection._read()
assert any(row["path"] == "deliberate_new_shader" for row in records[identity]["nodes"])
result = dict(
    result="passed",
    intact_archive_sha256=hashlib.sha256(intact).hexdigest(),
    unresolved_load_resave_preserves_archive=True,
    original_deletion_preserved=True,
    explicit_acceptance_refreshes_archive=True,
    healthy_loaded_artist_edits_refresh_archive=True,
)
(args.output / "structure.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
