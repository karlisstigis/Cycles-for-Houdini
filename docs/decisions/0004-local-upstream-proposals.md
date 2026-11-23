# Keep upstream proposals local

The [fork policy](0005-cycles-fork-policy.md) supersedes the local-only policy for reproducible upstream bug fixes. Proposal preparation stays local until submission.

Renderer fixes are developed in the local Cycles fork. Keep proposal branches,
patches, reproducers and review notes on the workstation and its recovery disk.
Changes enter the shipping build through the reviewed patch stack in
`source.lock.json`. Publishing a branch, issue or pull request to a third
party requires a separate instruction from the project owner.
