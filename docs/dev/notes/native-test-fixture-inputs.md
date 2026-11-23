# Native cross-case fixture inputs

The named-tier runner declares all ten catalogue consumers in
`scripts/native_test_runner.py::CROSS_CASE_INPUTS`. Each declaration names its
producer and the relative files it may read. The union is the producer output
receipt. `check_producers` requires the correct backend, a passing child identity,
and matching bytes in the producer's own output root. Independent producers use
the selected shared fixture root. A producer that also consumes prerequisites
uses its private `fixture_root` under the run evidence directory. A later
successful backend of the same producer may rewrite a shared HIP or OCIO file.
The latest successful writer in that same root controls the staged bytes.
Each backend's private root has its own set of output receipts.

Before launching a dependent child, the runner copies only its declared inputs
to `private-fixtures/<plan-index>-<test>-<device>` under the run evidence directory.
It sets `CYCLES_HOUDINI_TEST_ARTIFACTS` to that private root, so the child writes
its own files there. The required JSON report is still copied to the established
`<test>-<device>.json` evidence path. A caller's `-FixtureArtifacts` continues to
select the producer/shared root; `-Artifacts` continues to select run evidence.
The child command and CLI arguments are unchanged. The audit rederives each
private input receipt from the ordered, preceding producer rows and checks its
retained file hash, backend ownership, and private path.

The `private-fixtures-v2` contract records each input's `source_root` as
an evidence-relative private producer root or the absolute selected shared root.
Both producer verification and copying resolve the same writer and tree. This
supports A → B → C chains where B reads A privately and produces C's input in
that same private tree. Output cleanup touches only the executing producer's own
root. A stale or locked shared B output remains untouched and cannot satisfy C;
C verifies the fresh private B output. A consumer must leave its staged
prerequisites byte-identical. Declared outputs that overlap staged prerequisites
fail explicitly before cleanup or child launch.
The audit derives source roots from preceding producer rows and rejects missing
or forged roots. Private roots and relative file paths must stay within their
selected evidence/fixture tree. Nightly aggregation rebases private source roots
along with fixture roots without changing partition receipts. Historical v1
archives remain auditable without source roots. The audit checks the archived
runner source to detect a v2 contract downgrade.

For every v2 input, the archived consumer copy identifies the bytes available
to that child. The audit re-hashes the copy against the effective writer's
receipt. A private producer also retains its original output in the run, so the
audit re-hashes that file as well. For example, if an OptiX sibling rewrites a
shared HIP after a CPU consumer has staged it, the CPU consumer's archive still
verifies the earlier bytes. The current shared HIP may legitimately have a
different digest.

Source audit on 9 November 2026 covered each consumer's `artifacts_root()` calls,
composed destination paths, and files embedded or referred to by saved scenes:

| Consumer | Cross-case input and indirect reference |
| --- | --- |
| `houdini_panel_motion_timing_smoke.py` | Motion HIP |
| `houdini_transparency_batch_smoke.py` | Transparency HIP; creates its own OCIO and frame outputs |
| `houdini_queued_worker_smoke.py` | Showcase HIP and `showcase.ocio`; sets OCIO to the private companion |
| `houdini_texture_cache_smoke.py` | Texture HIP and `gui-texture.exr` resolved by `$HIP` |
| `houdini_texture_normal_smoke.py` | Texture HIP and `$HIP` image; creates normal/height images privately |
| `houdini_texture_auto_batch_smoke.py` | Texture HIP and `$HIP` image; creates its own OCIO and animated EXRs |
| `core_strand_material_smoke.py` | CPU and OptiX points/curves EXRs |
| `core_volume_material_smoke.py` | CPU and OptiX absorption/emission/blackbody EXRs |
| `core_displacement_smoke.py` | CPU and OptiX plane/seams EXRs |
| `core_subdivision_smoke.py` | CPU and OptiX cube EXRs |

The showcase builder cooks `procedural-plume.vdb` into a stash and destroys its
file node before saving the HIP, so that generated VDB is not an external scene
input. Its ROP picture is `$HIP/courtyard.$F4.exr`, which resolves privately
when a consumer renders. The consumer scripts' `CYCLES_HOUDINI_SOURCE_ROOT`
variables identify source code or are unused; none composes a shared artifact
read from that root. The fast runner test deliberately composes a filename at
runtime and confirms an old file in the shared root is invisible to a consumer.
