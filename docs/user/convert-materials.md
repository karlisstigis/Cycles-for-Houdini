# Convert Houdini materials to Cycles

Right-click a **Principled Shader**, a **MaterialX Standard Surface**, an **OpenPBR Surface**, or a material builder that holds one (Karma Material Builder, MaterialX Builder), then choose **Cycles > Convert to Cycles Material**. With several of them selected, each one is converted.

Each source gets a new Cycles Material beside it, named `<source>_cycles`. The source stays as it is and no object is reassigned: point your objects' Material at the new node when you are ready. Converting again makes another copy (`_cycles1`).

What could not be carried over is never guessed silently. The new material's comment (shown in the network editor) lists it under **Not carried** (left out, with the reason) and **Approximated** (carried, but Cycles behaves a little differently). The status bar says which materials have such a note. A material without a comment converted completely.

Scripts use the same function:

```python
from cycles_houdini import convert
for result in convert.convert([hou.node("/mat/principledshader1")]):
    print(result.material.path(), result.message)
```

Textures keep their file paths exactly as written, including `$HIP`, `$JOB` and `<UDIM>`; Mantra-style `%(UDIM)d` becomes `<UDIM>`. Colour textures keep their colour space; single-channel, normal, bump and displacement maps read as **Data**.

## Principled Shader

Every texture is multiplied by its parameter, as Houdini does. Colour textures are tinted by `lerp(white, colour, Tint Intensity)`. The texture **Channel** (Luminance, Red, Green, Blue) picks the channel. **UV Transform** becomes one Mapping node of type Texture, as Houdini divides by Scale (Scale 3 makes the texture three times larger); rotation is converted to radians.

| Houdini | Cycles Principled BSDF |
| --- | --- |
| Base Color × Albedo Multiplier (+ texture) | Base Color |
| Use Point Color | × Attribute `Cd` (white where the geometry has no `Cd`) |
| Metallic, Roughness, IOR (+ textures) | Metallic, Roughness, IOR |
| Reflectivity | IOR Level = Reflectivity × 0.5 (1 → 0.5, Cycles' neutral) |
| Reflect Tint | Specular Tint = mix(white, Base Color, Reflect Tint) |
| Anisotropy, Anisotropy Direction | Anisotropic, Rotation, with a UV Tangent; negative anisotropy turns a quarter |
| Coat, Coat Roughness | Coat Weight, Coat Roughness |
| Transparency | Transmission Weight |
| Transmission Color + At Distance | Volume Absorption on the Volume output (the colour is reached after that distance) |
| Subsurface, Subsurface Distance, Scattering Phase | Subsurface Weight, Scale (radius white), Anisotropy; Random Walk |
| Sheen, Sheen Tint | Sheen Weight, Sheen Tint = mix(white, Base Color, Sheen Tint) |
| Emission Intensity, Emission Color (+ texture) | Emission Strength, Emission Color |
| Opacity Scale × Opacity Color (+ texture), Use Texture Alpha | Alpha |
| Alpha Mode Opaque / Mask + Alpha Cutoff | Alpha 1 / Alpha > cutoff |
| Bump & Normals: Normal | Normal Map (tangent, object or world; Effect Scale → Strength; Flip X/Y and the −1..1 range are re-encoded) |
| Bump & Normals: Bump | Bump (Effect Scale → Distance) |
| Coat Bump & Normals / shared base normals | Coat Normal |
| Rounded Edges | Bevel |
| Texture Displacement (Offset, Effect Scale) | Displacement (Midlevel = −Offset, Scale) or Vector Displacement; True Displacements → material Displacement Method *Both* or *True* |

Not carried, and reported when used: Dispersion, Surface Priority (use the object's Cycles *Dielectric Priority*), Fake Caustics, Use Packed Color/Alpha, Transmission Color, At Distance, Reflect Tint and Sheen Tint *textures*, Subsurface Color texture, Occlusion texture, image planes, Input and Noise displacement, inputs wired into the shader, and *Emission Illuminates Objects* off. Approximated: Subsurface Color (Cycles scatters with Base Color), Sheen (Cycles uses a microfibre sheen), single-scattering SSS mode, a coloured Opacity Color (its average is used), the Rec. 709 texture space (Auto is used), animated parameters (the value at the current frame is copied).

Not carried and not reported, because they do not change the look: texture filter type and width. Use Point Alpha is not carried: an `Alpha` attribute on the geometry would make every converted surface transparent for Cycles' shadow rays; connect an Attribute node to Alpha by hand when you need it.

## MaterialX Standard Surface

| Standard Surface | Cycles Principled BSDF |
| --- | --- |
| base × base_color | Base Color |
| diffuse_roughness, metalness | Diffuse Roughness, Metallic |
| specular | IOR Level = specular × 0.5 |
| specular_color, specular_roughness, specular_IOR | Specular Tint, Roughness, IOR |
| specular_anisotropy, specular_rotation | Anisotropic, Rotation (UV Tangent) |
| transmission, transmission_color, transmission_depth | Transmission Weight; with depth > 0 a Volume Absorption |
| subsurface, subsurface_radius, subsurface_scale, subsurface_anisotropy | Subsurface Weight, Radius, Scale, Anisotropy |
| sheen, sheen_color, sheen_roughness | Sheen Weight, Tint, Roughness |
| coat, coat_color, coat_roughness, coat_IOR, coat_normal | Coat Weight, Tint, Roughness, IOR, Normal |
| thin_film_thickness (nm), thin_film_IOR | Thin Film Thickness, IOR |
| emission, emission_color | Emission Strength, Color |
| opacity | Alpha (a coloured opacity uses its average and says so) |
| thin_walled, normal | Thin Wall, Normal |
| displacement shader (scalar) | Displacement (Midlevel 0); method *Both* |

Cycles tints transmission and subsurface with Base Color. With transmission 1 (or subsurface 1) on a non-metal, the converter uses transmission_color (or subsurface_color) as Base Color, which is exact; partial mixes with a different colour are reported. Not carried, reported when set: transmission_scatter and its anisotropy, transmission_dispersion, transmission_extra_roughness, coat_anisotropy/rotation, coat_affect_color/roughness, a connected tangent, vector displacement.

## OpenPBR Surface

As Standard Surface, with: base_weight, base_metalness, base_diffuse_roughness; specular_weight (× 0.5 → IOR Level); transmission_dispersion_scale and abbe_number → Dispersion Scale and Abbe Number; subsurface_radius → Scale and subsurface_radius_scale → Radius; fuzz_weight/color/roughness → Sheen; thin_film_thickness in micrometres → nanometres (thin_film_weight other than 0 or 1 is reported); emission_luminance → Emission Strength; geometry_opacity, geometry_thin_walled, geometry_normal, geometry_coat_normal. Reported: specular_roughness_anisotropy (different curve), coat_darkening other than 1, coat_roughness_anisotropy.

## MaterialX nodes upstream

| MaterialX | Cycles |
| --- | --- |
| image | Image Texture (address mode → Extension, filter → Interpolation, colour space kept; float images read their first channel as Data) |
| tiledimage | Image Texture + Mapping (tiling, offset, real-world sizes) |
| texcoord index n / geompropvalue | UV Map `uv`, `uv2`, ... |
| normalmap | Normal Map (tangent; a scale other than 1 is reported as approximated) |
| bump | Bump (scale → Distance) |
| multiply, add, subtract, divide, power, mix, invert | Math (values) or Mix Color (colours); constants are folded |
| clamp, remap, range | Clamp, Map Range, Vector Map Range (+ Gamma for range) |
| contrast, colorcorrect | Brightness/Contrast, Hue/Saturation/Value, Gamma, Mix Color (colorcorrect saturation is reported) |
| hsvadjust | Hue/Saturation/Value |
| separate3c, extract (image alpha too), constant, dot | Separate Color, image Alpha, folded values |

Any other node (noise, ramps, switches, ...) leaves that surface input at the surface's own value and is listed under Not carried.
