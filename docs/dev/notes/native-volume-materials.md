# Native Cycles volume materials and fields

The October 21 binding/volume/camera increment adds finite tapered/frustum Volume resampling with local field recovery. Current contracts and evidence: [binding, tapered-volume and camera coverage](native-binding-volume-camera.md).

Native OBJ/SOP rendering accepts sparse VDBs and affine Houdini Volume
primitives. Cycles Viewer, native IPR and final/batch rendering share the
field and material recovery policy. The source-specific dense-volume evidence
is recorded in `artifacts/dense-volume-evidence-20261018.json`.

## Author and assign

Create **Cycles Volume Material** in `/mat` and assign it through the geometry
OBJ's Material parameter or a Material SOP. Enable native material assignments
on the Cycles ROP. The volume builder's default Principled Volume reads
`density`; temperature is required only when blackbody emission contributes.
The color-field name is empty by default. Shader values multiply named fields.

```python
import hou
import cycles_houdini as ch

pyro = ch.create_material(name="pyro", shader="principled_volume")
shader = pyro.node("volume_shader")
shader.parm("density").set(2.0)
shader.parm("blackbody_intensity").set(1.0)
shader.parm("temperature").set(1000.0)
hou.node("/obj/pyro").parm("shop_materialpath").set(pyro.path())
```

Choose the temperature multiplier for the simulation's units; field value 2
with multiplier 1000 produces 2000 K. Unconnected Absorption/Scatter Volume
Density inputs are constant over the volume bounds. The `absorption_volume`
and `scatter_volume` presets explicitly connect Volume Info Density through
a Math multiplier. Clear Principled Volume's Density Attribute when its input
already reads the field, to avoid multiplying two density reads.

For a native Volume SOP, set its field name to `density` or the name needed by
the shader. A field name identifies its physical quantity, not particle identity.
The adapter does not invent density for unnamed or unusable fields.

## Field collections, assignments and recovery

One volume component represents a collection of named fields and one medium.
Primitive assignments take precedence over detail, OBJ and ROP fallbacks.
When fields have conflicting materials, use the effective material of the
first **usable scalar density** in SOP order and report ignored assignments.
Selection occurs after voxel validation, so a NaN density cannot select its
material while a later valid density supplies the actual field. Separate OBJ
volume objects can use independent media and materials.

Duplicate or unusable fields are omitted locally; the first usable field of
each name wins. With no usable density, omit this volume component and retain
other meshes, points, hair and volume objects. Valid density with an unsupported
or incompatible shader uses density-based magenta scattering without emission.
Missing shader-required fields recover per binding. Repairs clear warnings in
the running IPR. EXR metadata retains accepted diagnostics.

Unsupported assignment ownership/layers are diagnosed and ignored while
supported bindings remain. Unused graph branches do not create field demand.
The Attribute node reads named scalar/vector fields; Volume Info exposes the
standard fields. Source recovery does not weaken strict internal engine,
transport or output contracts. Closed polygon media and combined shaders use
the separate [mesh-medium workflow](native-mesh-media.md).

## Affine native dense fields

Host `20261018T181813930` (Houdini 22.0.429, Python 3.13) adds native float, vec3
and integer Volume storage. Integers convert to renderer float grids. Vec2/vec4
storage, singular/non-finite transforms and non-finite voxel values recover per
field. Valid zero-density fields remain valid empty media.

Host capture retains independent voxel read handles and the voxel-centre
transform. Tile-aware conversion runs off the host thread, preserving compressed
constant regions and handling nonconstant and partial tiles. Frozen snapshots
remain readable through recooks and source deletion. Pending Houdini OpenCL
writes are synchronized during host capture; dedicated GPU-produced-volume
certification remains outstanding.

Unchanged native field storage reuses CPU conversion. Camera-only edits do no
geometry conversion. Requested but unusable volume sources retain bounded
conversion state and diagnostics; unrelated object movement does not reconvert
their valid fields. Repair, source removal and shutdown have separate checks.

Changed-volume transport and engine updates still send/reload the full field
collection. Per-field CPU conversion reuse does not imply per-field GPU reuse.
Source replacements now retire obsolete voxel attributes and images, preventing
renamed fields from accumulating device buffers or retaining stale bounds.
Previously unused volume, point, hair and light shaders are reactivated before
assignment, following Cycles' shader-reference contract.

Finite tapered/frustum resampling is now implemented and scoped-tested; see
[native volume resampling](native-binding-volume-camera.md#tapered-and-frustum-volume-fields). Additional curve
representations and broader shader/coordinate contexts remain separate
development work. Existing velocity aliases retain their documented
canonicalization and motion semantics.

## Native scalar components for vector fields

Host `20261018T191031427` adds demand-driven native color/custom vector reconstruction.
Houdini's [Volume SOP Collated Rank](https://www.sidefx.com/docs/houdini/nodes/sop/volume.html)
can represent one logical vector with scalar fields named `tint.x`, `tint.y`,
`tint.z`. An active Attribute node requesting `tint` now reads an assembled
vector. Volume Info Color similarly resolves `color.x/.y/.z`.

- Only active shader demand creates an alias. Original scalar components remain
  independently readable; an explicit root field retains precedence.
- Components must have scalar float storage after native conversion and matching
  affine maps. Partial, unusable or misaligned triplets recover per volume binding:
  usable density and other geometry remain, with a precise diagnostic. Repairing
  the source or changing the active field clears the warning automatically.
- Sparse assembly preserves background/inactive values and union activity, including
  compressed tiles. It runs before engine mutation. Shader-only demand changes
  use retained source fields without recooking/extracting SOP geometry.
- Internal immutable field versions cross worker protocol 43. A density-only edit
  reuses unchanged color-vector assembly even though transported headers are new.
  A changed component rebuilds its vector. No artist-authored identity is required.
- Only current demanded aliases and current owners are retained; unused aliases
  retire on demand changes and tracked ownership reaches zero on close.

Changed-volume transport and engine synchronization still send/reload the whole
source collection. CPU conversion/assembly reuse does not establish per-field GPU
reuse. Mismatched components require explicit Houdini resampling. General velocity
component-name access retains the earlier canonicalization behavior.

Source-specific proof: `artifacts/volume-vectors-evidence-20261018.json` and
`artifacts/verification/volume-vectors-20261018/index.json`. CPU and both OptiX
devices pass 104 updates each, native explicit-vector/VDB image references,
independent emission integrals, scalar access, standard color fields, local repair,
32 demand cycles and five ordered final frames. The final sixteen repetitions
have stable tracked allocation in each phase. The complete trajectory is retained,
including a small earlier CPU allocation step; it does not continue growing.

Both viewers pass 35 parameter/repair/navigation steps with one Render click,
one worker and twelve retained behind-camera instances of a million-triangle
prototype. Cycles Viewer canvas dimensions remain constant across diagnostics;
native IPR displayed pixels match the accepted frame. Its Qt screenshot has toolbar
capture artifacts. A gray-background GUI fixture
keeps the bright volume visibly distinct. These Qt/HOM checks do not certify
physical input, physical memory or production latency.

The current main suite registers `core_volume_vector_smoke.py`,
`houdini_volume_vector_smoke.py` and both vector GUI tests. The GUI fixtures are
saved as `artifacts/stage2/volumes/vector-components/vector-recovery-cycles.hiplc`
and `vector-recovery-native.hiplc`; use one viewer at a time.

## Dense-volume verification

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX pass:

- Native/Houdini-Convert-VDB comparisons and independent Beer-Lambert integrals.
- Float, integer, vec3, zero-density, duplicate, invalid and missing-field cases.
- 27 recovery states, navigation while density is unavailable, source removal,
  warning cleanup and five ordered final frames through one Persistent Data session.
- Twenty unused-field renames, three temperature-only edits and three camera edits.
- Varying voxels with VDB, mesh, point and hair components in one SOP, including
  concurrent frozen conversion through recooks and source deletion.
- Reactivation of previously unused point/hair shaders.

Each viewer passes 31 native-parameter/repair/navigation steps with one Render
click and one worker. Twelve behind-camera instances retain a million-triangle
prototype without recooking it. Tracked source, geometry and device bytes reach
zero on close. Nine focused regressions include volume materials/motion, packed
volume recovery, light shaders, native world IPR and color IPR.

These are scoped development checks, not physical-mouse, physical-memory,
mixed-device or production-latency certification. The native IPR Qt screenshot
has toolbar capture artifacts; its displayed image pixels are checked separately.
The measured allocation defect before the fix was 316,400 additional device bytes
per renamed field. Fixed warm allocation ranges are recorded below.

| Device | Warm tracked device bytes, minimum | Maximum |
| --- | ---: | ---: |
| CPU | 2,845,980 | 2,845,980 |
| RTX 4090 OptiX | 1,412,173,700 | 1,412,173,700 |
| RTX 3080 Ti OptiX | 883,664,780 | 883,664,780 |

Run `houdini_dense_volume_smoke.py`, `houdini_dense_volume_recovery_smoke.py`,
`houdini_dense_volume_cache_smoke.py` and `houdini_dense_volume_voxel_smoke.py`
through the prototype runner, or use the registered main verification suite.
The GUI pair is `houdini_dense_volume_gui_smoke.py` and
`houdini_dense_volume_native_gui_smoke.py`. Use one viewer at a time.

The following material evidence describes its earlier tested build. Its source
archives retain those results independently of the dense-volume milestone.

## Reproduce the checks

```powershell
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_volume_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/core_volume_material_smoke.py'
./scripts/run-hython.ps1 -HythonArguments 'tests/houdini_named_volume_regression.py'
./scripts/run-gui-test.ps1 -Test houdini_volume_material_ipr_smoke.py -Device CPU
```

The native test creates
`artifacts/stage3/materials/volumes/native-volume-materials.hiplc` with stashed
density, emission, temperature and velocity. The source VDB is deleted before
rendering. The fixture checks absorption against exp(-9/8), emission against
its independent path integral, field requirements, assignment precedence,
subnet closures, density ramps, animation, invalid transactions, warm tracked
allocations, save/load and three native batch frames.

An independently constructed matched-harness scene provides complete
absorption, emission and blackbody reference images without replaying the
plugin's graph or geometry capture. Actual GUI tests cover shader edits,
undo/redo, camera changes, reassignment, missing-field and closure errors,
rename recovery, shared viewers and normal shutdown on CPU and RTX 4090.
The aggregate run `artifacts/verification/20261010T183302253/summary.json`
passes 62 cases, finishing at 18:44:35 UTC on 2026-10-10. The new cases include
16 native groups, eight first-volume regressions and 16 GUI events per device,
plus three reference images and three native batch frames. Full-image maximum
RGBA errors are 0.0078125 for absorption, 0.005859375 for emission and
0.01953125 for blackbody; their corresponding mean errors are below 0.00018.

The earlier probes exposed named-density transmission near 0.99996 where
exp(-9/8), approximately 0.32465, was expected. Explicit Volume Info reads
worked, directing the investigation to volume classification rather than
field data. Host/kernel NanoVDB accessors agreed at the sampled coordinates.
The world-index collision explained the named-field failure; creating the
background-light object restored the expected integral. A later retained-
volume test exposed loss of native material references after failed capture.
Moving those identities into committed resources fixed that separate issue.

Two image-comparison setups also required correction: the independent camera
had to use the specified horizontal extent and image aspect, and GUI
blackbody comparisons had to respect its different active working space.
Alternating Density multipliers 1 and 1.1 compile to different instruction
sizes because Cycles folds multiplication by one. Warm memory comparisons
therefore compare the same compiled configuration across successive edits.

Production pyro sequences, additional grid formats, complete shader-port and
phase-function certification, volume field interpolation across time,
broader closed-mesh/combined-medium certification, displacement, texture inputs and
Solaris materials retain their own release gates.
