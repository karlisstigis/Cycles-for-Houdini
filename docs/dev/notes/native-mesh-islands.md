# Mesh island shading and subdivision recovery

Connect **Cycles Geometry → Random Per Island** to a shader input to obtain one
repeatable scalar per connected polygon-mesh component. No authored attribute,
name or ID is required. Connectivity follows shared point indices; coincident
but disconnected points are separate. Position-only deformation and face-order
edits retain the values. Renumbering or changing connectivity can change them.

The worker calculates values from the original mesh before displacement splits
UV/material seams. Linear and Catmull-Clark subdivision retain the coarse-face
values. Ordinary material assignments do not create islands. Packed copies of
one prototype share its island values; combine with Object Info Random or a
packed numeric attribute when each copy needs different shading.

Calculation is demand-driven. Owned per-face results are cached with the control
topology and shared by render variants. Position edits reuse compatible results;
camera/object transforms do not recapture geometry or recalculate connectivity.
Shader-only changes can add or remove the dependency from retained geometry.
The cache and engine face buffers contribute to tracked geometry ownership.

Points, hair, analytic lights and world media have no polygon-mesh island source.
The existing local recovery policy applies: supported components keep rendering,
affected surface shading uses the diagnostic material, and dependent effects are
disabled. **Pointiness** has its own mesh adapter, described in
`native-mesh-pointiness.md`. Its curvature follows geometric positions and
original polygon edges; island identity follows shared point indices.

## Invalid subdivision control meshes

Native capture detects repeated control-face points, nonmanifold edges and
inconsistent shared-edge winding before strict engine validation. It disables
subdivision for that mesh and triangulates usable polygons, retaining materials
and other scene components. The `geometry.subdivision_topology` diagnostic names
the source and omitted effect. Camera-only reuse retains the warning; repairing
the source restores subdivision and clears it during IPR.

This is explicit source recovery. Invalid owned indices, resource ownership,
transport messages and engine/programming contracts retain strict validation.

## Scoped verification

The pinned Houdini 22.0.429 build used host `20261018T231209499`, worker protocol
44, 45 patches, five compiled artifacts and six unchanged kernels. The
source-audited evidence (`artifacts/mesh-island-evidence-20261019.json`) records
13 passing final runs. Earlier fixture failures and the discovered subdivision
failure remain in the archive index (`artifacts/verification/mesh-islands-20261019/index.json`).

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 43 image/update/recovery checks
and write five ordered frames, including diagnostic shading on frame three and
repair afterward. Checks cover connectivity changes, face ordering, deformation,
multiple materials, UV seams, true displacement, both subdivision schemes,
packed sharing, mixed components and each invalid-control-topology case.

Cycles Viewer and native Houdini IPR separately pass 22 live Qt/HOM states with
one viewer open. Twelve copies retain one 1,048,576-triangle prototype. Its cook
count stays at one; camera/object edits and shader repairs add no host geometry
conversion. Tracked geometry bytes return to the initial value after repair;
owned/device allocation counts return to zero on close. These checks do not
certify physical input, process RSS, long sessions or mixed-device rendering.

Regressions include packed numeric fields, Principled input grouping/availability,
identity/subdivision metadata, local source/domain recovery, native subdivision
images, independent core subdivision references and retained object poses.

That round exposed slow topology/shader updates despite retained geometry. A
later profile identified unnecessary emitter-tree invalidation during device
buffer repacking. [The subsequent fix and 960×540 paint observations](native-retained-emitter-updates.md)
supersede that open diagnosis. The original island checks used 210×120; broad
preview-budget and production certification remain separate measurements.

The saved `artifacts/examples/mesh-islands/islands-cycles.hiplc` and
`islands-native.hiplc` contain the editable test setup and heavy shared prototype.
Use one viewer at a time. These targeted shader/recovery fixtures supplement the
main SOP showcase.
