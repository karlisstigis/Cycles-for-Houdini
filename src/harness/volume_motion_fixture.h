// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "volume_fixture.h"
#include "motion_fixture.h"

namespace cycles_houdini::harness {
inline double volume_profile(double x) {
  x = std::abs(x);
  return x <= 0.5 ? 1.0 : std::max(0.0, (0.625 - x) / 0.125);
}
inline double volume_transmission_reference(double x, double seconds, double reference_time, double speed = 96) {
  double total = 0;
  constexpr int steps = 4096;
  for (int step = 0; step < steps; ++step) {
    const double time = (step + 0.5) / steps - reference_time;
    total += std::exp(-1.125 * volume_profile(x - speed * seconds * time));
  }
  return total / steps;
}
inline void check_volume_transmission(const FrameEvent &frame, double seconds, double reference_time, double world_x,
                                      const std::string &name) {
  const int centre = std::lround((world_x / 4 + 0.5) * frame.width);
  double measured = 0, expected = 0;
  for (int y = frame.height / 2 - 2; y < frame.height / 2 + 2; ++y)
    for (int x = centre - 2; x < centre + 2; ++x) {
      measured += motion_red(frame, x, y);
      expected += volume_transmission_reference((x + 0.5) * 4 / frame.width - 2, seconds, reference_time);
    }
  expected /= 16;
  measured /= 16;
  motion_require(std::abs(measured - expected) < 0.025,
                 name + ": expected " + std::to_string(expected) + " got " + std::to_string(measured));
  std::cout << "{\"check\":\"" << name << "\",\"measured\":" << measured << ",\"analytic\":" << expected
            << ",\"tolerance\":0.025}\n";
}
inline void volume_motion_reference_test(const std::string &device, const std::filesystem::path &destination) {
  std::filesystem::create_directories(destination);
  SessionConfig config;
  config.device_id = device;
  config.width = 96;
  config.height = 64;
  config.samples = 512;
  RenderSession session(config);
  auto volume = std::make_shared<VolumeData>();
  volume->key = "motion/volume";
  volume->material = "motion/absorb";
  volume->revision = 1;
  volume->fields = {{"density", density_grid()}, {"velocity", motion_velocity_grid()}};
  volume->velocity_seconds = 1.0f / 48;
  volume->velocity_reference_time = 0.5f;
  volume->step_size = 0.03125f;
  SceneUpdate scene;
  scene.volumes = {volume};
  scene.background = Float3{1, 1, 1};
  scene.motion_blur = true;
  scene.camera = CameraData{};
  scene.camera->orthographic = true;
  scene.camera->ortho_scale = 4 * 64.0f / 96;
  MaterialData material;
  material.key = volume->material;
  material.kind = MaterialKind::VolumeAbsorption;
  material.color = {0, 0, 0};
  scene.materials = {material};
  scene.objects.push_back({"motion/object", volume->key});
  auto frame = motion_wait(session, session.apply(scene));
  check_volume_transmission(*frame, 1.0 / 48, 0.5, 0, "Centred two-unit volume trail");
  const auto conversions = session.status().geometry_conversions;
  for (const auto reference : {0.0f, 1.0f, 0.25f}) {
    volume = std::make_shared<VolumeData>(*volume);
    volume->revision++;
    volume->velocity_reference_time = reference;
    SceneUpdate edit;
    edit.volumes = {volume};
    frame = motion_wait(session, session.apply(edit));
    const double centre = (0.5 - reference) * 2;
    check_volume_transmission(*frame, 1.0 / 48, reference, centre,
                              "Explicit shutter reference " + std::to_string(reference));
    if (reference == 0) {
      check_volume_transmission(*frame, 1.0 / 48, reference, 1.75, "Fast trail beyond source volume bounds");
      session.write_exr((destination / "long-trail.exr").string());
    }
  }
  SceneUpdate toggle;
  toggle.motion_blur = false;
  frame = motion_wait(session, session.apply(toggle));
  check_volume_transmission(*frame, 0, 0.25, 0, "Disable held volume motion");
  const auto before = session.status().geometry_conversions;
  toggle.motion_blur = true;
  frame = motion_wait(session, session.apply(toggle));
  check_volume_transmission(*frame, 1.0 / 48, 0.25, 0.5, "Re-enable owned volume motion");
  motion_require(session.status().geometry_conversions == before, "Motion toggle recopied volume trees");
  SceneUpdate camera;
  camera.camera = scene.camera;
  frame = motion_wait(session, session.apply(camera));
  motion_require(session.status().geometry_conversions == before, "Camera recopied volume trees");
  const auto generation = session.status().generation;
  auto bad = std::make_shared<VolumeData>(*volume);
  bad->revision++;
  bad->velocity_seconds = -1;
  SceneUpdate invalid;
  invalid.volumes = {bad};
  bool rejected = false;
  try {
    session.apply(invalid);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  motion_require(rejected && session.status().generation == generation, "Invalid volume timing mutated the scene");
  // A single fast voxel must survive the conservative envelope, even when
  // nearly all velocity values are zero (no percentile estimate).
  auto outlier = openvdb::Vec3SGrid::create(openvdb::Vec3s(0));
  outlier->setName("velocity");
  outlier->fill(openvdb::CoordBBox(openvdb::Coord(-8), openvdb::Coord(7)), openvdb::Vec3s(0), true);
  outlier->tree().setValue(openvdb::Coord(0), openvdb::Vec3s(960, -480, 240));
  VolumeData envelope = *volume;
  envelope.fields = {{"density", density_grid()}, {"velocity", outlier}};
  const auto bound = volume_velocity_bounds(envelope);
  motion_require(bound[0] >= 15 && bound[1] >= 7.5 && bound[2] >= 3.75, "Sparse velocity outlier omitted from bounds");
  std::cout << "{\"check\":\"Sparse outlier envelope\",\"maximum_displacement\":[" << bound[0] << "," << bound[1] << ","
            << bound[2] << "]}\n";
  VolumeData component_volume;
  for (int axis = 0; axis < 3; ++axis) {
    auto scalar = openvdb::FloatGrid::create(0.0f);
    scalar->setTransform(openvdb::math::Transform::createLinearTransform(0.125));
    if (axis == 0)
      scalar->fill(openvdb::CoordBBox(openvdb::Coord(0), openvdb::Coord(7)), 96.0f, true);
    else if (axis == 1)
      scalar->tree().setValue(openvdb::Coord(0), 12);
    component_volume.fields.push_back({std::string("vel.") + "xyz"[axis], scalar});
  }
  normalize_volume_fields(component_volume);
  motion_require(component_volume.fields.size() == 1, "Component grids were not retired after merge");
  const auto merged = openvdb::gridConstPtrCast<openvdb::Vec3SGrid>(component_volume.fields.front().grid);
  motion_require(merged->tree().getValue(openvdb::Coord(0)) == openvdb::Vec3s(96, 12, 0) &&
                     merged->tree().getValue(openvdb::Coord(7)) == openvdb::Vec3s(96, 0, 0) &&
                     merged->tree().getValue(openvdb::Coord(8)) == openvdb::Vec3s(0),
                 "Sparse tile/leaf component merge changed vector values");
  std::cout << "{\"check\":\"Sparse tile and leaf velocity merge\",\"active_voxels\":" << merged->activeVoxelCount()
            << "}\n";
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
                 "Volume motion shutdown retained resources");
  std::cout << "{\"result\":\"passed\",\"checks\":9,\"initial_geometry_conversions\":" << conversions << "}\n";
}
} // namespace cycles_houdini::harness
