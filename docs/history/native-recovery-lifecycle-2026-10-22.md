# Diagnostic emission, first-frame delivery and long-operation fixes

October 22, 2026 increment for native Windows OBJ/SOP rendering. Worker protocol
51 and the 68 engine patches are unchanged. Evidence:
the local candidate folder of `4c62fd76b5e9` (`qualification-evidence.json`, first
candidate) and that of `218b8ae19932` (viewer, builder and
installer follow-up; final reruns and package in
`artifacts/native-implementation-checkpoint.json`).

## Diagnostic shading never becomes light

The magenta diagnostic surface is a camera-only marker: an emission closure for
camera rays and a magenta diffuse closure for all other rays. Whole diagnostic
materials already disabled emission sampling. Recovered per-geometry bindings
(for example a texture material on a mesh without UVs or Cd, or a sparse-volume
material on a surface) did not, so the engine added every affected triangle to
the light tree. These bindings, and graph materials whose surface was recovered
to the marker, now use `EMISSION_SAMPLING_NONE`. The marker is recognized by its
graph structure, not by node names.

A missing image used to replace its node with constant magenta on every
downstream input. Displacement and normal/bump branches were already omitted.
Links from the unavailable branch into emission inputs (Emission color/strength,
Principled BSDF emission, Principled Volume emission/blackbody) are now gated by
*Is Camera Ray*: the magenta stays visible to the camera but emits no light into
the scene. Independent valid emission in the same material, BSDF inputs and
displacement are unchanged. The diagnostic action text states the gating.
Missing IES profiles substitute a neutral 1 and remain ungated.

`tests/houdini_diagnostic_emission_smoke.py` compares light-tree emitter counts
with a plain-magenta control and checks an unlit neighbouring card with no lights
and a black world, so reflected magenta cannot mask emission. On the pre-fix host
it reproduces 3,204 emitters instead of 3 and magenta light on the unlit card.

## First-frame delivery and diagnostics cost

The worker hands each frame over exactly once. On a session's first scene the
controller replaced the apply reply with the preview-control reply, dropping a
frame that was already complete; a short render then stayed "Done" without an
image. The apply reply is now accepted first. `RenderSession::status()` indexes
capture diagnostics once instead of scanning them for every recovered mesh, so
status cost grows linearly. With 5,000 diagnosed meshes a steady poll exchange
measures about 15 ms and every case completes in under 0.6 s
(`tests/houdini_diagnostics_scaling_profile.py`).

## Long worker operations and saving

Every worker exchange keeps a 60 s hang window. Initialization, scene apply,
pause/cancel, output writing and closing may wait for non-interruptible engine
work (kernel loading, uploads, BVH/light-tree builds, dicing); their deadline
extends while the worker's CPU time or I/O advances, up to one hour. A worker that
stops progressing is still killed after one window. The worker DLL-provider check
allows 30 s for a cold first launch. **Save linear EXR** no longer blocks Houdini:
the panel reports "Saving…", then "Saved…" or the failure. Transient panel
messages stay visible for a few seconds. Worker logs keep the newest 64.

A colour-policy change carried by a rejected update stays active by design: the
host only sends snapshots matching its current policy, so a rollback would
disagree with every later update. The retained scene re-renders on the next
accepted update.

## Viewer controls after scene changes

Opening or clearing a scene deletes the viewer's ROP before the panel is bound
again. Every panel control now treats a deleted ROP as "no ROP" instead of
raising `hou.ObjectWasDeleted`. This happened, for example, when the editable
Resolution field lost focus to the file dialog. Leaving that field unchanged
no longer restarts rendering. Switching **Interactive/Final** always starts a fresh
render with the selected settings, even when both modes resolve to identical
values or Auto update is off; Stop and Pause keep their meaning
(`tests/houdini_panel_scene_replacement_smoke.py`).

## Viewer follows the Houdini theme

Houdini 22's Edit Theme presets paint the native UI from theme roles that the
older colour-scheme entries do not carry. The viewer now uses the same roles the
native style uses for the same parts: background and toolbars, the empty canvas
surround, fields, buttons and dropdowns, menus and selection, progress, text,
and checked/pressed buttons (`checkedSurface`, `pressed`). Builds without the
theme engine fall back to the scheme entries. Houdini announces a theme change by
replacing the application palette; the viewer re-reads its theme only then, not
on its quarter-second monitor/DPI check.

## Cycles builders

Cycles Material and Volume Material keep Houdini's default node colour and
World Material is blue (74, 144, 226); shader nodes keep Blender's category
colours. A new builder places its default shader left and its output right,
tops aligned, one node spacing apart. Inside the builders the Tab menu offers
only compatible Cycles nodes; at `/mat` every VOP type stays available, as for
Karma and MaterialX. Houdini ignores a Tab-menu mask on `/mat` itself (checked
in the live Tab menu) and hiding node types hides them inside builders too.

## ROP Resolution

Width and Height were two scalar parameters, each with a slider, and a separate
preset menu below them. Resolution is now one integer vector beside a mini menu of
Houdini's standard resolutions, as on Houdini's own render nodes. There is no
compatibility parameter or load-time migration; saved scenes are converted once
(see [current schema](../dev/current-development-schema.md)).

## World volume sampling

A World Material volume used Cycles' shader default, Multiple Importance, while
Blender's world default is Equiangular. Scene > Environment now has Volume Sampling
(default Equiangular), carried to the worker with the world settings (worker
protocol 52).

## Showcase launcher crash files

The launcher opened the scene with a backslash path, so `$HIPNAME` became the
whole path and Houdini could not write its crash scene and log. It now opens the
scene with forward slashes.

## Verification boundary

The 301-case combined matrix (CPU, RTX 4090 OptiX, RTX 3080 Ti OptiX, mixed
devices; 105 GUI cases with live viewers) passed on build `668ff505`. Candidate
`4c62fd76` has identical sources; its binaries differ only in PE timestamp fields
and its HDAs only in embedded creation times (`equivalence-668ff505.json`). On
`4c62fd76` itself the package builds byte-identically, isolated
install/uninstall/recovery passes, all five relocated checks pass, and a
runtime subset including the response benchmark passes. Warm 1M-triangle
960x540 p95: camera 52/60 ms, transform 81/80 ms, material 149/155 ms (RTX
4090/3080 Ti), with no camera-only geometry conversion. These are programmatic
edits to Qt paint; physical input, prolonged memory, physical monitor changes,
clean-machine installation and cold OptiX caches remain uncertified.

The follow-up candidate `218b8ae1` ran the same 301 cases plus the scene-change
and builder colour/layout smokes (305); 302 passed. One case passed but its
evidence-folder rename hit a transient Windows lock twice (passed on rerun). The
DPI test's new theme helper was shadowed by a local of the same name; once fixed,
its toolbar-state check showed that segmented button groups (transport, channels,
zoom) took the theme's button colour, while the mock-up and test keep their
backdrop at the toolbar tone with a border. Segments now use the toolbar colour.
The CPU GUI transform-response case measured 93 of the required 100 edits during
the gesture; its 4090 run passed with 118 against 187 before. That test loads the
artist's live `Test.hiplc`, which had since been saved with other viewport
lighting (scene lights, headlight off), a re-parented light and render-setting
changes; the extra time per edit was outside every instrumented Cycles call,
pointing to Houdini's own redraw. Like the transform profile, the test now accepts
`CYCLES_HOUDINI_PROFILE_SCENE`; qualification runs it on the frozen scene the
matrix first passed with (`Test-qualified-7b39b4e7.hiplc`). A planned diagnostic
run on the live scene received an empty scene path and is not evidence. The package builds byte-identically, isolated
install/uninstall/recovery and all relocated checks pass, and the Windows
installer passes its end-to-end test (see
[packaging](../dev/notes/native-release-packaging.md#windows-installer)).
