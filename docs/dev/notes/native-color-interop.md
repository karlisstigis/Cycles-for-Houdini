# Native portable color metadata

Native Auto textures resolve portable declarations against Houdini's configuration.
Each isolated worker loads the same resolved config file and captured context.
EXR output records the completed frame's source color identity.
This extends the native color implementation; full production certification
remains open.

## Selection and artist behavior

Explicit Image Texture inputs take priority. Auto then tries the configured
filename policy, a valid EXR ACES-container flag, a usable `colorInteropID`,
named metadata, recognized linear EXR primaries and Houdini's format defaults.
`data` bypasses conversion; `unknown` permits later selection steps. Unresolved
declarations without a reliable alternate require an explicit input. Known
encodings whose conversion is unavailable also fail visibly. Unqualified
internal storage markers require an explicit input when no portable declaration
establishes their source meaning.

IDs resolve through names and aliases, with one outer-namespace fallback.
Configuration-local IDs require the matching sanitized configuration name and
an unambiguous sanitized space name. The implementation does not treat the
`getInteropID` attribute as a unique lookup key. These rules follow the
[CIF ID recommendation](https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/03_ColorInteropID/ColorInteropID.md).

Six common linear RGB primary sets are recognized in legacy EXRs. Primaries
alone cannot establish PNG/TIFF encoding. An explicitly sRGB-encoded legacy
EXR retains its transfer function despite floating storage. Explicit inputs,
named spaces and portable IDs can describe other encodings.

## SDK compatibility and ownership

Houdini's OCIO 2.5 provides cross-configuration processors but lacks the newer
ID/local-ID/merge helpers. Internal compatibility code supplies the missing
operations without replacing host libraries.

Missing standard scene spaces use an immutable private conversion helper, connected
through the studio's declared `aces_interchange` role. This helper only compiles
glue CPU processors; neither Houdini's current config nor the engine config is
replaced or extended. A glue loader supplies already converted working-linear
pixels to stock Cycles. Private spaces stay outside input menus; diagnostics show
friendly source names. Graphs retain Auto. Configuration renames invalidate local input
IDs, while camera edits retain prepared geometry and texture resources.

The overlay covers the July 2026 gamma-2.4 scene ID and scene XYZ-D65, absent
from the installed catalogue. Gamma reuses the SDK's scene definition; XYZ
uses its analytic Bradford utility. The
[CIF encoding recommendation](https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/01_TextureAssetColorSpaces/TextureAssetColorSpaces.md)
and its [reference configuration](https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/01_TextureAssetColorSpaces/core-renderer-config.ocio)
provide the independent specification and numerical reference.

## Output and derived textures

Each source snapshot captures its output ID before rendering. Authored IDs
take priority. Otherwise compiled analytic equivalence through the interchange
role establishes standard encodings. Familiar names and sampled basis vectors
cannot establish an ID. Other named configurations receive unambiguous local
IDs; unnamed, ambiguous or invalid identities produce `unknown`. LUT-based
equivalence remains conservative and may retain a local ID.

EXR preserves float32 pixels and the original working/configuration/ID tags
after a later reload. It does not claim ST 2065-4 container compliance.
Configuration-local assets require the original matching configuration;
studios should version names when their intended color semantics change.
The [CIF EXR recommendation](https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/04_OpenEXRFiles/OpenEXRFiles.md)
describes the declaration and container priorities.

Converted texture caches replace source declarations with the captured working
identity and omit source primaries and ACES-container flags. Portable fallback
derivatives are generated in glue with float pixels and mipmaps, preserving RGB
conversion independently of alpha/channel-packed semantics. Their names include
the full source path, resource revision, processor identity and alpha mode. Only
the matching private namespace is consulted; captured metadata is validated and
publication uses the existing atomic output helper. The engine identity hook in
patch 0012 is retired. Ordinary named inputs retain the stock texture cache path.
Versioned native resource identity prevents reuse of preceding-policy derivatives.

## Evidence and remaining gates

`tests/houdini_color_interop_smoke.py` covers all 17 published encodings, ARRI
LogC4, standard/custom output, exact float32 pixels, namespace/local resolution,
conflicts, filename overrides, renames, legacy encodings and cache roundtrips.
CPU/RTX 4090 and cache off/on have independent references. The actual native
IPR/shared-panel variant exercises eight events per device. The SOHO batch
fixture adds portable headers, a filename conflict and output-ID checks.

These tests participate in the aggregate runner. Targeted logs remain under `artifacts/stage3/color/portable/`, including the
original cache-declaration and legacy-encoding failures.

Remaining gates include broader contexts and LUT dependencies,
transparent/filtered alpha, additional formats and multipart products,
Cryptomatte, long sequences, decode cancellation, disk retention and production
memory/performance measurements. Stock Hydra color behavior remains separate.
