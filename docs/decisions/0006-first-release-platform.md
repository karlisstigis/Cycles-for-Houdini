# Target Windows SOP artists for the first public release

Accepted 2026-11-10. The first public release serves freelancers and small studios rendering OBJ/SOP scenes on Windows x64. CPU rendering is supported. The GPU target is NVIDIA. Focused Houdini build qualification uses CPU and OptiX. The owner-triggered cross-backend suite compares CPU, CUDA and OptiX. The Houdini edition targets are Indie, Core and FX. State the Apprentice limitation on the download page.

Qualification note, 2026-11-20. Local checks used Houdini Indie 22.0.466. Verify asset creation and scene save/reopen in a commercially licensed Core or FX session before claiming compatibility with that edition. [Release acceptance](../dev/notes/native-release-acceptance.md)

Blender is an optional dependency for references to objects and collections in a `.blend` file. An artist without Blender must be able to install the renderer and produce a first image from ordinary Houdini geometry. Requesting a Blender asset in that setup should identify the missing dependency at the asset.

The first release is free and initially unsigned. Publish the integration source, Cycles fork and installer together after reviewing both repositories’ histories, tags, release assets and third-party redistribution terms. Keep the private cache experiment out of public refs.

Record the NVIDIA driver used for qualification. The free release requires one driver version. Revisit coverage on a second machine when the first driver issue is reported. The nightly must never change the workstation driver. The maintainer starts the heavy renderer suites and the nightly explicitly. A green nightly is required before the first public release.
