# Native material stylesheets

The October 21 binding/volume/camera increment adds native point subjects for particles and uniform complete curves, preserving native priorities and local repair. See [binding, tapered-volume and camera coverage](../dev/notes/native-binding-volume-camera.md).

The native OBJ/SOP renderer now consumes object, detail and primitive material
stylesheets for material assignments and exported material parameter overrides.
Scene selection and watched imported/registered sources are also implemented.
Scripted assignment and parameter rules now have local CVEX recovery and
watched compiled-file/SHOP sources.
Independent surface and displacement parameter categories are also supported.
OBJ geometry material assignments, overrides and stylesheets are sampled
across the shutter for true displacement.
Native containing-particle bindings and uniform whole-curve point/vertex
layers are supported beneath the existing stylesheet priority. Common numeric
channel functions and explicit sample-time references use the shared owned
override evaluator. See [current contracts](../dev/notes/native-binding-expression-optics.md).
The remaining integration work is listed below.

Assign a Cycles material and promote the shader controls that a stylesheet
will override. In Houdini's Material Stylesheets editor, add an object stylesheet
and use **Set Material** or **Material parameter** overrides. The standard object
parameter is `shop_materialstylesheet`. Geometry can provide JSON in a scalar
string `material_stylesheet` detail or primitive attribute. No Cycles-specific
copy of the material is required.

For scene stylesheets stored in the HIP, use **ROP > Scene > Objects >
Stylesheets**. The menu lists registered names; the field also accepts native
selection patterns such as `look_* ^look_debug`. Its default is empty. Native
manager order controls priority: the last selected sheet has highest priority,
above object/detail/primitive rules. Reorder sheets in Houdini's stylesheet
editor rather than relying on the order of typed names. See
[SideFX's style manager reference](https://www.sidefx.com/docs/houdini/hom/hou/styles.html).
The ROP's uniform material replacement still replaces native assignments.

For example, this object stylesheet targets the geometry's `wheels` group:

```json
{
  "styles": [{
    "target": {"group": "wheels"},
    "overrides": {
      "material": {"name": "/mat/cycles_rubber"},
      "materialParameters": {"tint": [0.05, 0.05, 0.05], "roughness": 0.6}
    }
  }]
}
```

`tint` and `roughness` must be exported controls on that Cycles builder.
Houdini's native evaluator handles targeting, self targets, group/name/attribute
conditions and parent/child priority. A `type: "self"` target styles its owner;
ordinary targets style its contents. Nested `subTarget` rules reach primitives
inside packed geometry. See [SideFX's stylesheet documentation](https://www.sidefx.com/docs/houdini/shade/stylesheets.html)
and [the native styler API](https://www.sidefx.com/docs/hdk/class_s_t_y___styler.html).

The same resolution is used by the **Cycles Render** panel, native Houdini IPR and final
rendering. Supported scalar, integer, toggle, enum, vector/color and string
values use the existing [read-only native override evaluator](native-material-overrides.md).
Material parameters affect both surface and displacement where their exported
channels contribute. Artist material parameters are not rewritten.

## Separate shading and displacement controls

Use the standard Material Stylesheets editor's material, surface and
displacement parameter categories. All three address promoted controls on
the assigned Cycles material builder:

| Native JSON category | Affected shader outputs |
| --- | --- |
| `materialParameters` | Both shading and displacement |
| `surfaceParameters` | Surface, volume and AOV shading |
| `displaceParameters` | Displacement only |

A shader-specific value overrides the generic material value on that branch.
For example, if the promoted `gain` drives both emission strength and height,
this rule gives shading a value of 2 and displacement a value of 3:

```json
{"styles":[{"overrides":{
  "materialParameters":{"gain":1},
  "surfaceParameters":{"gain":2},
  "displaceParameters":{"gain":3}
}}]}
```

The spelling is `displaceParameters`, matching Houdini's native category.
Global material metadata, such as Material Index, uses the generic category.
Use forward slashes in Windows filenames inside stylesheet JSON. Houdini's
normal string-parameter evaluation interprets escapes such as `\r` and `\n`.

The integration evaluates the two output graphs without changing parameters.
Equal dependencies remain shared; different values create only the required
shader branches. Displacement retains its original node identities, so a
surface-only edit can retain completed displacement. A missing surface texture
does not disable independent valid displacement. Capacity recovery omits
scoped fields locally and retains valid generic values with the same name.

SOP-authored and object-level animated rules are sampled across the shutter
for true displacement. This covers the documented mesh and packed paths under
Transform, Velocity and Deformation geometry policies. Surface/volume/AOV
shading and material identity still use the requested-frame assignment; an
off-frame material can supply displacement without replacing that shading.
The earlier failed object-parameter sampling test is retained as historical
evidence; the object-binding increment below closes that gap. Requested-time
CVEX execution is verified separately below.

No extra switch is needed: the existing motion/shutter settings determine the
sample times. Static geometry in Transform or Velocity mode can reuse its
acquired source while material values are sampled. Object source states are
immutable shared data, so packed placements do not copy the complete history.
Off-frame-only imports and missing material paths remain observed for repair.
An 8 MiB per-object source-history budget falls back to requested-frame bindings
with a diagnostic. Already observed imports remain watched during that fallback,
so reducing a large imported stylesheet can restore sampling automatically.

## Capture, sharing and recovery

Houdini styling runs during host acquisition. The converter and worker receive
owned material values and binding identities; no live STY subject, geometry
pointer or script closure crosses that boundary. Packed descendants receive
their filtered style context. Distinct effective bindings retain their own
material identities; compatible copies continue sharing prototypes.

Uniform object/self rules reuse the existing distinct binding table instead
of scanning every primitive. Stylesheet cache entries include geometry and
attribute identifiers, retain diagnostics across hits, and retire contexts no
longer used by a committed update. This does not eliminate all initial host
styling or conversion work for varying primitive rules.

Malformed object JSON and unsupported parameter values/categories produce
source diagnostics while supported layers and geometry remain usable. Unknown
exported fields are ignored independently of valid fields. Missing or foreign
assigned materials use the existing local diagnostic material. A bad color
override preserves valid displacement; an invalid height override keeps the
authored height. Repairs restore the intended result and clear warnings.
Stylesheet sources have a 1 MiB limit; import graphs have an 8 MiB, 256-document,
32-level acquisition budget. A source that exceeds a budget omits its owning
stylesheet layer while preserving other layers and geometry. Generated
parameter layers have 128-field/64 KiB limits. A global scene stylesheet
population policy is separate work.

## Imports and automatic repair

Use native `importFiles` to reference ordinary JSON files or registered style
names. For example:

```json
{"importFiles": ["$HIP/styles/look.json", "shared_look"]}
```

Houdini resolves registered names before filenames and determines imported-rule
priority. Relative filenames resolve from Houdini's working directory; use
`$HIP` or an absolute path for a portable, explicit source. URI streams are
diagnosed and omitted in this integration.

Both viewers observe file metadata, registered-style versions and referenced
bundle membership through the existing resource poll, approximately once per
second. This observer does not read or cook SOP geometry. Nested imports,
missing files and absent registered names remain watched so creation, deletion
and repair update IPR automatically. Source metadata also invalidates cached
bindings during capture, independently of the viewer; final/batch renders use
the same resolution. Content changes that preserve both size and modification
time are not detected automatically; update the file's modification time or
start a fresh session. The ordinary Refresh IPR action applies the current
scene without purging extraction caches.

Missing and malformed imports report their source and retain supported native
rules and underlying bindings. Native recursion handling retains usable rules
from cyclic imports and reports the cycle. An oversized/unsupported source
disables its owning layer rather than the geometry. Surface and true
displacement overrides both update after repair. The native object subject
filters paths, categories and bundles; object parameter text is evaluated at
the requested capture time. No registered stylesheet map is modified and no
live host subject crosses the worker boundary.

This increment also fixes shader lifetime bugs exposed by changing assignment
layouts. Geometry rebuilds preserve their old shader palette until the socket
setter replaces it. Removed shaders retire after palette rebinding, and a
remaining engine reference defers pooling. Negative reference counts remain
an internal failure. Pending retirement is included in the retired-shader
counter and is cleared on shutdown.

## Scoped verification, 2026-10-19

Evidence: `artifacts/material-stylesheets-evidence-20261019.json` and
`artifacts/verification/material-stylesheets-20261019/index.json`. Failed
development iterations and their exact source snapshots are retained.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX separately pass 26 image/recovery
  checks and five ordered final frames. Cases include object/detail/primitive
  priority, arrays, packed owners/sub-targets, missing material creation,
  malformed input, true displacement and later repair.
- Both viewers separately pass 19 live states on the 4090. Actual native
  parameters change rules, break and repair bindings, advance the timeline,
  navigate the camera, move objects and rename a material. Rules are also
  edited and repaired on a 1,048,576-triangle prototype shared twelve times.
  That prototype stays at one cook; camera/object navigation adds no geometry
  conversion. One worker remains active. Native IPR pixels are checked through
  Houdini's pixel API. Sessions close with zero tracked geometry/device bytes.
- The retained shading/displacement regression passes 138 checks separately
  on CPU and both GPUs. Its engine shader count starts at 8 and settles at 9;
  the earlier leaking implementation grew to 30 over the same edits. The new
  test asserts a bound. This is tracked ownership evidence, not a physical-RAM
  or prolonged-memory certificate.
- Cross-kind shader retirement exercises 32 lifetimes on CPU and the default
  OptiX GPU. Native local overrides, graph overrides and the larger mixed/packed
  assignment regression pass. The empty stylesheet expectation now correctly
  accepts a no-op rule without a warning.

All 55 engine patches reproduce from pristine pinned files. Worker protocol 45,
six kernels, five compiled artifacts and the ordinary runtime are recorded in
the tested manifest. No new engine patch is needed for this increment.

## Remaining stylesheet integration

Embedded VEX materials,
true varying per-key hair rules, conflicting shared-particle owner
representations, wider volume/shutter
contexts, unresolved dynamic channel paths and other scripted resource dependencies,
per-rule diagnostic aggregation and a global population policy remain.
Distinct effective bindings keep separate logical declarations. Compatible
polygon bindings share physical meshes/BVHs; actual displacement/geometry
divergence and point/hair storage retain their own boundaries. Incremental family
maintenance and incompatible-field indexing remain optimization work. Supported categories
are reported as `material`, `materialParameters`, `surfaceParameters` and
`displaceParameters`; other categories are
diagnosed and omitted.

## Source integration verification, 2026-10-19

Evidence: `artifacts/stylesheet-sources-evidence-20261019.json` and
`artifacts/verification/stylesheet-sources-20261019/index.json`. Eleven
selected passing runs use the same ordinary runtime and manifest. Seventeen
source-audited runs include four preserved test-development failures.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX separately pass 47 image/recovery
  checks and five ordered output frames. They cover native selection order and
  patterns, imported/registered/nested/cyclic/missing sources, local primitive
  recovery, packed descendants, actual displacement, repair, saved ROP layout
  and registered-style persistence. Positive and negative object filtering
  matches HOM for path/category/bundle targets.
- **Cycles Render** and native Houdini IPR separately pass 31 live states on the
  4090, including imported-file edits/deletion/repair, registered styles and
  bundle membership changes. The 1,048,576-triangle prototype shared twelve
  times stays at one cook. Geometry conversions stay at two throughout all
  edits, including camera and object navigation; each viewer retains one worker
  and closes with zero tracked geometry/device bytes.
- Core stylesheet regression passes 26 checks/five frames separately on all
  three devices. CPU regressions pass 14 native-override, 138 retained-shading
  and 88 sampled binding/motion checks. Shader slots remain bounded at nine.

Worker protocol 45, 55 unchanged engine patches, six kernels, five compiled
artifacts and 81 immutable host runtime files are recorded. This is scoped
development verification; physical-memory, mixed-device, prolonged physical
input and full production certification remain open.

## Scripted overrides and recovery

Use native CVEX override scripts for `material.name` and exported
`materialParameters`. Inline code, shared script definitions, attribute
shortcuts, compiled `.vex` files and CVEX SHOP nodes use Houdini's native
targeting, precedence, bindings and execution. For example, this rule computes
an exported material control from a primitive float attribute:

```json
{
  "styles": [{
    "target": {},
    "overrides": {
      "materialParameters": {
        "roughness": {
          "script": {
            "code": "cvex look(float gain=1; export float result=0){result=gain*0.1;}",
            "bindings": {"gain": "roughness_scale"},
            "return": "result"
          }
        }
      }
    }
  }]
}
```

The control must be promoted on the Cycles material. Primitive and point
targets retain native semantics: use `"type": "point"` for loose point
geometry, and primitive targets for complete hair curves. Target prebindings
continue through packed ancestors. Explicit bindings support compatible
scalar integer/float, string and vector data. An unbound optional function
argument retains the script's default; a missing explicitly requested binding
is a recoverable source problem. Unused declared bindings and unmatched rules
do not produce warnings. See [SideFX's binding format](https://www.sidefx.com/docs/houdini/shade/stylesheets.html).

Compilation/loading failures, missing exports, incompatible or non-finite
bindings and unusable results omit only the affected field. Native rules are
re-evaluated so a valid lower-priority rule can apply. Other fields, material
assignments and geometry remain usable. A broken height script keeps authored
displacement; a broken color script preserves independent valid displacement.
Missing script-selected materials use the ordinary local diagnostic material.
Repairs clear the source warnings without restarting the worker. A bounded
32-pass recovery guard omits remaining scripted fields on that component with
a diagnostic if successive invalid rule layers exhaust the budget.

Styling stays in host acquisition. Cloned styles retain native owning load
contexts, and primitive batches retain their original subject/index ownership.
Compiler/source error scopes prevent a failed rule from marking the last
evaluated SOP as failed. Unsupported scripted categories are omitted without
running their code. Ordinary non-scripted sheets retain the direct native
path. Invalid internal ownership and unsuccessful engine/output operations
still fail explicitly.

Compiled filenames and CVEX operator paths remain observed after deletion.
Both viewers update after file replacement, node parameter edits and
recreation. Changed sources invalidate only their named cached CVEX program;
unrelated programs are not flushed. The observation ledger is bounded to
256 resources. File observation uses the size/mtime limitation described above.
Successfully resolved channel operators are also observed, including control
nodes outside the rendered object network. Their native node identity and
parameter version participate in resource signatures; both viewers subscribe
to their parameter edits. Arbitrary file, texture and geometry reads inside
user CVEX do not yet have complete dependency tracking.

Implicit channel reads now use the requested frame and shutter sample. The
integration executes the selected CVEX export through the public
[CVEX runtime](https://www.sidefx.com/docs/hdk/_h_d_k__c_v_e_x.html), supplying
time and the calling node without changing the UI frame or rewriting artist
code. In the native OBJ workflow, inline rules use the owning OBJ as their
relative channel context; operator programs use their CVEX operator. Explicit
VEX time arguments remain explicit, for example
`chf("/obj/controls/gain", 0.5)`.

Houdini STY still owns target matching, priority, material barriers and packed
ancestor bindings. Selected lazy binding reads are executed in batches of at
most 4,096 subjects and imported through fresh native result containers. Owned
typed results and original/cloned source sheets remain in acquisition; no
borrowed STY/GA/CVEX pointer enters conversion or the worker. Partial packed
styles retain their original script definitions and frozen ancestor bindings.
Bindings used only by a different export are ignored; optional function and
compiled-command arguments retain their native values.

Object, SOP and packed stylesheet channels can animate true displacement under
Transform, Velocity and Deformation policies. Static geometry in Transform or
Velocity mode is retained while material values are sampled. Ordinary static
objects with no stylesheet source skip material shutter probing; the source
check inspects only attribute metadata in their acquired packed hierarchy,
visiting each unique prototype once.

Missing channel lookups retain VEX's zero/default behavior. The public caller
callback reports successfully resolved operators; discovering and observing
never-resolved dynamic channel paths remains open. No guessed dependency or
fabricated missing-channel diagnostic is introduced.

## CVEX recovery verification, 2026-10-19

Evidence: `artifacts/stylesheet-cvex-evidence-20261019.json` and
`artifacts/verification/stylesheet-cvex-20261019/index.json`.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX separately pass 78 image/recovery
  checks and five ordered output frames. They cover typed bindings, unused
  inputs, lower native rules, shared scripts, packed prebindings, actual
  displacement, points/hair, material assignment scripts and file/SHOP repair.
- **Cycles Render** and native Houdini IPR each pass 37 live states on the 4090.
  The 1,048,576-triangle prototype shared twelve times remains at one cook and
  two total geometry conversions throughout, including camera/object moves.
  Each viewer retains one worker and closes with zero tracked geometry/device
  allocations. This fixture uses uniform heavy-object rules; it does not prove
  equivalent storage reuse for arbitrary per-instance style variants.
- Existing core/source suites pass 26/47 checks and five frames per suite on
  each of the three devices. CPU regressions pass 138 retained-shading,
  88 sampled-binding/motion and 20 native-binding override checks.
- Fourteen selected passing runs share unchanged runtime bytes. A source
  inventory proof records relocation of eight unlinked HDK probe object files
  into artifacts; integration code, tests, compiled artifacts and host runtime
  are unchanged across those two tested inventories. All 29 audited runs and
  ten failed development iterations retain their exact source snapshots.

The ordinary runtime is `hosts/20261019T190830915/resources`, with protocol 45,
55 unchanged engine patches, six kernels, five compiled artifacts and 81 host
runtime files. Mixed-device, physical-memory, prolonged physical-input and
production certification remain open.

## Shader-category verification, 2026-10-19

Evidence: `artifacts/stylesheet-domains-evidence-20261019.json` and
`artifacts/verification/stylesheet-domains-20261019/index.json`.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 49 graph/image states
  plus five final frames, nine volume-extinction states and ten analytic
  SOP-authored shutter states. Cases include separate values on shared
  controls, tuples/components, per-face and packed bindings, CVEX/type/texture
  repair, AOVs, generic precedence and graph-capacity recovery.
- **Cycles Render** and Houdini IPR each pass 24 live states on the 4090, including
  automatic image-file deletion/recreation. The 1,048,576-triangle prototype
  shared twelve times remains at one cook and two total conversions. Each
  viewer retains one worker and closes with zero tracked geometry/device bytes.
- A warm per-face palette expansion exposed stale displacement ownership
  stamps. Geometry and stamps now advance together; an explicit contract
  verifies malformed internal palette ownership still fails. Eighty core
  contracts pass in each of twelve processes.
- Equal displacement histories retain recovery warnings before their sample
  identities are shared. Cached warnings remain visible, repair clears them,
  and incompatible materials retain their specific motion-recovery diagnostic.
- Regressions pass 78 CVEX states/five frames and 138 retained shading states
  on all three devices. CPU additionally passes core/source stylesheets
  (26/47 states and five frames each), independent sampled bindings/motion
  (88 states/five frames) and native override bindings (20 states).

Twenty-two selected passing runs share one final manifest/runtime; all 47
audited runs, including ten failed development iterations, retain source
snapshots. The ordinary runtime is `hosts/20261019T200425029/resources`,
protocol 45, 55 unchanged engine patches, six kernels, five compiled artifacts
and 82 host runtime files. This is scoped development verification. The
remaining integration and production certification above remain open.

## Object-binding shutter verification, 2026-10-19

Evidence: `artifacts/object-binding-shutter-evidence-20261019.json` and
`artifacts/verification/object-binding-shutter-20261019/index.json`.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 71 object-binding
  states and five ordered output frames. Cases include all three geometry
  motion policies, 3/5 samples, offset shutters, scoped/local overrides,
  malformed JSON, absent nominal rules, material deletion/recreation, packed
  sharing without authored IDs, off-frame imports and 129-sample capacity repair.
- Analytic image expectations come from independent piecewise linear
  box-shutter coverage. Requested-frame shading remains unchanged by different
  off-frame materials. Invalid fields retain authored height locally; an
  incompatible missing material retains requested-frame displacement and
  independent geometry/object/camera motion.
- SOP stylesheet animation passes 30 states per device across Transform,
  Velocity and Deformation. Regressions pass 49 category states/five frames,
  88 independent motion states/five frames, 138 retained shading states,
  78 CVEX states/five frames and nine volume states per device.
- CPU additionally passes 37 sampled override states/five frames, 26 core
  stylesheet states/five frames, 47 source states/five frames, 20 native binding
  states and 80 internal contracts in each of twelve processes.
- **Cycles Render** and Houdini IPR each pass 34 live states on the 4090, including
  off-frame file edit/deletion/recreation and repair after exceeding the source
  budget. The 1,048,576-triangle prototype shared twelve times stays at one
  cook; camera and object navigation add no conversions. Policy changes can
  recapture the receiver. Each workflow has one worker and closes with zero
  tracked geometry/device allocations. Physical input is not certified here.

Twenty-eight selected passing runs share the final runtime and manifest. The
archive retains all 63 audited runs, including one failed fixture iteration
using invalid one-line Python parameter expressions. The ordinary runtime is
`hosts/20261019T204031298/resources`, protocol 45, 55 unchanged engine patches,
six kernels, five compiled artifacts and 82 host runtime files. The wider
integration and production certification above remain open.

## Requested-time CVEX verification, 2026-10-19

Evidence: `artifacts/stylesheet-cvex-time-evidence-20261019.json` and
`artifacts/verification/stylesheet-cvex-time-20261019/index.json`.

- CPU, RTX 4090 OptiX and RTX 3080 Ti OptiX each pass 28 requested-time image
  states, three additional typed export checks and five ordered EXR frames.
  Coverage includes absolute/relative channels, explicit time, unrelated
  control-node edits, native compiled-command arguments, unused exports,
  batches above 4,096 faces and invalid-result repair.
- Each device passes 72 analytic object/SOP/packed shutter states across
  Transform, Velocity and Deformation policies, with three/five samples,
  local off-frame failure/repair and unchanged-geometry retention. Static
  Transform/Velocity sources require no additional material shutter cooks.
- Five heavy static states per device retain one 1,048,576-triangle prototype
  shared twelve times at one cook and two total conversions. Without any
  stylesheet sources, material shutter probes/cooks/samples stay at zero.
  A source-audited failing baseline preserves the unnecessary probe regression.
- Existing per-device regressions pass 78 CVEX recovery states/five frames,
  71 object-binding states/five frames, 49 category states/five frames and
  138 retained-shading states. Additional CPU motion, source, volume, override
  and internal-contract regressions are recorded in the evidence file.
- **Cycles Render** and native Houdini IPR each pass 19 new live channel states
  and 37 retained CVEX repair states on the 4090. The heavy shared prototype
  stays at one cook, camera/object moves add no geometry conversions, each
  workflow retains one worker and closes with zero tracked geometry/device
  allocations. Tests use actual HOM/Qt parameter and timeline edits and native
  display checks; physical-input and long-duration memory certification remain.

Thirty-three selected runs share the final verified runtime. Earlier failing
iterations and the static-probe baseline remain archived. There is no engine
patch or worker protocol change: 55 patches, protocol 45, six kernels, five
compiled artifacts and 84 immutable host runtime files. Single paired fixture
timings are retained as observations; they do not establish percentile budgets
or production latency certification.

## Constant targets and mesh transport follow-up

Protocol 46 now reuses identical mesh payloads across binding-only edits while
retaining independent palettes, face assignments and material identities.
Provably constant native targets match one actual STY subject instead of
walking every prototype face. Dense host preparation fell from about 4.6
seconds to 2-3 ms in the recorded probe. Imports, local styles, scripts,
requirements and subtargets keep native general evaluation.

CPU and both OptiX devices pass the paired transport, assignment/repair and
ordered-frame checks, along with the existing stylesheet, category, import and
CVEX timing suites. Both live viewers preserve queued material/geometry edits
during navigation. Full source/runtime evidence and remaining limits are in
[the mesh payload report](../dev/notes/native-mesh-payload-transport.md).

This completes payload reuse, not simultaneous physical mesh/BVH sharing.
Large nonuniform/scripted host work and dense emitter updates still need
optimization; no universal responsiveness or release certificate is claimed.
