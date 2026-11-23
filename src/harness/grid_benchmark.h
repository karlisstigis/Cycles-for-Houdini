// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <fstream>
#include <iomanip>
#include "cycles_houdini/memory.h"

namespace cycles_houdini::harness {
inline void grid_benchmark(const std::string &device, const std::filesystem::path &destination, int rows, int columns,
                           int repetitions, int samples, bool diagnostics = false, bool memory = false) {
  if (rows < 2 || columns < 2 || rows > 5001 || columns > 5001 || repetitions < 5 || repetitions > 200 || samples < 1)
    throw std::invalid_argument(
        "Grid benchmark requires 2..5001 rows/columns, 5..200 repetitions and positive samples");
  std::filesystem::create_directories(destination);
  const auto begin = std::chrono::steady_clock::now();
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "grid";
  mesh->revision = 1;
  mesh->materials = {"surface"};
  mesh->positions.reserve(std::size_t(rows) * columns);
  for (int row = 0; row < rows; ++row)
    for (int column = 0; column < columns; ++column)
      mesh->positions.push_back(
          {float(-2 + 4 * double(column) / (columns - 1)), float(-1.125 + 2.25 * double(row) / (rows - 1)), 0});
  mesh->triangles.reserve(std::size_t(rows - 1) * (columns - 1) * 2);
  for (int row = 0; row < rows - 1; ++row)
    for (int column = 0; column < columns - 1; ++column) {
      const int a = row * columns + column, b = a + 1, c = a + columns + 1, d = a + columns;
      mesh->triangles.push_back({a, b, c});
      mesh->triangles.push_back({a, c, d});
    }
  const double construction = std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count();
  SessionConfig config;
  config.device_id = device;
  config.width = 960;
  config.height = 540;
  config.samples = samples;
  config.interactive = true;
  config.passes = {"combined", "depth", "normal", "uv", "position"};
  config.collect_diagnostics = diagnostics;
  const std::string memory_before = memory ? process_memory_json() : "";
  const auto create_begin = std::chrono::steady_clock::now();
  RenderSession session(config);
  const double create_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - create_begin).count();
  std::ofstream report(destination / "core-grid-events.jsonl");
  report << std::setprecision(12);
  if (!report)
    throw std::runtime_error("Cannot open grid benchmark event report");
  report << "{\"event\":\"fixture\",\"rows\":" << rows << ",\"columns\":" << columns
         << ",\"triangles\":" << mesh->triangles.size() << ",\"samples\":" << samples
         << ",\"repetitions\":" << repetitions << ",\"construction_seconds\":" << construction
         << ",\"session_construct_seconds\":" << create_seconds << ",\"isolated_edit_blocks\":true";
  if (memory)
    report << ",\"memory_before\":" << memory_before;
  report << "}\n";
  MaterialData material;
  material.key = "surface";
  material.color = {.6f, .2f, .1f};
  material.roughness = 0;
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 2.25f;
  camera.near_clip = .01f;
  camera.far_clip = 10000;
  LightData light;
  light.key = "key";
  light.radius = 0;
  light.strength = {500, 500, 500};
  light.transform[3] = -2;
  light.transform[7] = 3;
  light.transform[11] = 4;
  ObjectData object;
  object.key = "object";
  object.geometry_key = mesh->key;
  Matrix previous = object.transform;
  bool first = true;
  const auto render = [&](const std::string &workload, int repeat) {
    SceneUpdate update;
    update.materials = {material};
    update.lights = {light};
    update.camera = camera;
    update.background = Float3{.025f, .025f, .025f};
    if (first) {
      update.meshes = {mesh};
      update.objects = {object};
      first = false;
    } else if (object.transform != previous)
      update.objects = {object};
    previous = object.transform;
    const auto frame = motion_wait(session, session.apply(update));
    const auto status = session.status();
    motion_require(frame->rendered_width == 960 && frame->rendered_height == 540,
                   "Grid benchmark delivered an enlarged preview");
    const auto center = (std::size_t(270) * 960 + 480) * 4;
    motion_require(std::isfinite(frame->planes[0].pixels[center]) && frame->planes[0].pixels[center] > 0,
                   "Grid benchmark receiver is blank");
    report << "{\"event\":\"render\",\"workload\":\"" << workload << "\",\"repeat\":" << repeat
           << ",\"generation\":" << status.generation << ",\"core_update_seconds\":" << status.last_update_seconds
           << ",\"first_image_seconds\":" << status.first_image_seconds
           << ",\"completed_seconds\":" << frame->published_seconds
           << ",\"geometry_conversions\":" << status.geometry_conversions
           << ",\"tracked_allocation_bytes\":" << status.device_allocation_bytes;
    if (diagnostics) {
      const auto snapshot = session.diagnostics();
      report << ",\"diagnostics\":{\"cancel_join_seconds\":" << snapshot.cancel_join_seconds
             << ",\"shader_graph_preparation_seconds\":" << snapshot.shader_graph_preparation_seconds
             << ",\"material_builds\":" << snapshot.material_builds << ",\"light_updates\":" << snapshot.light_updates
             << ",\"background_builds\":" << snapshot.background_builds
             << ",\"display_updates\":" << snapshot.display_updates
             << ",\"display_conversion_seconds\":" << snapshot.display_conversion_seconds
             << ",\"output_deliveries\":" << snapshot.output_deliveries
             << ",\"output_delivery_seconds\":" << snapshot.output_delivery_seconds << ",\"engine_update_seconds\":{";
      bool comma = false;
      for (const auto &[name, duration] : snapshot.engine_update_seconds) {
        if (comma)
          report << ",";
        comma = true;
        report << std::quoted(name) << ":" << duration;
      }
      report << "},\"buffer_copy_requests\":{";
      comma = false;
      for (const auto &[name, entry] : snapshot.buffer_copy_requests) {
        if (comma)
          report << ",";
        comma = true;
        report << std::quoted(name) << ":{\"calls\":" << entry.calls << ",\"requested_bytes\":" << entry.requested_bytes
               << ",\"host_seconds\":" << entry.host_seconds << "}";
      }
      report << "}}";
    }
    if (memory)
      report << ",\"memory\":" << process_memory_json();
    report << "}\n";
    report.flush();
  };
  render("cold", 0);
  camera.transform[3] = .01f;
  render("warmup-camera", 0);
  camera.transform[3] = 0;
  object.transform[3] = .01f;
  render("warmup-transform", 0);
  object.transform[3] = 0;
  material.color.x = .61f;
  render("warmup-material", 0);
  for (const auto &workload : std::vector<std::string>{"camera", "transform", "material"}) {
    camera.transform[3] = 0;
    object.transform[3] = 0;
    material.color.x = .6f;
    render("prepare-" + workload, 0);
    for (int repeat = 0; repeat < repetitions; ++repeat) {
      camera.transform[3] = workload == "camera" ? (repeat % 2 ? .02f : -.02f) : 0;
      object.transform[3] = workload == "transform" ? (repeat % 2 ? .02f : -.02f) : 0;
      material.color.x = workload == "material" ? float(.6 + (repeat % 2 == 0 ? .02 : 0)) : .6f;
      render(workload, repeat);
      motion_require(session.status().geometry_conversions == 1, "Warm core grid update reconverted geometry");
    }
  }
  session.write_exr((destination / "core-grid.exr").string());
  session.close();
  motion_require(session.status().device_allocation_bytes == 0, "Core grid benchmark retained allocations");
  if (memory)
    report << "{\"event\":\"memory_closed\",\"memory\":" << process_memory_json() << "}\n";
  report << "{\"result\":\"measured\",\"scope\":\"Matched configuration with independently constructed equivalent "
            "planar geometry; generation timings include preparation, sampling and full-quality output delivery\"}\n";
  std::cout << "{\"result\":\"measured\",\"report\":\"core-grid-events.jsonl\"}\n";
}
} // namespace cycles_houdini::harness
