# SPDX-License-Identifier: Apache-2.0
[CmdletBinding()]
param(
    [string]$HoudiniRoot='',
    [string]$BuildDirectory='',
    [switch]$Gui,
    [switch]$GuiOnly,
    [switch]$Hydra,
    [switch]$NativeUi,
    [switch]$StopOnFailure,
    [ValidateSet('fast','extended','nightly')][string]$Tier,
    [string]$Artifacts='',
    [string]$FixtureArtifacts='',
    [string]$TestDependencies='',
    [string]$NightlySelectionFile='',
    [string]$CatalogueSelectionFile='',
    [string[]]$Tests=@(),
    [switch]$ListTests,
    [int]$GuiRepetitions=2,
    [string[]]$GuiDevices=@(),
    [int]$TimeoutSeconds=120
)
$ErrorActionPreference='Stop'
if ($CatalogueSelectionFile -and ($Tier -ne 'nightly' -or $NightlySelectionFile)) { throw 'Catalogue selection requires -Tier nightly and no nightly partition.' }
if ($NightlySelectionFile -and $Tier -ne 'nightly') { throw 'Nightly partitions require -Tier nightly.' }
if ($Tier -and ($Gui -or $GuiOnly -or $NativeUi -or $Hydra -or $Tests.Count -or $GuiDevices.Count -or $PSBoundParameters.ContainsKey('GuiRepetitions'))) {
    throw 'A named tier selects its own tests, devices and repetitions. Use -Tests or the GUI options for a custom run.'
}
if($GuiRepetitions -lt 1 -or $GuiRepetitions -gt 10){throw 'GuiRepetitions must be from 1 to 10.'}
if($NativeUi -and $Hydra){throw 'NativeUi selects native SOP and viewer checks; omit Hydra.'}
if($GuiOnly -and $Hydra){throw 'GuiOnly selects native GUI workflows; omit Hydra.'}
if($GuiOnly -and $Tests.Count){throw 'Tests selects headless cases; omit GuiOnly.'}
if($GuiOnly -and -not $GuiDevices.Count){throw 'GuiOnly needs explicit GuiDevices and existing headless fixture artifacts.'}
if($GuiOnly){$Gui=$true}
$taskRoot=Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'native-test-context.ps1')
. (Join-Path $PSScriptRoot 'archive-test-manifest.ps1')
. (Join-Path $PSScriptRoot 'native-test-arguments.ps1')
. (Join-Path $PSScriptRoot 'test-artifact-context.ps1')
$context=Get-NativeTestContext -HoudiniRoot $HoudiniRoot -BuildDirectory $BuildDirectory
$HoudiniRoot=$context.HoudiniRoot
$build=$context.BuildDirectory
# Reference subprocesses resolve through native_test_runtime.py and verify the
# auxiliary binary against this same explicitly selected build manifest.
$runName=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
$artifactRoot=Get-TestEvidenceBase -Artifacts $Artifacts
$destination=Join-Path $artifactRoot $runName
New-Item -ItemType Directory -Path $destination | Out-Null
$manifestPath=$context.Manifest
$capturedManifestHash=Save-NativeTestManifest -Context $context -Destination (Join-Path $destination 'build-manifest.json')
$summary=[ordered]@{started_utc=[DateTime]::UtcNow.ToString('o');result='running';tests=@();build_directory=$context.BuildDirectory;build_manifest_sha256=$capturedManifestHash}
$summary.scope=if($GuiOnly){'native-gui-regression'}elseif($NativeUi){'native-ui'}elseif($Hydra){'full-native-and-hydra-smoke'}else{'full-native'}
$summary.runtime_identity=$context.RuntimeIdentity
$summaryPath=Join-Path $destination 'summary.json'
function Save-Summary {
    $temporary=$summaryPath+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($summary | ConvertTo-Json -Depth 16),[Text.UTF8Encoding]::new($false))
    if(Test-Path -LiteralPath $summaryPath){[IO.File]::Replace($temporary,$summaryPath,[NullString]::Value)}else{[IO.File]::Move($temporary,$summaryPath)}
}
Save-Summary
$previousTestEnvironment=Save-TestProcessEnvironment
try{
$env:HOUDINI_PACKAGE_DIR=$context.PackageDirectory
$env:HOUDINI_PATH='&'
# Developer runs use their own preferences: an installed Cycles package in the
# artist's Houdini preferences must never replace the build under test.
$env:HOUDINI_USER_PREF_DIR=Join-Path $destination 'prefs/houdini__HVER__'
$env:HOUDINI_NO_ENV_FILE='1'
$env:HOUDINI_DSO_ERROR='2'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:CYCLES_HOUDINI_TEST_IDENTITY=$context.RuntimeIdentity | ConvertTo-Json -Compress
. (Join-Path $PSScriptRoot 'test-dependency-context.ps1')
$toolsContext=Get-NativeTestToolsContext -Target $TestDependencies
$TestDependencies=$toolsContext.Target
$env:CYCLES_HOUDINI_TEST_DEPENDENCIES=[IO.Path]::GetFullPath($TestDependencies)
$env:CYCLES_HOUDINI_TEST_ARTIFACTS=if($FixtureArtifacts){[IO.Path]::GetFullPath($FixtureArtifacts)}elseif($GuiOnly){Get-DefaultTestArtifactRoot}else{Join-Path $destination 'migrated'}
$summary.test_dependencies=$TestDependencies
$summary.fixture_artifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS
Save-Summary
$testCases=@(
    @('runtime_assets_test.py','artifacts/stage0/runtime-assets/report.json'),
    @('native_test_runtime_test.py',$null),
    @('blend_cache_test.py',$null),
    @('core_subdivision_count_overflow_smoke.py','artifacts/stage2/subdivision/count-overflow/report.json'),
    @('core_object_volume_bounds_smoke.py','artifacts/stage2/objects/volume-bounds/report.json'),
    @('core_point_lifetime_smoke.py','artifacts/stage2/particles/lifetime/report.json'),
    @('verification_outcome_test.py','artifacts/stage0/verification-outcome/report.json'),
    @('native_verification_archive_test.py','artifacts/stage0/native-verification-archive/report.json'),
    @('release_package_test.py','artifacts/release-package-unit.json'),
    @('sdk_dependency_policy_test.py','artifacts/sdk-dependency-policy-unit.json'),
    @('startup_dependency_guard_test.py','artifacts/startup-dependency-guard-unit.json'),
    @('material_override_evaluation_test.py',$null),
    @('material_override_expression_test.py','artifacts/material-override-expressions-unit.json'),
    @('camera_dof_motion_source_test.py',$null),
    @('camera_intrinsic_reference_test.py','artifacts/stage2/cameras/intrinsic-motion/reference-unit.json'),
    @('tapered_volume_reference_test.py','artifacts/stage3/volumes/tapered/reference-unit.json'),
    @('houdini_startup_dependency_recovery_smoke.py','artifacts/stage0/startup-dependency/report.json'),
    @('control_bridge_test.py',$null),
    @('houdini_native_smoke.py','artifacts/stage0/houdini-native-smoke.json'),
    @('houdini_rop_layout_smoke.py','artifacts/rop-layout/houdini-rop-layout-smoke.json'),
    @('houdini_showcase_refresh_smoke.py','artifacts/current-schema-cleanup/showcase/report.json'),
    @('houdini_display_smoke.py','artifacts/stage1/houdini-display-smoke.json'),
    @('display_queue_test.py',$null),
    @('queued_feedback_test.py',$null),
    @('houdini_cop_image_ownership_smoke.py','artifacts/stage3/textures/cop-ownership/report.json'),
    @('houdini_cop_texture_smoke.py','artifacts/stage3/textures/cop-render/report.json'),
    @('houdini_cop_world_smoke.py','artifacts/stage3/world/cop/report.json'),
    @('houdini_environment_recovery_smoke.py','artifacts/stage3/world/recovery/report.json'),
    @('houdini_cop_shutter_smoke.py','artifacts/stage3/textures/cop-shutter/report.json'),
    @('houdini_cop_batch_smoke.py','artifacts/stage3/textures/cop-batch/report.json'),
    @('core_owned_image_smoke.py','artifacts/stage3/textures/owned-engine/report.json'),
    @('houdini_shader_bypass_preview_smoke.py','artifacts/stage3/materials/bypass-preview/report.json'),
    @('houdini_shader_preview_displacement_smoke.py','artifacts/stage3/materials/preview-displacement.json'),
    @('houdini_shader_input_states_smoke.py','artifacts/stage3/materials/input-states/report.json'),
    @('houdini_hash_displacement_smoke.py','artifacts/stage3/materials/hash-displacement/report.json'),
    @('houdini_hash_displacement_recovery_smoke.py','artifacts/stage3/materials/hash-displacement/recovery-report.json'),
    @('houdini_camera_coordinate_displacement_smoke.py','artifacts/stage3/displacement-camera-coordinates/report.json'),
    @('houdini_scene_query_displacement_smoke.py','artifacts/stage3/displacement-scene-query/report.json'),
    @('soho_update_guard_test.py',$null),
    @('houdini_scene_observer_smoke.py','artifacts/stage3/world/recovery/observer-report.json'),
    @('houdini_panel_motion_timing_smoke.py','artifacts/stage1/panel-motion-timing/houdini-panel-motion-timing-smoke.json'),
    @('houdini_display_queue_smoke.py','artifacts/stage1/display-queue/houdini-display-queue-smoke.json'),
    @('houdini_gpu_input_ownership_smoke.py','artifacts/stage1/display-input/ownership-report.json'),
    @('houdini_display_storage_smoke.py','artifacts/stage1/display-input/storage-report.json'),
    @('houdini_display_profile_smoke.py','artifacts/stage1/display-profile/houdini-display-profile-smoke.json'),
    @('houdini_display_data_reference_smoke.py','artifacts/stage1/display-profile/data-reference-report.json'),
    @('houdini_display_alpha_smoke.py','artifacts/stage3/color/display-alpha/houdini-display-alpha-smoke.json'),
    @('houdini_transparency_smoke.py','artifacts/stage3/color/transparency/houdini-transparency-smoke.json'),
    @('houdini_texture_alpha_smoke.py','artifacts/stage3/color/transparency/texture-alpha/houdini-texture-alpha-smoke.json'),
    @('houdini_transparency_batch_smoke.py','artifacts/stage3/color/transparency/batch/houdini-transparency-batch-smoke.json'),
    @('houdini_noise_smoke.py','artifacts/stage3/materials/noise/houdini-noise-smoke.json'),
    @('houdini_courtyard_smoke.py','artifacts/examples/sop-fx-courtyard/houdini-courtyard-smoke.json'),
    @('houdini_packed_batch_smoke.py','artifacts/stage2/packed/batches/houdini-packed-batch-smoke.json'),
    @('houdini_packed_scale_smoke.py','artifacts/packed-scale/houdini-packed-scale-worker-cpu.json'),
    @('core_snapshot_retention_smoke.py','artifacts/stage1/snapshots/core-snapshot-retention-smoke.json'),
    @('core_session_lifecycle_smoke.py','artifacts/stage3/session-lifecycle/core-session-lifecycle-smoke.json'),
    @('core_object_binding_smoke.py','artifacts/stage1/object-bindings/core-object-binding-smoke.json'),
    @('core_object_pose_smoke.py','artifacts/stage1/object-poses/core-object-pose-smoke.json'),
    @('core_displacement_hash_smoke.py','artifacts/stage3/displacement-hash/report.json'),
    @('core_instance_motion_smoke.py','artifacts/stage3/materials/instance-motion/core-report.json'),
    @('houdini_sampled_instance_attribute_smoke.py','artifacts/stage3/materials/sampled-instance-fields/report.json'),
    @('houdini_sampled_instance_reuse_smoke.py','artifacts/stage3/materials/sampled-instance-reuse.json'),
    @('core_pointiness_smoke.py','artifacts/stage3/materials/pointiness/core-report.json'),
    @('houdini_displacement_invalidation_smoke.py','artifacts/stage3/displacement-invalidation/report.json'),
    @('houdini_object_pose_acquisition_smoke.py','artifacts/stage1/early-poses/houdini-object-pose-acquisition-smoke.json'),
    @('houdini_pose_variants_smoke.py','artifacts/stage1/object-poses/houdini-pose-variants-smoke.json'),
    @('core_compact_image_smoke.py','artifacts/stage1/compact-images/core-compact-image-smoke.json'),
    @('core_mesh_payload_smoke.py','artifacts/stage3/stylesheet-storage/core-report.json'),
    @('core_mesh_sharing_smoke.py','artifacts/stage3/mesh-sharing/core-report.json'),
    @('core_mesh_family_scale_smoke.py','artifacts/stage3/mesh-family-scaling/core-report.json'),
    @('houdini_mesh_sharing_lighting_smoke.py','artifacts/stage3/mesh-sharing/lighting-report.json'),
    @('houdini_emission_face_energy_smoke.py','artifacts/stage3/light-tree-energy/face-report.json'),
    @('houdini_mesh_sharing_storage_smoke.py','artifacts/stage3/mesh-sharing/storage-report.json'),
    @('houdini_mesh_sharing_uv_pass_smoke.py','artifacts/stage3/mesh-sharing/uv-report.json'),
    @('houdini_light_update_profile_smoke.py','artifacts/stage3/lighting-transfer/profile-report.json'),
    @('houdini_light_tree_retained_cancel_smoke.py','artifacts/stage3/lighting-transfer/cancel-report.json'),
    @('core_device_range_smoke.py','artifacts/stage3/performance/device-ranges/report.json'),
    @('houdini_mesh_binding_preparation_smoke.py','artifacts/stage3/stylesheet-storage/preparation-report.json'),
    @('houdini_mesh_payload_transport_smoke.py','artifacts/stage3/stylesheet-storage/transport-report.json'),
    @('houdini_mesh_field_transport_smoke.py','artifacts/stage3/stylesheet-storage/field-transport-report.json'),
    @('houdini_named_capture_reuse_smoke.py','artifacts/stage3/stylesheet-storage/named-capture-reuse-report.json'),
    @('houdini_camera_capture_guard_smoke.py','artifacts/stage1/camera-capture/houdini-camera-capture-guard-smoke.json'),
    @('houdini_navigation_snapshot_smoke.py','artifacts/stage1/navigation/houdini-navigation-snapshot-smoke.json'),
    @('houdini_navigation_time_revisit_smoke.py','artifacts/stage1/navigation-time/houdini-navigation-time-revisit-smoke.json'),
    @('houdini_navigation_output_dependency_smoke.py','artifacts/stage1/navigation/houdini-navigation-output-dependency-smoke.json'),
    @('houdini_queued_worker_smoke.py','artifacts/stage1/queued-workers/houdini-queued-worker-smoke.json'),
    @('houdini_scene_interruption_smoke.py','artifacts/stage1/sampling-interruption/houdini-scene-interruption-smoke.json'),
    @('houdini_lightgroups_smoke.py','artifacts/stage3/passes/lightgroups/houdini-lightgroups-smoke.json'),
    @('houdini_cryptomatte_smoke.py','artifacts/stage3/passes/cryptomatte/houdini-cryptomatte-smoke.json'),
    @('houdini_index_passes_smoke.py','artifacts/stage3/passes/indexes/houdini-index-passes-smoke.json'),
    @('houdini_shadow_catcher_smoke.py','artifacts/stage3/passes/shadow-catcher/houdini-shadow-catcher-smoke.json'),
    @('houdini_mixed_geometry_smoke.py','artifacts/stage3/mixed-geometry/houdini-mixed-geometry-smoke.json'),
    @('houdini_mesh_capture_cache_smoke.py','artifacts/stage1/mesh-capture/houdini-mesh-capture-cache-smoke.json'),
    @('houdini_mixed_geometry_fx_smoke.py','artifacts/stage3/mixed-geometry/houdini-mixed-geometry-fx-smoke.json'),
    @('houdini_mixed_geometry_motion_smoke.py','artifacts/stage3/mixed-geometry/houdini-mixed-geometry-motion-smoke.json'),
    @('houdini_object_scene_smoke.py','artifacts/stage3/native-scene/houdini-object-scene-smoke.json'),
    @('houdini_object_properties_smoke.py','artifacts/stage3/object-properties/houdini-object-properties-smoke.json'),
    @('houdini_light_properties_smoke.py','artifacts/stage3/light-properties/houdini-light-properties-smoke.json'),
    @('houdini_light_parenting_smoke.py','artifacts/stage3/light-properties/parenting/report.json'),
    @('houdini_light_handle_coordinates_smoke.py','artifacts/stage3/light-properties/handle-coordinates/report.json'),
    @('houdini_camera_refinement_smoke.py','artifacts/stage2/subdivision/houdini-camera-refinement-smoke.json'),
    @('houdini_render_settings_smoke.py','artifacts/stage3/render-settings/houdini-render-settings-smoke.json'),
    @('houdini_device_selection_smoke.py','artifacts/stage3/devices/houdini-device-selection-smoke.json'),
    @('houdini_explicit_device_selection_smoke.py','artifacts/stage3/devices/explicit/report.json'),
    @('houdini_aov_smoke.py','artifacts/stage3/passes/aovs/houdini-aov-smoke.json'),
    @('houdini_object_lights_smoke.py','artifacts/stage3/native-scene/houdini-object-lights-smoke.json'),
    @('houdini_object_scene_batch_smoke.py','artifacts/stage3/native-scene/batch/houdini-object-scene-batch-smoke.json'),
    @('houdini_resource_smoke.py','artifacts/stage1/houdini-resource-smoke.json'),
    @('houdini_diagnostics_smoke.py','artifacts/stage1/houdini-diagnostics-smoke.json'),
    @('houdini_diagnostic_sampling_smoke.py','artifacts/stage3/materials/diagnostic-sampling/report.json'),
    @('houdini_light_tree_cache_smoke.py','artifacts/stage1/light-tree-cache/report.json'),
    @('core_light_tree_cache_smoke.py','artifacts/stage3/performance/light-tree-compaction/core-report.json'),
    @('houdini_light_tree_compact_smoke.py','artifacts/stage1/light-tree-cache/report.json'),
    @('houdini_stylesheet_storage_baseline.py','artifacts/stage3/stylesheet-storage/baseline.json'),
    @('houdini_light_tree_parallel_smoke.py','artifacts/stage1/light-tree-cache/report.json'),
    @('houdini_light_tree_split_snapshot_smoke.py','artifacts/stage3/light-tree-split/paired-report.json'),
    @('houdini_light_tree_cache_budget_smoke.py','artifacts/stage1/light-tree-cache/budget-report.json'),
    @('houdini_memory_smoke.py','artifacts/stage1/memory/houdini-memory-smoke.json'),
    @('houdini_shader_catalogue_smoke.py','artifacts/stage3/materials/houdini-shader-catalogue-smoke.json'),
    @('houdini_shader_layout_smoke.py','artifacts/stage3/materials/layout/report.json'),
    @('core_subdivision_shading_smoke.py','artifacts/stage3/performance/subdivision-shading/core-report.json'),
    @('houdini_shading_payload_smoke.py','artifacts/stage3/materials/shading-payload/report.json'),
    @('houdini_attribute_layout_smoke.py','artifacts/stage3/materials/attribute-layout/report.json'),
    @('houdini_shading_delivery_benchmark.py','artifacts/stage3/performance/shading-delivery/report.json'),
    @('houdini_light_tree_upload_smoke.py','artifacts/stage3/performance/light-tree-uploads/report.json'),
    @('houdini_light_tree_compact_upload_smoke.py','artifacts/stage3/performance/light-tree-uploads/report.json'),
    @('houdini_mix_nodes_smoke.py','artifacts/stage3/materials/mix-nodes/report.json'),
      @('houdini_packed_instance_attribute_smoke.py','artifacts/stage3/materials/packed-instance-attributes/report.json'),
      @('houdini_packed_cd_smoke.py','artifacts/stage3/materials/packed-cd/report.json'),
      @('houdini_constant_attribute_smoke.py','artifacts/stage3/materials/constant-attributes/report.json'),
      @('houdini_pointiness_smoke.py','artifacts/stage3/materials/pointiness/report.json'),
      @('houdini_mesh_island_smoke.py','artifacts/examples/mesh-islands/report.json'),
      @('houdini_retained_emitter_update_smoke.py','artifacts/stage3/performance/retained-emitters/report.json'),
    @('houdini_shader_graph_smoke.py','artifacts/stage3/materials/graphs/houdini-shader-graph-smoke.json'),
    @('houdini_texture_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-texture-recovery-smoke.json'),
    @('houdini_binding_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-binding-recovery-smoke.json'),
    @('houdini_mixed_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-mixed-recovery-smoke.json'),
    @('houdini_material_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-material-recovery-smoke.json'),
    @('houdini_source_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-source-recovery-smoke.json'),
    @('houdini_volume_shader_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-volume-shader-recovery-smoke.json'),
    @('houdini_volume_field_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-volume-field-recovery-smoke.json'),
    @('houdini_dense_volume_smoke.py','artifacts/stage2/volumes/dense/report.json'),
    @('houdini_dense_volume_recovery_smoke.py','artifacts/stage2/volumes/dense-recovery/report.json'),
    @('houdini_dense_volume_cache_smoke.py','artifacts/stage2/volumes/dense-recovery/cache-report.json'),
    @('houdini_dense_volume_voxel_smoke.py','artifacts/stage2/volumes/dense-recovery/voxel-report.json'),
    @('core_volume_vector_smoke.py','artifacts/stage2/volumes/vector-components/contracts.json'),
    @('houdini_volume_vector_smoke.py','artifacts/stage2/volumes/vector-components/report.json'),
    @('houdini_parametric_curve_contracts.py','artifacts/stage2/hair/parametric/contracts.json'),
    @('houdini_parametric_curve_smoke.py','artifacts/stage2/hair/parametric/report.json'),
    @('houdini_parametric_curve_recovery_smoke.py','artifacts/stage2/hair/parametric/recovery-report.json'),
    @('houdini_parametric_curve_packed_smoke.py','artifacts/stage2/hair/parametric/packed-report.json'),
    @('houdini_strand_binding_refresh_smoke.py','artifacts/stage2/hair/parametric/binding-refresh-report.json'),
    @('houdini_shader_reactivation_smoke.py','artifacts/stage3/materials/reactivation/report.json'),
    @('houdini_packed_source_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-packed-source-recovery-smoke.json'),
    @('houdini_identity_subdivision_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-identity-subdivision-recovery-smoke.json'),
    @('houdini_particle_attribute_smoke.py','artifacts/stage3/particle-attributes/houdini-particle-attribute-smoke.json'),
    @('houdini_size_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-size-recovery-smoke.json'),
    @('houdini_crease_packed_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-crease-packed-recovery-smoke.json'),
    @('houdini_motion_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-motion-recovery-smoke.json'),
    @('houdini_normal_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-normal-recovery-smoke.json'),
    @('houdini_displacement_motion_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-displacement-motion-recovery-smoke.json'),
    @('houdini_sampled_displacement_smoke.py','artifacts/stage2/displacement/houdini-sampled-displacement-smoke.json'),
    @('houdini_temporal_displacement_smoke.py','artifacts/stage2/displacement/houdini-temporal-displacement-smoke.json'),
      @('houdini_sampled_override_smoke.py','artifacts/examples/sampled-overrides/report.json'),
      @('houdini_temporal_binding_reuse_smoke.py','artifacts/stage2/displacement/temporal-binding-reuse/report.json'),
      @('houdini_velocity_normal_smoke.py','artifacts/stage2/motion/velocity-normals/report.json'),
      @('houdini_independent_binding_motion_smoke.py','artifacts/stage2/displacement/independent-bindings/report.json'),
      @('houdini_shutter_coordinate_recovery_smoke.py','artifacts/stage3/materials/recovery/shutter-coordinates/report.json'),
      @('houdini_spatial_displacement_smoke.py','artifacts/stage2/displacement/spatial/report.json'),
      @('houdini_camera_lens_motion_smoke.py','artifacts/stage2/cameras/lens-motion/report.json'),
      @('core_camera_dof_motion_smoke.py','artifacts/stage2/cameras/dof-motion/core-report.json'),
      @('houdini_camera_dof_motion_smoke.py','artifacts/stage2/cameras/dof-motion/report.json'),
      @('core_camera_intrinsic_motion_smoke.py','artifacts/stage2/cameras/intrinsic-motion/core-report.json'),
      @('houdini_camera_intrinsic_motion_smoke.py','artifacts/stage2/cameras/intrinsic-motion/report.json'),
      @('houdini_camera_local_volume_origin_smoke.py','artifacts/stage2/cameras/local-volume-origin/report.json'),
      @('houdini_camera_sampling_smoke.py','artifacts/stage2/cameras/sampling/report.json'),
      @('houdini_panorama_coverage_smoke.py','artifacts/stage2/cameras/panorama/report.json'),
    @('houdini_sampled_attribute_displacement_smoke.py','artifacts/stage2/displacement/houdini-sampled-attribute-displacement-smoke.json'),
    @('houdini_nominal_binding_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-nominal-binding-recovery-smoke.json'),
    @('houdini_geometry_binding_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-geometry-binding-recovery-smoke.json'),
    @('houdini_domain_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-domain-recovery-smoke.json'),
    @('houdini_named_field_displacement_smoke.py','artifacts/stage2/displacement/houdini-named-field-displacement-smoke.json'),
    @('houdini_tangent_basis_smoke.py','artifacts/stage3/materials/tangent-bases/houdini-tangent-basis-smoke.json'),
    @('houdini_field_motion_smoke.py','artifacts/stage3/materials/field-motion/houdini-field-motion-smoke.json'),
    @('houdini_named_attribute_smoke.py','artifacts/stage3/materials/named-attributes/houdini-named-attribute-smoke.json'),
    @('houdini_shader_source_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-shader-source-recovery-smoke.json'),
    @('houdini_shading_attribute_recovery_smoke.py','artifacts/stage3/materials/recovery/houdini-shading-attribute-recovery-smoke.json'),
    @('houdini_light_update_smoke.py','artifacts/stage3/native-scene/light-update.json'),
    @('houdini_combined_material_smoke.py','artifacts/stage3/materials/combined/houdini-combined-material-smoke.json'),
    @('houdini_material_vop_smoke.py','artifacts/stage3/materials/vops/houdini-material-vop-smoke.json'),
    @('houdini_material_extensions_smoke.py','artifacts/stage3/materials/extensions/houdini-material-extensions-smoke.json'),
    @('houdini_texture_smoke.py','artifacts/stage3/textures/houdini-texture-smoke.json'),
    @('houdini_texture_cache_smoke.py','artifacts/stage3/textures/cache/houdini-texture-cache-smoke.json'),
    @('houdini_texture_normal_smoke.py','artifacts/stage3/textures/normals/houdini-texture-normal-smoke.json'),
    @('houdini_color_epoch_smoke.py','artifacts/stage3/color/epochs/houdini-color-epoch-smoke.json'),
    @('houdini_color_config_smoke.py','artifacts/stage3/color/houdini-config/report.json'),
    @('houdini_color_interop_smoke.py','artifacts/stage3/color/portable/houdini-color-interop-smoke.json'),
    @('houdini_texture_auto_smoke.py','artifacts/stage3/color/auto/houdini-texture-auto-smoke.json'),
    @('houdini_texture_auto_batch_smoke.py','artifacts/stage3/color/auto/batch/houdini-texture-auto-batch-smoke.json'),
    @('houdini_material_subnet_smoke.py','artifacts/stage3/materials/subnets/houdini-material-subnet-smoke.json'),
    @('houdini_material_ramp_smoke.py','artifacts/stage3/materials/ramps/houdini-material-ramp-smoke.json'),
    @('houdini_ramp_interpolation_smoke.py','artifacts/stage3/materials/ramp-interpolation/report.json'),
    @('houdini_shader_curves_smoke.py','artifacts/stage3/materials/shader-curves/report.json'),
    @('houdini_hue_ramp_smoke.py','artifacts/stage3/materials/hue-ramps/report.json'),
    @('core_hue_ramp_smoke.py','artifacts/stage3/materials/hue-ramp-contracts/report.json'),
    @('houdini_curve_mapping_table_smoke.py','artifacts/stage3/materials/curve-mapping/report.json'),
    @('houdini_color_ramp_rgba_smoke.py','artifacts/stage3/materials/color-ramp-rgba/report.json'),
    @('houdini_rgb_ramp_smoke.py','artifacts/stage3/materials/rgb-ramp/report.json'),
    @('houdini_rgba_promotion_storage_smoke.py','artifacts/stage3/materials/rgba-promotion/report.json'),
    @('houdini_color_ramp_storage_smoke.py','artifacts/stage3/materials/color-ramp-storage/report.json'),
    @('houdini_color_ramp_migration_smoke.py','artifacts/stage3/materials/color-ramp-migration/report.json'),
    @('houdini_ramp_control_recovery_smoke.py','artifacts/stage3/materials/ramp-control-recovery/report.json'),
    @('houdini_ramp_contracts_smoke.py','artifacts/stage3/materials/ramp-contracts/report.json'),
    @('houdini_material_assignment_smoke.py','artifacts/stage3/materials/assignments/houdini-material-assignment-smoke.json'),
    @('houdini_material_override_smoke.py','artifacts/stage3/material-overrides/report.json'),
    @('houdini_material_ramp_override_smoke.py','artifacts/stage3/materials/material-ramp-overrides/report.json'),
    @('houdini_geometry_key_bindings_smoke.py','artifacts/stage3/materials/geometry-key-bindings/report.json'),
    @('houdini_point_subject_stylesheets_smoke.py','artifacts/stage3/materials/point-subject-stylesheets/report.json'),
    @('houdini_tapered_volume_smoke.py','artifacts/stage3/volumes/tapered/report.json'),
    @('houdini_material_expression_smoke.py','artifacts/stage3/materials/material-expressions/report.json'),
    @('houdini_material_stylesheet_smoke.py','artifacts/stage3/material-stylesheets/report.json'),
    @('houdini_stylesheet_acquisition_profile.py','artifacts/stage3/stylesheet-storage/acquisition/report.json'),
    @('houdini_stylesheet_attribute_acquisition_profile.py','artifacts/stage3/stylesheet-storage/attribute-acquisition/report.json'),
    @('houdini_material_motion_acquisition_profile.py','artifacts/stage3/stylesheet-storage/material-motion-acquisition/report.json'),
    @('houdini_material_motion_general_acquisition_profile.py','artifacts/stage3/stylesheet-storage/material-motion-general-acquisition/report.json'),
    @('houdini_packed_material_motion_acquisition_profile.py','artifacts/stage3/stylesheet-storage/packed-material-motion-acquisition/report.json'),
    @('houdini_scene_publication_smoke.py','artifacts/stage3/stylesheet-storage/scene-publication-report.json'),
    @('houdini_capture_retirement_profile.py','artifacts/stage3/stylesheet-storage/capture-retirement-report.json'),
    @('houdini_capture_retirement_shutdown_smoke.py','artifacts/stage1/capture-retirement-shutdown/report.json'),
    @('houdini_transaction_contract_smoke.py','artifacts/stage3/transactions/contract-report.json'),
    @('houdini_sky_smoke.py','artifacts/stage3/world/sky/houdini-sky-smoke.json'),
    @('houdini_sky_source_recovery_smoke.py','artifacts/examples/sky-source-recovery/report.json'),
    @('houdini_sky_light_recovery_smoke.py','artifacts/stage3/sky-light-recovery/report.json'),
    @('houdini_stylesheet_batch_boundaries_smoke.py','artifacts/stage3/stylesheet-storage/boundaries/report.json'),
    @('houdini_stylesheet_sources_smoke.py','artifacts/stage3/stylesheet-sources/report.json'),
    @('houdini_stylesheet_cvex_smoke.py','artifacts/stage3/stylesheet-cvex/report.json'),
    @('houdini_stylesheet_cvex_time_smoke.py','artifacts/stage3/stylesheet-cvex-time/report.json'),
    @('houdini_stylesheet_cvex_shutter_smoke.py','artifacts/examples/stylesheet-cvex-shutter/report.json'),
    @('houdini_material_motion_mesh_gate_smoke.py','artifacts/stage3/stylesheet-storage/material-motion-mesh-gate/report.json'),
    @('houdini_static_style_motion_gate_smoke.py','artifacts/stage3/static-style-motion-gate/report.json'),
    @('houdini_stylesheet_domains_smoke.py','artifacts/stage3/stylesheet-domains/report.json'),
    @('houdini_stylesheet_domains_volume_smoke.py','artifacts/examples/stylesheet-domains-volumes/report.json'),
    @('houdini_stylesheet_domains_motion_smoke.py','artifacts/examples/stylesheet-domains-motion/report.json'),
    @('houdini_object_binding_shutter_smoke.py','artifacts/examples/object-binding-shutter/report.json'),
    @('houdini_material_override_bindings_smoke.py','artifacts/stage3/material-override-bindings/report.json'),
    @('houdini_material_override_graph_smoke.py','artifacts/stage3/material-override-graphs/report.json'),
    @('houdini_material_override_volume_smoke.py','artifacts/examples/material-override-volumes/report.json'),
    @('houdini_material_capacity_smoke.py','artifacts/stage3/material-capacity/report.json'),
    @('houdini_strand_material_smoke.py','artifacts/stage3/materials/strands/houdini-strand-material-smoke.json'),
    @('core_strand_material_smoke.py','artifacts/stage3/materials/strands/core-strand-material-smoke.json'),
    @('houdini_volume_material_smoke.py','artifacts/stage3/materials/volumes/houdini-volume-material-smoke.json'),
    @('core_volume_material_smoke.py','artifacts/stage3/materials/volumes/core-volume-material-smoke.json'),
    @('houdini_displacement_smoke.py','artifacts/stage2/displacement/houdini-displacement-smoke.json'),
    @('core_displacement_smoke.py','artifacts/stage2/displacement/core-displacement-smoke.json'),
    @('houdini_subdivision_smoke.py','artifacts/stage2/subdivision/houdini-subdivision-smoke.json'),
    @('houdini_tessellation_update_smoke.py','artifacts/tessellation-update/report.json'),
    @('core_subdivision_smoke.py','artifacts/stage2/subdivision/core-subdivision-smoke.json'),
    @('houdini_named_volume_regression.py','artifacts/stage3/materials/volumes/houdini-named-volume-regression.json'),
    @('houdini_shader_retirement_smoke.py','artifacts/stage3/materials/retirement/houdini-shader-retirement-smoke.json'),
    @('houdini_progressive_smoke.py','artifacts/stage1/houdini-progressive-smoke.json'),
    @('houdini_progressive_layout_smoke.py','artifacts/stage1/progressive-layout/houdini-progressive-layout-smoke.json'),
    @('houdini_output_smoke.py','artifacts/stage1/output/houdini-output-smoke.json'),
    @('farm_output_policy_test.py',$null),
    @('houdini_farm_output_policy_smoke.py','artifacts/stage3/farm-output-policy/report.json'),
    @('houdini_exr_policy_smoke.py','artifacts/stage3/exr-policy/houdini-exr-policy-smoke.json'),
    @('houdini_pass_outputs_smoke.py','artifacts/stage3/pass-outputs/houdini-pass-outputs-smoke.json'),
    @('houdini_single_pass_exr_smoke.py','artifacts/stage3/single-pass-exr/houdini-single-pass-exr-smoke.json'),
    @('houdini_viewer_resolution_smoke.py','artifacts/stage3/viewer-resolution/houdini-viewer-resolution-smoke.json'),
    @('houdini_viewer_timing_smoke.py','artifacts/stage3/viewer-timing/houdini-viewer-timing-smoke.json'),
    @('houdini_multipart_smoke.py','artifacts/stage3/multipart/houdini-multipart-smoke.json'),
    @('houdini_passes_smoke.py','artifacts/stage3/passes/houdini-passes-smoke.json'),
    @('houdini_progressive_passes_smoke.py','artifacts/stage3/passes/houdini-progressive-passes-smoke.json'),
    @('houdini_batch_smoke.py','artifacts/stage1/batch/houdini-batch-smoke.json'),
    @('houdini_final_sequence_smoke.py','artifacts/stage3/final-sequence/houdini-final-sequence-smoke.json'),
    @('houdini_fatal_gpu_render_smoke.py','artifacts/stage3/fatal-gpu-render/report.json'),
    @('houdini_saved_hip_farm_smoke.py','artifacts/stage3/saved-hip-farm/report.json'),
    @('houdini_packed_smoke.py','artifacts/stage2/packed/houdini-packed-smoke.json'),
    @('houdini_implicit_packed_smoke.py','artifacts/stage2/packed/houdini-implicit-packed-smoke.json'),
    @('attribute_conventions_probe.py','artifacts/stage2/attribute-conventions.json'),
    @('camera_conventions_probe.py','artifacts/stage2/camera/camera-conventions.json'),
    @('houdini_points_smoke.py','artifacts/stage2/particles/houdini-points-smoke.json'),
    @('houdini_lighting_smoke.py','artifacts/stage2/lighting/houdini-lighting-smoke.json'),
    @('houdini_hair_smoke.py','artifacts/stage2/hair/houdini-hair-smoke.json'),
    @('core_motion_smoke.py','artifacts/stage2/motion/core-motion-smoke.json'),
    @('houdini_motion_smoke.py','artifacts/stage2/motion/houdini-motion-smoke.json'),
    @('houdini_changing_topology_velocity_smoke.py','artifacts/stage2/motion/houdini-changing-topology-velocity-smoke.json'),
    @('houdini_deformation_smoke.py','artifacts/stage2/motion/deformation/houdini-deformation-smoke.json'),
    @('houdini_packed_deformation_smoke.py','artifacts/stage2/motion/packed-deformation/report.json'),
    @('houdini_packed_alias_conversion_smoke.py','artifacts/stage3/packed-alias-conversion/report.json'),
    @('houdini_packed_volume_deformation_smoke.py','artifacts/stage2/motion/packed-deformation/mixed-volume/report.json'),
    @('houdini_background_final_smoke.py','artifacts/stage3/background-final/report.json'),
    @('houdini_background_jobs_smoke.py','artifacts/stage3/background-jobs/report.json'),
    @('houdini_material_thumbnail_smoke.py','artifacts/stage3/material-thumbnails/report.json'),
    @('houdini_material_thumbnail_resources_smoke.py','artifacts/stage3/material-thumbnail-resources/report.json'),
    @('houdini_material_thumbnail_domains_smoke.py','artifacts/stage3/material-thumbnail-domains/report.json'),
    @('houdini_world_volume_smoke.py','artifacts/stage3/world-volume/report.json'),
    @('houdini_world_volume_thumbnail_smoke.py','artifacts/stage3/world-volume-thumbnails/report.json'),
    @('houdini_light_shader_smoke.py','artifacts/stage3/light-shaders/report.json'),
    @('houdini_light_texture_smoke.py','artifacts/stage3/light-shaders/texture-report.json'),
    @('houdini_linking_smoke.py','artifacts/stage3/linking/report.json'),
    @('houdini_linking_emitters_smoke.py','artifacts/stage3/linking-emitters/report.json'),
    @('houdini_linking_limits_smoke.py','artifacts/stage3/linking-limits/report.json'),
    @('core_ies_ownership_smoke.py','artifacts/stage3/light-shaders/ownership-report.json'),
    @('houdini_background_controls_smoke.py','artifacts/stage3/background-controls/report.json'),
    @('houdini_background_stop_smoke.py','artifacts/stage3/background-stop-profile/report.json'),
    @('houdini_cancel_restart_smoke.py','artifacts/stage2/motion/packed-deformation/cancel-restart-report.json'),
    @('houdini_packed_deformation_retention_smoke.py','artifacts/stage2/motion/packed-deformation/retention-report.json'),
    @('houdini_packed_deformation_variants_smoke.py','artifacts/stage2/motion/packed-deformation/variants-report.json'),
    @('houdini_packed_resource_identity_smoke.py','artifacts/stage2/motion/packed-deformation/resource-identity-report.json'),
    @('houdini_packed_deformation_frames_smoke.py','artifacts/stage2/motion/packed-deformation/frames-report.json'),
    @('houdini_packed_deformation_material_smoke.py','artifacts/stage2/motion/packed-deformation/material-report.json'),
    @('houdini_packed_velocity_smoke.py','artifacts/stage2/motion/packed-velocity/report.json'),
    @('houdini_frozen_material_candidates_smoke.py','artifacts/stage1/geometry-ownership/frozen-material-candidates.json'),
    @('houdini_material_acquisition_smoke.py','artifacts/stage1/geometry-ownership/material-acquisition.json'),
    @('houdini_volume_motion_smoke.py','artifacts/stage2/motion/volume/houdini-volume-motion-smoke.json'),
    @('houdini_volume_smoke.py','artifacts/stage2/volume/houdini-volume-smoke.json'),
    @('houdini_pyro_smoke.py','artifacts/stage2/pyro/houdini-pyro-smoke.json'),
    @('houdini_panel_smoke.py','artifacts/stage1/houdini-panel-smoke.json'),
    @('houdini_viewer_memory_smoke.py','artifacts/stage3/viewer-memory/report.json'),
    @('houdini_viewer_redesign_smoke.py','artifacts/viewer-redesign/report.json'),
    @('houdini_native_normals_smoke.py','artifacts/stage1/houdini-native-normals-smoke.json'),
    @('houdini_display_parallel_smoke.py','artifacts/stage1/output/houdini-display-parallel-smoke.json'),
    @('houdini_denoising_smoke.py','artifacts/stage3/denoising/houdini-denoising-smoke.json'),
    @('houdini_oidn_provider_smoke.py','artifacts/stage3/denoising/sdk-provider/report.json'),
    @('houdini_unchanged_update_smoke.py','artifacts/stage1/unchanged-update/houdini-unchanged-update-smoke.json')
)
if($NativeUi){
    $nativeUiTests=@('houdini_native_smoke.py','houdini_shader_graph_smoke.py','houdini_courtyard_smoke.py','houdini_panel_smoke.py','houdini_mixed_geometry_smoke.py','houdini_mixed_geometry_fx_smoke.py','houdini_mixed_geometry_motion_smoke.py','houdini_object_scene_smoke.py','houdini_object_properties_smoke.py','houdini_light_properties_smoke.py','houdini_render_settings_smoke.py','houdini_object_lights_smoke.py','houdini_object_scene_batch_smoke.py')
    $testCases=@($testCases | Where-Object {$_[0] -in $nativeUiTests})
}
if($Hydra){$testCases+=,@('hydra_smoke.py','artifacts/stage0/hydra-smoke.json')}
if($Tests.Count){
    $unknown=@($Tests | Where-Object {$_ -notin @($testCases | ForEach-Object {$_[0]})})
    if($unknown.Count){throw ('Unknown or excluded test names: '+($unknown -join ', '))}
    $testCases=@($testCases | Where-Object {$_[0] -in $Tests})
    $summary.scope='selected-native'
    $summary.selected_tests=@($testCases | ForEach-Object {$_[0]})
    Save-Summary
}
if ($Tier) {
    . (Join-Path $PSScriptRoot 'native-test-tiers.ps1')
    $plan=@(Get-NativeTierCases $Tier $destination -NightlySelectionFile $NightlySelectionFile -CatalogueSelectionFile $CatalogueSelectionFile)
    foreach ($case in $plan) {
        if (-not (Test-Path -LiteralPath (Join-Path $taskRoot "tests/$($case.test)") -PathType Leaf)) {
            throw "Tier '$Tier' references a missing test: $($case.test)"
        }
    }
    $summary.scope="native-$Tier"
    if ($NightlySelectionFile) { $summary.scope='native-nightly-partition' }
    if ($CatalogueSelectionFile) { $summary.scope='native-catalogue-selection' }
    $summary.plan=$plan
    Save-Summary
    Write-Output "Houdini $($context.Version), host $($context.RuntimeIdentity.host), manifest $capturedManifestHash"
    foreach ($case in $plan) {
        $required=if ($case.report) {$case.report} else {'exit status and verified runtime identity'}
        Write-Output "$($case.test) | $($case.device) | $required | prerequisites: $($case.prerequisites -join ', ')"
    }
    $headlessCases=@($plan | Where-Object {-not $_.gui})
    $tierGui=@($plan | Where-Object gui)
    $Gui=$tierGui.Count -gt 0
    $GuiDevices=@($tierGui.device | Where-Object {$_} | Select-Object -Unique)
    $GuiRepetitions=1
} else {
    $headlessCases=@($testCases | ForEach-Object {
        [pscustomobject]@{test=$_[0];report=$_[1];arguments=@();device=''}
    })
}
if($ListTests){
    $summary.result='listed';$summary.execution_status='not_dispatched';Save-Summary
    if (-not $Tier) {$testCases | ForEach-Object {$_[0]}}
    return
}
if($Tier){
    # Context, environment and GUI entry points remain compatible; Python owns
    # named-tier headless execution, backend/producer policy and receipts.
    $runnerArguments=@((Join-Path $PSScriptRoot 'native_test_runner.py'),'--summary',$summaryPath,'--tier',$Tier,'--timeout',[string]$TimeoutSeconds)
    if($StopOnFailure){$runnerArguments+='--stop-on-failure'}
    & python @runnerArguments
    if($LASTEXITCODE -ne 0){throw 'Native Python headless runner failed.'}
    $summary=Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json -AsHashtable
    $headlessCases=@()
    if($summary.execution_status -eq 'stopped_on_failure'){$Gui=$false}
}
if($GuiOnly){$headlessCases=@()}
foreach($case in $headlessCases){
    $test=@($case.test,$case.report)
    $name=[IO.Path]::GetFileNameWithoutExtension($test[0])
    if ($case.device) {$name+="-$($case.device)"}
    $source=Join-Path $taskRoot "tests/$($test[0])"
    if((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash -ne $summary.build_manifest_sha256){throw 'Build identity changed during the broad test queue.'}
    $env:CYCLES_HOUDINI_TEST_IDENTITY_REPORT=Join-Path $destination "$name.runtime-identity.json"
    $testStartedUtc=[DateTime]::UtcNow
    $started=[Diagnostics.Stopwatch]::StartNew()
    $entryPoint=Join-Path $taskRoot 'tests/native_test_entry.py'
    $launchArguments=@(Get-NativeHeadlessLaunchArguments -EntryPoint $entryPoint -Source $source -Arguments $case.arguments)
    $quoted=@($launchArguments | ForEach-Object {Quote-NativeArgument $_})
    $process=Start-Process -FilePath (Join-Path $HoudiniRoot 'bin/hython.exe') -ArgumentList $quoted -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $destination "$name.stdout.log") -RedirectStandardError (Join-Path $destination "$name.stderr.log")
    $taskNativeHandle=$process.Handle
    $testTimeout=if($test[0] -in @('houdini_mesh_payload_transport_smoke.py','houdini_mesh_field_transport_smoke.py')){[Math]::Max(480,$TimeoutSeconds)}else{$TimeoutSeconds}
    if($Tier -eq 'nightly' -or $test[0] -eq 'native_review_contracts.py'){$testTimeout=[Math]::Max(900,$testTimeout)}
    if($Tier -eq 'nightly' -and $test[0] -eq 'pytest_contracts.py'){$testTimeout=[Math]::Max(10800,$testTimeout)}
    if($test[0] -in @('houdini_scene_query_displacement_smoke.py','houdini_oidn_provider_smoke.py')){$testTimeout=[Math]::Max(300,$testTimeout)}
    if($test[0] -eq 'core_instance_motion_smoke.py'){$testTimeout=[Math]::Max(660,$testTimeout)}
    if($test[0] -eq 'houdini_sampled_instance_attribute_smoke.py'){$testTimeout=[Math]::Max(360,$testTimeout)}
    if($test[0] -eq 'houdini_material_ramp_override_smoke.py'){$testTimeout=[Math]::Max(360,$testTimeout)}
    if($test[0] -eq 'houdini_geometry_key_bindings_smoke.py'){$testTimeout=[Math]::Max(360,$testTimeout)}
    if($test[0] -eq 'core_camera_dof_motion_smoke.py'){$testTimeout=[Math]::Max(900,$testTimeout)}
    if($test[0] -eq 'core_camera_intrinsic_motion_smoke.py'){$testTimeout=[Math]::Max(900,$testTimeout)}
    if($test[0] -eq 'houdini_material_capacity_smoke.py'){$testTimeout=[Math]::Max(900,$testTimeout)}
    if($test[0] -in @('houdini_camera_intrinsic_motion_smoke.py','houdini_point_subject_stylesheets_smoke.py','houdini_tapered_volume_smoke.py')){$testTimeout=[Math]::Max(600,$testTimeout)}
    if($test[0] -in @('houdini_camera_dof_motion_smoke.py','houdini_material_expression_smoke.py')){$testTimeout=[Math]::Max(360,$testTimeout)}
    $finished=$process.WaitForExit($testTimeout*1000)
    if(-not $finished){
        # The worker belongs to this test. Retire its process tree on timeout,
        # preserving independent artist sessions and preventing orphan workers.
        try{$process.Kill($true)}catch{& taskkill.exe /PID $process.Id /T /F | Out-Null}
        $process.WaitForExit();$code=-1
    }else{$code=$process.ExitCode;if($null -eq $code){$code=-1}}
    $entry=[ordered]@{test=$test[0];seconds=$started.Elapsed.TotalSeconds;exit_code=$code;result=$(if($code -eq 0){'passed'}else{'failed'});test_sha256=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash}
    if ($case.device) {$entry.device=$case.device}
    if ($Tier -and $code -eq 0) {
        $identity=$env:CYCLES_HOUDINI_TEST_IDENTITY_REPORT
        if ((Test-Path -LiteralPath $identity) -and (Get-Content -LiteralPath $identity -Raw | ConvertFrom-Json).result -eq 'passed') {
            $entry.runtime_identity=[IO.Path]::GetFileName($identity)
        } else {
            $entry.result='failed';$entry.error='Missing runtime identity from the selected Houdini process.'
        }
    }
    if($finished -and $code -eq 0 -and $test[1]){
        $resolvedReport=Resolve-NativeTestReport -Report $test[1] -SourceRoot $taskRoot -ArtifactsRoot $env:CYCLES_HOUDINI_TEST_ARTIFACTS
        $evidence=$resolvedReport.Path
        if($resolvedReport.Route){$entry.artifact_route=$resolvedReport.Route}
        if((Test-Path -LiteralPath $evidence) -and (Get-Item -LiteralPath $evidence).LastWriteTimeUtc -ge $testStartedUtc){
            Copy-Item -LiteralPath $evidence -Destination (Join-Path $destination "$name.json")
            $entry.evidence="$name.json"
            if($Tier){
                $reported=Get-Content -LiteralPath $evidence -Raw | ConvertFrom-Json
                if($Tier -eq 'nightly' -and $reported.PSObject.Properties.Name -notcontains 'result'){
                    $entry.report_contract='legacy assertions, exit status and fresh report'
                }elseif($reported.result -ne 'passed'){
                    $entry.result='failed';$entry.error='The required report did not record a pass.'
                }
            }
        }else{$entry.result='failed';$entry.error='Test exited without fresh required evidence'}
    }
    $summary.tests+=,$entry;Save-Summary
    Write-Output "$($entry.result): $($test[0]) ($([Math]::Round($entry.seconds,2)) s)"
    if($StopOnFailure -and $entry.result -ne 'passed'){
        $summary.execution_status='stopped_on_failure';$Gui=$false;break
    }
}
if($Gui -and -not $GuiDevices.Count -and -not (Test-Path -LiteralPath (Join-Path $destination 'houdini_native_smoke.json'))){
    $summary.tests+=,[ordered]@{test='GUI device discovery';exit_code=-1;result='failed';error='Native device discovery failed. Pass explicit GuiDevices to run GUI checks independently.'}
    Save-Summary
    $Gui=$false
}
if($Gui){
    $shaderEditingGui=@('houdini_shader_bypass_preview_gui_smoke.py','houdini_shader_preview_shortcut_gui_smoke.py','houdini_shader_layout_gui_smoke.py','houdini_shader_input_states_gui_smoke.py','houdini_mix_nodes_gui_smoke.py','houdini_cop_texture_gui_smoke.py','houdini_cop_world_shutter_gui_smoke.py','houdini_environment_recovery_gui_smoke.py')
    if(-not $GuiDevices.Count){
        $native=Get-Content -LiteralPath (Join-Path $destination 'houdini_native_smoke.json') -Raw | ConvertFrom-Json
        $gpu=@($native.devices | Where-Object {$_.backend -eq 'OPTIX' -and $_.name -eq 'NVIDIA GeForce RTX 4090'})
        if($gpu.Count -ne 1){throw 'Pass explicit GuiDevices; no unique RTX 4090 default was verified.'}
        $GuiDevices=@('CPU',$gpu[0].id)
    }
    $guiTests=@('houdini_tangent_basis_gui_smoke.py','houdini_named_field_displacement_gui_smoke.py','houdini_field_motion_gui_smoke.py','houdini_named_attribute_gui_smoke.py','houdini_sampled_attribute_displacement_gui_smoke.py','houdini_temporal_displacement_gui_smoke.py','houdini_sampled_displacement_gui_smoke.py','houdini_displacement_motion_recovery_gui_smoke.py','houdini_geometry_binding_recovery_gui_smoke.py','houdini_domain_recovery_gui_smoke.py','houdini_shader_source_recovery_gui_smoke.py','houdini_volume_shader_recovery_gui_smoke.py','houdini_volume_field_recovery_gui_smoke.py','houdini_packed_source_recovery_gui_smoke.py','houdini_identity_subdivision_recovery_gui_smoke.py','houdini_particle_attribute_gui_smoke.py','houdini_size_recovery_gui_smoke.py','houdini_crease_packed_recovery_gui_smoke.py','houdini_shading_attribute_recovery_gui_smoke.py','houdini_material_recovery_gui_smoke.py','houdini_combined_material_gui_smoke.py','houdini_ipr_smoke.py','houdini_active_shutdown_smoke.py','houdini_volume_ipr_smoke.py','houdini_strands_ipr_smoke.py','houdini_motion_ipr_smoke.py','houdini_volume_motion_ipr_smoke.py','houdini_particle_attribute_ipr_smoke.py','houdini_material_ipr_smoke.py','houdini_material_subnet_ipr_smoke.py','houdini_material_ramp_ipr_smoke.py','houdini_material_assignment_ipr_smoke.py','houdini_strand_material_ipr_smoke.py','houdini_volume_material_ipr_smoke.py','houdini_displacement_ipr_smoke.py','houdini_subdivision_ipr_smoke.py','houdini_texture_ipr_smoke.py','houdini_color_ipr_smoke.py','houdini_texture_auto_ipr_smoke.py','houdini_color_interop_ipr_smoke.py','houdini_transparency_ipr_smoke.py','houdini_courtyard_ipr_smoke.py')
      $guiTests+=@('houdini_ramp_interpolation_gui_smoke.py','houdini_ramp_interpolation_native_gui_smoke.py','houdini_packed_instance_attribute_gui_smoke.py','houdini_packed_instance_attribute_native_gui_smoke.py','houdini_packed_cd_gui_smoke.py','houdini_packed_cd_native_gui_smoke.py','houdini_constant_attribute_gui_smoke.py','houdini_constant_attribute_native_gui_smoke.py','houdini_mesh_island_gui_smoke.py','houdini_mesh_island_native_gui_smoke.py')
      $guiTests+=@('houdini_curve_inline_gui_smoke.py')
      $guiTests+=@('houdini_hue_ramp_gui_smoke.py','houdini_hue_ramp_native_gui_smoke.py')
      $guiTests+=@('houdini_rgb_ramp_gui_smoke.py','houdini_rgb_ramp_native_gui_smoke.py')
      $guiTests+=@('houdini_pointiness_gui_smoke.py','houdini_pointiness_native_gui_smoke.py')
      $guiTests+=@('houdini_shading_payload_gui_smoke.py','houdini_shading_payload_native_gui_smoke.py')
    $guiTests+=@('houdini_object_scene_ipr_smoke.py','houdini_artist_workflow_smoke.py','houdini_denoising_ipr_smoke.py','houdini_object_properties_ipr_smoke.py','houdini_panel_navigation_ipr_smoke.py','houdini_artist_scene_ipr_smoke.py','houdini_passes_ipr_smoke.py','houdini_tabs_menu_smoke.py','houdini_camera_subdivision_ipr_smoke.py','houdini_aov_ipr_smoke.py')
    $guiTests+=@('houdini_camera_dependencies_ipr_smoke.py','houdini_material_recovery_ipr_smoke.py','houdini_light_update_gui_smoke.py')
    $guiTests+=@('houdini_light_handles_smoke.py','houdini_parented_light_ipr_smoke.py')
    $guiTests+=@('houdini_gpu_display_ipr_smoke.py','houdini_gpu_display_probe.py','houdini_gpu_display_lifecycle_smoke.py')
    $guiTests+=@('houdini_geometry_lease_probe.py')
    $guiTests+=@('houdini_deformation_ownership_probe.py')
    $guiTests+=@('houdini_volume_ownership_probe.py')
    $guiTests+=@('houdini_packed_ownership_probe.py')
    $guiTests+=@('houdini_material_graph_ownership_probe.py')
    $guiTests+=@('houdini_scene_ownership_probe.py')
    $guiTests+=@('houdini_async_capture_ipr_smoke.py')
    $guiTests+=@('houdini_capture_retirement_gui_smoke.py')
    $guiTests+=@('houdini_scene_load_gui_smoke.py')
    $guiTests+=@('houdini_sky_source_recovery_gui_smoke.py','houdini_sky_source_recovery_native_gui_smoke.py')
    $guiTests+=@('houdini_transaction_recovery_gui_smoke.py','houdini_transaction_recovery_native_gui_smoke.py')
    $guiTests+=@('houdini_hash_displacement_gui_smoke.py','houdini_hash_displacement_native_gui_smoke.py')
    $guiTests+=@('houdini_camera_coordinate_displacement_gui_smoke.py','houdini_camera_coordinate_displacement_native_gui_smoke.py')
    $guiTests+=@('houdini_scene_query_displacement_gui_smoke.py','houdini_scene_query_displacement_native_gui_smoke.py')
    $guiTests+=@('houdini_geometry_key_bindings_gui_smoke.py','houdini_geometry_key_bindings_native_gui_smoke.py')
    $guiTests+=@('houdini_point_subject_stylesheets_gui_smoke.py','houdini_point_subject_stylesheets_native_gui_smoke.py')
    $guiTests+=@('houdini_tapered_volume_gui_smoke.py','houdini_tapered_volume_native_gui_smoke.py')
    $guiTests+=@('houdini_camera_intrinsic_motion_gui_smoke.py','houdini_camera_intrinsic_motion_native_gui_smoke.py')
    $guiTests+=@('houdini_camera_dof_motion_gui_smoke.py','houdini_camera_dof_motion_native_gui_smoke.py')
    $guiTests+=@('houdini_material_expression_gui_smoke.py','houdini_material_expression_native_gui_smoke.py')
    $guiTests+=@('houdini_sampled_instance_attribute_gui_smoke.py','houdini_sampled_instance_attribute_native_gui_smoke.py')
    $guiTests+=@('houdini_worker_active_failure_smoke.py')
    $guiTests+=@('houdini_heavy_packed_response_benchmark.py','houdini_heavy_packed_cooking_response_benchmark.py','houdini_heavy_packed_async_ipr_smoke.py','houdini_heavy_packed_animation_benchmark.py','houdini_navigation_priority_ipr_smoke.py')
    $guiTests+=@('houdini_continuous_packed_ipr_smoke.py')
    $guiTests+=@('houdini_camera_delivery_benchmark.py')
    $guiTests+=@('houdini_release_response_gui_smoke.py')
    $guiTests+=@('houdini_viewer_wakeup_smoke.py')
    $guiTests+=@('houdini_compact_display_gui_smoke.py')
    $guiTests+=@('houdini_viewer_session_recreation_smoke.py')
    $guiTests+=@('houdini_packed_pose_ipr_smoke.py')
    $guiTests+=@('houdini_early_pose_ipr_smoke.py')
    $guiTests+=@('houdini_viewport_drag_benchmark.py')
    $guiTests+=@('houdini_panel_history_fullscreen_smoke.py')
    $guiTests+=@('houdini_camera_denoising_ipr_smoke.py')
    if($NativeUi){$guiTests=@('houdini_tangent_basis_gui_smoke.py','houdini_named_field_displacement_gui_smoke.py','houdini_field_motion_gui_smoke.py','houdini_named_attribute_gui_smoke.py','houdini_ipr_smoke.py','houdini_active_shutdown_smoke.py','houdini_transparency_ipr_smoke.py','houdini_courtyard_ipr_smoke.py','houdini_object_scene_ipr_smoke.py','houdini_object_properties_ipr_smoke.py','houdini_panel_navigation_ipr_smoke.py','houdini_artist_scene_ipr_smoke.py','houdini_passes_ipr_smoke.py','houdini_tabs_menu_smoke.py','houdini_camera_subdivision_ipr_smoke.py')}
    $guiTests+=@('houdini_subdivision_ipr_toggle_gui_smoke.py','houdini_subdivision_ipr_toggle_native_gui_smoke.py')
    $guiTests+=@('houdini_tessellation_update_ipr_gui_smoke.py')
    $guiTests+=@('houdini_rop_actions_gui_smoke.py')
    $guiTests+=@('houdini_viewer_final_gui_smoke.py','houdini_viewer_sequence_gui_smoke.py','houdini_pass_outputs_gui_smoke.py','houdini_multipart_gui_smoke.py')
    $guiTests+=@('houdini_final_cancel_gui_smoke.py')
    $guiTests+=$shaderEditingGui
    $guiTests+=@('houdini_world_native_ipr_smoke.py')
    $guiTests+=@('houdini_panel_update_reuse_smoke.py')
    $guiTests+=@('houdini_camera_focus_capture_guard_smoke.py')
    $guiTests+=@('houdini_progressive_focus_smoke.py')
    $guiTests+=@('houdini_packed_deformation_gui_smoke.py')
    $guiTests+=@('houdini_background_final_gui_smoke.py')
    $guiTests+=@('houdini_background_final_gui_cleanup_smoke.py')
    $guiTests+=@('houdini_background_jobs_gui_smoke.py')
    $guiTests+=@('houdini_material_thumbnail_gui_smoke.py')
    $guiTests+=@('houdini_material_thumbnail_domains_gui_smoke.py')
    $guiTests+=@('houdini_world_volume_gui_smoke.py')
    $guiTests+=@('houdini_light_shader_gui_smoke.py')
    $guiTests+=@('houdini_linking_gui_smoke.py')
    $guiTests+=@('houdini_material_override_gui_smoke.py','houdini_material_override_native_gui_smoke.py')
    $guiTests+=@('houdini_material_ramp_override_gui_smoke.py','houdini_material_ramp_override_native_gui_smoke.py')
    $guiTests+=@('houdini_material_stylesheet_gui_smoke.py','houdini_material_stylesheet_native_gui_smoke.py')
    $guiTests+=@('houdini_stylesheet_sources_gui_smoke.py','houdini_stylesheet_sources_native_gui_smoke.py')
    $guiTests+=@('houdini_mesh_payload_gui_smoke.py','houdini_mesh_payload_native_gui_smoke.py','houdini_mesh_field_gui_smoke.py','houdini_mesh_field_native_gui_smoke.py')
    $guiTests+=@('houdini_stylesheet_cooperative_gui_smoke.py','houdini_stylesheet_cooperative_native_gui_smoke.py')
    $guiTests+=@('houdini_material_motion_cooperative_gui_smoke.py','houdini_material_motion_cooperative_native_gui_smoke.py')
    $guiTests+=@('houdini_particle_binding_recovery_gui_smoke.py','houdini_particle_binding_recovery_native_gui_smoke.py')
    $guiTests+=@('houdini_stylesheet_cvex_gui_smoke.py','houdini_stylesheet_cvex_native_gui_smoke.py')
    $guiTests+=@('houdini_stylesheet_cvex_time_gui_smoke.py','houdini_stylesheet_cvex_time_native_gui_smoke.py')
    $guiTests+=@('houdini_stylesheet_domains_gui_smoke.py','houdini_stylesheet_domains_native_gui_smoke.py')
    $guiTests+=@('houdini_object_binding_shutter_gui_smoke.py','houdini_object_binding_shutter_native_gui_smoke.py')
    $guiTests+=@('houdini_material_capacity_gui_smoke.py','houdini_material_capacity_native_gui_smoke.py')
      $guiTests+=@('houdini_sampled_override_gui_smoke.py','houdini_sampled_override_native_gui_smoke.py')
      $guiTests+=@('houdini_velocity_normal_gui_smoke.py','houdini_velocity_normal_native_gui_smoke.py')
      $guiTests+=@('houdini_independent_binding_gui_smoke.py','houdini_independent_binding_native_gui_smoke.py')
      $guiTests+=@('houdini_shutter_coordinate_gui_smoke.py','houdini_shutter_coordinate_native_gui_smoke.py')
      $guiTests+=@('houdini_spatial_displacement_gui_smoke.py','houdini_spatial_displacement_native_gui_smoke.py')
      $guiTests+=@('houdini_camera_lens_motion_gui_smoke.py','houdini_camera_lens_motion_native_gui_smoke.py')
      $guiTests+=@('houdini_camera_sampling_gui_smoke.py','houdini_camera_sampling_native_gui_smoke.py')
      $guiTests+=@('houdini_panorama_gui_smoke.py','houdini_panorama_native_gui_smoke.py')
      $guiTests+=@('houdini_dense_volume_gui_smoke.py','houdini_dense_volume_native_gui_smoke.py')
      $guiTests+=@('houdini_volume_vector_gui_smoke.py','houdini_volume_vector_native_gui_smoke.py')
      $guiTests+=@('houdini_parametric_curve_gui_smoke.py','houdini_parametric_curve_native_gui_smoke.py')
    $guiTests+=@('houdini_dense_packed_preview_smoke.py')
    $guiTests+=@('houdini_packed_velocity_gui_smoke.py')
    if ($Tier) {$guiTests=@($tierGui.test)}
    :guiDeviceLoop foreach($device in $GuiDevices){
        foreach($test in $guiTests){
            for($repeat=1;$repeat -le $GuiRepetitions;$repeat++){
                $guiRun="verify-$runName-$([IO.Path]::GetFileNameWithoutExtension($test))-$(($device -replace '[^a-zA-Z0-9_-]','_'))-$repeat"
                $started=[Diagnostics.Stopwatch]::StartNew()
                try{
                    $guiTimeout=if($test -eq 'houdini_panel_history_fullscreen_smoke.py'){[Math]::Max(270,$TimeoutSeconds)}elseif($test -in @('houdini_packed_deformation_gui_smoke.py','houdini_packed_velocity_gui_smoke.py')){[Math]::Max(360,$TimeoutSeconds)}elseif($test -eq 'houdini_world_native_ipr_smoke.py'){[Math]::Max(260,$TimeoutSeconds)}elseif($test -eq 'houdini_camera_subdivision_ipr_smoke.py'){[Math]::Max(210,$TimeoutSeconds)}elseif($test -eq 'houdini_viewport_drag_benchmark.py'){[Math]::Max(190,$TimeoutSeconds)}elseif($test -in @('houdini_object_properties_ipr_smoke.py','houdini_panel_navigation_ipr_smoke.py','houdini_passes_ipr_smoke.py')){[Math]::Max(180,$TimeoutSeconds)}else{$TimeoutSeconds}
                    if($test -eq 'houdini_dense_packed_preview_smoke.py'){$guiTimeout=[Math]::Max(360,$TimeoutSeconds)}
                    if($test -in @('houdini_mesh_payload_gui_smoke.py','houdini_mesh_payload_native_gui_smoke.py','houdini_mesh_field_gui_smoke.py','houdini_mesh_field_native_gui_smoke.py')){$guiTimeout=[Math]::Max(630,$TimeoutSeconds)}
                    if($test -in @('houdini_hash_displacement_gui_smoke.py','houdini_hash_displacement_native_gui_smoke.py')){$guiTimeout=[Math]::Max(630,$TimeoutSeconds)}
                    if($test -in @('houdini_scene_query_displacement_gui_smoke.py','houdini_scene_query_displacement_native_gui_smoke.py')){$guiTimeout=[Math]::Max(210,$TimeoutSeconds)}
                    if($test -in @('houdini_sampled_instance_attribute_gui_smoke.py','houdini_sampled_instance_attribute_native_gui_smoke.py')){$guiTimeout=[Math]::Max(360,$TimeoutSeconds)}
                    if($test -in @('houdini_material_ramp_override_gui_smoke.py','houdini_material_ramp_override_native_gui_smoke.py')){$guiTimeout=[Math]::Max(360,$TimeoutSeconds)}
                    if($test -in @('houdini_geometry_key_bindings_gui_smoke.py','houdini_geometry_key_bindings_native_gui_smoke.py')){$guiTimeout=[Math]::Max(360,$TimeoutSeconds)}
                    if($test -in @('houdini_point_subject_stylesheets_gui_smoke.py','houdini_point_subject_stylesheets_native_gui_smoke.py','houdini_tapered_volume_gui_smoke.py','houdini_tapered_volume_native_gui_smoke.py','houdini_camera_intrinsic_motion_gui_smoke.py','houdini_camera_intrinsic_motion_native_gui_smoke.py')){$guiTimeout=[Math]::Max(600,$TimeoutSeconds)}
                    if($test -in @('houdini_camera_dof_motion_gui_smoke.py','houdini_camera_dof_motion_native_gui_smoke.py','houdini_material_expression_gui_smoke.py','houdini_material_expression_native_gui_smoke.py')){$guiTimeout=[Math]::Max(360,$TimeoutSeconds)}
                    if($test -eq 'houdini_release_response_gui_smoke.py'){$guiTimeout=[Math]::Max(1230,$TimeoutSeconds)}
                    if($test -in @('houdini_stylesheet_cooperative_gui_smoke.py','houdini_stylesheet_cooperative_native_gui_smoke.py')){$guiTimeout=[Math]::Max(450,$TimeoutSeconds)}
                    if($test -in @('houdini_material_motion_cooperative_gui_smoke.py','houdini_material_motion_cooperative_native_gui_smoke.py')){$guiTimeout=[Math]::Max(450,$TimeoutSeconds)}
                    $guiEvidenceRoot=Join-Path $destination 'gui'
                    & (Join-Path $PSScriptRoot 'run-gui-test.ps1') -Test $test -Device $device -RunName $guiRun -HoudiniRoot $HoudiniRoot -BuildDirectory $build -PackageDirectory $context.PackageDirectory -EvidenceDirectory $guiEvidenceRoot -TimeoutSeconds $guiTimeout
                    $code=$LASTEXITCODE
                    $guiReport=Get-Content -LiteralPath (Join-Path $guiEvidenceRoot "$guiRun/$([IO.Path]::GetFileNameWithoutExtension($test)).json") -Raw | ConvertFrom-Json
                    if($guiReport.result -ne 'passed' -or $guiReport.process_exit_code -ne 0){$code=1}
                    $errorText=$null
                }catch{$code=-1;$errorText=$_.Exception.Message}
                $source=Join-Path $taskRoot "tests/$test"
                $entry=[ordered]@{test=$test;device=$device;repeat=$repeat;seconds=$started.Elapsed.TotalSeconds;exit_code=$code;result=$(if($code -eq 0){'passed'}else{'failed'});test_sha256=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash;evidence="gui/$guiRun"}
                if($errorText){$entry.error=$errorText}
                $summary.tests+=,$entry;Save-Summary
                if($StopOnFailure -and $entry.result -ne 'passed'){
                    $summary.execution_status='stopped_on_failure';break guiDeviceLoop
                }
            }
        }
    }
}
$summary.result=if(@($summary.tests | Where-Object {$_.result -ne 'passed'}).Count){'failed'}else{'passed'}
$summary.finished_utc=[DateTime]::UtcNow.ToString('o')
Save-Summary
Write-Output "Verification $($summary.result): $summaryPath"
if($summary.result -ne 'passed'){exit 1}

}catch{
    $summary.result='failed';$summary.runner_error=$_.Exception.Message
    $summary.finished_utc=[DateTime]::UtcNow.ToString('o');Save-Summary
    throw
}finally{Restore-TestProcessEnvironment $previousTestEnvironment}
