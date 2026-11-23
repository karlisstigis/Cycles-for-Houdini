# Point instancing

Cycles renders Houdini point instances the way Houdini defines them:

* an **Instance object** (Object > Instance) with *Point Instancing* set to *Full* or *Fast*: every
  template point places a copy of the object named by its `instance` point attribute, or of the
  *Instance Object* when the point has no (empty) value;
* an Instance object with *Point Instancing* off: the *Instance Object* replaces the Instance object once;
* **any other geometry object whose points carry a string `instance` attribute** (as Redshift and Octane do):
  points with a value are instances; the object's other primitives render as usual, and while the attribute
  exists its loose points are not rendered as spheres. Objects without the attribute are unaffected.

Instances share the prototype's converted geometry (one conversion per prototype and material variant,
however many instances), and work in IPR, the viewer's final render, Render to Disk and hython/farm renders.

## Paths

`instance` values are object paths, resolved relative to the instancing object, as Mantra does:
`../tree` is a sibling, `/obj/tree` is absolute, `op:/obj/tree` is accepted, `tree` means a child of the
instancer. Prototypes render as instances whether or not they are displayed; a displayed prototype also
renders itself. An object subnet can be a prototype: its member objects (chosen by
*Subnetwork Filtering*) are placed at their own world transforms. Lights cannot be prototypes (diagnostic). Missing objects, cycles and
values that only appear away from the current frame are reported; the other instances keep rendering.

## Animated Blender objects and collections

To work with a viewport box and a reference that can be saved in a geometry file, use the **Cycles Blender Reference** SOP. [Packed Blender reference workflow](blender-references.md)

In a Point Wrangle on the template points, select an object or collection by its exact Blender name. Spaces in file and datablock names are supported.

```c
s@instance = "$HIP/assets/vehicles.blend#collection=Delivery Van";
f@index = @Frame - 3.0 * @ptnum;
```

For a single object, use a value such as `$HIP/assets/vehicles.blend#object=Front Wheel`. A collection leaves out child collections that the file excludes from its view layer; write `#collection+disabled=Name` to include them. The `index` float is the **absolute Blender frame**. For example, `7.5` samples halfway between frames 7 and 8. Points can share a pose or sample different poses in the same render. Omitting `index` uses the current Houdini frame. Ordinary `/obj/...`, relative OBJ paths and the usual point placement attributes still work.

Enable **Use Material Assignments** on the Cycles ROP. A background Blender process keeps the source file open and evaluates missing object or collection frames. It streams mesh arrays through a local pipe to the native Cycles adapter. Houdini supplies the placement of each point. Hidden material nodes represent the supported Cycles shader graphs, and texture files remain available to the renderer.

The adapter caches sampled geometry in memory for the current Houdini session. Points that use the same selection and source frame reference the same mesh. Moving points or shuffling their frame indices reuses resident meshes. Material palettes are applied separately, so a different material assignment can reuse the same geometry. Matching topology and unchanged UV, color and attribute buffers can share storage across sampled poses. The pipe identifies immutable numeric buffers by content and transfers only buffers missing from its receiving cache. Geometry does not pass through temporary geometry files. Saving the HIP preserves the original instance references.

For a static collection, set `f@index = 1;` on every point to share Blender frame 1. A collection with four mesh members on 51,294 points needs four shared meshes and 205,176 object placements. The first image includes starting Blender, evaluating that one pose, and building the render device's acceleration structure for the placements. An unchanged update in the same render session reuses that work.

During scene capture, the adapter starts each Blender worker as soon as it discovers a source, overlapping discovery of other instancers and capture of other scene data. An ordinary OBJ render launches no Blender worker. Missing selections and frames are batched by source file, Blender executable and color configuration. Blender opens each source once per worker, reuses its batch render setup and consecutive matching selections, and streams results one frame at a time. Each requested frame still receives a render dependency-graph evaluation; a failure in one selection is reported without discarding successful selections.

Source files, linked libraries, textures, Blender executable identity and color configuration are checked around evaluation and before publication. If an input changes while Blender is working, the candidate is discarded and retried against the new input state. An invalidated worker result cannot overwrite a newer source generation. An input that keeps changing produces a diagnostic instead of publishing a mixed sample.

Worker rendering starts device initialization when the render session opens. This can overlap Blender startup and scene capture. The first scene update uses the initialized worker when its color configuration matches the captured scene.

### Memory targets

Geometry used by every live ROP is pinned. Inactive samples are evicted in least recently used order to meet a byte target; shared allocations count once. Active geometry can exceed the retention target and remains available to the render. The diagnostic reports active and resident bytes, the target and the largest retained assets, making large collections or many distinct sampled poses visible.

Configure these environment variables before starting Houdini (values are MiB):

| Variable | Default | Scope |
|---|---:|---|
| `CYCLES_HOUDINI_BLEND_CACHE_MB` | 1024 | Retained prototype geometry and material records in the Houdini session. |
| `CYCLES_HOUDINI_BLEND_CHUNKS_MB` | 256 | Immutable received numeric buffers per Blender worker. |
| `CYCLES_HOUDINI_BLEND_DIGEST_MB` | 128 | Owned byte snapshots used to reuse content digests in each Blender worker. |
| `CYCLES_HOUDINI_BLEND_INFLIGHT_MB` | 2048 | Aggregate referenced numeric payloads awaiting consumption in the host. |

Targets must be finite values between 1 and 65536 MiB. The in-flight limit is enforced before receiving numeric payloads and remains reserved until the consumer releases a result. A request or concurrent batch that cannot fit is rejected with the required and configured byte counts. Increase the target or reduce the selections and sampled frames in that render.

The digest cache reuses an array's SHA-256 identity only when its shape, data type and bytes match an immutable snapshot. The comparison catches deformation, same-count topology changes and attribute edits. The worker hashes arrays smaller than 4096 bytes or larger than its snapshot budget directly.

Each worker evicts its least recently used snapshots before exceeding its budget, uses up to 1 MiB of comparison scratch space, and clears snapshots on source reload or dependency-version changes. With four workers at the default budget, snapshot storage can reach 512 MiB in addition to received chunks and evaluated geometry. Set `CH_BLEND_DIGEST_REUSE=0` before starting Houdini to disable snapshots and hash every array. Both modes use the same SHA-256 content identities.

For opt-in profiling, set `CH_BLEND_TIMING_SIDECAR` to an output directory before starting Houdini. Per-process JSONL records identify each array's owning object, bytes hashed, bytes compared and payload bytes sent. Shared payloads count once in the sent-byte total. The `hashing` phase includes byte comparison, hashing when necessary and snapshot copying. Use profiling-off runs to compare time to first image.

Accounting uses allocated capacities of owned native mesh arrays and counts their shared backing storage once. These targets describe adapter storage, rather than total Houdini or Blender memory. Houdini material nodes, decoded textures, Blender's evaluated scene, renderer copies, acceleration structures and device allocations are outside these totals.

Set `CYCLES_HOUDINI_BLENDER` to the full Blender executable path to choose a specific version. Otherwise the adapter checks the executable search path and Windows Blender Foundation installations. Use a version that opens the asset on each render machine. Keep linked libraries and external images accessible from the source file. Packed image data travels with the blend. For a blend authored with a custom OCIO configuration, set `CYCLES_HOUDINI_BLENDER_OCIO` to that configuration.

The adapter currently imports evaluated polygonal geometry and materials its Cycles node adapter can reproduce, including Principled, Emission, shader groups, and file-backed or packed textures. Camera and light objects in a collection are skipped. Unsupported connected shaders, animated image sources and unsupported geometry types produce a source diagnostic. The Blender frame is sampled once per prototype for each render. Intra-shutter Blender deformation sampling is outside the current implementation.

Blender material slots have the same priority as primitive material assignments on a Houdini prototype: an assigned slot wins over the point or instancer material; an unassigned slot uses the point material, then the instancer material. Blanket instancer material style sheets can replace these materials or override their parameters. Conditional face/group/attribute targets, nested targets and point-local material style sheets require Houdini geometry subjects and are not evaluated against direct Blender meshes. These rules produce a diagnostic; supported blanket rules and underlying materials keep rendering. Ordinary OBJ prototypes retain their full native stylesheet behavior.

### Subdivision at render time

**Cycles → Instancing → Subdivide Blender Meshes in Cycles** is on by default. When a supported Catmull-Clark Subdivision Surface modifier is last in Blender's render stack, Blender evaluates the preceding modifiers at the point's source frame and sends the control cage. Cycles subdivides that cage at the modifier's render level. Instances sharing a source frame can reuse the cage.

The native path supports closed, smooth quad meshes with compatible crease, UV and boundary settings. Other configurations use Blender's evaluated mesh automatically. A source diagnostic gives the fallback reason. Native limit-surface normals can look different near sharp creases and poles, especially at low subdivision levels. To use Blender's evaluated normals throughout, turn the checkbox off on the Houdini instancer. If an older scene is missing the checkbox, refresh the instancer's Cycles object properties.

## Subnetwork filtering and hiding sources

On a regular Geometry object carrying `s@instance`, add or refresh the Cycles object
parameters and open **Cycles > Instancing > Subnetwork Filtering**:

* **All Objects** (default for regular Geometry objects, including those without a Cycles tab)
  includes all geometry members, even when the source subnet is hidden.
* **Displayed Objects** respects member and parent display flags.
* **Specify by a pattern** uses **Subnetwork Pattern**, relative to each source subnet,
  without filtering out hidden members.

Choose **All Objects**, then turn off the source subnet's display flag to hide it in
the viewport while keeping its instances in Cycles. An Instance OBJ continues to use
its native controls on the Instance tab; the Cycles tab does not duplicate them.
The filter controls prototype membership, not whether the original objects render
independently. Use the ROP's **Exclude objects** when the originals should remain
visible in the viewport but be excluded from the render.

Filter, pattern and display-flag edits update during IPR, including recovery from a
selection matching no members. Existing tabs can be refreshed with the same
**Cycles Tools > Add Object Tab** shelf action; authored values are preserved.

## Placement

`instance world = prototype object transform · point transform · instancer object transform`.
The point transform is Houdini's own (HDK `GA_AttributeInstanceMatrix`, identical to Copy to Points):

| Attribute | Effect |
|---|---|
| `P`, `trans` | Translation (`P + trans`). |
| `pivot` | Local pivot. |
| `transform` (matrix3/matrix4) | Replaces orient/N/up/v/rot **and** pscale/scale. |
| `orient` | Orientation; beats N/up/v. `rot` applies after it. |
| `N` (else `v`) + `up` | Alignment (+Z along N, +Y towards up) when there is no orient. |
| `pscale` × `scale` | Uniform × per-axis scale; negative scale is supported. |

Zero or non-finite scales render nothing for that point (diagnostic), as in Houdini.

## What comes from where

| | Source |
|---|---|
| Geometry and its settings (subdivision, point radius, curve width/shape, volume step) | Prototype object |
| Material | Point `shop_materialpath` > prototype object material > instancer object material. Primitive materials inside the prototype always win. |
| `material_override` on a point | Layered on the instance's effective material (with or without a point material), as for packed primitives. |
| Ray visibility, holdout, shadow catcher, caustics, light group, pass index, linking, Cryptomatte names | Instancing object (each instance gets its own Cryptomatte object id, as packed primitives do). |
| Dielectric Priority | For a single-object prototype, the instancing object. For a subnet prototype, each member object's own priority. A point `cycles_dielectric_priority` overrides the inherited priority; a closer packed-primitive attribute takes precedence. |
| `Cd`, `Alpha`, custom attributes read by Attribute nodes | The instancing point, as for packed primitives. |
| Motion | Instance object *Point Motion Blur*: *Deformation* samples instances and prototypes over the shutter, *Velocity* uses point `v` and, when present, the angular velocity `w` (radians per second, about the instance point) without cooking the SOP at other times, *No motion blur* disables it. *Acceleration* additionally reads point `accel` for curved paths. The Cycles object property *Deformation* overrides it (Velocity, Acceleration, Geometry Samples, Velocity if Available). Geometry objects with `instance` use their usual motion setting. |

Use point `id` (or `name`) for motion blur when points are reordered, born or die: instances whose identity
is missing from a shutter sample, duplicated, or that switch prototype inside the shutter render at the
requested frame without instance motion (reported). Without ids, correspondence is by point number.

## Limitations

* *Full point instancing* renders like *Fast*: per-point `instancepoint()` expressions on object
  parameters are not evaluated (diagnostic). Use point attributes for per-instance variation.
* `instancefile` (Mantra delayed load) is not supported.
* With *Velocity*, an instance that turns half a turn or more between two motion steps renders without its velocity blur (reported); raise the ROP's motion samples.
* Points with an `instance` attribute inside an object that is itself instanced are not instanced again.
* A prototype's own material style sheet is not applied to its instances; the instancer's style sheet is.
