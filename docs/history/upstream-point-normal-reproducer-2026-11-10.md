# Point-sphere normal correction: local upstream reproducer

The standalone CPU regression fails on unmodified Cycles
`74e9d9e48f86330987d7392166a51b3652d7c557` and passes after applying only patch
0003 (`3445bf84e54bfdd16e680a7d053a00b90f1f15a41e23c04e775bd71f1f3e784b`).
An isolated local branch in
`artifacts/phase3-proposals/cycles` contains that single engine correction at
`ea3282ca425d3a78235c03d8977254464042cc8a`. Nothing was submitted publicly.

| Measurement | Stock upstream | Isolated correction |
| --- | ---: | ---: |
| Kernel checks | 504 | 504 |
| Failed checks | 144 | 0 |
| Identity/uniform/applied control failures | 0 | 0 |
| Maximum normal-vector error | 1.08215 | 0.000002208 |
| Maximum directional diffuse-response error | 0.86459 | 0.000000506 |

The fixture includes the actual engine `point_intersect.h`, `object.h`,
`motion_point.h`, math utilities and allocator implementation. It calls
`point_intersect()` and `point_shader_setup()` directly. There is no Houdini
adapter, exporter, installed renderer library or copied point-normal algorithm.
The only harness type adaptation supplies the actual shared `KernelGlobalsCPU`
arrays in place of constructing a render-thread/profiler/OSL lifetime. Kernel
arithmetic and geometry functions remain the source headers under test.

The independent oracle differentiates the implicit ellipsoid equation in double
precision. Tests cover identity, uniform and nonuniform scale, rotation,
reflection, a precomputed object-motion transform and the transform-applied
branch. Both static and three-step sampled point centers are evaluated at three
ray times and twelve surface directions. The selected point is intersected by
the real kernel before normal setup. Besides geometric/shading normals, the
fixture checks intersection distance and shader identity. Normal-vector error
must stay below `3e-6`.

Reproduce from a Windows developer environment, or let the optional toolchain
initializer supply Visual Studio's environment:

```powershell
python scripts/reproduce-point-normal.py `
  --cycles-root artifacts/phase3-proposals/cycles `
  --tbb-include third_party/cycles/lib/windows_x64/tbb/include `
  --toolchain-cmd scripts/toolchain.cmd `
  --output artifacts/phase3-proposals/point-normal-reproduction-new
```

The script archives the exact unmodified base twice, applies only 0003 to the
second isolated tree, compiles the same fixture against both sets of headers,
and requires stock failure, corrected success and unaffected controls. It never
edits the supplied repository. A C++20 compiler and TBB headers are required;
there is no Houdini SDK requirement. A non-MSVC compiler can be selected with
`--compiler`. MSVC execution used 14.42.34433 with `/O2`, and linked no TBB DLL.

Raw evidence is `artifacts/phase3-proposals/point-normal-final/report.json`,
the neighboring `before.json` and `after.json`, compile logs, both archived source
trees and executables. The report records patch/fixture/header hashes and exact
compiler commands. The fixture hash is
`613d7bc0057ff3f54afcf8fee25b0e40005fa3e53f8b8abd034d24d2ec3a718a`.

The kernel fixture measures the analytic Lambert cosine. A second fixture
rendered native point spheres and a smooth 65,536-triangle reference ellipsoid
in a standalone CPU Cycles session at 64 × 64. On the nonuniform case the
maximum absolute Beauty error fell from 0.614715 to 0.001137. The uniform
control stayed below 0.0001. The mirrored nonuniform case also passed the
0.002 reference tolerance with the isolated fix. Raw float planes and the
comparison report are in `artifacts/phase3-proposals/point-images/`.
The standalone libraries used here have Embree and OptiX disabled. Those
backend image comparisons and an audit against the selected stable release
remain proposal checks. Production patch 0003 stays in the locked stack.
