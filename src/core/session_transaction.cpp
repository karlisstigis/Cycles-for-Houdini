// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"

namespace cycles_houdini {
void RenderSession::Impl::publish_generation(const SceneUpdate &u, const PreparedUpdate &prepared,
                                             const EngineUpdatePlan &plan) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  const auto &crypto = prepared.crypto;
  const auto metadata_dirty = plan.metadata_dirty;
  const auto prepared_vector_merges = prepared.prepared_vector_merges;
  const auto volume_vector_seconds = prepared.volume_vector_seconds;
  const auto started = plan.started;
  const auto reset_denoising_history = plan.reset_denoising_history;
  if (metadata_dirty) {
    std::lock_guard frame_lock(i.store->mutex);
    i.store->output_metadata = crypto_scene_metadata(scene, crypto);
  }
  if (u.crop)
    i.crop = *u.crop;
  i.volume_vector_merges += prepared_vector_merges;
  i.volume_vector_assembly_seconds = volume_vector_seconds;
  i.direct_attribute_updates = prepared.attribute_records.size();
  i.expanded_attribute_updates = prepared.expanded_attribute_updates;
  i.attribute_fallback_reason = prepared.attribute_fallback_reason;
  if (prepared.expanded_attribute_updates)
    ++i.attribute_fallback_count;
  ++i.generation;
  i.source_color = i.target_color;
  i.source_color_epoch = i.target_color_epoch;
  i.color_update_pending = false;
  {
    std::lock_guard frame_lock(i.store->mutex);
    i.store->generation = i.generation;
    i.store->complete = false;
    i.store->interrupted = false;
    i.store->error.clear();
    i.store->first_image_seconds = 0;
    i.store->finished_seconds = 0;
    i.store->started = started;
    i.store->crop = i.crop;
    i.store->full_width = i.config.width;
    i.store->full_height = i.config.height;
    i.store->color_configuration = i.source_color;
    i.store->color_epoch = i.source_color_epoch;
    i.store->focus.reset();
    i.store->diagnostics = SessionDiagnostics{};
    i.store->diagnostics.enabled = i.config.collect_diagnostics;
    i.store->diagnostics.generation = i.generation;
  }
  ccl::BufferParams buffers;
  buffers.width = i.crop.width;
  buffers.height = i.crop.height;
  // Cycles serializes this window for disk-backed tiled buffers. Whole-frame
  // reload/output needs the cropped image extent, not the default empty window.
  buffers.window_width = i.crop.width;
  buffers.window_height = i.crop.height;
  buffers.full_x = i.crop.x;
  buffers.full_y = i.crop.y;
  buffers.full_width = i.config.width;
  buffers.full_height = i.config.height;
  if (reset_denoising_history)
    i.session->reset_denoising_history();
  i.session->progress.reset();
  i.session->reset(i.params, buffers);
  i.session->set_pause(i.paused);
  i.state = i.paused ? SessionState::Paused : SessionState::Rendering;
  if (!i.config.persistent_updates)
    i.session->start();
}

void RenderSession::Impl::mutate_engine(const SceneUpdate &u, PreparedUpdate &prepared, EngineUpdatePlan &plan,
                                        UpdateProfile &profile) {
  auto &i = *this;
  auto &prepared_linking = plan.prepared_linking;
  const auto world_surface_emission = plan.world_surface_emission;
  const auto graph_preparation = plan.graph_preparation;
  profile.mark("boundary_wait");
  if (u.source_diagnostics)
    i.source_diagnostics = *u.source_diagnostics;
  if (prepared_linking)
    i.linking_plan = std::move(*prepared_linking);
  if (u.linking)
    i.linking_definition = *u.linking;
  i.world_surface_emission = world_surface_emission;
  // The baseline joins before mutation. Persistent controllers invoke this
  // transaction on the engine thread between iterations, after every old
  // render/output/display callback has returned. No UI owns this wait.
  if (!i.config.persistent_updates)
    i.cancel_engine();
  else {
    i.cancel_join_seconds = 0;
    i.cancel_output_deliveries = i.cancel_display_updates = 0;
    i.cancel_output_reads_skipped = i.cancel_display_updates_skipped = 0;
    i.cancel_output_delivery_seconds = i.cancel_display_conversion_seconds = 0;
  }
  i.shader_graph_preparation_seconds = graph_preparation;
  auto *scene = i.session->scene.get();
  {
    ccl::thread_scoped_lock scene_lock(scene->mutex);
    // The engine copies this into its device update memory record; publish_generation below makes
    // it the session generation.
    scene->device_update_tag = i.generation + 1;
    i.install_prepared_images(prepared);

    const auto flags = i.configure_candidate_rendering(u, prepared, profile);
    // A material override may rename logical resources while leaving all
    // source geometry and object owners intact. Preserve physical storage
    // and shader displacement hashes across that exact ownership transfer.

    // Preserve physical geometry across material-binding handoffs.
    const auto retained_handoff_shaders = i.apply_binding_handoffs(prepared.binding_handoffs);
    auto variant_updates = i.resolve_geometry_variants(u, prepared, profile);
    i.delete_objects(u);
    i.delete_lights(u);
    const auto retired_binding_shaders = i.sync_material_bindings(u, prepared, variant_updates);
    i.sync_geometry(u, prepared);
    i.sync_object_attributes(u, prepared.attribute_records);
    profile.mark("resource_updates");
    i.sync_object_poses(u, prepared.pose_records);
    i.sync_lights(prepared);
    profile.mark("object_poses_and_lights");

    const auto retired_material_shaders = i.stage_resource_retirement(u, retained_handoff_shaders);
    i.sync_camera(u, prepared, variant_updates);
    i.sync_world(u, prepared, plan.linking_plan_changed);

    i.finish_geometry_sync(u, prepared, variant_updates, flags, retired_binding_shaders, retired_material_shaders);
  }
  i.publish_generation(u, prepared, plan);
  profile.mark("publish");
}

RenderSession::Impl::RenderUpdateFlags
RenderSession::Impl::configure_candidate_rendering(const SceneUpdate &u, const PreparedUpdate &prepared,
                                                   UpdateProfile &profile) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  const auto &denoise_policy = prepared.denoise_policy;
  const auto aovs_changed = prepared.aovs_changed;
  const auto lightgroups_changed = prepared.lightgroups_changed;
  const auto &crypto = prepared.crypto;
  const auto crypto_changed = prepared.crypto_changed;
  // Resolve passes, denoising and integrator settings before shader sync.
  const bool previous_vectors = i.motion_vectors();
  const bool previous_uv_pass =
      std::find(i.config.passes.begin(), i.config.passes.end(), "uv") != i.config.passes.end();
  const bool passes_changed =
      (u.passes && *u.passes != i.config.passes) || aovs_changed || lightgroups_changed || crypto_changed;
  if (u.passes)
    i.config.passes = *u.passes;
  const bool uv_pass_changed =
      previous_uv_pass != (std::find(i.config.passes.begin(), i.config.passes.end(), "uv") != i.config.passes.end());
  if (u.aovs)
    i.config.aovs = *u.aovs;
  if (u.lightgroups)
    i.config.lightgroups = *u.lightgroups;
  i.config.cryptomatte = crypto;
  if (passes_changed || (u.denoising && *u.denoising != i.config.denoising) ||
      denoise_policy.backend != i.denoise_policy.backend || denoise_policy.warning != i.denoise_policy.warning)
    i.configure_denoising(u.denoising.value_or(i.config.denoising), denoise_policy, passes_changed);
  const bool rendering_changed = u.rendering && *u.rendering != i.rendering;
  if (rendering_changed) {
    configure_render_settings(*u.rendering, *i.session, i.params, i.config.interactive);
    i.rendering = *u.rendering;
  }
  if (i.color_update_pending) {
    scene->shader_manager->init_xyz_transforms();
    for (auto *shader : scene->shaders)
      shader->tag_update(scene);
  }
  if (scene->update_stats)
    scene->update_stats->clear();
  return {previous_vectors, uv_pass_changed, rendering_changed, &profile};
}

} // namespace cycles_houdini
