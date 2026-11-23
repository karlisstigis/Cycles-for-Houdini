# Native light and shadow linking

Restart Houdini to load the current matched host and worker. Native OBJ/SOP is
the supported workflow. Use **Cycles Tools > Add Object Tab** for selected geometry,
or **Add Native Light Tab** for a standard Houdini light. These tabs remain optional.
New Cycles Lights include their controls already.

In Houdini's **Light Linker**, choose **Light-Centric**, expand **obj** under
**Light Sources**, and select the Cycles Light by its scene name. Ctrl-click a
receiver or caster to toggle its link to the selected light.

For **Light Mask**, edit receivers under **Lit Objects**. The pane writes each
receiver's **Light Mask**, which intersects the emitter's **Illuminates**
selection. To make one Cycles Light illuminate only the fog, set **Illuminates**
on that light to `/obj/fog`. For **Shadow Mask**, edit blockers under **Shadow
Casters**. The pane edits **Shadow Casters** on the same named Cycles Light.

## Controls

- An object's **Light Mask** selects illuminating objects/lights. `*` keeps all.
  `/obj/key_light` selects a light and `* ^/obj/key_light` excludes it. Native paths,
  relative paths, wildcards, exclusions and `@bundle` references work. Existing
  native controls stay in their original tabs; absent controls appear in the
  optional Cycles **Linking** panel.
- An emitter's **Illuminates** selects receiving geometry OBJ roots. This
  intersects each receiver's Light Mask and category filter. It also works on
  emissive geometry. It does not hide the emitter from the camera.
- **Shadow Casters** selects which geometry OBJ roots block that emitter.
  Other objects remain visible. On a Cycles Light, the Light Linker's Shadow Mask
  edits this same setting. Standard Houdini lights use their native Shadow Mask.
  Shadow Selection can further restrict the eligible casters. Cast Shadow must be
  enabled.
- **Categories** supplies native tags separated by spaces or commas.
  **Light Selection** and **Shadow Selection** can filter those tags with `&`,
  `|`, `-tag`, `*`, `-*`, `+` and `-`. Empty means no category filter.
  Native category syntax has no grouping parentheses. A standard Houdini light
  normally starts with Shadow Selection `-noshadow`; that is an active filter.

For example, set a light's Illuminates to `/obj/courtyard_turbine` to restrict
its receivers. For category filtering, put `warm` in the light's Categories and
`warm` in the receiver's Light Selection. Categories are not object masks.
Packed children and mixed SOP components inherit their containing OBJ selection.
Different light links alone do not duplicate a shared prototype. No numeric ID
attributes are needed. Referenced bundle edits update IPR automatically.

## Recovery and limits

Malformed category expressions or tag parameter types omit the affected filtering
and report a local warning. Other masks remain valid. Irrelevant metadata stays
quiet; repairing or deleting the source clears its warning. Missing mask matches
mean an empty selection, not missing geometry. Source problems do not stop the
scene or the five-frame final sequence.

The engine supports 64 sets per domain, including unrestricted set zero. There
are 63 nondefault receiver sets and 63 nondefault blocker sets. Equal selections
share a set. Overflow groups use unrestricted linking and report affected roots.
These internal sets are managed automatically.

**World linking is unsupported in the pinned Cycles global background path.**
Requested world exclusions retain ordinary world illumination and occlusion and
report the limitation. `__world` can keep world lighting while filtering other
emitters; it cannot exclude the world. The native environment OBJ path also
identifies the selected world. Camera visibility is independent. No unsupported
world-link controls are added to the Environment tab.

## Try the courtyard

```powershell
.\scripts\launch-sop-showcase.ps1 -SceneFile .\artifacts\examples\sop-fx-courtyard\sop-fx-courtyard-light-linking.hiplc
```

Use one Cycles Viewer with `/out/cycles_sop_courtyard`.
`/obj/linking_cool_fill` illuminates only `/obj/courtyard_turbine`.
The turbine's Light Mask excludes `/obj/linking_warm_key`.
The warm key's Shadow Casters excludes the turbine while keeping the ground lit.
Set each control to `*` to compare. Adaptive turbine subdivision and the displaced
ground remain in this separate example; existing artist HIPs are preserved.
Linear EXRs and active-OCIO comparison PNGs are in the adjacent `light-linking/`
folder. This scene illustrates the controls; independent small fixtures establish
illumination correctness.

## Verification boundary

The 32-run matrix covers CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX linking images,
source repair, set exhaustion, packed sharing and five-frame final sequences.
Both viewers pass native parameter/bundle/category/camera workflows on CPU and
4090 with a million-triangle prototype shared by twelve instances. Link edits
retain mesh buffers and stable warm tracked device allocations in 100 alternating
updates per device. World exclusion is tested as a diagnosed limitation.

This does not certify physical mouse input, screen appearance, mixed-device
rendering, long-run physical memory or production latency budgets. See
`artifacts/light-linking-evidence-20261018.json`.

Native mask/category semantics: [SideFX light linking](https://www.sidefx.com/docs/houdini/render/lights.html).
The pinned engine commit is `a456b761034dda42c32eef9f4aae0fa5a5c9f604`;
`src/kernel/types.h` defines the set limit, and the global background path in
`src/kernel/integrator/shade_background.h` establishes the world-linking boundary.
