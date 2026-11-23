# Current renderer patch test recipes

These recipes cover the **44 unique related primary fixtures** associated with
75 of the 76 locked patches. Association is not isolated patch certification.
Patch 0002 is an SDK link/build contract with no targeted behavioral fixture;
its build/delegate-load release checks remain separate. The catalog records
existing assertions and executable invocation requirements. It does not claim
that these cases, backends or every branch have been run on the current build.

The backend policies are deliberately different:

- **29 selected-device fixtures**, including four GUI workflows, can be queued
  separately for CPU and an explicit supported GPU identity.
- **11 coupled fixtures** run CPU plus `ch.default_device()` internally and
  ignore a launcher's single-device selection. Each runs once; its actual
  default GPU is checked explicitly and recorded. Passing a 3080 ID does not
  make these fixtures stop using a 4090 selected by `ch.default_device()`.
- **Three native host contracts** exercise arithmetic or storage, not GPU
  rendering. They run once: light-tree cache blocks, subdivision/socket bits,
  and subdivision-count overflow.
- **One pure SDK dependency-policy unit suite** runs with Python separately;
  repeating it as GPU work would add no backend evidence.

The complete plan adds two fresh prerequisites: the CPU texture fixture creates
its current texture HIP, and the independent subdivision harness fixture creates
reference images after the native subdivision fixture. These are generated from
maintained sources before their consumers; old artifact files are not required.
One identity preflight binds the generated plan to its manifest and checks the
actual GPU/default GPU. With one selected/default GPU, the complete opt-in plan
has **75 queued cases** (eight GUI), plus one pure Python command. These counts
are recipes, not passing-test counts.

Generate a bounded current plan after the build is frozen:

```powershell
python scripts/patch-test-matrix.py --build-directory build/windows-h22.0.466-release --gpu-device "EXACT_GPU_ID_FROM_THIS_RUNTIME" --label patch-boundaries --output artifacts/patch-boundary-cases.json
./scripts/verify-native-release.ps1 -Cases artifacts/patch-boundary-cases.json -Name patch-boundaries -TimeoutSeconds 300
```

The default contains only the device/manifest preflight, host overflow contract,
and lifetime plus mesh-sharing contracts on CPU and the selected GPU: six queued
cases. It is suitable for the current host-save change's renderer boundary check.
It is not a complete renderer requalification. CPU-only generation uses
`--backends CPU` and produces four queued cases. No native work happens during
plan generation.

Use `--fixtures` with exact primary file names for a targeted plan. Use
`--all-primary` only when complete renderer qualification is intended; some
primary fixtures have lengthy 1024-sample references, heavy mesh-light scenes
or GUI workflows. Select an appropriate queue timeout for those cases rather
than interpreting the bounded example's timeout as a universal budget.
`--default-gpu-device` can explicitly name the actual default GPU when selected
cases also run on a second card. The preflight rejects a falsely relabelled
coupled backend before the tests begin. Native queues remain serialized.

Every generated cases file has an adjacent `.plan.json` recording fixture hashes,
manifest hash, exact required CLI arguments, backend policies, expected actual
device scopes, the pure command and unassigned patch. Release-runtime recipes
supply the exact SDK, selected host and report; RLS hair/selective recipes supply
unique labels and output directories. Current selective-RLS equality checks
require no historical baseline. Nested-dielectrics plumbing receives its output
root; it remains plumbing/diagnostic evidence rather than a complete optical
reference for every overlapping-medium path.

Patch 0063 now points to the real harness socket-bit assertions (scalar signed
zero/subnormal, vector, float2 and transform values) through the subdivision
contract wrapper. Patch 0075's arithmetic checks cross signed 32-bit boundaries
without enormous allocation; private `DiagSplit` allocation-error branches are
outside their coverage. Historical records remain context and do not qualify a
new SDK, installation or GPU. Feature-off image, EXR metadata and mutation claims
must follow each actual assertion, not the number of linked patches.

Inspect the maintained machine-readable catalog with
`python scripts/patch-test-matrix.py --catalog`; pure regression coverage is
`python tests/patch_test_matrix_test.py`. The following table lists each current
primary association and its declared execution scope.

| Patches | Primary related fixture | Scope | GUI |
|---|---|---|---|
| 0001, 0070 | [houdini_release_runtime_smoke.py](../tests/houdini_release_runtime_smoke.py) | CPU + actual default GPU |  |
| 0003 | [houdini_points_smoke.py](../tests/houdini_points_smoke.py) | CPU + actual default GPU |  |
| 0004 | [houdini_volume_motion_smoke.py](../tests/houdini_volume_motion_smoke.py) | CPU + actual default GPU |  |
| 0005 | [core_point_lifetime_smoke.py](../tests/core_point_lifetime_smoke.py) | Selected CPU/GPU |  |
| 0006, 0018, 0021, 0036, 0037 | [houdini_scene_interruption_smoke.py](../tests/houdini_scene_interruption_smoke.py) | Selected CPU/GPU |  |
| 0007 | [houdini_shader_retirement_smoke.py](../tests/houdini_shader_retirement_smoke.py) | CPU + actual default GPU |  |
| 0008, 0009, 0015 | [houdini_subdivision_smoke.py](../tests/houdini_subdivision_smoke.py) | CPU + actual default GPU |  |
| 0010 | [houdini_texture_cache_smoke.py](../tests/houdini_texture_cache_smoke.py) | CPU + actual default GPU |  |
| 0011, 0012 | [houdini_color_interop_smoke.py](../tests/houdini_color_interop_smoke.py) | CPU + actual default GPU |  |
| 0013 | [houdini_texture_alpha_smoke.py](../tests/houdini_texture_alpha_smoke.py) | CPU + actual default GPU |  |
| 0014 | [houdini_unchanged_update_smoke.py](../tests/houdini_unchanged_update_smoke.py) | CPU + actual default GPU |  |
| 0016, 0017 | [houdini_progressive_smoke.py](../tests/houdini_progressive_smoke.py) | CPU + actual default GPU |  |
| 0019 | [houdini_world_smoke.py](../tests/houdini_world_smoke.py) | Selected CPU/GPU |  |
| 0020 | [houdini_world_sampling_smoke.py](../tests/houdini_world_sampling_smoke.py) | Selected CPU/GPU |  |
| 0022 | [houdini_progressive_layout_smoke.py](../tests/houdini_progressive_layout_smoke.py) | Selected CPU/GPU |  |
| 0023, 0026, 0027, 0031 | [houdini_velocity_normal_smoke.py](../tests/houdini_velocity_normal_smoke.py) | Selected CPU/GPU |  |
| 0024, 0038 | [houdini_sampled_displacement_smoke.py](../tests/houdini_sampled_displacement_smoke.py) | Selected CPU/GPU |  |
| 0025 | [houdini_temporal_displacement_smoke.py](../tests/houdini_temporal_displacement_smoke.py) | Selected CPU/GPU |  |
| 0028, 0048 | [houdini_sampled_attribute_displacement_smoke.py](../tests/houdini_sampled_attribute_displacement_smoke.py) | Selected CPU/GPU |  |
| 0029, 0065 | [houdini_sampled_instance_attribute_smoke.py](../tests/houdini_sampled_instance_attribute_smoke.py) | Selected CPU/GPU |  |
| 0030 | [houdini_tangent_basis_smoke.py](../tests/houdini_tangent_basis_smoke.py) | Selected CPU/GPU |  |
| 0032, 0055 | [houdini_pass_outputs_smoke.py](../tests/houdini_pass_outputs_smoke.py) | Selected CPU/GPU |  |
| 0033 | [core_light_tree_cache_smoke.py](../tests/core_light_tree_cache_smoke.py) | Native host only |  |
| 0034, 0035, 0046, 0047, 0054, 0056, 0058, 0059, 0060, 0061, 0062 | [houdini_retained_emitter_update_smoke.py](../tests/houdini_retained_emitter_update_smoke.py) | Selected CPU/GPU |  |
| 0039 | [houdini_light_material_smoke.py](../tests/houdini_light_material_smoke.py) | Selected CPU/GPU |  |
| 0040, 0050, 0051, 0052 | [houdini_shading_payload_smoke.py](../tests/houdini_shading_payload_smoke.py) | Selected CPU/GPU |  |
| 0041 | [houdini_camera_coordinate_displacement_smoke.py](../tests/houdini_camera_coordinate_displacement_smoke.py) | Selected CPU/GPU |  |
| 0042, 0066 | [houdini_camera_sampling_gui_smoke.py](../tests/houdini_camera_sampling_gui_smoke.py) | Selected CPU/GPU | Yes |
| 0043, 0044 | [houdini_panorama_smoke.py](../tests/houdini_panorama_smoke.py) | Selected CPU/GPU |  |
| 0045 | [houdini_ramp_contracts_smoke.py](../tests/houdini_ramp_contracts_smoke.py) | Selected CPU/GPU |  |
| 0049 | [houdini_subdivision_ipr_smoke.py](../tests/houdini_subdivision_ipr_smoke.py) | Selected CPU/GPU | Yes |
| 0053 | [houdini_shader_curves_smoke.py](../tests/houdini_shader_curves_smoke.py) | Selected CPU/GPU |  |
| 0057 | [core_mesh_sharing_smoke.py](../tests/core_mesh_sharing_smoke.py) | Selected CPU/GPU |  |
| 0063 | [core_subdivision_shading_smoke.py](../tests/core_subdivision_shading_smoke.py) | Native host only |  |
| 0064 | [sdk_dependency_policy_test.py](../tests/sdk_dependency_policy_test.py) | Pure Python host |  |
| 0067 | [houdini_camera_dof_motion_smoke.py](../tests/houdini_camera_dof_motion_smoke.py) | Selected CPU/GPU |  |
| 0068 | [houdini_camera_intrinsic_motion_gui_smoke.py](../tests/houdini_camera_intrinsic_motion_gui_smoke.py) | Selected CPU/GPU | Yes |
| 0069 | [houdini_volume_smoke.py](../tests/houdini_volume_smoke.py) | CPU + actual default GPU |  |
| 0071 | [houdini_image_iris_color_smoke.py](../tests/houdini_image_iris_color_smoke.py) | Selected CPU/GPU | Yes |
| 0072 | [houdini_adaptive_target_smoke.py](../tests/houdini_adaptive_target_smoke.py) | Selected CPU/GPU |  |
| 0073 | [houdini_rls_hair_smoke.py](../tests/houdini_rls_hair_smoke.py) | Selected CPU/GPU |  |
| 0074 | [houdini_nested_dielectrics_plumbing_smoke.py](../tests/houdini_nested_dielectrics_plumbing_smoke.py) | Selected CPU/GPU |  |
| 0075 | [core_subdivision_count_overflow_smoke.py](../tests/core_subdivision_count_overflow_smoke.py) | Native host only |  |
| 0076 | [houdini_selective_rls_smoke.py](../tests/houdini_selective_rls_smoke.py) | Selected CPU/GPU |  |
