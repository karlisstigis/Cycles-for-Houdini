# Native Auto texture inputs

Native Image Texture nodes now resolve Auto from the rendering configuration
and Houdini's filename mode. The original graph retains the Auto token; its
resolved input space belongs to the prepared render resource. Configuration,
file-rule and input-transform changes can therefore re-evaluate a retained
graph without converting mesh topology again.

This is a tested development feature on Houdini 22.0.429, CPU and RTX 4090
OptiX. It does not complete production color, output, texture or performance
certification.

## Artist controls

Choose **Auto from Houdini OCIO and file** for color textures. Native Auto
tries the configured filename policy, then portable and named image metadata,
then Houdini's image-format interpretation. See [portable selection](native-color-interop.md)
for declaration priorities, legacy EXR encodings and immutable output tags.
A matched filename rule takes precedence over
the header. This follows the ordering described in the
[Karma texture guide](https://www.sidefx.com/docs/houdini/solaris/kug/textures.html).

Choose **Data** for values that must bypass color conversion, including height
and normal maps. **Render working space** means the values already use the
current scene-linear role. An explicit OCIO name, alias or role selects that
input interpretation independently of the filename and header.

The input-space menu follows the active Houdini OCIO spaces when opened.
Hiding a space from the menu does not change an already saved parameter.
Inactive names and aliases may be entered explicitly; validity and conversion
are checked before the live render changes.

Enable **Texture color spaces** in the Cycles Render panel to inspect the
spaces used by the current render. It shows the file and whether its input
came from a filename, metadata, format fallback or explicit selection. During
a pending color update it identifies the previous render's inputs. The panel
limits the displayed list to 128 entries and 32,000 characters. The development
API `Session.texture_inputs()` returns the complete committed list.

Use an active, valid Houdini OCIO configuration. Native Auto reports an error
when Houdini has disabled OCIO. In the installed-build probe, disabling OCIO
emptied Houdini's configuration path while the OCIO library still retained
the prior ACES configuration. That retained pointer cannot establish which
configuration Houdini currently uses. Other rendering behavior with host OCIO
disabled remains outside the color certification.

## Filename modes and the chosen evaluator

`HOUDINI_OCIO_FILENAME_COLORSPACE` controls the native policy:

| Mode | Native behavior |
| --- | --- |
| 0 | Skip filename detection; use metadata and format fallback. |
| 1, 2 | Evaluate OCIO file rules, including their default-only result. |
| 3 | Search the basename for configured names/aliases delimited by periods, underscores, hyphens or spaces; prefer the rightmost match and the longer match at the same endpoint. |
| 4, 5 | Evaluate OCIO file rules and suppress a default-only result, allowing metadata/format fallback. |

These mode intentions are documented in
[Houdini's environment reference](https://www.sidefx.com/docs/houdini/ref/env.html).
Native mode 4 deliberately uses OCIO's exact evaluator, as mode 5 does,
instead of Houdini's optimized evaluator. Evaluation happens when preparing
a changed graph; unchanged camera edits retain the graph and image resources.
No large-scene speed claim is made for this choice.

The differences are intentional and based on installed-build observations:

- The public PXL parser still evaluates rules in mode 0. Its caller must
  implement the disable behavior.
- Changing the mode through `hou.putenv()` leaves the public parser's old
  evaluator active until `hou.Color.reloadOCIO()`.
- The installed strict parser rejects tested aliases and some documented
  delimiters. For example, `foo_lin_ap1.exr`, `foo-RAW.exr` and `foo Raw.exr`
  did not produce their expected strict-name matches in that parser.
- Normal IMG opening and `readAsIs()` both retain format/header interpretation;
  they do not complete filename selection. TIL queries and VEX `colormap`
  also exposed different behavior in the probes.

Native mode 3 implements the stated delimiter policy with frozen definitions,
including aliases and basename boundaries. It intentionally differs from those
installed PXL exclusions. Modes 4 and 5 use the same frozen file rules as the
renderer. This avoids mixing a newly selected mode with an older cached
evaluator. The integration does not claim identical behavior across every
Houdini image subsystem.

Observation scripts and reports are under `artifacts/stage3/color`:
`auto-policy-probes`, `native-header-probes`, `live-policy-mode-probe.json`,
`strict-policy-probe.json` and `disabled-ocio-probe.json`. These research
observations are separate from the rendering regressions.

## Transactions, caches and UDIMs

The native adapter supplies an internal texture-policy callback to the shared
render core. Cycles types remain inside the core; the policy consumes owned
header metadata and returns an owned selection. Pixel decoding, image storage
and derived texture-cache generation remain in the pinned upstream engine.
The standalone harness retains its engine-default policy unless a host policy
is supplied. Reference scenes must specify their intended input spaces.

The core canonicalizes selected names through the frozen OCIO configuration
and checks conversion to `scene_linear`. Its texture revision includes the
resolved space, policy identity, rendering identity and selected file stamps.
Policy identity covers the mode, active-configuration state and Houdini sRGB
file-space override. Only Auto inputs respond to filename-mode changes;
explicit Data and named inputs keep their interpretation.

Every selected UDIM/UVTILE file is validated. Uniform Auto spaces can share
the upstream texture node. Differently resolved input spaces are rejected
with the conflicting files and spaces, because this node's input-space
parameter is shared across its tiles. Use an explicit common space when that
correctly describes the files, or separate texture nodes. A failed update
preserves the committed frame and input diagnostics.

Unresolved named header spaces fail visibly. Standard portable IDs and six
common linear EXR primary sets now resolve through the frozen configuration or
a private render overlay. Unsupported declarations without a reliable alternate
require an explicit input. The renderer does not infer ACEScg from a local name
or silently ignore unsupported primaries. Production certification remains open.

## Verification and runnable examples

`tests/houdini_texture_auto_smoke.py` uses fixed EXR/PNG/HDR texels and independent
OCIO processors on CPU and RTX 4090, with cache disabled and generated cache
enabled. Six fresh-process mode runs additionally cover filename/header
conflicts, strict aliases/delimiters, default-only fallback, live mode changes,
retained Auto graphs, rule and same-name transform reloads, active/inactive
menus, UDIM consistency and reloads, explicit overrides, and rejected updates.
Reports are under `artifacts/stage3/color/auto/mode-0` through `mode-5`.

`tests/houdini_texture_auto_ipr_smoke.py` runs the real native color lifecycle
with Auto textures and changing file rules. It shares the interaction fixture
with `houdini_color_ipr_smoke.py` and verifies both viewers, display-only
updates, manual updates, pause/resume, cancellation, restart and the panel's
input diagnostics. GUI reports include actual pixels, source identities,
generations and tracked allocations on close.

`tests/houdini_texture_auto_batch_smoke.py` renders two-frame native SOHO
sequences on both devices and cache routes. Each frame has an independently
specified input space; output pixels and ACEScg metadata are checked. A
missing input produces a ROP error and preserves an earlier output file.
The editable example is
`artifacts/stage3/color/auto/batch/native-auto-batch.hiplc`; use its accompanying
`native-auto-batch.ocio` configuration to reproduce the custom frame-two rule.

All three regressions participate in `scripts/run-tests.ps1`. Aggregate results
and source audits are recorded in versioned `artifacts/verification` runs.

The complete run `artifacts/verification/20261011T010612853/summary.json` passes
all 82 cases, including 18 actual GUI cases per CPU/RTX 4090 device. Its audit
matches 201 integration files, every executed test source, four compiled
renderer artifacts, eleven patches and six kernels before these documentation
annotations. The six separate filename-mode runs each pass 88 checks across
the four device/cache combinations. The earlier 81-pass run and its pyro
float32 comparator failure remain separately preserved.

The 82-case result above precedes portable color conversion. Its reports remain
a historical baseline.

Remaining texture/color gates include broader
OCIO contexts and LUT dependencies, alpha/filtering conventions, environment
and `op:` images, cancellation during decoding, large texture-heavy scenes,
long sequences and cache disk-retention budgets. Functional cache tests do
not establish a production performance advantage or Redshift parity.
