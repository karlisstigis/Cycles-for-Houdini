// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"

namespace cycles_houdini {
void RenderSession::Impl::install_prepared_images(PreparedUpdate &prepared) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &world_requirements = prepared.world_requirements;
  auto &prepared_world = prepared.prepared_world;
  auto &light_requirements = prepared.light_requirements;
  auto &prepared_lights = prepared.prepared_lights;
  auto &prepared_graphs = prepared.prepared_graphs;
  auto &requirements = prepared.requirements;
  auto &recovered_requirements = prepared.recovered_requirements;
  auto &prepared_bindings = prepared.prepared_bindings;
  for (auto &[key, graph] : prepared_graphs)
    install_memory_images(*scene, *graph, requirements.at(key));
  for (auto &[key, graph] : prepared_bindings)
    install_memory_images(*scene, *graph, recovered_requirements.at(key));
  if (prepared_world)
    install_memory_images(*scene, *prepared_world, world_requirements);
  for (auto &[key, graph] : prepared_lights)
    install_memory_images(*scene, *graph, light_requirements.at(key));
  i.light_requirements = std::move(light_requirements);
}

void RenderSession::Impl::sync_object_pose_values(ObjectRecord &record, const Matrix &matrix,
                                                  const std::vector<Matrix> &samples,
                                                  const std::optional<Matrix> &nominal) {
  auto *object = record.node;
  record.motion = samples;
  record.matrix = record.render_matrix = matrix;
  record.nominal_transform = nominal;
  object->set_tfm(transform(matrix));
  auto motion = interactive_denoising_motion()
                    ? denoising_motion(object->get_motion(), object->get_tfm())
                    : transform_motion(sampled_transforms() ? samples : std::vector<Matrix>{});
  object->set_motion(motion);
}

void RenderSession::Impl::sync_object_poses(const SceneUpdate &u, const std::vector<ObjectRecord *> &pose_records) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  // Validation resolved these records under the control lock. std::map
  // insertion preserves them, and conflicting deletion/upserts rejected
  // before mutation. No borrowed record survives this transaction.
  if (pose_fast_rule() && u.object_poses.size() >= 4096) {
    // Each pose writes only its own record and engine object: all cores, any schedule gives the same
    // values. Engine tagging touches shared managers and stays serial, in pose order.
    tbb::parallel_for(tbb::blocked_range<std::size_t>(0, u.object_poses.size(), 2048),
                      [&](const tbb::blocked_range<std::size_t> &range) {
                        for (std::size_t n = range.begin(); n != range.end(); ++n) {
                          const auto &pose = u.object_poses[n];
                          i.sync_object_pose_values(*pose_records[n], pose.transform, pose.motion,
                                                    pose.nominal_transform);
                        }
                      });
    for (std::size_t n = 0; n < u.object_poses.size(); ++n)
      pose_records[n]->node->tag_update(scene);
    i.object_updates += u.object_poses.size();
    return;
  }
  for (std::size_t n = 0; n < u.object_poses.size(); ++n) {
    const auto &pose = u.object_poses[n];
    auto &record = *pose_records[n];
    i.sync_object_pose_values(record, pose.transform, pose.motion, pose.nominal_transform);
    record.node->tag_update(scene);
    ++i.object_updates;
  }
}

void RenderSession::Impl::sync_camera(const SceneUpdate &u, const PreparedUpdate &prepared,
                                      std::set<std::string> &variant_updates) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &desired_camera = prepared.desired_camera;
  auto &desired_dicing = prepared.desired_dicing;
  auto &force_dicing = prepared.force_dicing;
  auto &hold_dicing = prepared.hold_dicing;
  auto &desired_dicing_camera = prepared.desired_dicing_camera;
  auto &dicing_projection_changed = prepared.dicing_projection_changed;
  auto &prepared_iris = prepared.prepared_iris;
  auto &iris_changed = prepared.iris_changed;
  if (desired_camera &&
      (iris_changed || (u.camera && (!i.camera_definition ||
                                     camera_render_state(*i.camera_definition) != camera_render_state(*u.camera))))) {
    const auto &c = *desired_camera;
    auto *camera = scene->camera;
    const auto matrix = camera_engine_transform(c, c.transform);
    const bool matrix_changed =
        !i.camera_definition || camera_engine_transform(*i.camera_definition, i.camera_definition->transform) != matrix;
    const bool projection_changed =
        !i.camera_definition || camera_coordinate_projection(*i.camera_definition) != camera_coordinate_projection(c);
    configure_camera_projection(camera, c, i.config);
    camera->set_aperturesize(c.aperture_size);
    camera->set_focaldistance(c.focus_distance);
    camera->set_blades(c.aperture_blades);
    camera->set_bladesrotation(c.aperture_rotation);
    camera->set_aperture_ratio(c.aperture_ratio);
#ifdef WITH_IMAGE_IRIS
    if (iris_changed) {
      ccl::array<float> table;
      if (prepared_iris) {
        table.resize(prepared_iris->table.size());
        std::copy(prepared_iris->table.begin(), prepared_iris->table.end(), table.begin());
      }
      camera->set_image_iris(table);
    }
    camera->set_image_iris_method(c.image_iris_method);
    camera->set_image_iris_color(c.image_iris_color);
#endif
    if (iris_changed) {
      i.image_iris = prepared_iris;
      if (prepared_iris)
        ++i.image_iris_preparations;
    }
    ccl::array<float> dof_motion;
    dof_motion.resize(c.dof_motion.size() * 4);
    for (std::size_t index = 0; index < c.dof_motion.size(); ++index) {
      const auto &sample = c.dof_motion[index];
      dof_motion[index * 4] = sample.aperture_size;
      dof_motion[index * 4 + 1] = sample.focus_distance;
      dof_motion[index * 4 + 2] = sample.aperture_rotation;
      dof_motion[index * 4 + 3] = sample.aperture_ratio;
    }
    camera->set_dof_motion(dof_motion);
    camera->set_shuttertime(c.shutter_frames);
    camera->set_motion_position(ccl::MOTION_POSITION_CENTER);
    camera->set_rolling_shutter_type(c.sampling.rolling_shutter ? ccl::Camera::ROLLING_SHUTTER_TOP
                                                                : ccl::Camera::ROLLING_SHUTTER_NONE);
    camera->set_rolling_shutter_duration(c.sampling.rolling_duration);
    ccl::array<float> shutter_curve;
    shutter_curve.resize(c.sampling.shutter_curve.empty() ? 256 : c.sampling.shutter_curve.size());
    if (c.sampling.shutter_curve.empty())
      std::fill(shutter_curve.begin(), shutter_curve.end(), 1.f);
    else
      std::copy(c.sampling.shutter_curve.begin(), c.sampling.shutter_curve.end(), shutter_curve.begin());
    camera->set_shutter_curve(shutter_curve);
    i.camera_motion.clear();
    for (const auto &sample : c.motion)
      i.camera_motion.push_back(camera_engine_transform(c, sample));
    auto camera_motion = i.interactive_denoising_motion()
                             ? i.denoising_motion(camera->get_motion(), camera->get_matrix())
                             : transform_motion(i.sampled_transforms() ? i.camera_motion : std::vector<Matrix>{});
    camera->set_motion(camera_motion);
    // Socket setters capture projection and pose changes. Tagging every socket
    // here would also invalidate the image iris and shutter lookup tables.
    {
      std::lock_guard lock(i.store->mutex);
      i.store->pixel_aspect = c.pixel_aspect;
    }
    i.camera_definition = c;
    if (matrix_changed || projection_changed)
      for (auto &[key, record] : i.meshes)
        if (record.source && i.camera_displacement(record, matrix_changed, projection_changed)) {
          record.dirty = true;
          variant_updates.insert(key);
        }
  } else if (u.camera)
    i.camera_definition = u.camera;
  if (u.dicing) {
    if (u.dicing->topology_cache_mib != i.dicing_settings.topology_cache_mib)
      i.set_topology_cache_limit(u.dicing->topology_cache_mib);
    i.dicing_settings = *u.dicing;
  }
  if (desired_dicing_camera &&
      ((!hold_dicing && (!u.defer_camera_subdivision || force_dicing)) || !i.dicing_camera_definition) &&
      (dicing_projection_changed || i.suspended_camera_refinement)) {
    // The render camera defines rays and shader camera coordinates.
    // Dicing has its own projection, using the same Cycles camera model.
    auto *camera = scene->camera;
    auto *dicing = scene->dicing_camera;
    for (const auto &socket : camera->type->inputs)
      dicing->copy_value(socket, *camera, socket);
    configure_camera_projection(dicing, *desired_dicing_camera, i.config);
    std::vector<Matrix> motion;
    for (const auto &matrix : desired_dicing_camera->motion)
      motion.push_back(camera_engine_transform(*desired_dicing_camera, matrix));
    auto dicing_motion = transform_motion(i.sampled_transforms() ? motion : std::vector<Matrix>{});
    dicing->set_motion(dicing_motion);
    dicing->set_offscreen_dicing_scale(desired_dicing.offscreen_scale);
    dicing->tag_modified();
    dicing->need_device_update = true;
    for (auto &[key, record] : i.meshes)
      if (record.pixel_adaptive_subdivision()) {
        record.dirty = true;
        variant_updates.insert(key);
      }
    i.dicing_camera_definition = desired_dicing_camera;
    i.applied_offscreen_scale = desired_dicing.offscreen_scale;
  }
}

} // namespace cycles_houdini
