# Incremental headless tests

Ordinary pytest discovers the migrated pure contracts and opt-in native adapters.
The remaining mapped headless recipes are available through `--catalogue`, using
the existing catalogue and PowerShell runner. Collection needs neither Houdini
nor a renderer:

```powershell
python -m pytest tests/pytest_cases/test_catalogue_native.py --catalogue --collect-only -q
```

Select a recipe/backend with an ordinary pytest node ID or `-k`, then enable
native execution and select the staged build:

```powershell
python -m pytest tests/pytest_cases/test_catalogue_native.py --catalogue --native `
  -k 'houdini_panel_motion_timing_smoke-CPU' `
  --build-directory <build-directory> `
  --artifacts C:/TestRuns/panel-motion
```

`--houdini-root` and `--test-dependencies` forward explicit SDK and pinned
dependency selections to the existing runner. An inherited verified runtime
selects its exact build when `--build-directory` is omitted. Set
`CYCLES_HOUDINI_TEST_OPTIX_DEVICE` to pin an exact available OptiX device. Each
selected CPU/OptiX child verifies its actual backend and loaded runtime.

Only items remaining after pytest deselection execute. Their producer closure
runs once in catalogue order, sharing a fresh fixture directory for that queue.
Each pytest item checks both its own outcome and its prerequisites. A
`script-declared` recipe retains its original device policy, which can include
CPU and GPU; such consumers request all declared producer backends. These cases
do not carry a CPU-only marker. Queue startup, identity or source-audit failures
cannot become passing pytest items.

The queue retains its selected plan, original stdout/stderr, per-child runtime
identities, build manifest, source audit and actual reports beneath
`pytest-catalogue/<run>/`. Execution is labelled `native-catalogue-selection`.
This selection does not qualify a full nightly. The original 518 nightly
invocations and focused installer checks remain independent. Standard pytest,
including `--native`, does not collect catalogue recipes unless `--catalogue`
is explicitly supplied, so the nightly pytest step adds no duplicate renders.

# Evidence and reusable fixtures

Default pytest runs use a unique directory beneath
`_work/test-runs/pytest` of the checkout. Standalone prototype, GUI and
custom runner evidence defaults beneath `_work/test-runs`. Direct legacy
fixture scripts share `_work/test-artifacts`, allowing a producer followed by
a consumer. `_work` is ignored by Git and never scanned, so nothing else in the
checkout is written (until 22 November 2026 these were in `%USERPROFILE%` and
`%LOCALAPPDATA%`). A development package's source-root setting does not change
this default. Installed packages retain their relocated
data-root `artifacts` contract.

For isolated producer/consumer work, select the same fixture root explicitly:

```powershell
./scripts/run-prototype-test.ps1 -Test houdini_showcase_refresh_smoke.py `
  -Device CPU -Artifacts C:/TestRuns/showcase `
  -Report artifacts/current-schema-cleanup/showcase/report.json
./scripts/run-prototype-test.ps1 -Test houdini_queued_worker_smoke.py `
  -Device CPU -Artifacts C:/TestRuns/showcase
```

`run-prototype-test.ps1` and `run-gui-test.ps1` accept `-Artifacts` as their
shared fixture root. Prototype evidence lives in its unique `verification`
subdirectory; actual GUI reports, preferences and logs stay beneath the selected
evidence directory. `run-gui-test.ps1 -EvidenceDirectory` retains its explicit
GUI destination. Explicit absolute report paths and preference paths retain
their existing meanings. Relative reports are resolved against the selected
fixture root and must be fresh.

`run-tests.ps1 -Artifacts` retains its existing meaning as the evidence parent;
each invocation creates a unique queue directory and fresh `migrated` fixtures.
Use `-FixtureArtifacts` to share a selected fixture directory across custom
producer/consumer or GUI-only invocations. GUI-only runs default to the external
shared legacy fixture root. Launchers restore their process environment after
completion, including failures, so another invocation does not inherit nested
artifact paths. GUI interactions and performance probes remain explicitly
runnable and require the human review recorded in the test catalogue; this
headless adapter does not automatically launch them or claim they passed.
