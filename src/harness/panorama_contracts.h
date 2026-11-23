// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "../core/camera_motion.h"
#include "cycles_houdini/snapshot.h"
#include "scene/camera.h"
#include "kernel/camera/projection.h"

namespace cycles_houdini::harness {
inline void panorama_contracts(const std::string &device) {
  int checks = 0;
  const auto require = [&](bool value, const char *text) {
    motion_require(value, text);
    ++checks;
  };
  PanoramaSettings p;
  p.type = "fisheye_equisolid";
  for (const auto [width, height, aspect, x, y] :
       {std::tuple{128, 64, 1.f, 36.f, 18.f}, std::tuple{64, 128, 1.f, 18.f, 36.f},
        std::tuple{128, 64, 2.f, 36.f, 9.f}}) {
    const auto fitted = panorama_sensor(p, width, height, aspect);
    require(std::abs(fitted.first - x) < 1e-6 && std::abs(fitted.second - y) < 1e-6,
            "Panorama Auto sensor fitting is incorrect");
  }
  p.sensor_fit = "vertical";
  const auto vertical = panorama_sensor(p, 128, 64, 1);
  require(vertical.first == 48 && vertical.second == 24, "Vertical sensor fitting is incorrect");
  CameraData camera;
  camera.panorama = p;
  const Matrix identity = identity_matrix;
  auto axis = camera_engine_transform(camera, identity);
  require(axis[0] == 0 && axis[4] == 0 && axis[8] == 1,
          "Panorama +X axis does not follow the source camera forward axis");
  camera.panorama->type = "mirrorball";
  axis = camera_engine_transform(camera, identity);
  require(axis[1] == 0 && axis[5] == 0 && axis[9] == -1,
          "Mirrorball -Y axis does not follow the source camera forward axis");
  WorkerUpdate packet;
  packet.scene.camera = camera;
  require(decode_worker_update(encode_worker_update(packet)).scene.camera == packet.scene.camera,
          "Panoramic settings did not retain wire ownership");
  CameraSourceSample nominal;
  nominal.projection = 2;
  nominal.value.panorama = PanoramaSettings{};
  nominal.value.short_axis_fov = std::numeric_limits<float>::quiet_NaN();
  nominal.value.panorama->fisheye_lens = std::numeric_limits<float>::quiet_NaN();
  auto resolved = resolve_camera_source(nominal, {}, "/obj/panorama", .5);
  require(std::isfinite(resolved.short_axis_fov) && std::isfinite(resolved.panorama->fisheye_lens) &&
              resolved.source_diagnostics.empty(),
          "Unused panoramic controls affected capture");
  std::vector<CameraSourceSample> samples(3, nominal);
  samples.front().value.transform[3] = -.1f;
  samples.back().value.transform[3] = .1f;
  samples.front().value.panorama->longitude_max = std::numeric_limits<float>::quiet_NaN();
  resolved = resolve_camera_source(nominal, samples, "/obj/panorama", .5);
  require(resolved.motion.size() == 3 && resolved.source_diagnostics.size() == 1 &&
              resolved.source_diagnostics.front().code == "camera.shutter_panorama",
          "Panorama recovery removed valid camera motion");

  // Direct pinned-engine round trips include legitimate calibrations and
  // longitude windows that expose inverse-mapping defects independently of UI.
  for (int model :
       {ccl::PANORAMA_EQUIRECTANGULAR, ccl::PANORAMA_EQUIANGULAR_CUBEMAP_FACE, ccl::PANORAMA_FISHEYE_EQUIDISTANT,
        ccl::PANORAMA_FISHEYE_EQUISOLID, ccl::PANORAMA_FISHEYE_LENS_POLYNOMIAL, ccl::PANORAMA_MIRRORBALL,
        ccl::PANORAMA_CENTRAL_CYLINDRICAL}) {
    ccl::KernelCamera k{};
    k.panorama_type = model;
    k.equirectangular_range = ccl::make_float4(-2 * M_PI_F, M_PI_F, -M_PI_F, M_PI_F);
    k.central_cylindrical_range = ccl::make_float4(M_PI_F, -M_PI_F, -1, 1);
    k.fisheye_fov = M_PI_F;
    k.fisheye_lens = 10.5f;
    k.sensorwidth = 36;
    k.sensorheight = 18;
    k.fisheye_lens_polynomial_coefficients = ccl::make_float4(-.095238095f, 0, 0, 0);
    for (const auto [u, v] : {std::pair{.5f, .5f}, std::pair{.3f, .4f}, std::pair{.7f, .6f}, std::pair{.5f, .8f}}) {
      const auto d = ccl::panorama_to_direction(&k, u, v);
      if (ccl::is_zero(d))
        continue;
      const auto actual = ccl::direction_to_panorama(&k, d);
      require(std::isfinite(actual.x) && std::isfinite(actual.y) && std::abs(actual.x - u) < .0002f &&
                  std::abs(actual.y - v) < .0002f,
              "Ordinary panoramic inverse differs from its ray mapping");
    }
  }
  const auto wrapped = ccl::make_float4(-2 * M_PI_F, 0, -M_PI_F, M_PI_F);
  for (float u : {.1f, .3f, .7f, .9f}) {
    const auto d = ccl::equirectangular_range_to_direction(u, .4f, wrapped);
    const auto result = ccl::direction_to_equirectangular_range(d, wrapped);
    require(std::abs(result.x - u) < .0001, "Offset equirectangular longitude window did not round-trip");
    const auto dual = ccl::direction_to_equirectangular_range(
        ccl::dual3(d, ccl::make_float3(.01f, .02f, .03f), ccl::make_float3(.03f, -.02f, .01f)), wrapped);
    require(std::abs(dual.val.x - u) < .0001 && std::isfinite(dual.dx.x) && std::isfinite(dual.dy.y),
            "Longitude unwrapping discarded ray derivatives");
    const auto cylinder_range = ccl::make_float4(0, -2 * M_PI_F, -2, 1);
    const auto cylinder = ccl::direction_to_central_cylindrical(
        ccl::central_cylindrical_to_direction(u, .4f, cylinder_range), cylinder_range);
    require(std::abs(cylinder.x - u) < .0001 && std::abs(cylinder.y - .4f) < .0001,
            "Offset cylindrical longitude did not round-trip");
  }
  for (const auto coefficients :
       {ccl::make_float4(0, -.005f, 0, 0), ccl::make_float4(0, 0, -.0004f, 0), ccl::make_float4(0, 0, 0, -.00003f)})
    for (const auto [u, v] : {std::pair{.6f, .6f}, std::pair{.7f, .5f}, std::pair{64.5f / 128, 32.5f / 64}}) {
      const auto d = ccl::fisheye_lens_polynomial_to_direction(u, v, 0, coefficients, M_PI_F, 36, 18);
      const auto actual = ccl::direction_to_fisheye_lens_polynomial(d, 0, coefficients, 36, 18);
      require(std::isfinite(actual.x) && std::isfinite(actual.y) && std::abs(actual.x - u) < .0002f &&
                  std::abs(actual.y - v) < .0002f,
              "Zero linear coefficient breaks an invertible radial polynomial");
    }
  const auto axial = ccl::fisheye_equisolid_to_direction(64.5f / 128, 32.5f / 64, 100.f, M_PI_F, 36, 18);
  const auto axial_uv = ccl::direction_to_fisheye_equisolid(axial, 100.f, 36, 18);
  require(std::abs(axial_uv.x - 64.5f / 128) < .00001 && std::abs(axial_uv.y - 32.5f / 64) < .00001,
          "Equisolid inverse loses small angles at the optical axis");
  ccl::Camera sized;
  sized.set_camera_type(ccl::CAMERA_PANORAMA);
  sized.set_full_width(128);
  sized.set_full_height(64);
  sized.worldtocamera = ccl::transform_identity();
  auto &k = sized.kernel_camera;
  k = {};
  k.type = ccl::CAMERA_PANORAMA;
  k.panorama_type = ccl::PANORAMA_FISHEYE_EQUIDISTANT;
  k.fisheye_fov = M_PI_F;
  k.cameratoworld = ccl::transform_identity();
  k.rastertocamera = ccl::projection_identity();
  k.rastertocamera.x.x = 1.f / 128;
  k.rastertocamera.y.y = 1.f / 64;
  k.aperturesize = .5f;
  k.focaldistance = 3;
  k.inv_aperture_ratio = 1;
  k.aperture_ratio = 1;
  k.nearclip = .01f;
  k.cliplength = 100;
  k.differential_scale = 1;
  k.intrinsic.projection = ccl::make_float4(k.nearclip, k.nearclip + k.cliplength, 0, 0);
  k.intrinsic.fisheye = ccl::make_float4(k.fisheye_fov, 0, 0, 0);
  const auto pixel_width = sized.world_to_raster_size(ccl::make_float3(3, 0, 0));
  require(std::isfinite(pixel_width) && pixel_width > 0, "Panoramic DOF pole produces an invalid dicing footprint");

  SessionConfig config;
  config.device_id = device;
  config.width = 8;
  config.height = 8;
  config.samples = 1;
  config.interactive = false;
  RenderSession session(config);
  SceneUpdate good;
  camera.panorama = PanoramaSettings{};
  good.camera = camera;
  good.background = Float3{.1f, .2f, .3f};
  motion_wait(session, session.apply(good));
  const auto generation = session.status().generation;
  for (int kind = 0; kind < 5; ++kind) {
    SceneUpdate bad;
    bad.camera = camera;
    if (kind == 0)
      bad.camera->panorama->type = "unknown";
    if (kind == 1)
      bad.camera->panorama->latitude_max = -2;
    if (kind == 2)
      bad.camera->orthographic = true;
    if (kind == 3)
      bad.camera->intrinsic_motion = {{.8f}, {.85f}, {.9f}};
    if (kind == 4) {
      bad.camera->panorama->type = "fisheye_lens_polynomial";
      bad.camera->panorama->polynomial = {};
    }
    bool strict = false;
    try {
      session.apply(bad);
    } catch (const std::invalid_argument &) {
      strict = true;
    }
    require(strict && session.status().generation == generation && session.status().state != SessionState::Failed,
            "Malformed panorama data modified the accepted scene");
  }
  session.close();
  require(session.status().owned_geometry_bytes == session.status().device_allocation_bytes &&
              session.status().owned_geometry_bytes == 0,
          "Panorama contracts retained resources");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"worker_protocol\":" << worker_protocol_version
            << "}\n";
}
} // namespace cycles_houdini::harness
