# Surface and volume in a native material

Cycles Material Output now accepts Surface, Volume and Displacement. Its operator
identifier remains `cycles::surface_output::1`; Volume is appended so existing
Surface/Displacement connections keep their saved indices. The input names remain
stable. Cycles Volume Material remains the normal sparse VDB workflow.

## Author a closed-mesh medium

1. Create a Cycles Material and assign it to a closed polygon mesh or its packed
   copies using Houdini's ordinary material assignment.
2. Connect a surface shader, such as Glass BSDF, to Material Output / Surface.
3. Add Volume Absorption and connect its Volume output to Material Output / Volume.
   Density is extinction per world-space length. Color controls its channel response.
4. Leave Surface disconnected for a Volume-only boundary, such as a fog container.
   Surface plus Volume can also use true displacement through the existing output.

The mesh must define a closed, outward-facing interior. Open surfaces or reversed
boundaries do not establish reliable entering/exiting volume events. This matches
[Blender's volume-boundary conventions](https://docs.blender.org/UATEST/manual/en/dev/render/materials/components/volume.html).
The distinction between Surface, Volume and Displacement is described in the
[Blender Material Output documentation](https://docs.blender.org/manual/en/5.0/render/shader_nodes/output/material.html).

Closed mesh media use shader values or procedural coordinates, rather than named
sparse grids. A named field the shader reads (Principled Volume's Density, Color and
Temperature attributes, an Attribute node, Volume Info) is not found in a mesh medium, as
in Blender's Cycles: Principled Volume keeps its unscaled Density, Color and Temperature,
and an Attribute node returns its Missing value (0). Since task L1 (2026-11-14) this is
the only behaviour for native, converted and Link materials; the earlier recovery
(diagnostic surface, `material.geometry_binding` "Polygon mesh media cannot read sparse
volume fields") is gone. A mesh point, primitive or vertex attribute of such a name is
not read either; native meshes get one info note `material.mesh_volume_attribute`
(Link meshes get none, as Blender shows none).
Surface UV requirements are independent of the medium. Medium textures need explicit
3D coordinates; a shared Texture Coordinate node may supply UV to the surface and
Object to a separate volume branch. Sparse VDBs still require Volume-only materials.
Points and hair do not acquire polygon-volume bounds from this feature.

Surface/volume parameter edits retain compatible geometry. A parameter affecting
displacement can rebuild it; changing unrelated surface or medium values does not.
Packed objects share a prototype when their rendering state permits it.

## Live updates and ownership

Asynchronous native capture discovers material assignments after submission.
The session now publishes a material-path revision when capture finishes. Both
native IPR and the Cycles panel subscribe to newly discovered builders and children
after that publication. Otherwise the first parameter edit, or an edit of a newly
assigned material, could be missed until another scene change.

Revision checks do not wait for conversion or inspect geometry. Callback lists
are rebuilt only when published assignment paths differ. Camera-only captures
do not change the revision. Ordinary VOP creation/rewiring retains the existing
submission-time observation refresh.

## Scope and verification

`houdini_combined_material_smoke.py` independently specifies extinction through a
one-unit shell, transmission at both boundaries, transform-dependent path length,
Volume-only bounds, IOR-1 glass, true displacement of the complete shell, and UV/Object
coordinate requirements. It checks four packed objects sharing one prototype,
nonmutating invalid output wiring and saved HIP connections on CPU/OptiX.

`houdini_combined_material_gui_smoke.py` uses one real Houdini Cycles panel. It clicks
Render, types Density through the native parameter editor, takes a snapshot,
disconnects/restores Volume, assigns a new material and edits it, and checks worker
retirement. Compatible parameter changes retain geometry and the renderer process.

The example is `artifacts/stage3/materials/combined/surface-volume-example.hiplc`.
It contains four packed closed boxes, two editable shader nodes, a camera and a
Cycles ROP. Finished runs and their source/binary audits are under
`artifacts/verification`.

This is a development slice. General scattering/phase functions, refractive caustics,
heterogeneous production media, motion/displacement across shutter samples, overlapping
or per-face media, physical memory stress and renderer parity retain separate gates.
Volume-only displacement and world volumes remain unsupported by this graph adapter.
