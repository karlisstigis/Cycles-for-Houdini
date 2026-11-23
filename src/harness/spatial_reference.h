// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <cmath>
#include <fstream>
#include <numbers>

namespace cycles_houdini::harness {
struct SpatialSpec {
  std::string name;
  int mode{}, kind{}, from{}, to{1}; // vector transform=0; Generated vector/scalar/temporal=1/2/3.
  bool world{}, mirror{}, camera_motion{}, velocity{};
  int input_mode{};
  bool input_animation{}, surface_generated{}, diagnostic{};
  float input_x{.25f}, camera_x{}, camera_angle{}, shape_rate{48}, object_rate{18};
};
inline std::vector<SpatialSpec> spatial_specs() {
  std::vector<SpatialSpec> specs = {{"generated_vector", 1}, {"generated_scalar", 2}, {"generated_temporal", 3}};
  for (const auto &spaces : {std::pair{1, 0}, std::pair{0, 1}, std::pair{1, 1}, std::pair{2, 0}, std::pair{0, 2},
                             std::pair{1, 2}, std::pair{2, 1}, std::pair{2, 2}})
    for (int kind = 0; kind < 3; ++kind) {
      const char *labels[] = {"world", "object", "camera"};
      const char *kinds[] = {"vector", "point", "normal"};
      SpatialSpec s;
      s.name = std::string(labels[spaces.first]) + "_" + labels[spaces.second] + "_" + kinds[kind];
      s.kind = kind;
      s.from = spaces.first;
      s.to = spaces.second;
      s.world = spaces.second == 0;
      specs.push_back(s);
    }
  auto mirror = specs.at(3);
  mirror.name = "mirrored_object_world_vector";
  mirror.mirror = true;
  specs.push_back(mirror);
  mirror = specs.at(8);
  mirror.name = "mirrored_world_object_normal";
  mirror.mirror = true;
  specs.push_back(mirror);
  SpatialSpec rotated;
  rotated.name = "rotated_camera_vector";
  rotated.from = 2;
  rotated.to = 0;
  rotated.world = true;
  rotated.camera_angle = .35f;
  specs.push_back(rotated);
  auto animated = rotated;
  animated.name = "animated_camera_point";
  animated.kind = 1;
  animated.to = 1;
  animated.world = false;
  animated.camera_motion = true;
  specs.push_back(animated);
  SpatialSpec high;
  high.name = "gui_input_high";
  high.from = 0;
  high.to = 1;
  high.input_x = .4f;
  specs.push_back(high);
  auto slow = high;
  slow.name = "gui_object_slow";
  slow.input_x = .25f;
  slow.object_rate = 9;
  specs.push_back(slow);
  SpatialSpec camera;
  camera.name = "gui_camera_point";
  camera.kind = 1;
  camera.from = 2;
  camera.to = 0;
  camera.world = true;
  camera.camera_x = .1f;
  specs.push_back(camera);
  camera.name = "gui_camera_shift";
  camera.camera_x = .2f;
  specs.push_back(camera);
  SpatialSpec generated;
  generated.name = "gui_generated_slow";
  generated.mode = 2;
  generated.shape_rate = 24;
  specs.push_back(generated);
  generated = specs[0];
  generated.name = "generated_world";
  generated.world = true;
  specs.push_back(generated);
  for (int mode : {1, 2, 3}) {
    auto velocity = specs[mode - 1];
    velocity.name += "_velocity";
    velocity.velocity = true;
    specs.push_back(velocity);
  }
  auto velocity = specs[8];
  velocity.name = "world_object_normal_velocity";
  velocity.velocity = true;
  specs.push_back(velocity);
  SpatialSpec input;
  input.name = "position_world_object";
  input.kind = 1;
  input.input_mode = 1;
  specs.push_back(input);
  input.name = "normal_world_object";
  input.kind = 2;
  input.input_mode = 2;
  specs.push_back(input);
  input.name = "animated_world_object";
  input.kind = 0;
  input.input_mode = 0;
  input.input_animation = true;
  specs.push_back(input);
  SpatialSpec plain;
  plain.name = "gui_plain_camera_shift";
  plain.camera_x = .1f;
  specs.push_back(plain);
  plain.name = "gui_surface_generated";
  plain.camera_x = 0;
  plain.surface_generated = true;
  specs.push_back(plain);
  plain.name = "gui_surface_diagnostic";
  plain.surface_generated = false;
  plain.diagnostic = true;
  specs.push_back(plain);
  return specs;
}
// Independent affine arithmetic; neither native transforms nor a displacement,
// Generated-coordinate or Vector Transform shader builds the expected geometry.
struct SpatialAffine {
  float angle{}, sx{1}, sy{1}, sz{1};
  Float3 translation{};
  Float3 rotate(Float3 p) const {
    return {std::cos(angle) * p.x - std::sin(angle) * p.y, std::sin(angle) * p.x + std::cos(angle) * p.y, p.z};
  }
  Float3 unrotate(Float3 p) const {
    return {std::cos(angle) * p.x + std::sin(angle) * p.y, -std::sin(angle) * p.x + std::cos(angle) * p.y, p.z};
  }
  static Float3 unit(Float3 p) {
    const float length = std::sqrt(p.x * p.x + p.y * p.y + p.z * p.z);
    return length ? Float3{p.x / length, p.y / length, p.z / length} : Float3{};
  }
  Float3 forward(Float3 p, int kind) const {
    p = kind == 2 ? unit(rotate({p.x / sx, p.y / sy, p.z / sz})) : rotate({p.x * sx, p.y * sy, p.z * sz});
    if (kind == 1) {
      p.x += translation.x;
      p.y += translation.y;
      p.z += translation.z;
    }
    return p;
  }
  Float3 inverse(Float3 p, int kind) const {
    if (kind == 1) {
      p.x -= translation.x;
      p.y -= translation.y;
      p.z -= translation.z;
    }
    p = unrotate(p);
    return kind == 2 ? unit({p.x * sx, p.y * sy, p.z * sz}) : Float3{p.x / sx, p.y / sy, p.z / sz};
  }
  Matrix matrix() const {
    return {std::cos(angle) * sx,
            -std::sin(angle) * sy,
            0,
            translation.x,
            std::sin(angle) * sx,
            std::cos(angle) * sy,
            0,
            translation.y,
            0,
            0,
            sz,
            translation.z};
  }
};
inline SpatialAffine spatial_object(const SpatialSpec &s, float t) {
  return {.15f + t * s.object_rate,
          (s.mirror ? -1.f : 1.f) * (1.15f + .15f * std::sin(t * 24)),
          .85f + .1f * std::sin(t * 36),
          1.2f,
          {.1f + .2f * std::sin(t * 24), -.05f + .1f * std::sin(t * 12), .15f}};
}
inline SpatialAffine spatial_camera(const SpatialSpec &s, float t) {
  return {s.camera_angle, 1, 1, -1, {s.camera_x + (s.camera_motion ? .4f * std::sin(t * 24) : 0.f), 0, 5}};
}
inline std::vector<Float3> spatial_shape(const SpatialSpec &s, float t) {
  const float h = std::sin(t * s.shape_rate);
  std::vector<Float3> result;
  for (const auto p : std::vector<Float3>{{-.7f, -.5f, 0}, {.7f, -.5f, 0}, {.7f, .5f, 0}, {-.7f, .5f, 0}})
    result.push_back({p.x * (1 + .4f * h) + .12f * std::sin(t * 24), p.y + .6f * h * p.x, .25f * h * p.x});
  return result;
}
inline std::vector<Float3> spatial_generated(const std::vector<Float3> &points) {
  auto low = points.front(), high = low;
  for (const auto p : points) {
    low = {std::min(low.x, p.x), std::min(low.y, p.y), std::min(low.z, p.z)};
    high = {std::max(high.x, p.x), std::max(high.y, p.y), std::max(high.z, p.z)};
  }
  const Float3 span{high.x - low.x, high.y - low.y, high.z - low.z};
  std::vector<Float3> result;
  for (const auto p : points)
    result.push_back({span.x ? (p.x - low.x) / span.x : 0, span.y ? (p.y - low.y) / span.y : 0,
                      span.z ? (p.z - low.z) / span.z : 0});
  return result;
}
inline std::vector<Float3> spatial_shape_sampled(const SpatialSpec &s, float t, float nominal) {
  if (!s.velocity)
    return spatial_shape(s, t);
  auto values = spatial_shape(s, nominal);
  const float derivative = std::cos(nominal * s.shape_rate) * s.shape_rate;
  const float original_x[] = {-.7f, .7f, .7f, -.7f};
  for (std::size_t i = 0; i < values.size(); ++i) {
    const float dt = t - nominal;
    values[i].x += dt * (original_x[i] * .4f * derivative + .12f * 24 * std::cos(nominal * 24));
    values[i].y += dt * (original_x[i] * .6f * derivative);
    values[i].z += dt * (original_x[i] * .25f * derivative);
  }
  return values;
}
inline std::vector<Float3> spatial_positions(const SpatialSpec &s, float t, float nominal,
                                             const std::vector<Float3> &generated) {
  auto points = spatial_shape_sampled(s, t, nominal);
  // Camera-space shader coordinates use the requested-frame matrix. Shifted
  // shutter poses below still control ray motion independently.
  const auto object = spatial_object(s, t), camera = spatial_camera(s, nominal);
  const auto normal = [&] {
    const auto a = points[1], b = points[0], c = points[3];
    const Float3 u{a.x - b.x, a.y - b.y, a.z - b.z}, v{c.x - b.x, c.y - b.y, c.z - b.z};
    return SpatialAffine::unit({u.y * v.z - u.z * v.y, u.z * v.x - u.x * v.z, u.x * v.y - u.y * v.x});
  }();
  for (std::size_t i = 0; i < points.size(); ++i) {
    Float3 offset{};
    if (s.mode) {
      if (s.mode == 3 && t <= 0)
        offset = {.12f, .06f, 0};
      else if (s.mode == 2) {
        const float height = .25f * generated[i].x;
        offset = {normal.x * height, normal.y * height, normal.z * height};
      } else
        offset = {generated[i].x * .24f, generated[i].y * .12f, generated[i].z * .08f};
      if (s.world)
        offset = object.inverse(offset, 0);
    } else {
      offset = {s.input_x + (s.input_animation ? t * 12 : 0.f), .1f, .2f};
      if (s.input_mode == 1)
        offset = object.forward(points[i], 1);
      else if (s.input_mode == 2)
        offset = object.forward(normal, 2);
      if (s.from != s.to) {
        if (s.from == 1)
          offset = object.forward(offset, s.kind);
        else if (s.from == 2)
          offset = camera.forward(offset, s.kind);
        if (s.to == 1)
          offset = object.inverse(offset, s.kind);
        else if (s.to == 2)
          offset = camera.inverse(offset, s.kind);
      }
      offset = {offset.x * .4f, offset.y * .4f, offset.z * .4f};
      if (s.world)
        offset = object.inverse(offset, 0);
    }
    points[i].x += offset.x;
    points[i].y += offset.y;
    points[i].z += offset.z;
  }
  return points;
}
inline void spatial_displacement_reference(const std::string &device, const std::string &output) {
  std::filesystem::create_directories(output);
  const auto specs = spatial_specs();
  std::ofstream catalogue(std::filesystem::path(output) / "specifications.json");
  catalogue << '[';
  for (std::size_t i = 0; i < specs.size(); ++i) {
    const auto &s = specs[i];
    if (i)
      catalogue << ',';
    catalogue << "{\"name\":\"" << s.name << "\",\"mode\":" << s.mode << ",\"kind\":" << s.kind
              << ",\"from\":" << s.from << ",\"to\":" << s.to << ",\"world\":" << s.world << ",\"mirror\":" << s.mirror
              << ",\"camera_motion\":" << s.camera_motion << ",\"velocity\":" << s.velocity
              << ",\"input_mode\":" << s.input_mode << ",\"input_animation\":" << s.input_animation
              << ",\"surface_generated\":" << s.surface_generated << ",\"diagnostic\":" << s.diagnostic
              << ",\"input_x\":" << s.input_x << ",\"camera_x\":" << s.camera_x
              << ",\"camera_angle\":" << s.camera_angle << ",\"shape_rate\":" << s.shape_rate
              << ",\"object_rate\":" << s.object_rate << '}';
  }
  catalogue << ']';
  catalogue.close();
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 96;
  config.samples = 1024;
  config.interactive = false;
  config.passes = {"combined", "position", "normal", "depth"};
  RenderSession session(config);
  std::uint64_t revision = 0;
  int images = 0;
  const auto render = [&](const SpatialSpec &s, int steps, float offset, float nominal, const std::string &label,
                          SubdivisionType subdivision = SubdivisionType::None) {
    SceneUpdate scene;
    MaterialData material;
    material.key = "reference";
    material.kind = MaterialKind::Emission;
    material.color = {.2f, .4f, .8f};
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "spatial_reference";
    mesh->revision = ++revision;
    mesh->materials = {material.key};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    const auto generated = spatial_generated(spatial_shape(s, nominal));
    const float centre = nominal + offset / 96;
    if (s.mode == 1 || s.mode == 2 || s.surface_generated) {
      material.kind = MaterialKind::Graph;
      material.graph.emplace();
      auto &g = *material.graph;
      g.nodes = {{"color", "cycles/attribute/1", {{"attribute", std::string("reference_color")}}},
                 {"surface", "cycles/emission/1", {{"strength", 1.f}}}};
      g.links = {{{"color", "color"}, {"surface", "color"}}};
      g.surface = {"surface", "emission"};
      NamedAttribute field;
      field.name = "reference_color";
      for (int i : {0, 1, 2, 0, 2, 3})
        field.values.push_back({.1f + .7f * generated[i].x, .15f + .5f * generated[i].y, .3f});
      mesh->attributes.push_back(std::move(field));
    }
    if (s.diagnostic) {
      material.kind = MaterialKind::DiagnosticSurface;
      material.color = {1, 0, 1};
    }
    if (subdivision != SubdivisionType::None) {
      mesh->triangles.clear();
      mesh->subdivision.emplace();
      auto &data = *mesh->subdivision;
      data.face_sizes = {4};
      data.face_corners = {0, 1, 2, 3};
      data.settings.type = subdivision;
      data.settings.space = SubdivisionSpace::Object;
      data.settings.dicing_rate = .1f;
      data.settings.max_level = 3;
      if (!mesh->attributes.empty()) {
        auto &values = mesh->attributes[0].values;
        values.clear();
        for (int i : {0, 1, 2, 3})
          values.push_back({.1f + .7f * generated[i].x, .15f + .5f * generated[i].y, .3f});
      }
    }
    ObjectData object;
    object.key = "reference_object";
    object.geometry_key = mesh->key;
    object.transform = spatial_object(s, centre).matrix();
    mesh->positions = spatial_positions(s, centre, nominal, generated);
    CameraData camera;
    camera.orthographic = true;
    camera.ortho_scale = 3;
    camera.transform = spatial_camera(s, nominal).matrix();
    for (int step = 0; step < steps; ++step) {
      const float t = centre + (float(step) / float(steps - 1) - .5f) / 48;
      if (step != steps / 2)
        mesh->motion.positions.push_back(spatial_positions(s, t, nominal, generated));
      object.motion.push_back(spatial_object(s, t).matrix());
      camera.motion.push_back(spatial_camera(s, t).matrix());
    }
    if (s.camera_motion) {
      if (camera.motion.front() == camera.motion.back())
        throw std::logic_error("Animated spatial reference lost independent shutter camera motion");
      if (offset != 0 && camera.motion[std::size_t(steps / 2)] == camera.transform)
        throw std::logic_error("Shifted spatial reference collapsed nominal and shutter-centre camera poses");
    }
    scene.materials = {material};
    scene.meshes = {mesh};
    scene.objects = {object};
    scene.camera = camera;
    scene.motion_blur = true;
    scene.background = Float3{};
    RenderSettings settings;
    settings.samples = 1024;
    settings.adaptive_sampling = false;
    scene.rendering = settings;
    motion_wait(session, session.apply(scene));
    session.write_exr((std::filesystem::path(output) / (label + ".exr")).string());
    ++images;
  };
  for (const auto &s : specs)
    for (int steps : {3, 5})
      for (const auto [offset, label] : {std::pair{-.75f, "m"}, std::pair{0.f, "c"}, std::pair{.75f, "p"}})
        render(s, steps, offset, 0, s.name + "-" + std::to_string(steps) + "-" + label);
  for (const auto &s : specs)
    if (s.name == "generated_scalar" || s.name == "world_object_normal")
      for (const auto [type, label] :
           {std::pair{SubdivisionType::Linear, "linear"}, std::pair{SubdivisionType::CatmullClark, "catmull_clark"}})
        render(s, 3, .75f, 0, s.name + "-" + label, type);
  for (int frame = 1; frame <= 5; ++frame)
    render(specs.at(1), 3, 0, float(frame - 1) / 24, "frame-" + std::to_string(frame));
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0, "Spatial references retained geometry");
  std::cout << "{\"result\":\"passed\",\"specifications\":" << specs.size() << ",\"images\":" << images << "}\n";
}
} // namespace cycles_houdini::harness
