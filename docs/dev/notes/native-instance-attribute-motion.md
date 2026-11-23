# Shutter-sampled packed instance fields

Numeric fields on packed primitives and their containing owners can change during
an exposure. The adapter samples the fields requested by the active shader at the
ROP's shutter times. Cycles linearly interpolates their numeric values at ray time,
before evaluating the shader. Nonlinear shaders therefore see the interpolated
input; the renderer does not average already-shaded endpoint colors.

Scalar and one/two/three-component float or integer attributes and packed `Cd` use
the existing Attribute and Color Attribute nodes. Integers convert to shader float
precision. Neither an `id` nor a `name` attribute is required; duplicate values are
ordinary scene data. Prototype fields take precedence, followed by the nearest
packed primitive, single vertex, point and detail owner. An enclosing packed layer
is considered only when the nearer layer has no matching field.

## Timing and recovery

The requested-frame tuple is owned separately from the chronological shutter
samples. Odd sample counts from 3 through 129 are independent of the prototype's
deformation sample count. Exact constant sample sequences collapse to static
storage without changing their floating-point bits. ROP and object blur switches
control these samples. A motion-vector-only render does not acquire shader shutter
fields. Geometry Motion set to Velocity holds instance fields at the requested
frame and acquires no shutter roots for them (task M1, November 14, 2026: these
shutter cooks were the only SOP cooks of a velocity capture and replaced the
nominal detail, so the next capture re-sent every packed prototype). A missing
velocity field holds placement without disabling otherwise valid fields.

Only demanded source fields are read. Preserved Houdini geometry owners remain
inside the host capture/conversion lifetime; owned numeric values cross the worker
boundary. Material-motion captures can supply the same capture epoch's sample
owners. A later capture reacquires off-frame source values even when its nominal
data IDs and values are unchanged.

An unavailable, incompatible or non-finite off-frame value holds that affected
field at its valid requested-frame value. It does not discard the instance or
freeze independently valid camera, placement or prototype motion. The accepted
`geometry.shutter_instance_attribute` warning identifies the source, attribute and
fallback. A nominally invalid field follows ordinary local material recovery.
Warnings persist through reuse and clear after repair. Unusable off-frame packed
correspondence does not require the artist to add identity attributes.

## Sharing and displacement

Surface-only values stay on render objects and do not create a mesh for each
numeric value. Attribute-map descriptors retain each field's sample count, so
changes between 3/5/129 samples, incomplete holds and static values reach the
engine even when the number of requested attributes is unchanged.

A field contributing to true displacement can require distinct shape variants.
Compatible geometry, coordinate and field samples are evaluated on an exact common
grid of at most 129 samples. If that grid cannot represent the independent sampled
shader state safely, only the contributing instance field is held at its requested
frame during displacement evaluation. Surface shading retains its sampled field;
independent geometry and shader motion remain active. The
`motion.instance_attribute_displacement` diagnostic explains this limit and clears
after repair. No arbitrary shader or texture interpolation is invented.

## Exposure distribution

The pinned camera now gives an empty or exactly constant positive shutter curve
an exact identity inverse table over the full unit interval. This fixes a small
inherited CDF compression bias that became measurable with nonlinear animated
attributes. Authored nonuniform shutter curves retain the previous weighted CDF
path bit for bit; pixel-filter CDF behavior is unchanged. Uniform exposure tests
use an independent analytic integral, not the renderer's own lookup table.

Verification of this increment is recorded separately from earlier static-field
milestones in the packed-attribute guide. Automated tests exercise actual live
viewer edits and repairs; they are not physical-input or release certification.

## Current sampled field verification - October 21, 2026

The accepted runtime `hosts/20261020T234635239/resources` uses worker protocol **49** and **66** engine
patches. Its source-archived evidence (`artifacts/sampled-instance-overrides-final-evidence-20261021.json`) records **37** selected passing
native runs, including **28** feature runs, **528** live viewer states and **40**
ordered output frames. The feature checks cover CPU, RTX 4090 OptiX, RTX 3080 Ti
OptiX and both GPUs together, with one viewer open in each live workflow.

The verified scope includes scalar, integer-converted, vector and `Cd` shutter
inputs; prototype sharing; point/hair and nested owners; local holds and repairs;
independent motion; and supported sampled true displacement. Patch 65 supplies
the independent uniform-field descriptors; patch 66 corrects the uniform camera
shutter table while preserving authored nonuniform curves. Native ramp/curve
overrides and first-read channel recovery have separate cases in the same index.

Cycles Viewer can publish an early object pose preview without SOP acquisition.
Native IPR may apply its SOHO update before that preview runs. A later
authoritative object update may cook the requested-frame and shutter field SOPs;
retained heavy prototypes and geometry conversion counts are checked separately.
These programmatic checks do not certify physical-input latency, prolonged
physical memory, monitor changes or a public release.
