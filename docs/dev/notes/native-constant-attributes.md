# Constant shader attributes and White Noise

Copy to Points can transfer one target-point value to every vertex of an
unpacked copy. Those equal values must remain equal when sampled by a shader.
Previously the triangle sampler's weighted sum changed their last floating-point
bits across a face. White Noise hashes float bits, making that small error a
visible pattern. Packed instance fields bypass that interpolation and stayed
flat. The source values captured from Houdini were already correct.

Patch `0048-constant-attribute-sampling.patch` preserves exactly equal components
when sampling triangle and curve attributes. Constant triangle components also
have exactly zero screen-space derivatives. Equal attribute samples retain their
exact value over the shutter. The comparison is exact, component by component;
varying components retain the preceding interpolation expression. Attribute
precision, White Noise's hash, geometry-position interpolation and smooth-normal
interpolation are unchanged. There is no artist setting or rounding requirement.

The upstream [triangle sampler](https://github.com/blender/blender/blob/main/intern/cycles/kernel/geom/triangle.h)
uses the same weighted-sum expression. This is a local Cycles sampling fix,
rather than a change to Houdini's Copy to Points or its attribute ownership.

## Verification, 2026-10-19

`artifacts/constant-attributes-evidence-20261019.json` indexes the source-audited
reports. The tested manifest is
`daf2d5dae9c7256b18e059de75d788c0294ca618990c94359e6330bdd0d1eb68`:
host `20261019T012244107`, worker protocol 45, 48 reproduced engine patches,
five compiled artifacts and six freshly rebuilt GPU kernels.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 42 checks and five ordered
  output frames. Checks include float/integer/two- and three-component fields,
  packed/unpacked equivalence, primitive and vertex fields, both subdivision
  schemes, a constant component of a varying vector, normal derivatives, hair,
  deformation motion, camera/object movement and field failure/repair.
- A separate analytic position-based gradient confirms that genuinely varying
  point fields still interpolate. Static per-copy colors must be exactly equal
  at every inspected interior pixel, not merely close in their average.
- Cycles Viewer and Houdini IPR each pass 12 live states with one viewer open.
  A 1,048,576-triangle prototype shared twelve times stays at one cook while
  the small copies switch packing, change fields, recover invalid data, and
  move the camera and object. Both close with zero tracked owned geometry and
  device allocations. These are Qt/HOM integration checks, not physical-input
  latency certification.
- The existing packed-field suite passes 100 checks and five frames on all
  three devices. Animated-field and tangent/normal/displacement suites pass on
  CPU and the 4090; the general named-attribute suite passes on CPU.

The initial exploratory CPU probe records one-ULP input changes and large hash
changes before the fix. One formal run failed because its analytic gradient
mask included the opaque background; that failed report and its source archive
are retained. The fixture was corrected to use a transparent background before
the accepted runs. No image tolerance was relaxed to make the hash checks pass.

A genuinely varying coordinate still produces varying White Noise. This fix
does not make nearby but distinct floats equivalent, or repair field variation
already authored into the geometry.

## Exact scalar output, 2026-10-19

The later hue-ramp tests found another last-bit change: Attribute carried a
scalar as three equal components, then averaged them to write Factor. Patch 55
preserves these exact equal components, including derivatives, while retaining
ordinary conversion for varying vectors. This also avoids repeated rounding of
already converted scalar outputs. It does not quantize or modify authored fields.

CPU and both OptiX devices pass exact float32 output checks over 170 values,
including ramp keys and their nearest normal float neighbors. The 42-check,
five-frame packed/unpacked hash/derivative/hair/motion regression passes again
on all three devices. Both live hue viewers pass 24 edits and repairs with a
heavy retained prototype. See [hue-ramp evidence and scope](native-hue-ramps.md).
