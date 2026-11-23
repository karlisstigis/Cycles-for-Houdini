// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "cycles_houdini/snapshot.h"
#include "scene/camera.h"
#include "kernel/camera/camera.h"
#include <numbers>

namespace cycles_houdini::harness {
inline CameraDofSample camera_dof_reference_value(float time, int shape) {
  if (shape == 1)
    return {.2f + .4f * time, 2.5f, 0, 1};
  if (shape == 2)
    return {.5f, 2.5f, .2f + 4 * std::numbers::pi_v<float> * time, .7f + .6f * time};
  return {.35f, 2.5f + time, 0, 1};
}
inline CameraData camera_dof_camera(int projection) {
  CameraData camera;
  camera.short_axis_fov = 2 * std::atan(.4f);
  camera.focus_distance = 7;
  camera.aperture_size = .17f;
  if (projection == 1) {
    camera.orthographic = true;
    camera.ortho_scale = 4;
  }
  if (projection == 2) {
    camera.panorama = PanoramaSettings{};
    camera.panorama->longitude_min = -.8f;
    camera.panorama->longitude_max = .8f;
    camera.panorama->latitude_min = -.5f;
    camera.panorama->latitude_max = .5f;
  }
  return camera;
}
inline void camera_dof_motion_contracts(const std::vector<std::string> &devices, const std::string &output) {
  int checks = 0, images = 0, rejections = 0;
  double max_image_error = 0;
  const auto require = [&](bool ok, const char *text) {
    ++checks;
    motion_require(ok, text);
  };
  ccl::KernelCamera kernel{};
  kernel.aperturesize = .17f;
  kernel.focaldistance = 7;
  kernel.aperture_ratio = 1;
  kernel.inv_aperture_ratio = 1;
  for (int count : {3, 5, 129}) {
    ccl::vector<ccl::float4> values;
    for (int i = 0; i < count; ++i) {
      const float t = float(i) / float(count - 1);
      values.push_back(ccl::make_float4(.25f + .5f * t, 2.5f + 5 * t,
                                        -4 * std::numbers::pi_v<float> + 8 * std::numbers::pi_v<float> * t, .5f + t));
    }
    kernel.num_dof_motion_steps = count;
    for (float t : {0.f, .125f, .5f, .625f, 1.f}) {
      const auto actual = ccl::camera_sample_dof(&kernel, values.data(), t);
      const float expected[] = {.25f + .5f * t, 2.5f + 5 * t,
                                -4 * std::numbers::pi_v<float> + 8 * std::numbers::pi_v<float> * t, .5f + t};
      for (int component = 0; component < 4; ++component)
        require(std::abs((&actual.x)[component] - expected[component]) < 3e-5f,
                "Continuous DOF sampling lost a component or wrapped authored rotation");
    }
  }
  kernel.num_dof_motion_steps = 0;
  require(ccl::camera_sample_dof(&kernel, nullptr, .5f).y == 7,
          "Disabled optics motion did not retain requested-frame focus");
  const auto direction = ccl::camera_dof_normalize(ccl::make_float3(1e30f, -1e30f, 1));
  require(std::isfinite(direction.x) && std::isfinite(direction.y) && std::abs(ccl::len(direction) - 1) < 1e-6f,
          "Large finite DOF direction overflowed normalization");

  // Bounds are checked directly against independent conservative extrema,
  // including pupil sizes absent at the requested frame.
  ccl::Camera bound;
  bound.set_camera_type(ccl::CAMERA_PANORAMA);
  bound.set_aperturesize(.25f);
  bound.set_aperture_ratio(1);
  bound.set_focaldistance(5);
  bound.set_nearclip(.01f);
  bound.cameratoworld = ccl::transform_identity();
  bound.kernel_camera_dof_motion.resize(3);
  bound.kernel_camera_dof_motion[0] = ccl::make_float4(1, 2, 0, .5f);
  bound.kernel_camera_dof_motion[1] = ccl::make_float4(.25f, 5, 0, 1);
  bound.kernel_camera_dof_motion[2] = ccl::make_float4(.5f, 4, 0, 1);
  const auto panorama_bounds = bound.viewplane_bounds_get();
  require(panorama_bounds.min.x <= -2.009f && panorama_bounds.max.x >= 2.009f,
          "Camera volume-origin bounds ignored shutter-only aperture/ratio extrema");
  bound.set_camera_type(ccl::CAMERA_ORTHOGRAPHIC);
  bound.set_nearclip(10);
  bound.set_full_width(1);
  bound.set_full_height(1);
  bound.full_rastertocamera = ccl::projection_identity();
  const auto ortho_bounds = bound.viewplane_bounds_get();
  require(ortho_bounds.min.x <= -11.999f, "Orthographic origin bounds ignored shutter-only near/focus extent");

  WorkerUpdate packet;
  packet.scene.camera = camera_dof_camera(0);
  packet.scene.camera->dof_motion = {{.2f, 2, 0, 1}, {.4f, 3, 4, 1.2f}, {.6f, 4, 8, 1.4f}};
  const auto original = packet.scene.camera;
  const auto bytes = encode_worker_update(packet);
  auto decoded = decode_worker_update(bytes);
  packet.scene.camera->dof_motion.front().focus_distance = 99;
  require(decoded.scene.camera == original, "Owned DOF wire samples alias caller storage or lose fields");
  SessionConfig config;
  config.device_ids = devices;
  config.width = 80;
  config.height = 48;
  config.samples = 1024;
  config.threads = 2;
  config.interactive = false;
  RenderSession session(config);
  SceneUpdate scene;
  scene.background = Float3{};
  scene.motion_blur = true;
  MaterialData emission;
  emission.key = "dof/emission";
  emission.kind = MaterialKind::Emission;
  emission.color = {.8f, .25f, .1f};
  scene.materials = {emission};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "dof/bar";
  mesh->revision = 1;
  mesh->materials = {emission.key};
  mesh->positions = {{-.6f, -20, 0}, {.6f, -20, 0}, {.6f, 20, 0}, {-.6f, 20, 0}};
  mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  scene.meshes = {mesh};
  scene.objects = {{"dof/object", mesh->key}};
  scene.camera = camera_dof_camera(0);
  auto frame = motion_wait(session, session.apply(scene));
  const auto generation = session.status().generation;
  for (int bad = 0; bad < 16; ++bad) {
    SceneUpdate update;
    update.camera = *original;
    auto &camera = *update.camera;
    if (bad < 5)
      camera.dof_motion.resize(std::array<std::size_t, 5>{1, 2, 4, 130, 131}[bad]);
    if (bad == 5)
      camera.dof_motion[0].focus_distance = std::numeric_limits<float>::quiet_NaN();
    if (bad == 6)
      camera.dof_motion[0].focus_distance = 0;
    if (bad == 7)
      camera.dof_motion[0].aperture_size = -1;
    if (bad == 8)
      camera.dof_motion[0].aperture_ratio = 0;
    if (bad == 9)
      camera.dof_motion[0].aperture_rotation = std::numeric_limits<float>::infinity();
    if (bad == 10)
      camera.dof_motion[0].aperture_ratio = 1e-40f;
    if (bad == 11) {
      camera.dof_motion[0].aperture_size = 1e30f;
      camera.dof_motion[0].aperture_ratio = 1e-20f;
    }
    if (bad == 12) {
      camera.near_clip = 1e30f;
      camera.far_clip = 2e30f;
      camera.dof_motion[0].aperture_size = 1e20f;
      camera.dof_motion[0].focus_distance = 1e-5f;
    }
    if (bad == 13) {
      camera.aperture_ratio = 1e-40f;
      camera.dof_motion.clear();
    }
    if (bad == 14) {
      camera.aperture_size = 1e30f;
      camera.aperture_ratio = 1e-20f;
      camera.dof_motion.clear();
    }
    if (bad == 15) {
      camera.dof_motion[0] = {1e25f, 5, 0, 1e25f};
      camera.dof_motion[2] = {1, 5, 0, 1e-25f};
    }
    bool rejected = false;
    try {
      session.apply(update);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == generation && session.status().state != SessionState::Failed,
            "Malformed internal DOF changed the accepted scene");
    ++rejections;
  }
  std::filesystem::create_directories(output);
  for (int projection = 0; projection < 3; ++projection)
    for (int shape : {0, 2}) {
      auto camera = camera_dof_camera(projection);
      camera.aperture_blades = shape == 2 ? 5 : 0;
      const int count = projection == 0 ? 5 : 3;
      for (int i = 0; i < count; ++i)
        camera.dof_motion.push_back(camera_dof_reference_value(float(i) / float(count - 1), shape));
      SceneUpdate actual;
      actual.camera = camera;
      actual.motion_blur = true;
      RenderSettings settings;
      settings.samples = 1024;
      settings.adaptive_sampling = false;
      actual.rendering = settings;
      const auto measured = motion_wait(session, session.apply(actual));
      session.write_exr((std::filesystem::path(output) /
                         ("sampled-" + std::to_string(projection) + "-" + std::to_string(shape) + ".exr"))
                            .string());
      std::vector<double> expected(measured->planes.front().pixels.size(), 0);
      // Independent quadrature: separate static cameras at analytic exposure
      // midpoints, with no DOF motion array or new interpolation helper.
      constexpr int quadrature = 33;
      for (int i = 0; i < quadrature; ++i) {
        const auto value = camera_dof_reference_value((float(i) + .5f) / quadrature, shape);
        auto still = camera;
        still.dof_motion.clear();
        still.aperture_size = value.aperture_size;
        still.focus_distance = value.focus_distance;
        still.aperture_rotation = value.aperture_rotation;
        still.aperture_ratio = value.aperture_ratio;
        SceneUpdate reference;
        reference.camera = still;
        reference.motion_blur = false;
        settings.samples = 64;
        reference.rendering = settings;
        const auto part = motion_wait(session, session.apply(reference));
        for (std::size_t pixel = 0; pixel < expected.size(); ++pixel)
          expected[pixel] += part->planes.front().pixels[pixel] / double(quadrature);
      }
      double error = 0;
      for (std::size_t pixel = 0; pixel < expected.size(); ++pixel)
        if (pixel % 4 < 3) {
          const double value = measured->planes.front().pixels[pixel];
          require(std::isfinite(value), "Sampled DOF produced non-finite image data");
          error += std::abs(value - expected[pixel]);
        }
      error /= double(config.width * config.height * 3);
      max_image_error = std::max(max_image_error, error);
      require(error < .003, "Sampled DOF differs from independent static-camera exposure quadrature");
      ++images;
      std::cout << "{\"check\":\"Independent static optics exposure\",\"projection\":" << projection
                << ",\"shape\":" << shape << ",\"mean_difference\":" << error
                << ",\"tolerance\":0.003,\"static_cameras\":" << quadrature << "}" << std::endl;
    }
  // Motion disabled must use the requested-frame fields, not shutter centre.
  SceneUpdate off;
  off.camera = *original;
  off.motion_blur = false;
  auto disabled = motion_wait(session, session.apply(off));
  off.camera->dof_motion.clear();
  auto static_frame = motion_wait(session, session.apply(off));
  require(disabled->planes.front().pixels == static_frame->planes.front().pixels,
          "Disabled motion consumed shutter optics instead of requested-frame fields");
  ++images;
  session.close();
  const auto status = session.status();
  require(status.owned_geometry_bytes == 0 && status.device_allocation_bytes == 0,
          "Sampled camera optics retained renderer allocations");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"image_comparisons\":" << images
            << ",\"rejected_inputs\":" << rejections << ",\"maximum_mean_difference\":" << max_image_error
            << ",\"closed_owned_bytes\":0,\"closed_device_bytes\":0,\"worker_protocol\":" << worker_protocol_version
            << "}" << std::endl;
}
} // namespace cycles_houdini::harness
