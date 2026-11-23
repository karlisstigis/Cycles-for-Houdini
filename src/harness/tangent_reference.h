// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <numbers>
#include <tuple>

namespace cycles_houdini::harness {
struct TangentReferenceSpec {
  const char *name;
  int mode{}, map{}, mirror{1}, directx{}, tilt{};
  float uv_speed{}, geometry_speed{};
};
inline const std::vector<TangentReferenceSpec> &tangent_reference_specs() {
  static const std::vector<TangentReferenceSpec> result = {{"tangent3", 0, 0},
                                                           {"tangent2", 0, 1},
                                                           {"tangent_uv", 0, 0, 1, 0, 0, 48, 0},
                                                           {"tangent_geometry", 0, 0, 1, 0, 0, 0, 24},
                                                           {"tangent_both", 0, 1, 1, 0, 1, 48, 24},
                                                           {"normal_original", 1, 0, 1, 0, 0, 48, 0},
                                                           {"normal_displaced", 1, 1, 1, 0, 1, 48, 24},
                                                           {"normal_mirrored", 1, 0, -1, 1, 0, 48, 0},
                                                           {"normal_tilt", 1, 0, 1, 0, 1, 0, 24},
                                                           {"displacement_uv", 2, 0, 1, 0, 0, 48, 0},
                                                           {"displacement_geometry", 2, 0, 1, 0, 1, 0, 24},
                                                           {"displacement_both", 2, 1, -1, 0, 1, 48, 24},
                                                           {"tangent_displacement", 3, 0, 1, 0, 1, 48, 24},
                                                           {"normal_to_displacement", 4, 1, 1, 0, 1, 48, 24},
                                                           {"normal_displaced_to_displacement", 4, 0, 1, 0, 1, 48, 24},
                                                           {"shared_normal_displaced", 5, 1, 1, 0, 1, 48, 24}};
  return result;
}
// The reference has no UV maps, tangent generator, Normal Map or displacement
// nodes. Its geometry and reference-vector fields are independently analytic.
inline void tangent_reference_images(const std::string &device, const std::string &output) {
  std::filesystem::create_directories(output);
  SessionConfig config;
  config.device_id = device;
  config.width = 192;
  config.height = 96;
  config.samples = 1024;
  config.interactive = false;
  config.passes = {"combined", "position", "normal"};
  RenderSession session(config);
  std::uint64_t revision = 0;
  {
    // A user's field named map.tangent must not replace map's generated
    // tangent, even when both fields contribute to the same material.
    SceneUpdate scene;
    MaterialData material;
    material.key = "reference";
    material.kind = MaterialKind::Graph;
    material.graph.emplace();
    auto &graph = *material.graph;
    graph.nodes = {
        {"t", "cycles/tangent/1", {{"direction_type", std::string("uv_map")}, {"attribute", std::string("map")}}},
        {"field", "cycles/attribute/1", {{"attribute", std::string("map.tangent")}}},
        {"sum", "cycles/vector_math/1", {{"math_type", std::string("add")}}},
        {"half",
         "cycles/vector_math/1",
         {{"math_type", std::string("multiply")}, {"vector2", std::array<float, 3>{.5f, .5f, .5f}}}},
        {"surface", "cycles/emission/1", {{"strength", 1.f}}}};
    graph.links = {{{"t", "tangent"}, {"sum", "vector1"}},
                   {{"field", "vector"}, {"sum", "vector2"}},
                   {{"sum", "vector"}, {"half", "vector1"}},
                   {{"half", "vector"}, {"surface", "color"}}};
    graph.surface = {"surface", "emission"};
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "right";
    mesh->revision = ++revision;
    mesh->positions = {{-.6f, -.5f, 0}, {.6f, -.5f, 0}, {.6f, .5f, 0}, {-.6f, .5f, 0}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
    mesh->materials = {material.key};
    mesh->attributes = {{"map", 3, {{0, 0, 23}, {1, 0, 24}, {1, 1, 25}, {0, 0, 23}, {1, 1, 25}, {0, 1, 26}}},
                        {"map.tangent", 3, std::vector<Float3>(6, {0, .4f, .8f})}};
    ObjectData object;
    object.key = "right";
    object.geometry_key = mesh->key;
    object.transform[3] = 1;
    scene.materials = {material};
    scene.meshes = {mesh};
    scene.objects = {object};
    scene.background = Float3{};
    CameraData camera;
    camera.orthographic = true;
    camera.ortho_scale = 2.3f;
    scene.camera = camera;
    auto frame = motion_wait(session, session.apply(scene));
    const auto offset = (std::size_t(config.height / 2) * config.width + config.width * 3 / 4) * 4;
    const auto &pixels = frame->planes.front().pixels;
    motion_require(std::abs(pixels[offset] - .5f) < .0001f && std::abs(pixels[offset + 1] - .2f) < .0001f &&
                       std::abs(pixels[offset + 2] - .4f) < .0001f,
                   "User field collided with a generated tangent attribute");
  }
  for (const auto &spec : tangent_reference_specs())
    for (int steps : {3, 5}) {
      SceneUpdate scene;
      MaterialData left;
      left.key = "unaffected";
      left.kind = MaterialKind::Emission;
      left.color = {.2f, .6f, .3f};
      MaterialData right = left;
      right.key = "reference";
      if (spec.mode == 0 || spec.mode == 1 || spec.mode == 5) {
        right.kind = MaterialKind::Graph;
        right.graph.emplace();
        auto &graph = *right.graph;
        const auto node = [&](const std::string &id, const std::string &type,
                              std::map<std::string, ShaderValue> parameters = {}) {
          graph.nodes.push_back({id, "cycles/" + type + "/1", std::move(parameters)});
        };
        const auto link = [&](const std::string &from, const std::string &out, const std::string &to,
                              const std::string &in) { graph.links.push_back({{from, out}, {to, in}}); };
        node("t", "attribute", {{"attribute", std::string("reference_tangent")}});
        node("n", "attribute", {{"attribute", std::string("reference_normal")}});
        if (spec.mode == 5 || std::string(spec.name) == "normal_displaced") {
          graph.nodes.back().id = "n_raw";
          node("n", "vector_math", {{"math_type", std::string("normalize")}});
          link("n_raw", "vector", "n", "vector1");
        }
        if (spec.mode == 0) {
          node("nn", "vector_math", {{"math_type", std::string("normalize")}});
          link("n", "vector", "nn", "vector1");
          node("axis", "vector_math", {{"math_type", std::string("cross_product")}});
          link("t", "vector", "axis", "vector1");
          link("nn", "vector", "axis", "vector2");
          node("unit_axis", "vector_math", {{"math_type", std::string("normalize")}});
          link("axis", "vector", "unit_axis", "vector1");
          node("result", "vector_math", {{"math_type", std::string("cross_product")}});
          link("nn", "vector", "result", "vector1");
          link("unit_axis", "vector", "result", "vector2");
        } else {
          node("b", "vector_math", {{"math_type", std::string("cross_product")}});
          link("n", "vector", "b", "vector1");
          link("t", "vector", "b", "vector2");
          for (const auto &[id, input, weight] :
               {std::tuple{"wt", "t", .5f}, std::tuple{"wb", "b", .2f * spec.mirror * (spec.directx ? -1.f : 1.f)},
                std::tuple{"wn", "n", .8f}}) {
            node(id, "vector_math",
                 {{"math_type", std::string("multiply")}, {"vector2", std::array<float, 3>{weight, weight, weight}}});
            link(input, "vector", id, "vector1");
          }
          node("tb", "vector_math", {{"math_type", std::string("add")}});
          link("wt", "vector", "tb", "vector1");
          link("wb", "vector", "tb", "vector2");
          node("sum", "vector_math", {{"math_type", std::string("add")}});
          link("tb", "vector", "sum", "vector1");
          link("wn", "vector", "sum", "vector2");
          node("result", "vector_math", {{"math_type", std::string("normalize")}});
          link("sum", "vector", "result", "vector1");
        }
        node("half", "vector_math",
             {{"math_type", std::string("multiply")}, {"vector2", std::array<float, 3>{.5f, .5f, .5f}}});
        link("result", "vector", "half", "vector1");
        node("display", "vector_math",
             {{"math_type", std::string("add")}, {"vector2", std::array<float, 3>{.5f, .5f, .5f}}});
        link("half", "vector", "display", "vector1");
        node("surface", "emission", {{"strength", 1.f}});
        link("display", "vector", "surface", "color");
        graph.surface = {"surface", "emission"};
      }
      scene.materials = {left, right};
      const std::vector<Float3> square = {{-.6f, -.5f, 0}, {.6f, -.5f, 0}, {.6f, .5f, 0}, {-.6f, .5f, 0}};
      auto unaffected = std::make_shared<MeshData>();
      unaffected->key = "left";
      unaffected->revision = ++revision;
      unaffected->positions = square;
      unaffected->triangles = {{0, 1, 2}, {0, 2, 3}};
      unaffected->materials = {left.key};
      auto mesh = std::make_shared<MeshData>(*unaffected);
      mesh->key = "right";
      mesh->materials = {right.key};
      mesh->attributes = {{"reference_tangent", 3, {}}, {"reference_normal", 3, {}}};
      const auto pose = [&](float time, bool displaced = true, bool hold_uv = false) {
        const float theta = .35f + spec.map * std::numbers::pi_v<float> / 2 + (hold_uv ? 0.f : spec.uv_speed * time);
        const float phi = spec.geometry_speed * time;
        const auto rotate = [&](Float3 p) {
          return spec.tilt ? Float3{std::cos(phi) * p.x + std::sin(phi) * p.z, p.y,
                                    -std::sin(phi) * p.x + std::cos(phi) * p.z}
                           : Float3{std::cos(phi) * p.x - std::sin(phi) * p.y,
                                    std::sin(phi) * p.x + std::cos(phi) * p.y, p.z};
        };
        const auto t = rotate({std::cos(theta), std::sin(theta), 0}), n = rotate({0, 0, 1});
        auto b = rotate({-std::sin(theta) * spec.mirror, std::cos(theta) * spec.mirror, 0});
        auto positions = square;
        for (auto &p : positions) {
          p = rotate(p);
          if (!displaced)
            continue;
          if (spec.mode == 2) {
            p.x += .3f * t.x + .12f * n.x + .15f * b.x;
            p.y += .3f * t.y + .12f * n.y + .15f * b.y;
            p.z += .3f * t.z + .12f * n.z + .15f * b.z;
          } else if (spec.mode == 3) {
            p.x += .3f * t.x;
            p.y += .3f * t.y;
            p.z += .3f * t.z;
          } else if (spec.mode >= 4) {
            const float scale = .25f / std::sqrt(.93f);
            p.x += scale * (.5f * t.x + .2f * b.x + .8f * n.x);
            p.y += scale * (.5f * t.y + .2f * b.y + .8f * n.y);
            p.z += scale * (.5f * t.z + .2f * b.z + .8f * n.z);
          } else
            p.z += .15f;
        }
        return std::tuple{positions, std::vector<Float3>(6, t), std::vector<Float3>(6, n)};
      };
      std::tie(mesh->positions, mesh->attributes[0].values, mesh->corner_normals) = pose(0);
      mesh->attributes[1].values = mesh->corner_normals;
      for (int step = 0; step < steps; ++step)
        if (step != steps / 2) {
          auto [p, t, n] = pose((float(step) / float(steps - 1) - .5f) * .5f / 24.f);
          mesh->motion.positions.push_back(std::move(p));
          mesh->attributes[0].motion_values.push_back(std::move(t));
          mesh->attributes[1].motion_values.push_back(n);
          mesh->motion_corner_normals.push_back(std::move(n));
        }
      ObjectData a;
      a.key = "left";
      a.geometry_key = unaffected->key;
      a.transform[3] = -1;
      auto b = a;
      b.key = "right";
      b.geometry_key = mesh->key;
      b.transform[3] = 1;
      scene.meshes = {unaffected, mesh};
      scene.objects = {a, b};
      scene.motion_blur = true;
      scene.background = Float3{};
      CameraData camera;
      camera.orthographic = true;
      camera.ortho_scale = 2.3f;
      camera.shutter_frames = .5f;
      scene.camera = camera;
      RenderSettings rendering;
      rendering.samples = 1024;
      rendering.adaptive_sampling = false;
      scene.rendering = rendering;
      motion_wait(session, session.apply(scene));
      session.write_exr(
          (std::filesystem::path(output) / (std::string(spec.name) + "-" + std::to_string(steps) + ".exr")).string());
      if (spec.mode >= 2) {
        auto bare = std::make_shared<MeshData>(*mesh);
        bare->revision = ++revision;
        bare->positions = std::get<0>(pose(0, false));
        bare->motion.positions.clear();
        for (int step = 0; step < steps; ++step)
          if (step != steps / 2)
            bare->motion.positions.push_back(
                std::get<0>(pose((float(step) / float(steps - 1) - .5f) * .5f / 24.f, false)));
        auto plain = right;
        plain.graph.reset();
        plain.kind = MaterialKind::Emission;
        plain.color = {.2f, .6f, .3f};
        scene.meshes = {bare};
        scene.materials = {plain};
        motion_wait(session, session.apply(scene));
        session.write_exr(
            (std::filesystem::path(output) / ("bare-" + std::string(spec.name) + "-" + std::to_string(steps) + ".exr"))
                .string());
        auto nominal = std::make_shared<MeshData>(*mesh);
        nominal->revision = ++revision;
        nominal->motion = {};
        nominal->motion_corner_normals.clear();
        for (auto &field : nominal->attributes)
          field.motion_values.clear();
        scene.meshes = {nominal};
        scene.materials = {right};
        motion_wait(session, session.apply(scene));
        session.write_exr((std::filesystem::path(output) /
                           ("frozen-" + std::string(spec.name) + "-" + std::to_string(steps) + ".exr"))
                              .string());
        // Independently retain geometry rotation while fixing only the UV
        // basis at requested time. No UV, tangent or displacement nodes
        // participate in these pre-displaced reference images.
        auto held = std::make_shared<MeshData>(*mesh);
        held->revision = ++revision;
        held->motion.positions.clear();
        held->motion_corner_normals.clear();
        for (auto &field : held->attributes)
          field.motion_values.clear();
        for (int step = 0; step < steps; ++step)
          if (step != steps / 2) {
            auto [p, t, n] = pose((float(step) / float(steps - 1) - .5f) * .5f / 24.f, true, true);
            held->motion.positions.push_back(std::move(p));
            held->attributes[0].motion_values.push_back(std::move(t));
            held->attributes[1].motion_values.push_back(n);
            held->motion_corner_normals.push_back(std::move(n));
          }
        scene.meshes = {held};
        motion_wait(session, session.apply(scene));
        session.write_exr((std::filesystem::path(output) /
                           ("held-uv-" + std::string(spec.name) + "-" + std::to_string(steps) + ".exr"))
                              .string());
      }
    }
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0, "Tangent references retained geometry");
  std::cout << "{\"result\":\"passed\",\"independent_tangent_references\":32,\"recovery_reference_images\":28,\"held_"
               "uv_reference_images\":14,\"generated_name_collision\":true}\n";
}
} // namespace cycles_houdini::harness
