# Native camera motion and source recovery

The October 21 binding/volume/camera increment extends these contracts with protocol 51 continuous intrinsics and complete shifted-shutter poses. See [binding, tapered-volume and camera coverage](native-binding-volume-camera.md). Earlier host-specific results retain their own scope.

Current projection coverage is described in [Panoramic and fisheye cameras](#panoramic-and-fisheye-cameras).
The earlier sections retain their own tested hosts and development scope.

## Current continuous depth of field

Worker protocol 50 and engine patch 0067 carry aperture size, focus distance,
aperture rotation and aperture ratio through odd 3..129 chronological shutter
samples. Requested-frame optics remain separate, including shifted shutters.
Native capture and SOHO use the same source resolver. Camera-only edits retain
the existing geometry/cook/upload policy.

Invalid requested-frame DOF renders a pinhole camera with `camera.invalid_dof`.
Invalid shutter-only DOF holds requested-frame optics with `camera.shutter_dof`.
Changing blade count holds the requested count while retaining valid continuous
optics. Independent camera/object and supported projection motion survive.
Disabled DOF ignores unused controls. Essential projection/transform configuration
and malformed engine or worker data remain strict failures.

The later protocol 51 adapter adds continuous orthographic width, clipping and
applicable panoramic lens controls. Discrete projection/sensor-fit changes hold
requested values; stereo/custom lenses remain outside this adapter. The earlier sections' statement that all animated DOF is held applies
to their historical runtime. See [current contracts and verification scope](native-binding-expression-optics.md).

## Historical perspective-motion implementation

Host `20261018T150043676` adds perspective zoom motion and local camera-source
recovery to the native OBJ/SOP integration. Houdini's camera parameters supply
focal length, aperture, clipping and transforms. The optional Cycles camera tab
supplies depth-of-field controls; the ROP retains its global DOF override.

## Supported sampling

Perspective cameras now supply Cycles' existing open, centre and close field-of-view
keys alongside sampled camera transforms. The pinned engine interpolates projected
rays between those three keys. Increasing transform/deformation samples does not
add lens keys or implement arbitrary lens interpolation. If extra samples reveal
variation while all three engine keys are identical, translation holds the
requested-frame projection and reports the omitted curve.

Static perspective/orthographic optics and supported transform motion remain
available. Animated orthographic width, clipping and DOF are not interpolated by
this path. Their recovery uses separately captured requested-frame values, which
matters when the shutter is offset from that frame. Other supported motion stays
active.

## Local recovery

| Source problem | Result |
| --- | --- |
| Invalid requested-frame DOF inputs or derived numeric range | Pinhole camera; warning `camera.invalid_dof` |
| Animated or invalid shutter-only DOF | Requested-frame optics; warning `camera.shutter_dof` |
| Projection type changes or invalid shutter focal/aperture | Requested-frame projection; warning `camera.shutter_projection` |
| Lens variation absent at all three engine keys | Requested-frame projection; warning `camera.shutter_projection_curve` |
| Animated or invalid shutter orthographic width | Requested-frame width; warning `camera.shutter_orthographic_scale` |
| Animated or invalid shutter clipping | Requested-frame near/far; warning `camera.shutter_clipping` |
| Invalid shutter camera transform | Requested-frame transform, retaining valid zoom and object motion; warning `camera.shutter_transform` |

Warnings carry the camera path, affected controls and applied action. They travel
with owned camera data, so camera-only updates replace camera warnings while
preserving unrelated scene diagnostics. Repair clears the corresponding warning;
final EXR metadata receives the accepted camera diagnostics too.

Inactive controls do not invalidate the effective camera: orthographic capture
ignores focal length, disabled DOF ignores bad focus inputs, and motion-vector-only
sampling ignores shutter-only DOF/clipping that does not contribute to that pass.

An invalid requested-frame projection, clipping range or transform remains an
explicit configuration error. Malformed engine/worker camera data remains strict.
A later valid request can resume the retained worker, including when repair returns
to exactly the last accepted camera. An identical request after Stop also restarts
tracing rather than acknowledging a cancelled image as a completed render.

## Updates and motion vectors

Camera-only acquisition reads camera parameters and transforms without acquiring
SOP geometry. Focus, aperture and diagnostic changes do not retessellate adaptive
geometry. Dicing uses the pinned engine's centre-camera projection; camera
navigation retains the existing defer-during-drag/refine-after-settling policy.
Camera-dependent displacement still follows its documented matrix dependencies.

Motion vectors use frame-relative pre/centre/post projections separately from
shutter blur. Cycles includes background samples in the motion-pass weight. A
defocused edge therefore averages surface and background vectors; it need not
equal the surface's fully covered motion. The regression checks this against
independent constant-emission coverage, as well as analytic perspective vectors.

## Verification and limits

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass eight source preflights,
109 RGB comparisons, analytic/coverage motion-vector checks and five ordered
final frames through one Persistent Data session. Independent reference images
use a fixed camera and densely prewarped mesh positions, with numerical optics
and explicit clipping witnesses. They do not build expected images through the
camera source resolver or perspective-motion camera fields. Twenty-seven core
contracts include recovery, wire ownership, warning isolation and strict rejection.

Both Cycles Viewer and native IPR pass seventeen live edit/repair steps on RTX
4090 with one viewer, one worker and one Render click. A 65k-triangle planar
target provides numeric image comparisons while twelve packed instances of a
million-triangle prototype remain resident offscreen. Fourteen navigation checks
retain actual SOP cook counts and geometry conversions, with no new mesh upload.
Native displayed pixels are checked separately from screenshots. Six expression
repairs explicitly remove channels through HOM before native keyboard entry;
the workflow records these authoring steps. Tracked device, geometry and shader
resources reach zero after asynchronous shutdown.

Eleven scoped regressions pass, including the 276-image/five-frame spatial
displacement suite, 100,000-object pose contracts, compact transport, camera tools,
adaptive navigation, native world updates and native color IPR.

This is scoped Qt/HOM validation, not physical mouse, physical-memory,
mixed-device or production latency certification. At that host, panoramic/fisheye
cameras, rolling shutter and shutter curves remained unfinished. The sampling
milestone below adds rolling shutter and shutter curves. The lens milestone
needed no new engine patch or GPU kernel change: its worker protocol was 39,
with 41 patches and six kernels from the spatial-displacement build.

Exact source archives, reports and independent reference images are in the
camera evidence index (`artifacts/verification/camera-lens-motion-20261018/index.json`).
The sealed proof (`artifacts/camera-lens-motion-evidence-20261018.json`) records
the tested manifests and remaining work.

## Rolling shutter and shaped exposure - October 18, 2026

Host `20261018T153714784` adds real temporal sampling controls in the optional
camera **Cycles > Motion Blur** panel. Use Cycles Tools to add or upgrade the
camera's Cycles parameters. Upgrading preserves native parameters and authored
focus/f-stop links. No controls are installed on ordinary cameras automatically.

Rolling Shutter offers the pinned engine's supported **None** and **Top to
Bottom** modes. Duration zero gives a scan without exposure blur within each row;
one gives a global shutter. Intermediate values blend scan timing and per-row
exposure. The default is None, with duration 0.1. Houdini's camera Shutter Time
and the existing ROP motion settings still define the sampled interval.

The collapsible Shutter Curve panel contains an enable toggle and a native float
ramp. Its default and native ramp Reset both restore uniform exposure. Active
ramps are evaluated into 256 owned scalar weights. These weights redistribute
sampling time and are normalized: their area does not scale image brightness.
Different curves can be authored on different frames. Changes within one shutter
interval are held at the requested frame and diagnosed, since one exposure has
one scan law and one temporal distribution.

| Source problem | Local recovery |
| --- | --- |
| Invalid direction or non-finite/out-of-range duration | Disable rolling shutter; retain a valid curve and other motion; `camera.invalid_rolling_shutter` |
| Missing/incompatible ramp, negative/non-finite weights or zero exposure | Uniform exposure; retain valid rolling shutter and other motion; `camera.invalid_shutter_curve` |
| Animated/invalid shutter-only scan settings | Requested-frame scan; `camera.shutter_rolling` |
| Animated/invalid shutter-only curve | Requested-frame curve; `camera.shutter_curve` |

Relevance follows the resolved scan: a pure scan ignores its unused exposure
curve, even if the curve is broken. A shutter-only scan failure cannot falsely
invalidate a valid curve. Disabled blur and vector-only output ignore these
shutter controls; motion vectors retain their separate frame-relative sampling.
Repairs update the running render and clear the corresponding camera warnings.
Final EXR metadata retains accepted source diagnostics. Malformed internal
sampling data remains an explicit pre-transaction rejection.

The immutable camera payload, worker wire format and camera-only signatures now
include sampling settings. Duration and curve edits do not acquire SOP geometry,
upload mesh buffers or retessellate adaptive geometry. The existing persistent
worker remains the sole artist-facing IPR execution path.

Patch 0042 corrects the pinned host curve lookup from `x * size - 1` to
`x * (size - 1)`. The old signed/unsigned comparison selects the **last** weight
at exactly time zero; small positive times extrapolate the opening segment.
It does not dereference index -1 at exactly zero. Direct engine tests check
open/close, intermediate interpolation, clamping and empty/single-sample cases.
This is host code; all six GPU kernel binaries remain unchanged. The recorded
42-patch stack reproduces successfully. Worker protocol is now 40.

CPU and both OptiX devices each pass 121 image checks, seventeen engine/wire/strict
contracts and five ordered final frames through one Persistent Data session.
The independent reference integrates exact box-filtered bar coverage against
analytic uniform, triangular, rising and falling temporal CDFs. It uses no engine
camera, LUT, curve resolver or rendered image to construct expected pixels.
Cases include three/five geometry samples, offset shutters, scan endpoints,
coupled scan/curve recovery, camera-only repair, vector isolation and bounded
warm tracked allocations. The sequence includes invalid scan/curve frames,
warning removal and distinct valid exposure shapes.

Each heavy one-viewer Qt/HOM workflow passes seventeen live steps with one worker
and one Render click. Sixteen follow-up checks retain actual SOP cook counts,
conversion counts and mesh buffers. The 65k-triangle camera target is accompanied
by twelve resident offscreen instances of a million-triangle prototype. Two
expression repairs explicitly remove channels before native keyboard entry.
Native displayed pixels are checked separately from Qt screenshots. Tracked
device, geometry and shader resources reach zero after asynchronous shutdown.
Nine focused regressions pass, including the lens suite, DOF, motion vectors,
100,000-object poses, compact output, focus capture, adaptive navigation and
native world/color IPR.

This remains scoped validation, not physical input, physical-memory,
mixed-device or production latency certification. Panoramic/fisheye projections
remain unfinished. Sources, reports, screenshots, patch verification and 48
analytic reference images are recorded in the
sampling evidence index (`artifacts/verification/camera-sampling-20261018/index.json`)
and sealed sampling proof (`artifacts/camera-sampling-evidence-20261018.json`).

## Panoramic and fisheye cameras

Host `20261018T165945958` adds seven monoscopic projection models to the native
OBJ/SOP integration (October 18, 2026). Restart Houdini, select a camera, and use
**Cycles Tools > Add Camera Tab** to add or upgrade its optional controls.
In **Cycles > Lens**, choose **Projection: Panoramic**, then the model below.
Existing Houdini camera tabs and transform controls remain available.

| Model | Lens controls |
| --- | --- |
| Equirectangular | Latitude and longitude bounds |
| Equiangular Cubemap Face | One equiangular cubemap face |
| Fisheye Equidistant | Field of view |
| Fisheye Equisolid | Field of view, lens, sensor dimensions and fitting |
| Fisheye Lens Polynomial | Field of view, K0-K4 calibration, sensor and fitting |
| Mirror Ball | Mirror-ball projection |
| Central Cylindrical | Longitude bounds, height bounds and cylinder radius |

Only relevant controls appear. Sensor fitting uses full output resolution and
pixel aspect, including cropped renders. Camera axes and fitting follow
[Blender's camera adapter](https://raw.githubusercontent.com/blender/blender/main/intern/cycles/blender/camera.cpp).
The lens/sensor defaults and five polynomial coefficients match the documented
[Blender 5.3 camera properties](https://docs.blender.org/api/5.3/bpy.types.Camera.html).
The native integration accepts fisheye fields of view up to 360 degrees.
Polynomial inverse tests cover monotonic radial calibrations; a non-invertible
calibration cannot have a unique motion-vector inverse.

The Cycles override is explicit: Houdini's native Polar, Cylindrical and lens
shader modes are not silently aliased to these models. **Use Houdini** retains
the existing native perspective/orthographic path.

Supported camera transform motion, radial depth, position and motion-vector
passes use the same projection basis. Click-to-focus uses distance from the
camera rather than forward-plane depth, so surfaces behind a 360-degree camera
can be focused. The existing DOF aperture calculation still uses the native
camera focal length and units; the fisheye projection lens is independent,
as in Blender's adapter. DOF/source recovery remains local.

Animated or invalid shutter-only panoramic settings hold the requested-frame
lens, retain supported camera/object motion, and report `camera.shutter_panorama`.
Projection-kind changes use `camera.shutter_projection`. Repairs clear those
warnings in the running worker and final EXRs retain accepted diagnostics.
Unused lens/native focal controls are ignored when they do not affect the
resolved optics. Essential requested-frame camera errors and malformed internal
data remain transactional rejections.

Camera-only lens edits retain geometry and avoid mesh uploads. The existing
adaptive subdivision deferral remains active during camera dragging, with
refinement after settling. Scoped optical tests combine panoramic DOF with
adaptive linear wall subdivision; broader curved/FX dicing remains to certify.
The old `EdgeDice::scale_factor` linear-projection area estimator is under
`#if 0`; active edge factors already use panoramic ray footprints.

Patch 0043 fixes offset longitude inverse branches, polynomial inverses without
a linear coefficient, small-angle precision and a degenerate DOF aperture basis.
Patch 0044 wraps surface/background motion vectors by the actual angular image
period, including offset longitude windows. All six GPU kernels were rebuilt
together. The 44-patch stack reproduces; the worker protocol is 41.

CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 81 image checks, 66 engine/
wire/strict contracts, nine independent vector comparisons and five ordered
final frames through one Persistent Data session. The independent reference
uses colored enclosure intersections, analytic projection geometry, pixel-area
integration, uniform exposure and circular-pupil sampling. Comparing an averaged
motion pass with a pixel-centre vector is insufficient near nonlinear lens axes.

Each one-viewer Qt/HOM workflow passes 20 live steps and 19 retained-cook checks,
using one worker and one Render click. A 65k-triangle wall accompanies twelve
resident, occluded instances of a million-triangle prototype. Native parameter
entry, model changes, repair and displayed pixels are checked. The one scripted
Delete Channels operation before keyboard repair is recorded. Tracked geometry,
device and shader resources reach zero after shutdown. Ten focused regressions
pass, including existing lens/exposure suites, 100,000 poses, compact transport,
adaptive navigation and native world/color IPR.

These are scoped development checks. The enclosure permits independent image
comparisons and the occluded prototype exercises retention; this is not a
representative production rendering workload or a new latency certificate.
The native Qt screenshot has toolbar capture artifacts; native displayed pixels
are independently checked. Physical input/memory, mixed devices, arbitrary
animated optics, stereo/custom lenses and broader production coverage remain open.
Exact sources, reports, two captures and 90 standalone reference arrays are in
the camera evidence index (`artifacts/verification/panorama-20261018/index.json`)
and sealed camera proof (`artifacts/panorama-evidence-20261018.json`).

## Uniform exposure CDF correction - October 21, 2026

Patch 66 gives an empty or exactly constant positive shutter curve an exact
identity inverse CDF. This corrects inherited 255/256 interval compression that
was measurable with nonlinear shutter-sampled attributes. Authored nonuniform
curves keep their previous weighted CDF bit for bit; the pixel-filter CDF is
unchanged. Core checks cover both paths on CPU, each OptiX GPU and mixed devices,
using independent exposure/image references. See the
sampled-field evidence (`artifacts/sampled-instance-overrides-final-evidence-20261021.json`). This is not a new animated-optics
or physical-camera-navigation certificate.
