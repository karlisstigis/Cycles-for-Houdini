# Publication review

Keep source publication and binary distribution as separately reviewed decisions.
An internal candidate and a green runtime check do not close these gates.

- Select the exact integration and engine commits and the intended public refs.
  Review their complete reachable histories, annotated tags, notes, author metadata
  and release source archives. The engine checkout is shallow; document its boundary.
  Review upstream attribution separately from private contributor information.
- Exclude private experiments and the private Blender worker digest-cache prototype
  from the selected publication material and its history. Distinguish that prototype
  from accepted production digest reuse. Deleted files, archived experiment branches,
  old release tags and review notes remain reachable locally. Agree an explicit
  public/private file and ref inventory before exporting; do not publish all local
  refs or reuse a full workstation recovery archive as a public source release.
- Remove personal paths and private development evidence from the public export.
  Inspect all reachable text objects and metadata, plus binary/archive contents.
  A pattern scan with no credential matches is evidence of that scan's scope,
  not a certification that secrets are absent.
- Run `scripts/release-package.py audit` against the actual release build and
  preserve its report. Close every notice gate. The November 10 internal candidate
  reports blocked OptiX distribution-terms review, DLSS attribution review,
  and a missing OpenPGL notice inventory. The current source captures the pinned
  OpenPGL originals; verify their inclusion in the final rebuilt payload.
  Preserve the NVIDIA review records (kept with the private project records).
  Notice presence alone does not resolve distribution terms or mark approval.
- Verify the exact package, installer receipt, staged notices and isolated installed
  payload against their manifest hashes. Include SDK header/static dependencies,
  OpenPGL and icon source/license obligations in the component inventory. Keep host
  SDK runtime files external as required by the package design. Preserve original
  per-file notices and check license text encoding in the exported material.
- Qualify the selected final candidate using the
  [release workflow](release-workflow.md). Retain built and installed Image Iris
  support reports and render checks for both samplers, and preserve DLSS Ray
  Reconstruction and native Gaussian splats. Complete the required Windows
  Apps/uninstaller lifecycle and target coexistence matrix before publication.
- Record outstanding approvals and validation limits honestly. Keep private audit
  reports under ignored `artifacts/phase2-readiness/audit`; publish only a reviewed,
  anonymized summary. This checklist records engineering gates and does not grant
  legal permission to distribute.
