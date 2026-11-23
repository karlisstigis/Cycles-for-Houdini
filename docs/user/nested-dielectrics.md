# Nested dielectrics

**Nested Dielectrics** (Cycles ROP → Rendering → Light Paths) renders overlapping refractive objects as nested media. Typical cases are liquid in a glass, ice in a drink and air bubbles in water. Each object has a **Dielectric Priority** (object Cycles tab → Shading). Where objects overlap, the higher priority defines the medium and the other object's surface is ignored inside the overlap. Real interfaces refract and reflect with the IOR relative to the medium on the other side, for example glass against water instead of glass against air. The toggle is off by default, and off renders exactly like standard Cycles.

## Setting up a scene

- Model containers and contents as **closed meshes that overlap slightly**. Don't place faces exactly on top of each other. A liquid surface should reach a little into the glass wall, and the glass gets the higher priority.
- Give every participating object a priority. The higher priority wins in the overlap. Equal priorities form ordinary interfaces, so a scene left at 0 renders as before. Negative values are allowed.
- **Supported materials:** Glass BSDF, Refraction BSDF and Principled BSDF with Transmission. A hand-built Glossy + Refraction mix driven by Fresnel or Layer Weight is not nested-aware. Use Glass or Principled instead.
- Thin-walled Principled, subsurface, holdout and shadow-catcher objects never participate.

## Packed instances

Packed primitives inherit the priority of their object. To vary it per instance, give the packed primitives (or their points) an integer attribute `cycles_dielectric_priority`. With **Copy to Points** and **Pack and Instance** on, put the attribute on the template points and list `cycles_dielectric_priority` on the node's **Attributes** tab, so it is copied onto the packed instances. Point instancing through `s@instance` works the same way: a single-object prototype uses the instancing object's priority, while a subnet prototype preserves the priority of each member. A point attribute `cycles_dielectric_priority` on the instancing points overrides the inherited priority; an attribute on a packed primitive closer to the geometry takes precedence. An attribute inside the geometry itself is a shader attribute, not a priority (see point-instancing.md).

## Limitations

- An IOR driven by a texture is taken **where the ray enters** the object.
- The **coat** layer of Principled BSDF stays relative to air for now.
- OSL shaders aren't part of this build.
- MNEE shadow caustics inside nested media fall back to standard sampling: still correct, but noisier.
- Closed meshes are required. A missing face gives a local artefact.
- At most 31 nested media per path.

## Warnings

Warnings appear in the viewer status line (hover for details), in the Houdini console and in the final-render log, once per render:

- *Nested Dielectrics: medium could not be determined for N camera rays (probe limit). Check for very dense geometry above the camera.* The camera sits inside participating objects with more boundaries above it than the start-up probe can follow. Those rays start in the wrong medium.
- *Nested Dielectrics: more participating objects overlap than can be tracked (N media); the lowest-priority ones are ignored at those points.* More than 31 participating objects overlap in one place.

## GPU notes

Turning Nested Dielectrics on adds device memory for the larger path state, about 160 MB at default settings on an RTX 3080 Ti. The memory stays allocated after the toggle is switched off, until the render session restarts. GPU renders are not bit-identical between sessions in general, and the variation is somewhat larger after the feature has been on in the same session.
