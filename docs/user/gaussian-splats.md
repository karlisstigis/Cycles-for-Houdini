# Gaussian splats

Cycles for Houdini captures loose points and particle primitives as native Cycles
Gaussian splats on CPU and OptiX. Install the Cycles object properties, then use
**Cycles > Points > Render Mode**:

- **Auto** (default) detects `f_dc`/`f_dc_0`, `GS_Alpha`, or `GS_SPH_R` point attributes.
  Ordinary points continue to render as spheres.
- **Spheres** explicitly uses the existing sphere-point renderer and `pscale` radius.
- **Gaussian Splats** requires valid splat attributes, even without an Auto marker.

The Render Points toggle applies to both spheres and splats. Packed children
inherit the source object's mode. Point instances use the prototype object's
mode, including nested prototypes. Different interpretations of one SOP have
separate geometry cache keys.

## Original 3DGS PLY attributes

The SOP must provide float point attributes in either grouped tuple form or
expanded component form. Position comes from `P`.

| Attribute | Tuple size | Meaning |
| --- | --- | --- |
| `f_dc` or `f_dc_0` through `f_dc_2` | 3 or 3 scalars | Original RGB spherical harmonics L0 coefficients |
| `scale` or `scale_0` through `scale_2` | 3 or 3 scalars | Natural logarithm of three Gaussian standard deviations |
| `rot` or `rot_0` through `rot_3` | 4 or 4 scalars | Quaternion in **w,x,y,z** order; normalized during capture |
| `opacity` | 1 | Logit; converted with a numerically stable sigmoid |
| `f_rest` or `f_rest_0` through `f_rest_44` (optional) | 45 or 45 scalars | Degree 1–3 SH, channel-major: 15 red, then 15 green, then 15 blue |

The raw format uses standard deviations after exponentiation, independently of
the sphere fallback radius and Houdini `pscale`. Scale must be finite and
strictly positive after conversion. Quaternions must be finite and nonzero.
Missing tuple components, invalid values, and incomplete detected splats produce
an explicit capture error; they do not silently become sphere points.

## Bake GSplats attributes

Prepared Houdini geometry is recognized by `GS_Alpha` (final opacity in [0,1]).
`scale` contains three nonnegative standard deviations, and `orient` is a Houdini
**x,y,z,w** quaternion. Optional scalar `pscale` multiplies the standard deviations;
zero scale or `pscale` is supported. Missing prepared `scale`, `orient`, `pscale`,
and `Cd` retain defaults of unit axes, identity orientation, 1, and white.
Prepared fields preserve Houdini's point, containing-particle primitive, then
detail attribute precedence. Prepared attributes take precedence when Bake
retains the original PLY attributes, so later scale/orientation edits take effect.

For view-dependent radiance, supply **all three** `GS_SPH_R`, `GS_SPH_G`, and
`GS_SPH_B` float tuples of size 16. Each contains original, unnormalized SH
coefficients, starting with L0. Enable **Compute SPH Coefficients** on Bake GSplats
to generate these fields. Their values retain their original meaning with either
setting of Bake's Linearize Color control. The integration converts channel-major
storage into Cycles' RGB-interleaved coefficients; it does not apply an extra SH
normalization or reinterpret linear `Cd` as a coefficient.

When `restorient` is present, its Houdini quaternion records the orientation at
which the SH was baked. Rotating `orient` relative to `restorient` rotates the SH
basis, matching Houdini's prepared-splat convention. Object and packed instance
transforms are handled independently by Cycles.

Without `GS_SPH_*`, prepared `Cd` is scene-linear color in the current Houdini
OCIO working space, matching Bake GSplats with **Linearize Color enabled**.
The owned runtime color configuration converts it to the engine's radiance
encoding. For Bake output with **Linearize Color disabled**, set the integer
detail attribute `cycles_gsplat_srgb` to 1. This explicitly marks `Cd` as sRGB;
capture derives its original L0 coefficient `(sRGB - 0.5) / 0.28209479177387814`.
The marker is ignored when `GS_SPH_*` coefficients supply the radiance.

## Shading and current limits

Unassigned splats use Cycles' intrinsic radiance emission material. Original SH
evaluation computes `0.28209479177387814 * L0 + rest + 0.5`, then converts from
sRGB to linear Rec.709 and into the render working space. Explicit object,
primitive, or point materials retain normal material assignment behavior.
The public `geometry_request(..., point_render_mode='auto')` API accepts
`auto`, `spheres`, and `gaussian_splats`; its default material selects intrinsic
emission for splats.

Cycles stores degree 1–3 SH in its native packed representation, quantizing
coefficients to its supported range of [-1,1]. L0 remains float. There is no
support for SH above degree 3. This is a path-traced Gaussian primitive renderer;
sampling and visibility can differ from a viewport's sorted raster blend.

Object and instance transform blur, velocity motion, and deformation sampling
remain available. Deformation capture samples positions, opacity, scale,
orientation, and DC radiance together. The pinned Cycles engine holds higher SH
bands at the shutter center; changing those coefficients or their `restorient`
rotation across the shutter produces `geometry.gsplat_sh_motion` while retaining
the supported motion channels. Ordinary sphere-point and curve motion behavior
is unchanged. Edits to consumed raw and prepared attributes, including the sRGB
detail marker, participate in geometry fingerprints and cache signatures.
