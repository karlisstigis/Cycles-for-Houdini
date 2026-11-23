# Integrator session and output grants

Accepted 2026-11-10 under [ADR 0005](0005-cycles-fork-policy.md). Each row grants glue status for one patch on the recorded Cycles base. The directory contains path-trace scheduling, output and denoising code. The kernel and integrator review rule still applies to every new change.

Upstream Cycles commit `74e9d9e48f86330987d7392166a51b3652d7c557`

| Patch | Granted SHA-256 |
| --- | --- |
| `patches/0016-output-sample-statistics.patch` | `f6d00c8af08bc0c43c88f3ddb62436d7257c88ba43fc5a444211c2525695eba0` |
| `patches/0017-output-display-ownership.patch` | `3adb0840d0886a606ab6b2fd6b8e79097d3f542f3297d904d6be3e0b79b90334` |
| `patches/0021-scene-sampling-interruption.patch` | `96523849cccee06a1453eea9f7b754fc7a77fe2a04b630e94b62fe6ce32940a3` |
| `patches/0022-progressive-output-layout.patch` | `5f6420fd8bcdb089adb7a0d94447b8067f16df942ca97e5eecdaaba6ab965321` |
| `patches/0032-compact-output-passes.patch` | `1bee5f61a4292dccc123d26a01734e28b1ccddfbe6c3fdcf26be9c939f3ef0d7` |
| `patches/0037-latched-quick-cancellation.patch` | `a5b38078f5823c1f6ca2694c890423d2d3ab26e7e0b3d32f052f79ca55924e41` |
| `patches/0072-interactive-adaptive-target.patch` | `2deb22f9f09488e01c995a82539f0fa179d0c322f36da2f6af0a75ecb63806e6` |
| `patches/0085-temporal-output-sample-count.patch` | `23fa2425fc582df90c6c80cb8f15db0dfcf62a16e3c6e69436cb5dc80c96b92b` |

A changed diff, including a conflict resolution, requires full review. A rebase expires all eight grants even when the bytes replay unchanged. Leave this historical record intact. Retire each seam or issue a new explicit decision identifying the approved patch, hash and target commit. Ordinary edits to triage, inventory or the source lock cannot renew a grant.

0016, 0017, 0021 and 0037 are candidates for upstream API proposals. Remove each local exception once its upstream replacement has been verified.
