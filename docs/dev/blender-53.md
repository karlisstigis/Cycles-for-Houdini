# Cycles 5.3 in Houdini

## Glass

Open **Glass BSDF** and increase **Dispersion Scale** to separate the colors of transmitted light. **Abbe Number** controls the amount of wavelength variation for a given scale. Lower Abbe numbers give stronger separation. The default scale of zero preserves ordinary glass. Glass also accepts anisotropy, rotation and a tangent for directional roughness. On **Principled BSDF** use the corresponding dispersion inputs under transmission.

Objects with dielectric priorities can use different optical properties on each participating material. The renderer evaluates both media at the same sampled wavelength when it computes the relative IOR. For distance-dependent liquid color, continue using Volume Absorption.

## Interactive denoising

In the Cycles ROP’s **IPR** settings, enable **Denoising**, choose **DLSS** on a supported NVIDIA device, then select **DLSS Quality**. **Native (1x)** keeps the tracing and display resolutions equal. The other choices trace at a lower resolution and reconstruct Beauty at the Viewer’s requested size. Viewer zoom, region and resolution controls continue to set the display target. Inspect the effective denoiser in the Viewer when checking what ran.

DLSS uses motion guides between camera updates. For authored shutter motion blur or an explicit Motion pass, the Viewer currently uses a conventional denoiser to preserve the requested transport and pass. Shadow catchers require native tracing resolution. Disk rendering uses the final denoising settings.

A local final render suspends active interactive renders. After the last overlapping local job finishes or is cancelled, active previews can resume. A manually paused or stopped preview retains that choice.

Blender specifies NVIDIA driver 620 or newer for Ray Reconstruction. This Windows x64 candidate was tested with driver 621.14 on the RTX 4090 and RTX 3080 Ti. The render worker reports device support. The candidate also supports ordinary rendering on CPU. RTX Spark would require a separate platform build and qualification. [Blender 5.3 Cycles release notes](https://projects.blender.org/blender/blender-developer-docs/raw/branch/main/docs/release_notes/5.3/cycles.md)

## Gaussian splats

Import a Gaussian-splat PLY with a **File** SOP and append **Bake GSplats**. Use **Linearize Color** for a working-space Cd workflow and enable the spherical harmonic coefficients when the capture includes view-dependent color. The renderer recognizes the baked GS_Alpha attribute and consumes scale, orientation, opacity and color from the cooked geometry. Preserve these attributes when editing or packing splats.

When shutter deformation changes higher-order spherical harmonic coefficients, the renderer uses the center sample for those coefficients and reports the limitation. The remaining supported motion attributes still update. With the default splat shader, a splat exactly edge-on to an orthographic camera can have zero emitted radiance.

## Shader graph utilities

Boolean Math and Integer Math are available in the material graph. Switch and Index Switch select among inputs. A constant selector allows the translator to omit unselected branches, which is useful for branches with expensive or unavailable textures. Enable Output can gate a graph output, and Warning can report a constant authoring condition.

Blender’s string-node family and recursive closure authoring are deferred. This release targets Houdini OBJ/SOP. Solaris, OSL and a dedicated texture-bake ROP are outside its scope.

## Color configuration

The release checks use Houdini’s working color configuration. Importing the unmodified official Blender RC OCIO config into the Houdini worker can fail its identity check because serialization changes one AgX bound. The Blender comparison fixtures used an explicitly matched OCIO config and verified identical raw Blender reference pixels before and after serialization.
