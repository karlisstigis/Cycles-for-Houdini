# Renderer patch coverage audit

The maintained stack contains 76 production patches. The concrete current
[test recipes](renderer-patch-test-recipes.md) distinguish 44 unique primary
fixtures, host-only contracts, GUI cases and coupled default-GPU tests. The generated
[contract table](renderer-patch-contracts.md) and
[inventory](../patches/inventory.json) cover every patch, its touched files,
related fixture, fixture hash, source assertion counts, required CLI arguments,
and inferred file-overlap predecessors. These are source facts, not a record of
successful execution. Historical evidence in `upstream-patches.md` does not
qualify the current SDK, binary, backend or installed payload.

75 patches have a related runnable behavioral fixture. Patch 0002 adds SDK link
libraries; its direct contract is the current host build and delegate load, and
the inventory leaves its targeted behavioral fixture empty. A linked fixture
can cover several patches and may also contain assertions unrelated to the
patch. Removing a patch and observing the relevant assertion fail is a separate
mutation check; the inventory does not assert that this has happened.

Reference subprocesses in 68 maintained fixtures now obtain the build manifest,
harness, standalone renderer or debugger through `native_test_runtime.py`.
That helper requires the launcher's explicit context, binds its manifest hash
and SDK version, and verifies every auxiliary executable hash before launch.
It has no generic-build or old-SDK fallback. Synthetic unit-test directories and
side-by-side installer fixtures intentionally retain arbitrary version names;
they do not dispatch native reference binaries. GUI and headless launchers
verify the loaded Python module, native module, resources and ROP definition.

Two previously missing entries now have targeted callable tests:

| Patch | Runnable contract | Limit |
|---|---|---|
| 0005 | `core_point_lifetime_smoke.py` wraps the existing real engine lifetime fixture. It checks independent primary/shadow integrals, instances and resource offsets, enable/disable, invalid intervals, attribute removal and closed-session allocations. | Run each intended CPU/GPU backend explicitly; passing on one does not qualify the others. |
| 0075 | `core_subdivision_count_overflow_smoke.py` invokes real `SubPatch` count methods for triangular and quadrilateral patches at factors 1, 2, 3, 32768, 46341, 65536 and 92682, plus thin/asymmetric quads. The last factor conservatively covers a unit-square UV diagonal at maximum supported subdivision level 16. The independent 64-bit Euler-disk oracle crosses signed 32-bit product and result boundaries without large allocations. | Covers count arithmetic. It does not exercise private `DiagSplit` allocation-error branches or render a huge tessellation. |

The remaining important distinctions are explicit. The nested-dielectrics
plumbing fixture checks scene delivery, live edits and diagnostics; it is not
a complete independent optical reference for every overlapping-medium path.
RLS fixtures contain feature-on/off and transport comparisons, but their required
`--label` and `--output` arguments must be supplied. Release-runtime/Image Iris
checks also require the expected host, exact SDK and report destination. The
inventory lists these argument requirements instead of presenting the files as
argument-free smoke cases. EXR channel/type/metadata, alpha, feature-off controls
and backend mutation coverage must be assessed against each fixture's actual
assertions; broad fixture association alone does not establish them.

For bounded current checks, use `run-tests.ps1 -Tests` with exact file names or
the explicit cases queue. `-ListTests` lists the current selected catalog and
does not dispatch native processes. For example:

```powershell
./scripts/run-tests.ps1 -Tests native_test_runtime_test.py,core_subdivision_count_overflow_smoke.py,core_point_lifetime_smoke.py -StopOnFailure
./scripts/verify-native-release.ps1 -Cases tests/native-patch-boundary-cases.json -Name patch-boundaries -TimeoutSeconds 240
```

The first command uses the selected current build, records its manifest and
runtime identity, and executes only the selected cases. The second archives
fresh reports and matched source evidence. Both require an already built,
manifested runtime. Native checks must run sequentially with other GPU/GUI work.
Neither command is a claim that the entire historical suite or all 76 patches
have been independently certified. Backend qualification and Image Iris render
evidence belong to the resulting verification reports.

Pure maintenance checks are `python tests/native_test_runtime_test.py` and
`python scripts/patch-inventory.py --check`. Regenerate the source inventory
after fixture changes with `python scripts/patch-inventory.py --write`.
