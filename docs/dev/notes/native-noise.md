# Native procedural noise

The authored `cycles::noise_texture::1` VOP exposes the pinned Cycles engine's
1D, 2D, 3D and 4D Perlin/fractal noise, Fac and Color outputs, five fractal types,
normalization, scale, detail, roughness, lacunarity, offset, gain and distortion.
Parameters evaluate at the native render time; W can animate 1D/4D noise.

The Houdini adapter gives an unconnected Vector **world-space position**, in
Houdini units. Blender's Generated attribute is not available in this native
geometry adapter. An explicit Geometry Position connection gives the same result;
UV Map and Mapping allow other coordinate choices. The default therefore moves
through the noise when the object moves. This authored Houdini default deliberately
differs from the pinned engine's Generated-coordinate default.

Connect Fac to Height on a Displacement node, connect its vector output to
Surface Output's Displacement port, and choose True or Both on the material
builder. Supply enough render vertices through SOP geometry or supported
renderer subdivision. Scale and Midlevel retain the documented scalar
displacement semantics. The courtyard example uses this path for dense ground
and rocks, with separate high-frequency noise driving Bump.

World-position dependencies are inserted before displacement resource analysis.
Their geometry variants include object transforms; an instance cannot accidentally
reuse another transform's world-dependent displaced vertices. Surface-only
noise does not require geometry reconversion. Existing deformation/displacement
limitations still apply across shutter samples.

`tests/houdini_noise_smoke.py` passes 45 groups on each of CPU and RTX 4090.
Forty scalar cases compare native VOP renders against direct pinned Cycles
kernel evaluation, bypassing graph conversion, material bindings and coordinate
extraction: four dimensions, five fractal types and normalized/raw settings.
Additional checks cover Color's scalar red channel, implicit/explicit coordinate
equivalence, spatial variation, subdivided true displacement, source ownership,
edit restoration, geometry retention and instance variants. This is not an
independent proof of the engine's noise algorithm, nor exhaustive certification
of all distortion and mapping combinations.

The richer native IPR example also verifies a promoted displacement edit, a
camera edit and a procedural SOP edit in both viewers. The versioned reflection
catalogue remains broader than the authored/validated node context; unsupported
engine nodes continue to produce explicit diagnostics.
