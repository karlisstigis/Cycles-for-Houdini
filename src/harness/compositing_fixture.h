// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <fstream>
#include <iomanip>

namespace cycles_houdini::harness {
// Independent geometry and engine socket constants. No Houdini graph,
// extracted buffers, binding identities or converted scene are replayed.
inline void compositing_reference_test(const std::string &device, const std::filesystem::path &output) {
  struct Case {
    const char *name;
    bool film, glass;
    float roughness, tint;
    bool holdout{};
  };
  const Case cases[] = {{"glass_opaque", true, false, 0, 1},        {"glass_white", true, true, 0, 1},
                        {"glass_tinted", true, true, 0, .8f},       {"glass_rough", true, true, .5f, 1},
                        {"glass_film_disabled", false, true, 0, 1}, {"holdout", true, false, 0, 1, true}};
  SessionConfig config;
  config.device_id = device;
  config.width = 96;
  config.height = 64;
  config.samples = 1024;
  config.interactive = false;
  RenderSession session(config);
  std::ofstream report(output);
  if (!report)
    throw std::runtime_error("Cannot open compositing reference output");
  report << std::setprecision(9) << "{\"cases\":[";
  bool first = true;
  for (const auto &sample : cases) {
    SceneUpdate update;
    update.camera = CameraData{};
    update.camera->orthographic = true;
    update.camera->ortho_scale = 8.0f / 3;
    update.background = Float3{.05f, .1f, .15f};
    update.transparent_background = sample.film;
    update.transparent_glass = sample.glass;
    update.transparent_roughness_threshold = .1f;
    MaterialData material;
    material.key = "independent/surface";
    material.kind = MaterialKind::Graph;
    ShaderGraphData graph;
    if (sample.holdout) {
      graph.nodes.push_back({"matte", "cycles/holdout/1", {}});
      graph.surface = {"matte", "holdout"};
    } else {
      graph.nodes.push_back({"dielectric",
                             "cycles/glass_bsdf/1",
                             {{"color", std::array<float, 3>{sample.tint, sample.tint, sample.tint}},
                              {"roughness", sample.roughness},
                              {"IOR", 1.5f}}});
      graph.surface = {"dielectric", "BSDF"};
    }
    material.graph = graph;
    update.materials = {material};
    if (first) {
      auto mesh = std::make_shared<MeshData>();
      mesh->key = "independent/plane";
      mesh->revision = 1;
      mesh->positions = {{-3, -3, 0}, {.173f, -3, 0}, {.173f, 3, 0}, {-3, 3, 0}};
      mesh->triangles = {{3, 0, 1}, {1, 2, 3}};
      mesh->materials = {material.key};
      update.meshes = {mesh};
      update.objects.push_back({"independent/object", mesh->key});
    }
    auto frame = motion_wait(session, session.apply(update));
    const auto &pixels = frame->planes.front().pixels;
    const auto offset = (std::size_t(32) * 96 + 12) * 4;
    for (int channel = 0; channel < 4; ++channel)
      motion_require(std::isfinite(pixels[offset + channel]), "Non-finite compositing reference");
    if (!first)
      report << ',';
    first = false;
    report << "{\"name\":\"" << sample.name << "\",\"rgba\":[";
    for (int c = 0; c < 4; ++c) {
      if (c)
        report << ',';
      report << pixels[offset + c];
    }
    report << "]}";
    session.write_exr((output.parent_path() / (output.stem().string() + "-" + sample.name + ".exr")).string());
  }
  session.close();
  const auto status = session.status();
  motion_require(status.owned_geometry_bytes == 0 && status.device_allocation_bytes == 0,
                 "Compositing reference retained allocations at close");
  report << "],\"result\":\"passed\"}\n";
  if (!report)
    throw std::runtime_error("Cannot write compositing reference report");
}
} // namespace cycles_houdini::harness
