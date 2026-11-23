# Subdivision source audit

The native subdivision slice now implements the audited construction,
ownership and update policies below. Current controls, evidence and
remaining limitations are in [native subdivision](../user/native-subdivision.md).

## Preserve the control mesh

The non-subdivided HDK converter copies the GU_Detail and calls `convex(3)`
before reading triangles. Subdivision preserves the original faces. Supplying
the resulting triangle mesh as the control mesh would change a quad's
limit surface and lose its original face-varying interpolation domain.

Owned coarse-face indices, face sizes, host primitive/subface provenance,
corner attributes and original-face bindings are implemented. Polygon soups
use their explicit face/vertex iterator. Their six separate control faces
are checked against the original rounded cube.

Geometry data IDs remain the cache trigger. Subdivision settings participate
in the resource key; crease/corner attribute IDs and unsupported override
state participate in invalidation. A setting edit cannot reuse a mesh merely
because its position data ID is unchanged.

## Pinned engine construction

The inspected interface uses `Mesh::resize_subd_faces`; there is no legacy
`add_subd_face` call. The official standalone application's construction in
`third_party/cycles/src/app/cycles_xml.cpp` provides the geometry layout:

1. Resize mesh vertices with zero render triangles.
2. Resize coarse faces and corners.
3. Add/resize `ATTR_STD_POSITION` in `subd_attributes` and copy coarse P.
4. Fill face corners, start/count, shader, smooth and PTex offsets.
5. Set subdivision and dicing parameters and tag their sockets.

`Mesh::tessellate` generates render vertices/triangles and interpolates
coarse attributes. Catmull–Clark uses OpenSubdiv; Linear uses bilinear
patches, splitting n-gons into quads. Both paths need independent coarse
fixtures. Triangle equivalence alone cannot prove that subdivision is
correct.

The standalone XML UV-import loop was not copied into the adapter. In the
inspected revision it requests packed float3 storage for the standard UV
attribute; the current native mesh adapter correctly uses float2. Confirm
the coarse attribute's actual TypeDesc/element size and write matching
storage. Coarse UV seams, bilinear/smooth Cd and an analytic continuous
color field now have explicit image tests. The audit additionally found
that smooth float3 face-varying merging used an unpacked stride and its
evaluation was limited to float2. Patch 0009 corrects packed-color merging
and enables the existing smooth evaluator for packed float3 attributes.

## Dicing and ownership

The engine exposes **object** and **pixel** adaptive spaces. Pixel mode uses
a camera and object-to-world transform. Shared packed prototypes at
different scales or distances therefore need a deliberate dicing policy;
displacement compatibility alone does not establish subdivision sharing.
Object-space variants share compatible state; pixel-space variants include
the centre instance transform. Camera changes redice only pixel variants,
and identical supplied camera state is retained. Rotated nonuniform curved
geometry under perspective matches an independently baked equivalent.

`EdgeDice::eval_projected` appeared to omit the object transform during the
audit. Its only scale-factor call is inside a disabled `#if 0` branch in
this revision; active edge splitting uses `DiagSplit::to_world`, which
applies the transform. No speculative patch was made to inactive code.

Owned snapshots now include coarse topology and attributes. Re-tessellation
and displacement start from that immutable mesh. Applying polygon seam
splitting before Catmull–Clark would change its limit surface. Patch 0008's
optional post-tessellation hook prepares render-vertex seams instead, with
the native two-material offset fixture checking both connected controls
and separate displacement.

Generated sizes are published at that post-tessellation hook under a lock,
so status never reads changing mesh arrays. Payload estimates exclude
temporary OpenSubdiv refinement data and subsequent tangent generation.
The triangle guard stops before an excessive interior grid allocation;
cancel checks cover splitting/dicing/interpolation. OpenSubdiv's refiner
construction is not internally cancellable in this slice. Its peak memory,
latency and larger FX acceptance remain architectural requirements.

## Motion and creases

The deformation converter now compares original oriented coarse faces,
bindings and crease state across stable point identities. Coarse motion
positions are evaluated at each supported sample; authored Linear normals
retain their correspondence. Changing faces/creases fail before commit.
Normal-dependent displacement still needs its separately tracked
per-shutter evaluation.

The pinned OpenSubdiv adapter multiplies Cycles edge crease weights by its
`CREASE_SCALE`. Host sharpness is divided by 10 before that adapter multiplies
it back. Separate sharpness-one independent refinement and sharpness-ten
extent tests verify the mapping. Primitive fallback, vertex crease removal,
boundary policies and face-varying controls have a saved native fixture.

## Initial exit evidence

Native, independent harness and actual IPR/shared-panel checks now cover
these small fixtures. A finite uniform reference can shift camera samples
across discontinuous color seams. The reference uses six levels and 64
samples, retains the original maximum-error tolerance and also checks mean
error. An independent analytic affine field avoids that seam ambiguity.
Increasing engine isolation depth did not improve the result and was
reverted. Larger latency and physical-memory gates remain open.
