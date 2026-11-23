// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "../core/camera_motion.h"
#include "cycles_houdini/snapshot.h"
#include "scene/camera.h"
#include "kernel/camera/camera.h"
#include <numbers>
#include <iomanip>

namespace cycles_houdini::harness {
inline CameraIntrinsicSample intrinsic_authored(int kind, float t) {
  CameraIntrinsicSample s;
  s.short_axis_fov = 2 * std::atan(.4f + .3f * t);
  if (kind == 1 || kind == 2) {
    s.ortho_scale = 3 + 2 * t;
    s.short_axis_fov = .785398163f;
  }
  if (kind == 2) {
    s.ortho_scale = 4;
    s.far_clip = 2 + 3 * t;
  }
  if (kind == 3) {
    s.short_axis_fov = .785398163f;
    s.near_clip = 2 + 2 * t;
  }
  if (kind == 4) {
    s.short_axis_fov = .785398163f;
    s.far_clip = 2 + 3 * t;
  }
  if (kind == 5) {
    const float q = t * 4, w = q < 1 ? -q : q < 3 ? q - 2 : 4 - q;
    s.short_axis_fov = 2 * std::atan(.5f + .15f * w);
  }
  if (kind >= 6) {
    auto &p = s.panorama.emplace();
    if (kind == 6) {
      p.type = "equirectangular";
      p.longitude_min = -3.14159265f + .5f * t;
      p.longitude_max = 3.14159265f + t;
      p.latitude_min = -1.2f + .3f * t;
      p.latitude_max = 1.1f + .2f * t;
    }
    if (kind == 7) {
      p.type = "central_cylindrical";
      p.cylindrical_u_min = -3.14f + .5f * t;
      p.cylindrical_u_max = 3.14f + t;
      p.cylindrical_v_min = -1 - .5f * t;
      p.cylindrical_v_max = 1 + t;
    }
    if (kind == 8) {
      p.type = "fisheye_equidistant";
      p.fisheye_fov = 1.7f + 2.5f * t;
    }
    if (kind == 9 || kind == 10) {
      p.type = "fisheye_equisolid";
      p.fisheye_fov = 4.8f;
      p.fisheye_lens = 8 + 4 * t;
      p.sensor_width = 30 + 8 * t;
    }
    if (kind == 10) {
      p.sensor_fit = "vertical";
      p.sensor_height = 18 + 12 * t;
    }
    if (kind == 11) {
      p.type = "fisheye_lens_polynomial";
      p.polynomial = {0, -.05f - .02f * t, 0, .000005f * t, 0};
      p.sensor_width = 30 + 6 * t;
    }
    if (kind == 12) {
      p.type = "mirrorball";
      s.far_clip = 2.5f + 3 * t;
    }
  }
  return s;
}
inline void apply_intrinsic(CameraData &c, const CameraIntrinsicSample &s) {
  c.short_axis_fov = s.short_axis_fov;
  c.near_clip = s.near_clip;
  c.far_clip = s.far_clip;
  c.ortho_scale = s.ortho_scale;
  c.panorama = s.panorama;
}
inline void camera_intrinsic_motion_contracts(const std::vector<std::string> &devices, const std::string &output) {
  int checks = 0, images = 0, rejections = 0;
  double worst_error = 0;
  const auto require = [&](bool ok, const std::string &reason) {
    ++checks;
    motion_require(ok, reason);
  };
  CameraSourceSample nominal;
  nominal.value.panorama = PanoramaSettings{};
  nominal.value.panorama->type = "fisheye_equisolid";
  nominal.projection = 2;
  std::vector<CameraSourceSample> source(3, nominal);
  source[0].value.panorama->sensor_width = 1e30f;
  source[0].value.panorama->fisheye_fov = 2;
  source[1].value.panorama->fisheye_fov = 3;
  source[2].value.panorama->fisheye_fov = 4;
  source[0].value.transform[3] = -.2f;
  source[2].value.transform[3] = .2f;
  auto recovered = resolve_camera_source(nominal, source, "/obj/source", .5f, false, 160, 96, 1.5f);
  require(recovered.intrinsic_motion.size() == 3 && recovered.motion.size() == 3,
          "Local sensor recovery lost independent lens or transform motion");
  require(recovered.source_diagnostics.size() == 1 &&
              recovered.source_diagnostics[0].attribute == "sensor_width sensor_height",
          "Derived sensor failure was not attributed locally");
  require(recovered.intrinsic_motion[0].panorama->sensor_width == 36 &&
              recovered.intrinsic_motion[0].panorama->fisheye_fov == 2,
          "Sensor recovery replaced valid FOV samples");
  source.assign(3, nominal);
  source[0].value.panorama->sensor_fit = "vertical";
  source[0].value.panorama->sensor_width = 30;
  recovered = resolve_camera_source(nominal, source, "/obj/source", .5f, false, 160, 96, 1);
  require(recovered.source_diagnostics.size() == 1 &&
              recovered.source_diagnostics[0].code == "camera.shutter_sensor_fit" &&
              recovered.intrinsic_motion[0].panorama->sensor_width == 30,
          "Discrete fit recovery lost the active requested-fit size");
  source.assign(3, nominal);
  for (int n = 0; n < 3; ++n)
    source[n].value.transform[3] = .2f + .1f * n;
  recovered = resolve_camera_source(nominal, source, "/obj/shifted_camera", .5f, false, 160, 96, 1);
  require(recovered.transform == nominal.value.transform && recovered.motion[1] == source[1].value.transform &&
              recovered.panorama == canonical_panorama(*nominal.value.panorama),
          "Shifted camera source lost the independent requested pose/optics");
  source.front().value.transform[0] = -1;
  recovered = resolve_camera_source(nominal, source, "/obj/shifted_camera", .5f, false, 160, 96, 1);
  require(recovered.transform == nominal.value.transform && recovered.motion.empty() &&
              recovered.source_diagnostics.size() == 1 &&
              recovered.source_diagnostics.front().code == "camera.shutter_transform",
          "Known camera handedness failure did not hold the valid requested pose locally");
  CameraIntrinsicSample huge;
  huge.near_clip = 1e10f;
  huge.far_clip = 1e30f;
  require(!camera_intrinsic_error(huge, true, 160, 96, 1),
          "Perspective-only product guard rejected valid orthographic clipping");
  huge.panorama = PanoramaSettings{};
  require(!camera_intrinsic_error(huge, false, 160, 96, 1),
          "Perspective-only product guard rejected valid panoramic clipping");
  huge.panorama.reset();
  require(camera_intrinsic_error(huge, false, 160, 96, 1), "Unsafe perspective matrix product was accepted");
  CameraIntrinsicSample aspect_range;
  require(camera_intrinsic_error(aspect_range, false, 160, 96, 3e38f),
          "Effective perspective inverse overflow was not checked");
  aspect_range.ortho_scale = 1e-10f;
  require(camera_intrinsic_error(aspect_range, true, 160, 96, 3e38f),
          "Effective orthographic height inverse overflow was not checked");
  ccl::KernelCamera kernel{};
  kernel.type = ccl::CAMERA_PERSPECTIVE;
  kernel.width = 80;
  kernel.height = 48;
  kernel.intrinsic_raster = ccl::make_float4(-5.f / 3, -1, 1.f / 24, 1.f / 24);
  kernel.intrinsic.projection = ccl::make_float4(.01f, 10000, .4f, 2);
  require(ccl::is_zero(ccl::camera_intrinsic_project(&kernel, kernel.intrinsic, ccl::make_float3(1, 1, 0))),
          "Zero-Z motion endpoint lost the pinned finite projection guard");
  const auto close =
      ccl::camera_intrinsic_project(&kernel, kernel.intrinsic, ccl::make_float3(1e-10f, -1e-10f, 1e-10f));
  require(std::isfinite(close.x) && std::isfinite(close.y),
          "Finite near-plane endpoint produced nonfinite raster data");
  WorkerUpdate packet;
  packet.scene.camera = CameraData{};
  packet.scene.camera->intrinsic_motion = {intrinsic_authored(0, 0), intrinsic_authored(0, .5f),
                                           intrinsic_authored(0, 1)};
  auto expected = packet.scene.camera;
  const auto bytes = encode_worker_update(packet);
  auto decoded = decode_worker_update(bytes);
  packet.scene.camera->intrinsic_motion[0].short_axis_fov = .1f;
  require(decoded.scene.camera == expected, "Owned intrinsic samples alias their caller or lose wire fields");
  auto mixed = *expected;
  mixed.panorama = PanoramaSettings{};
  for (auto &s : mixed.intrinsic_motion) {
    s.panorama = *mixed.panorama;
    s.near_clip = 1e30f;
    s.far_clip = 2e30f;
  }
  mixed.intrinsic_motion.front().panorama->longitude_min = -2;
  mixed.dof_motion = {{1e20f, 1, 0, 1}, {2e20f, 2, 0, 1}, {3e20f, 3, 0, 1}};
  const auto before_dof = mixed.dof_motion;
  resolve_camera_source_optics(mixed, "/obj/combined_optics");
  require(mixed.source_diagnostics.size() == 1 && mixed.source_diagnostics.front().code == "camera.shutter_clipping" &&
              mixed.dof_motion == before_dof && mixed.intrinsic_motion.front().near_clip == mixed.near_clip &&
              mixed.intrinsic_motion.front().panorama->longitude_min == -2,
          "Coupled clip recovery lost independent valid optics");
  kernel.type = ccl::CAMERA_PANORAMA;
  kernel.panorama_type = ccl::PANORAMA_EQUIRECTANGULAR;
  auto period = kernel.intrinsic;
  period.equirectangular_range = ccl::make_float4(-std::numbers::pi_v<float>, 0, 0, 0);
  require(std::abs(ccl::panorama_motion_wrap(&kernel, period, 150.f) + 10) < 1e-4f,
          "Endpoint longitude window lost its own motion seam period");
  // Independent near-plane origin witnesses. Populate nominal affine
  // raster geometry explicitly; do not reuse the new interpolation/bounds.
  ccl::Camera bound_camera;
  bound_camera.set_camera_type(ccl::CAMERA_ORTHOGRAPHIC);
  bound_camera.set_full_width(80);
  bound_camera.set_full_height(48);
  bound_camera.set_nearclip(.01f);
  bound_camera.set_focaldistance(10);
  bound_camera.set_aperturesize(0);
  bound_camera.set_aperture_ratio(1);
  bound_camera.cameratoworld = ccl::transform_identity();
  const float pa = 1.7f, ratio = 80.f / 48, nominal_width = 4 * ratio, nominal_height = 4 / pa;
  bound_camera.set_viewplane_left(-nominal_width / 2);
  bound_camera.set_viewplane_right(nominal_width / 2);
  bound_camera.set_viewplane_bottom(-nominal_height / 2);
  bound_camera.set_viewplane_top(nominal_height / 2);
  bound_camera.full_rastertocamera =
      ccl::make_projection(nominal_width / 80, 0, 0, -nominal_width / 2, 0, nominal_height / 48, 0, -nominal_height / 2,
                           0, 0, 1, 0, 0, 0, 0, 1);
  bound_camera.kernel_camera_intrinsic_motion.resize(3);
  for (auto &s : bound_camera.kernel_camera_intrinsic_motion) {
    s = {};
    s.projection = ccl::make_float4(.01f, 100, .4f, 6 / pa);
  }
  auto origin_bound = bound_camera.viewplane_bounds_get();
  const float expected_x = 6 * ratio / 2, expected_y = 6 / pa / 2;
  require(origin_bound.min.x <= -expected_x && origin_bound.max.x >= expected_x && origin_bound.min.y <= -expected_y &&
              origin_bound.max.y >= expected_y,
          "Non-square-pixel sampled orthographic origin escaped its volume bound");
  bound_camera.set_camera_type(ccl::CAMERA_PERSPECTIVE);
  bound_camera.set_fov(2 * std::atan(.4f));
  bound_camera.set_viewplane_left(-ratio);
  bound_camera.set_viewplane_right(ratio);
  bound_camera.set_viewplane_bottom(-1);
  bound_camera.set_viewplane_top(1);
  bound_camera.full_rastertocamera =
      ccl::make_projection(2 * ratio * .4f / 80, 0, 0, -ratio * .4f, 0, .8f / 48, 0, -.4f, 0, 0, 0, 1, 0, 0, 0, 1);
  for (auto &s : bound_camera.kernel_camera_intrinsic_motion)
    s.projection = ccl::make_float4(1, 100, 1.3f, 2);
  origin_bound = bound_camera.viewplane_bounds_get();
  require(origin_bound.min.x <= -ratio * 1.3f && origin_bound.max.x >= ratio * 1.3f && origin_bound.min.y <= -1.3f &&
              origin_bound.max.y >= 1.3f && origin_bound.max.z >= 1,
          "Wider sampled perspective near plane escaped its volume bound");
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
  scene.camera = CameraData{};
  scene.camera->transform[11] = 0;
  auto cube = std::make_shared<MeshData>();
  cube->key = "intrinsic/cube";
  cube->revision = 1;
  const std::array<Float3, 6> colors = {Float3{.8f, .1f, .1f}, {.1f, .8f, .1f}, {.1f, .1f, .8f},
                                        {.8f, .8f, .1f},       {.8f, .1f, .8f}, {.1f, .8f, .8f}};
  for (int axis = 0; axis < 3; ++axis)
    for (int side = 0; side < 2; ++side) {
      const int material = axis * 2 + side;
      MaterialData m;
      m.key = "intrinsic/face/" + std::to_string(material);
      m.kind = MaterialKind::Emission;
      m.color = colors[material];
      scene.materials.push_back(m);
      cube->materials.push_back(m.key);
      const int base = int(cube->positions.size());
      for (const auto uv : std::array<Float2, 4>{Float2{-3, -3}, {3, -3}, {3, 3}, {-3, 3}}) {
        Float3 p{};
        (&p.x)[axis] = side ? -3 : 3;
        (&p.x)[(axis + 1) % 3] = uv.x;
        (&p.x)[(axis + 2) % 3] = uv.y;
        cube->positions.push_back(p);
      }
      cube->triangles.push_back({base, base + 1, base + 2});
      cube->triangles.push_back({base, base + 2, base + 3});
      cube->triangle_material.push_back(material);
      cube->triangle_material.push_back(material);
    }
  scene.meshes = {cube};
  scene.objects = {{"intrinsic/object", cube->key}};
  motion_wait(session, session.apply(scene));
  for (int bad = 0; bad < 4; ++bad) {
    SceneUpdate malformed;
    malformed.camera = *scene.camera;
    malformed.camera->motion.assign(3, malformed.camera->transform);
    if (bad == 0)
      malformed.camera->motion.resize(2);
    if (bad == 1)
      malformed.camera->motion.front()[3] = std::numeric_limits<float>::quiet_NaN();
    if (bad == 2)
      malformed.camera->motion[1][0] = 0;
    if (bad == 3)
      malformed.camera->motion.back()[0] = -1;
    const auto before = session.status();
    bool rejected = false;
    try {
      session.apply(malformed);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == before.generation &&
                session.status().state != SessionState::Failed,
            "Malformed camera transform samples changed the accepted scene");
  }
  for (int bad = 0; bad < 16; ++bad) {
    SceneUpdate update;
    update.camera = *expected;
    auto &c = *update.camera;
    if (bad < 5)
      c.intrinsic_motion.resize(std::array<std::size_t, 5>{1, 2, 4, 130, 131}[bad]);
    if (bad == 5)
      c.intrinsic_motion[0].near_clip = std::numeric_limits<float>::quiet_NaN();
    if (bad == 6)
      c.intrinsic_motion[0].far_clip = .001f;
    if (bad == 7)
      c.intrinsic_motion[0].short_axis_fov = 0;
    if (bad == 8)
      c.intrinsic_motion[0].panorama = PanoramaSettings{};
    if (bad >= 9) {
      c.panorama = PanoramaSettings{};
      c.panorama->type = "fisheye_equisolid";
      for (auto &s : c.intrinsic_motion)
        s.panorama = *c.panorama;
    }
    if (bad == 9)
      c.intrinsic_motion[0].panorama->type = "mirrorball";
    if (bad == 10)
      c.intrinsic_motion[0].panorama->sensor_fit = "vertical";
    if (bad == 11)
      c.intrinsic_motion[0].panorama->sensor_width = 1e30f;
    if (bad == 12)
      c.intrinsic_motion[0].panorama->fisheye_lens = 0;
    if (bad == 13)
      c.intrinsic_motion[0].panorama->fisheye_fov = 0;
    if (bad == 14) {
      c.panorama->type = "fisheye_lens_polynomial";
      for (auto &s : c.intrinsic_motion)
        s.panorama = *c.panorama;
      c.intrinsic_motion[0].panorama->polynomial[1] = std::numeric_limits<float>::infinity();
    }
    if (bad == 15) {
      c.panorama.reset();
      for (auto &s : c.intrinsic_motion)
        s.panorama.reset();
      c.intrinsic_motion[0].near_clip = 1e10f;
      c.intrinsic_motion[0].far_clip = 1e30f;
    }
    const auto before = session.status();
    bool rejected = false;
    try {
      session.apply(update);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == before.generation &&
                session.status().state != SessionState::Failed,
            "Malformed intrinsic payload changed the accepted scene");
    ++rejections;
  }
  std::filesystem::create_directories(output);
  for (int kind = 0; kind < 13; ++kind) {
    CameraData camera;
    camera.transform[11] = 0;
    camera.orthographic = kind == 1 || kind == 2;
    camera.pixel_aspect = kind == 1 || kind == 2 ? 1.7f : 1;
    apply_intrinsic(camera, intrinsic_authored(kind, .37f)); // Requested frame differs from shutter midpoint.
    const int count = kind == 5 ? 5 : 3;
    for (int step = 0; step < count; ++step)
      camera.intrinsic_motion.push_back(intrinsic_authored(kind, float(step) / (count - 1)));
    if (kind == 0) {
      camera.aperture_size = .2f;
      camera.focus_distance = 2;
      camera.dof_motion = {{.1f, 2, 0, 1}, {.2f, 3.5f, 0, 1}, {.3f, 5, 0, 1}};
      camera.transform[3] = -.15f;
      for (float x : {.1f, .3f, .5f}) {
        auto pose = camera.transform;
        pose[3] = x;
        camera.motion.push_back(pose);
      }
    }
    RenderSettings settings;
    settings.samples = 1024;
    settings.adaptive_sampling = false;
    SceneUpdate update;
    update.camera = camera;
    update.motion_blur = true;
    update.rendering = settings;
    const auto measured = motion_wait(session, session.apply(update));
    session.write_exr((std::filesystem::path(output) / ("sampled-" + std::to_string(kind) + ".exr")).string());
    std::vector<double> reference(measured->planes.front().pixels.size(), 0);
    constexpr int quadrature = 48;
    for (int step = 0; step < quadrature; ++step) {
      const float t = (float(step) + .5f) / quadrature;
      auto fixed = camera;
      fixed.intrinsic_motion.clear();
      fixed.dof_motion.clear();
      apply_intrinsic(fixed, intrinsic_authored(kind, t));
      if (kind == 0) {
        fixed.aperture_size = .1f + .2f * t;
        fixed.focus_distance = 2 + 3 * t;
        fixed.motion.clear();
        fixed.transform[3] = .1f + .4f * t;
      }
      update.camera = fixed;
      update.motion_blur = false;
      settings.samples = 64;
      update.rendering = settings;
      const auto part = motion_wait(session, session.apply(update));
      for (std::size_t n = 0; n < reference.size(); ++n)
        reference[n] += part->planes.front().pixels[n] / double(quadrature);
    }
    double error = 0;
    for (std::size_t n = 0; n < reference.size(); ++n)
      if (n % 4 < 3) {
        require(std::isfinite(measured->planes.front().pixels[n]), "Nonfinite intrinsic image");
        error += std::abs(measured->planes.front().pixels[n] - reference[n]);
      }
    error /= config.width * config.height * 3;
    worst_error = std::max(worst_error, error);
    require(error < .0035, "Sampled intrinsic image differs from independent static-camera exposure quadrature: " +
                               std::to_string(kind) + " / " + std::to_string(error));
    ++images;
    std::cout << "{\"check\":\"Independent static projection exposure\",\"kind\":" << kind
              << ",\"mean_difference\":" << error << ",\"tolerance\":0.0035,\"static_cameras\":" << quadrature << "}"
              << std::endl;
  }
  // A valid tiny shutter FOV drives a huge dicing estimate. The configured
  // maximum must bound conversion before a float can overflow an integer.
  auto diced = std::make_shared<MeshData>(*cube);
  diced->revision = 2;
  diced->triangles.clear();
  diced->triangle_material.clear();
  auto &sub = diced->subdivision.emplace();
  sub.settings.type = SubdivisionType::Linear;
  sub.settings.adaptive = true;
  sub.settings.max_level = 2;
  sub.settings.dicing_rate = .0001f;
  for (int face = 0; face < 6; ++face) {
    sub.face_sizes.push_back(4);
    sub.face_material.push_back(face);
    for (int n = 0; n < 4; ++n)
      sub.face_corners.push_back(face * 4 + n);
  }
  SceneUpdate bounded;
  bounded.meshes = {diced};
  bounded.motion_blur = true;
  bounded.camera = CameraData{};
  bounded.camera->transform[11] = 0;
  for (float fov : {1e-20f, .785398163f, 1.1f})
    bounded.camera->intrinsic_motion.push_back({fov, .01f, 10000, 2});
  const auto tessellations = session.status().subdivision_tessellations;
  const auto finite = motion_wait(session, session.apply(bounded));
  require(session.status().subdivision_tessellations > tessellations &&
              std::all_of(finite->planes.front().pixels.begin(), finite->planes.front().pixels.end(),
                          [](float v) { return std::isfinite(v); }),
          "Tiny animated footprint escaped finite bounded dicing");
  MaterialData displacement;
  displacement.key = "intrinsic/window";
  displacement.kind = MaterialKind::Graph;
  ShaderGraphData graph;
  graph.nodes = {{"surface", "cycles/emission/1", {{"color", std::array<float, 3>{.1f, .6f, .2f}}, {"strength", 1.f}}},
                 {"coordinates", "cycles/texture_coordinate/1", {}},
                 {"split", "cycles/separate_xyz/1", {}},
                 {"combine", "cycles/combine_xyz/1", {}},
                 {"height",
                  "cycles/vector_displacement/1",
                  {{"space", std::string("object")}, {"midlevel", 0.f}, {"scale", .1f}}}};
  graph.links = {{{"coordinates", "window"}, {"split", "vector"}},
                 {{"split", "x"}, {"combine", "z"}},
                 {{"combine", "vector"}, {"height", "vector"}}};
  graph.surface = {"surface", "emission"};
  graph.displacement = ShaderEndpoint{"height", "displacement"};
  graph.displacement_method = DisplacementMethod::True;
  displacement.graph = graph;
  auto flat = std::make_shared<MeshData>();
  flat->key = "intrinsic/window_body";
  flat->revision = 1;
  flat->positions = {{-1, -1, -3}, {1, -1, -3}, {1, 1, -3}, {-1, 1, -3}};
  flat->triangles = {{0, 1, 2}, {0, 2, 3}};
  flat->materials = {displacement.key};
  SceneUpdate window;
  window.background = Float3{};
  RenderSettings window_settings;
  window_settings.samples = 1024;
  window_settings.adaptive_sampling = false;
  window.rendering = window_settings;
  window.deleted_objects = {"intrinsic/object"};
  window.deleted_geometry = {cube->key};
  window.meshes = {flat};
  window.materials = {displacement};
  window.objects = {{"intrinsic/window_object", flat->key}};
  window.camera = CameraData{};
  window.camera->transform[11] = 0;
  window.motion_blur = true;
  window.camera->intrinsic_motion = {{.6f, .01f, 10000, 2}, {.785398163f, .01f, 10000, 2}, {1.1f, .01f, 10000, 2}};
  motion_wait(session, session.apply(window));
  const auto baked = session.status().displacement_rebuilds;
  window.deleted_objects.clear();
  window.deleted_geometry.clear();
  window.camera->intrinsic_motion.front().short_axis_fov = .5f;
  window.camera->intrinsic_motion.back().short_axis_fov = 1.2f;
  for (float x : {.2f, .3f, .4f}) {
    auto pose = window.camera->transform;
    pose[3] = x;
    window.camera->motion.push_back(pose);
  }
  const auto retained = motion_wait(session, session.apply(window));
  require(session.status().displacement_rebuilds == baked,
          "Shutter-only projection/pose edits invalidated requested-frame Window displacement");
  RenderSession independent(config);
  const auto static_height = motion_wait(independent, independent.apply(window));
  float max_error = 0;
  for (std::size_t n = 0; n < retained->planes.front().pixels.size(); ++n)
    max_error =
        std::max(max_error, std::abs(retained->planes.front().pixels[n] - static_height->planes.front().pixels[n]));
  require(max_error < .0001f, "Retained requested-frame Window height differs from a fresh source");
  independent.close();
  require(independent.status().device_allocation_bytes == 0 && independent.status().owned_geometry_bytes == 0,
          "Window reference retained allocations");
  SceneUpdate numeric = window;
  auto numeric_body = std::make_shared<MeshData>(*flat);
  for (auto &p : numeric_body->positions)
    p.z += .1f * (.5f + p.x / (6 * ratio * std::tan(window.camera->short_axis_fov * .5f)));
  MaterialData numeric_surface;
  numeric_surface.key = "intrinsic/numeric_window";
  numeric_surface.kind = MaterialKind::Emission;
  numeric_surface.color = {.1f, .6f, .2f};
  numeric_body->materials = {numeric_surface.key};
  numeric.meshes = {numeric_body};
  numeric.materials = {numeric_surface};
  RenderSession numeric_session(config);
  const auto numeric_image = motion_wait(numeric_session, numeric_session.apply(numeric));
  double numeric_error = 0;
  for (std::size_t n = 0; n < retained->planes.front().pixels.size(); ++n)
    numeric_error += std::abs(retained->planes.front().pixels[n] - numeric_image->planes.front().pixels[n]);
  numeric_error /= retained->planes.front().pixels.size();
  require(numeric_error < .00005,
          "Window height used a shutter pose instead of the independently computed requested projection: " +
              std::to_string(numeric_error));
  numeric_session.close();
  require(numeric_session.status().device_allocation_bytes == 0 && numeric_session.status().owned_geometry_bytes == 0,
          "Numeric Window reference retained allocations");
  window.camera->short_axis_fov = .9f;
  motion_wait(session, session.apply(window));
  require(session.status().displacement_rebuilds > baked,
          "Nominal projection edit failed to invalidate requested-frame Window height");
  auto actual_devices = session.status().render_device_ids, requested_devices = devices;
  std::sort(actual_devices.begin(), actual_devices.end());
  std::sort(requested_devices.begin(), requested_devices.end());
  require(actual_devices == requested_devices, "Intrinsic session did not instantiate the selected devices");
  session.close();
  require(session.status().device_allocation_bytes == 0 && session.status().owned_geometry_bytes == 0,
          "Intrinsic camera close retained allocations");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"image_comparisons\":" << images
            << ",\"rejected_inputs\":" << rejections
            << ",\"camera_transform_rejections\":4,\"maximum_mean_difference\":" << worst_error
            << ",\"bounded_dicing_witness\":true,\"camera_origin_bound_witnesses\":2,\"camera_displacement_"
               "comparisons\":2,\"render_device_ids\":[";
  for (std::size_t n = 0; n < actual_devices.size(); ++n) {
    if (n)
      std::cout << ',';
    std::cout << std::quoted(actual_devices[n]);
  }
  std::cout << "],\"closed_owned_bytes\":0,\"closed_device_bytes\":0,\"worker_protocol\":" << worker_protocol_version
            << "}" << std::endl;
}
} // namespace cycles_houdini::harness
