# Mesh Pointiness

Connect **Cycles Geometry > Pointiness** to a material input or AOV. The adapter
derives curvature from the original polygon mesh; no authored attribute is
required. A flat surface evaluates to 0.5. Convex and concave regions move toward
1 and 0, respectively; a regular outward-facing octahedron evaluates to 0.75.

The implementation follows Blender's Apache-2.0 Pointiness adapter, with the
attribution retained in `src/core/mesh_pointiness.h` and `THIRD_PARTY_NOTICES.md`.
It builds angle-weighted geometric vertex normals, uses original polygon edges,
combines coincident/near-coincident vertices using the adapter's duplicate
search, and averages the local angular measure over neighboring vertices.
Triangulation diagonals and artist-authored shading normals do not define the
curvature. Invalid authored normals retain their existing local recovery and
diagnostics without invalidating Pointiness.

Curvature is calculated in the prototype's local space at the requested frame,
before render-time subdivision and displacement. It remains a requested-frame
field over deformation shutter samples, including offset shutters. Subdivision
interpolates that field; displacement seams retain its canonical vertex mapping.
Packed copies share the prototype field. Camera and object movement do not
recapture or reconvert the mesh just to calculate Pointiness.

The intrinsic is independent of an ordinary shader attribute named
`pointiness`. Both can be used in the same material. A missing or invalid named
color field cannot invalidate independent displacement driven by the intrinsic.
Unused malformed fields and unregistered AOV writers remain inactive.

Only mesh components supply this intrinsic. When a Houdini geometry object also
contains hair, points or volumes, the existing local recovery policy applies to
their unsupported use of Pointiness; usable components keep rendering. Changing
the material to supported inputs restores those components and clears warnings.

## Demand, caching and ownership

Demand follows reachable surface/displacement outputs and registered AOV
writers. Native capture computes one owned scalar per original vertex only when
needed. Compatible geometry reuses the field; position/topology changes
invalidate it. Unchanged packed prototypes share it across copies and render
variants. Removing the last dependency releases the field rather than keeping
an unbounded cache.

Worker protocol 45 carries the owned optional field. Strict internal validation
checks its vertex count and finite normalized values. Geometry and field buffers
remain part of tracked ownership and are released at session close. Invalid
owned messages and programming errors retain strict failure behavior.

## Verification, 2026-10-19

`artifacts/mesh-pointiness-evidence-20261019.json` indexes the audited sources and
reports. Host `20261019T015022448` uses protocol 45, 48 engine patches, five
compiled artifacts and the six matching kernels. Pointiness uses the engine's
existing standard attribute; this adapter requires no additional kernel patch.

- Twelve core contracts cover analytic convex/concave/flat curvature, split and
  nearby vertices, equal-coordinate-sum sorting, isolated/degenerate inputs,
  extreme finite coordinates and 20,000 coincident vertices.
- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 59 checks and five ordered
  output frames. Independent NumPy references use the original polygons;
  explicit expanded geometry provides a separate true-displacement reference.
- Checks include authored-normal recovery, namespace independence, packed
  sharing, repeated demand retirement with stable tracked resources, AOV-only
  demand, mixed mesh/hair/point recovery, both subdivision schemes, and nominal
  curvature during deformation blur with centered and offset shutters.
- Cycles Viewer and Houdini IPR each pass 18 live states with one viewer open.
  A million-triangle prototype shared twelve times stays at one cook. Named
  color failure/repair preserves independent displacement; both sessions close
  with zero tracked geometry and device allocations. These are Qt/HOM checks,
  not physical-input or universal latency certification.
- Mesh-island, shader-source, material-extension and geometry-domain recovery
  checks pass on CPU and the 4090. Both island-viewer workflows pass again, and
  the 4090 passes the 42-check constant-attribute/White Noise suite plus five
  frames. Unsupported instancer-coordinate tests replace the former assumption
  that mesh Pointiness was unsupported. Volume Pointiness remains a recovery
  case.

One legacy extension test initially selected object mode while editing its SOP
path. Its failed source/report is retained. Explicit SOP mode restores its
original Hair Info assertions; a staging proof confirms this was a test-only
change with identical binaries and kernels.

## Remaining performance work

Correct output is verified, but some independent color-field failures/repairs
and shader palette transitions still refill unchanged displaced meshes. The
observed counters and separate constant-color control are preserved in
`artifacts/pointiness-displacement-followup-20261019.json`. Constant color-value
edits and camera edits in that control do not rebuild displacement. Avoiding the
remaining palette/attribute-only rebuilds is the next performance priority;
general field-only transport is not claimed complete.
