# Committed engine source and offline recovery

The project root is the production integration source. `release/466-28f9f480`
preserves the exact 1,631 integration inputs of the qualified November 8 source
snapshot; package `82355eb8` remains the feature-preservation baseline. Recovery
archives preserve committed integration refs, the fork objects and the historical
production working-source layout. Keep the archive and its outer SHA-256 on a
second disk: an archive on this workstation is a tested local restore, not a
backup against disk failure.

## Production engine identity

`source.lock.json` records both `cycles.commit` (the pinned upstream ancestor)
and `cycles.authoring_tip` (the production fork HEAD). The accepted fork is
`151c8b056dce274686e268589fa10aa25a0beaa9`: 86 linear commits above
`74e9d9e48f86330987d7392166a51b3652d7c557`. The first 84 imported commits carry
patch path/hash, purpose, contract and related-fixture pointers in their bodies.
The two subsequently shipped commits retain their original IDs and patch bytes;
local Git notes supply their rationale, contract and test pointers. The original
shallow upstream boundary remains supported; earlier upstream history must be
fetched before attempting a rebase requiring those ancestors.

The production build consumes a **clean committed fork at third_party/cycles**.
The companion `artifacts/engine-authoring/locked-repo` preserves the original
84-commit import. A bootstrap source must contain the current locked tip; use a
current source recovery archive or the current clean production fork. Upstream
Git does not contain the private fork. For a new checkout, restore it from recovery or pass the local fork
repository to bootstrap:

```powershell
./scripts/bootstrap.ps1 -EngineRepository E:/Recovery/engine-authoring
python scripts/engine-fork.py verify
```

Bootstrap refuses an existing legacy or dirty engine, and never resets it. The
fork verifier checks HEAD, linear commit count, commit-to-patch provenance,
original patch hashes and a full independent replay against every engine source
file, including ignored additions under src. Default builds run this verifier.
DLSS and OptiX headers and Windows precompiled dependencies retain their separate
locked identities. Image Iris remains ON; production RLS and Gaussian splats
remain part of the locked engine.

## Commit metadata in Git log

The ordered lock maps commits to inventory entries; historical patch trailers or
format-patch From headers bind each pair. `patches/inventory.json` supplies the
contract and fixture, and hash-bound `patches/triage.json` supplies the rationale.
These existing versioned files are the portable source of truth. There is no
second metadata inventory.

```powershell
python scripts/engine-fork.py notes
git -C third_party/cycles log -2
```

The notes command validates all locked commits and patch bytes before recreating
the two missing bodies in `refs/notes/cycles-houdini`. Local `notes.displayRef`
makes them visible in ordinary `git log`; other note namespaces are preserved.
It changes neither commit objects nor source, index, branch or patch bytes.
The fork verifier checks metadata without writing notes. Bootstrap, materialize,
version 3 snapshot restore and recovery of the current committed fork reconstruct
notes from the same versioned sources, so copying notes refs is unnecessary.
Historical archives keep their historical identities and metadata policy.

## Coordinated legacy migration

Stop source-writing and build jobs before migration. Existing renderer processes
and installed binaries are not changed by these source operations. First capture
and verify a full recovery archive of the legacy state:

```powershell
python scripts/source-history.py recovery --output artifacts/recovery/pre-fork.zip --report artifacts/recovery/pre-fork.json
python scripts/source-history.py restore --archive artifacts/recovery/pre-fork.zip --output artifacts/recovery/pre-fork-restore --report artifacts/recovery/pre-fork-restore.json
```

Then run the reviewed migration, using a NEW evidence directory:

```powershell
python scripts/engine-fork.py adopt-legacy --engine-root artifacts/engine-authoring/locked-repo --destination third_party/cycles --output artifacts/engine-migration/production
```

The migration refuses any source drift from the preserved baseline. It imports
fork objects without changing dependency checkouts, saves the old index, HEAD
reference and the only three changed raw source files, normalizes those verified
CRLF files to the accepted LF blobs, and switches the index/HEAD to
`houdini/production`. Full replay and exact legacy fingerprint reconstruction
must pass after the switch. Failure restores touched source bytes, index and HEAD
and verifies the original fingerprint. An imported branch can remain after a
failed migration; inspect the report before retrying. No build/tmp/library moves
are performed. The backup is retained under the evidence directory.

`patches/legacy-source-layout.json` binds the exact old manifest SHA-256, old
source fingerprint, upstream and fork tip, and raw/fork hashes of the three
CRLF files. `engine_source.legacy_fingerprint` recreates the old source layout in
a disposable Git repository, with its 12 untracked and two intent-to-add source
files, and requires exact equality of the original raw diff/status/file hashes.
Release comparison against this legacy baseline uses that independently
reproduced fingerprint; it does not discard the baseline gate. Other historical
layouts fail closed and need their own independently evidenced migration record.

## Future engine changes

Create an ordinary feature branch/worktree from the accepted production tip.
Commit one logical renderer change per commit. Include these trailers, with
substantive rationale/contract text and comma-separated existing integration
paths under `tests/` or `scripts/`:

```text
Rationale: Explain the concrete failure or constraint motivating the change.
Contract: Describe the externally observable behavior that must remain true.
Tests: tests/affected_behavior_test.py
```

Use actual fixture paths and record execution evidence separately; a test pointer
alone does not claim that the test passed. Export rejects missing, empty or duplicate
trailers and missing or escaping test paths.
Enable `git config rerere.enabled true` for future rebases and inspect
`git range-diff <old-upstream>..<old-tip> <new-upstream>..<new-tip>` when upgrading.
Export only commits after the accepted tip:

```powershell
python scripts/export-engine-patches.py --author-repo E:/Work/engine-feature --output artifacts/engine-export-next
```

The exporter rejects dirty work, merges, an alternate historical --since,
changed original patch bytes and new commits without the required metadata. `git format-patch --binary --full-index` writes only
new numbered patches and a proposed lock. An isolated index applies the actual
exported patch bytes and must reproduce HEAD's exact Git tree, including binary
changes/deletions. Review the proposal, append fixture/contract/triage metadata,
regenerate the inventory, independently replay and run affected backend tests
before accepting the new tip. Historical patch bytes are never regenerated.
The former 50 record-*-patch.py scripts are retired; recover them with
`git show 73600da:scripts/<historical-name>` if an investigation needs their
original behavior. The patch baselines are historical evidence, not authoring
inputs. See [patch triage](../patch-triage.md) for disposition and local proposals.

## Offline restore

A version 3 release source snapshot packs the exact fork tip and reachable
ancestors, retains the shallow boundary, hashes all inputs, and includes launchers,
presets, formatting/pytest/CI configuration and Git hooks. After extracting its
integration/ tree, restore the engine at a new location:

```powershell
python scripts/source-history.py restore-engine-snapshot --archive source-snapshot.zip --output third_party/cycles
```

This verifies member hashes, matches the current lock, restores exact commit IDs
and independently replays the patch stack. Version 2 snapshots remain checkable as
historical evidence; they contain upstream plus patch bytes and cannot restore
the exact accepted authoring commit IDs by themselves.

The broader source-history recovery workflow includes all committed integration
refs (including the experiments archive branch), all reachable authoring refs,
detached authoring HEAD and exact production source delta/index:

```powershell
python scripts/source-history.py recovery --output artifacts/recovery/source-history.zip --report artifacts/recovery/capture.json
python scripts/source-history.py restore --archive artifacts/recovery/source-history.zip --output artifacts/recovery/restore-check --report artifacts/recovery/restore.json
```

Destinations must be new. Restore uses Python and Git offline, verifies hashes,
object graphs and the production fingerprint. SDKs, dependency payloads and GPU
binaries remain external. A source migration is not runtime qualification: every
new release still requires Image Iris support in built/installed runtimes and an
Image Iris render, DLSS and Gaussian-splat preservation checks before delivery or
activation.
