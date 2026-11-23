# Implementation notes on native rendering

These notes record how one part of native OBJ/SOP rendering was built: the contract it implements, the engine
patches it needs, the tests that check it and the limits that were known at the time. Many of them were written
while the work was in progress, so counts such as protocol versions, patch numbers or measured times are those
of their date; the code and the tests are the reference. For an overview start with the
[developer guide](../README.md) and the [architecture overview](../architecture-overview.md).

## Execution, scheduling and the worker

- [native-execution-policy.md](native-execution-policy.md): when interactive rendering refreshes, cancels or restarts a job.
- [native-workflow-policy.md](native-workflow-policy.md): which execution path interactive and final renders use, and why.
- [native-interactive-updates.md](native-interactive-updates.md): fixes to interactive rendering and the work that remains.
- [native-worker-prototype.md](native-worker-prototype.md): the persistent render worker and earlier scheduling experiments.
- [native-python-bindings.md](native-python-bindings.md): the `_cycles_houdini` Python module and its bindings.

## Scene capture, publication and lifetimes

- [native-async-capture.md](native-async-capture.md): asynchronous scene capture for interactive rendering.
- [native-capture-retirement.md](native-capture-retirement.md): releasing cancelled captures and closing cleanly.
- [native-geometry-ownership.md](native-geometry-ownership.md): who owns geometry before it is converted in the background.
- [native-scene-publication.md](native-scene-publication.md): swapping a prepared scene in and retiring the old one.
- [native-scene-lifecycle.md](native-scene-lifecycle.md): scene changes, opening a new file and callback lifetimes.
- [native-cooperative-acquisition.md](native-cooperative-acquisition.md): gathering materials and stylesheets without blocking Houdini.
- [native-stylesheet-acquisition.md](native-stylesheet-acquisition.md): material stylesheets as owned value pools.
- [native-test-fixture-inputs.md](native-test-fixture-inputs.md): inputs that several test cases share.

## Geometry

- [native-object-scenes.md](native-object-scenes.md): rendering several OBJ render SOPs and Houdini lights.
- [native-mixed-sops.md](native-mixed-sops.md): one render SOP that mixes meshes, soups, curves, points and volumes.
- [native-parametric-curves.md](native-parametric-curves.md): Bezier and NURBS curves.
- [native-packed-fragments.md](native-packed-fragments.md): packed fragments.
- [native-packed-instance-attributes.md](native-packed-instance-attributes.md): shader attributes on packed instances.
- [native-instance-attribute-motion.md](native-instance-attribute-motion.md): instance values that change during the shutter.
- [native-camera-motion.md](native-camera-motion.md): camera motion and recovery from unusable camera sources.
- [native-binding-volume-camera.md](native-binding-volume-camera.md): material bindings, tapered volumes and sampled camera intrinsics.
- [native-binding-expression-optics.md](native-binding-expression-optics.md): the earlier binding, expression and optics contracts.

## Meshes

- [native-mesh-payload-transport.md](native-mesh-payload-transport.md): how mesh data travels to the renderer.
- [native-mesh-field-updates.md](native-mesh-field-updates.md): updating single mesh fields instead of whole meshes.
- [native-mesh-sharing.md](native-mesh-sharing.md): one physical mesh shared by several material bindings.
- [native-mesh-family-scaling.md](native-mesh-family-scaling.md): shared metadata for large families of meshes.
- [native-mesh-islands.md](native-mesh-islands.md): Random Per Island and subdivision recovery.
- [native-mesh-pointiness.md](native-mesh-pointiness.md): the Pointiness output.
- [native-mesh-media.md](native-mesh-media.md): surface and volume in one material.
- [native-scene-query-displacement.md](native-scene-query-displacement.md): scene queries and true displacement.

## Materials, shaders and textures

- [native-material-extensions.md](native-material-extensions.md): how the Cycles shader nodes are registered as VOPs.
- [native-constant-attributes.md](native-constant-attributes.md): constant shader attributes and White Noise.
- [native-noise.md](native-noise.md): procedural noise.
- [native-ramp-interpolation.md](native-ramp-interpolation.md): interpolation of curve and colour ramps.
- [native-shader-curves.md](native-shader-curves.md): Float Curve, RGB Curves and Vector Curves.
- [native-color-ramp-rgba.md](native-color-ramp-rgba.md): Color Ramp with separate colour and alpha ramps.
- [native-blender-rgb-ramps.md](native-blender-rgb-ramps.md): Blender's RGB interpolation for Color Ramp.
- [native-hue-ramps.md](native-hue-ramps.md): Color Ramp hue modes.
- [native-texture-auto.md](native-texture-auto.md): Auto colour space for image textures.
- [native-cop-images.md](native-cop-images.md): COP images as texture inputs.
- [native-sky-source-recovery.md](native-sky-source-recovery.md): recovery from an unusable Sky Texture.
- [native-volume-materials.md](native-volume-materials.md): volume materials and fields.

## Colour and display

- [native-color.md](native-color.md): colour configuration and retained renders.
- [native-color-interop.md](native-color-interop.md): portable colour metadata of textures.
- [native-gpu-display.md](native-gpu-display.md): display conversion on the GPU in the viewer panel.
- [native-progressive-output.md](native-progressive-output.md): pass layout while a render is still refining.

## Lights and the light tree

- [native-light-linking.md](native-light-linking.md): light and shadow linking.
- [native-light-shaders.md](native-light-shaders.md): textured and IES lights.
- [native-retained-emitter-updates.md](native-retained-emitter-updates.md): updating mesh lights without rebuilding them.
- [native-cached-emitter-views.md](native-cached-emitter-views.md): borrowing cached triangle emitters.
- [native-light-tree-uploads.md](native-light-tree-uploads.md): keeping light-tree storage on the device.
- [native-light-tree-range-uploads.md](native-light-tree-range-uploads.md): uploading only the changed ranges of the light tree.
- [native-light-tree-compaction.md](native-light-tree-compaction.md): bounded working sets for large mesh lights.
- [native-light-tree-interior-reuse.md](native-light-tree-interior-reuse.md): reusing prepared interior rows.
- [native-light-tree-split-work.md](native-light-tree-split-work.md): less work when a new mesh-light hierarchy is built.
- [native-parallel-light-tree.md](native-parallel-light-tree.md): updating cached mesh lights in parallel.
- [native-mesh-light-energy-keys.md](native-mesh-light-energy-keys.md): exact energy keys for mesh lights.

## Checks, release and packaging

- [native-hardening-regressions.md](native-hardening-regressions.md): the focused native regression checks.
- [native-release-acceptance.md](native-release-acceptance.md): what a release candidate had to pass.
- [native-release-packaging.md](native-release-packaging.md): building the relocatable Windows package and installer.
