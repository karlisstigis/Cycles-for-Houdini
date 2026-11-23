# GPU light-pass reconstruction precision

Blender 5.3 stores diffuse and glossy path fractions independently in
`SpectrumCompressedOnGPU`, whose GPU storage is `packed_half3`. The film computes
transmission as `1 - diffuse - glossy`. For a mixture with no transmission,
independent half rounding can leave a nonzero remainder. Transmission's zero
color prevents that remainder from surviving light-pass division and subsequent
reconstruction. This affects direct shadow contributions and fractions retained
after the first surface bounce, including mixed subsurface/reflection materials.

The simple non-transmitting pair 0.2 / 0.8 demonstrates the defect: its stored
half fractions are 0.199951171875 / 0.7998046875, leaving a false transmission
remainder of 0.000244140625. Float fractions give a zero remainder. This is an
auxiliary decomposition error; it does not require resampled light sampling.

Two complete candidate captures reproduced it with RLS off and on. Mixed-case
peak reconstruction errors were 0.000493 / 0.000632, while independent same-seed
Beauty repeats differed by at most 0.000002861. An independent official Blender
5.3 RC f78a5e153839 OptiX control also failed the unchanged 0.00002 absolute and
relative reconstruction tolerance: peak 0.000365, RMSE 0.00001675, 130 failing RGB
entries. All raw light passes, percentiles and repeats are retained in the
candidate QA `rls-mixed-diagnosis` directory.

Patch 0083 restores `PackedSpectrum` (`packed_float3`) only for
`pass_diffuse_weight` and `pass_glossy_weight` in main and shadow path state.
Existing float accessors/copies remain valid. Each changed GPU field grows from
6 to 12 bytes: 12 additional bytes per enabled main slot and 12 per enabled
shadow slot, before allocator overhead. These fields remain gated by
`KERNEL_FEATURE_LIGHT_PASSES`, which the film enables for written light/volume
passes. Beauty-only state has no additional fields or allocation. Other compact
state, path throughput, the RLS estimator and assertion tolerances are unchanged.

The maintained 24-case selective-RLS fixture saves every raw plane before its
reconstruction assertion and records peak/RMSE, including on failure. All 24
cases passed the original tolerances after the repair on CPU, OptiX RTX 4090 and
CUDA RTX 3080 Ti. The largest reconstruction residuals were respectively
0.00000477, 0.00000572 and 0.00000381. Before/after Beauty was bit-identical on CPU
and within the existing 0.000003 absolute/relative repeatability tolerance on
both GPUs; coverage alpha was exact for all 72 cases. The candidate QA
`rls-mixed-diagnosis/after-analysis.json` records all raw-plane deltas per case.
