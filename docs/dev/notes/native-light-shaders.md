# Native textured and IES lights

Restart Houdini to load the current versioned host. Create a light from
**Cycles Tools**, then open **Cycles Light > Emission**. Standard Houdini lights
can receive the optional Cycles Light tab through Cycles Tools; native transforms
and other renderer controls remain accessible.

- **Color Texture** accepts a file or owned `op:` COP image. Choose its input
  interpretation from Houdini's active OCIO configuration. It multiplies the
  native Color and Power. Area coordinates map the full width/height to 0..1;
  Point, Spot and Sun use local outgoing equirectangular coordinates. Native
  rectangular/disk Area Map uses this adapter too. Other native projected maps
  remain diagnosed as unsupported.
- **IES Profile** uses the emitter's local -Z axis and follows parent transforms.
  The engine converts file photometry to radiometric values. **Power 1 preserves
  that file-derived scale**; larger powers multiply it. The profile is not
  normalized to a prescribed total power. Current input accepts TILT=NONE only.
- **Emission Material** selects a Cycles Material with Emission or a color output,
  plus supported color/procedural/utility nodes. Cycles Tools can create an
  Emission Material. Assigning it overrides the texture/profile shortcuts, which
  become disabled. Native Color/Power still multiply its emission. BSDFs are not
  analytic-light emission shaders; an unsupported assignment uses the base light
  and reports a warning. Independent volume/displacement outputs are omitted.

Missing or invalid color textures and IES profiles omit only the affected
modulation. A valid profile survives a broken color texture and vice versa.
The native light remains useful without introducing diagnostic magenta lighting.
File-only repairs, upstream COP changes and material repairs update IPR and clear
warnings. Dormant shortcut paths are not loaded while Emission Material is set.
Lights do not blur over the shutter, as in Blender: an animated or moving light,
including an animated COP texture, renders at the requested frame. An emissive
mesh does blur.

IES text is bounded and validated before it reaches the engine parser. The
supported path retains owned immutable text, shares live profile payloads and
releases them with their last owner. Patch 0039 avoids permanently interning whole
profiles. Lookup records are bounded; this focused lifetime test is not a full
production memory certificate. Malformed schema, ownership and worker messages
remain explicit failures.

## Try the courtyard

```powershell
.\scripts\launch-sop-showcase.ps1 -SceneFile .\artifacts\examples\sop-fx-courtyard\sop-fx-courtyard-light-shaders.hiplc
```

Open one Cycles Viewer using `/out/cycles_sop_courtyard`. Edit `/obj/ies_downlight`
and `/obj/textured_area`. Their Emission panels use authored local assets in the
adjacent `light-shaders/` folder. Rotate the IES light, replace its profile or
replace the area texture while IPR runs. The existing adaptive turbine and
displaced ground remain in this separate example. Original courtyard HIPs are
preserved. Comparison EXRs and active-OCIO PNGs are beside the assets.

## Verification boundary

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX pass independent point-light photometry,
visible area-radiance/coordinate tests, local resource repairs, owned COP images
and five-frame final sequences. CPU and 4090 Qt/HOM workflows use one viewer with
a 524,288-polygon prototype shared by 12 instances. Edits retain two geometry
resources and one worker. Physical mouse handling, screen appearance and broad
production latency/memory/device certification remain separate. Native object/light
linking now has [its own guide and verification boundary](native-light-linking.md). Evidence: `artifacts/light-shaders-evidence-20261018.json`.
The empty material reference action in GUI automation uses HOM Revert to Defaults;
synthetic delete-key clearing is not certified by this run.
