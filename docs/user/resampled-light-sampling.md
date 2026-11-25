# Resampled Light Sampling

On the Cycles ROP, open **Rendering → Sampling → Light Sampling** and enable
**Resampled Light Sampling**. Compare renders at the same render time to see
whether it reduces direct-lighting noise in your scene.

**Resampled Light Sampling** is optional and selective. At a primary shading point
made entirely of sharp reflective microfacet lobes, it retains standard Cycles
light sampling and the normal reflected-ray light contribution. Both microfacet
alpha axes must be at most 0.01 (isotropic artist roughness about 0.1 or below).
Anisotropic lobes with a broader axis, diffuse/subsurface/transmission mixtures,
and later bounces keep the existing RLS strategy. The decision uses the evaluated
closures, including textures, and is shared by direct sampling and the forward
MIS weight; no transport is discarded. Huang hair retains its separate native
fallback. RLS can improve direct lighting but is not a universal speed or noise win.
