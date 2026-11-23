// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/memory.h"
#include "motion_fixture.h"
#include <iomanip>

namespace cycles_houdini::harness {
inline void mesh_family_scale_contracts(const std::string &device) {
  std::size_t checks = 0, frames = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  const std::array<Float3, 4> colors{{{.8f, .1f, .2f}, {.2f, .7f, .3f}, {.1f, .3f, .8f}, {.7f, .6f, .1f}}};
  const auto verify_image = [&](const FrameEvent &frame) {
    const auto plane = [&](const char *name) -> const ImagePlane & {
      const auto found =
          std::find_if(frame.planes.begin(), frame.planes.end(), [&](const auto &value) { return value.pass == name; });
      require(found != frame.planes.end(), std::string("Missing scale output ") + name);
      return *found;
    };
    const auto &beauty = plane("combined");
    const auto &ids = plane("object_id");
    std::size_t visible = 0;
    require(beauty.pixels.size() == ids.pixels.size() * 4, "Scale pass dimensions differ");
    for (std::size_t pixel = 0; pixel < ids.pixels.size(); ++pixel) {
      const int id = int(ids.pixels[pixel]);
      if (!id)
        continue;
      require(id >= 1 && id <= 4, "Scale object index lost its binding");
      const auto &color = colors[std::size_t(id - 1)];
      require(std::abs(beauty.pixels[pixel * 4] - color.x) < 2e-5f &&
                  std::abs(beauty.pixels[pixel * 4 + 1] - color.y) < 2e-5f &&
                  std::abs(beauty.pixels[pixel * 4 + 2] - color.z) < 2e-5f,
              "Logical material palette differs from independent object index");
      ++visible;
    }
    require(visible > 100, "Scale fixture has no substantial visible geometry");
    ++frames;
  };
  for (const std::size_t count : {1000, 10000, 100000, 1024}) {
    const bool adaptive = count == 1024;
    const std::size_t placements = adaptive ? 32 : count;
    const auto columns = std::size_t(std::ceil(std::sqrt(double(placements))));
    const float step = 1.8f / float(columns), half = step * .34f;
    SessionConfig config;
    config.device_id = device;
    config.width = 128;
    config.height = 96;
    config.samples = 1;
    config.threads = 2;
    config.interactive = false;
    config.persistent_updates = true;
    config.collect_diagnostics = true;
    config.use_texture_cache = false;
    config.passes = {"combined", "object_id"};
    SceneUpdate scene;
    scene.background = Float3{};
    scene.camera = CameraData{};
    scene.camera->orthographic = true;
    scene.camera->ortho_scale = 2.4f;
    scene.rendering = RenderSettings{};
    scene.rendering->samples = 1;
    scene.rendering->adaptive_sampling = false;
    scene.rendering->light_tree = false;
    scene.rendering->pixel_filter = "box";
    scene.rendering->filter_width = 1;
    for (std::size_t index = 0; index < colors.size(); ++index) {
      MaterialData material;
      material.key = "style/" + std::to_string(index);
      material.kind = MaterialKind::Emission;
      material.color = colors[index];
      scene.materials.push_back(material);
    }
    std::vector<std::shared_ptr<MeshData>> sources;
    sources.reserve(count);
    scene.meshes.reserve(count);
    scene.objects.reserve(count);
    for (std::size_t index = 0; index < count; ++index) {
      auto source = std::make_shared<MeshData>();
      std::ostringstream name;
      name << "binding/" << std::setw(6) << std::setfill('0') << index;
      source->key = name.str();
      source->revision = 1;
      source->materials = {"style/" + std::to_string(index % 4)};
      source->positions = {{-half, -half, 0}, {half, -half, 0}, {half, half, 0}, {-half, half, 0}};
      if (adaptive) {
        auto &sub = source->subdivision.emplace();
        sub.settings.type = SubdivisionType::Linear;
        sub.settings.adaptive = true;
        sub.settings.space = SubdivisionSpace::Pixel;
        sub.settings.dicing_rate = 4;
        sub.settings.max_level = 2;
        sub.face_sizes = {4};
        sub.face_corners = {0, 1, 2, 3};
      } else
        source->triangles = {{0, 1, 2}, {0, 2, 3}};
      if (index % 2 == 0)
        source->attributes = {{"unused_scalar", 1, std::vector<Float3>(adaptive ? 4 : 6, {.3f, 0, 0})}};
      source->instance_attributes = {{"uniform_field", 1}};
      ObjectData object;
      object.key = "owner/" + std::to_string(index);
      object.geometry_key = source->key;
      object.properties.pass_index = int(index % 4) + 1;
      object.attributes = {{"uniform_field", 1, {float(index % 9), 0, 0}}};
      const auto placement = index % placements;
      object.transform[3] = (float(placement % columns) - float(columns - 1) * .5f) * step;
      object.transform[7] = (float(placement / columns) - float(columns - 1) * .5f) * step;
      scene.objects.push_back(object);
      sources.push_back(source);
      scene.meshes.push_back(source);
    }
    RenderSession session(config);
    const auto run = [&](const char *label, const SceneUpdate &update, std::size_t logical, std::size_t families,
                         std::size_t physical, bool visible = true) {
      const auto start = std::chrono::steady_clock::now();
      const auto generation = session.apply(update);
      const auto applied = std::chrono::steady_clock::now();
      const auto frame = motion_wait(session, generation);
      const auto status = session.status();
      require(status.state == SessionState::Complete && status.error.empty() && status.source_diagnostics.empty(),
              std::string(label) + ": scale render failed");
      require(status.geometry_resources == logical && status.object_resources == logical,
              std::string(label) + ": logical owners changed");
      require(status.mesh_family_resources == families && status.mesh_family_members == logical,
              std::string(label) + ": family membership is duplicated or stale");
      require(status.mesh_variant_resources == physical && status.mesh_variant_bindings == physical &&
                  status.mesh_metric_records == physical,
              std::string(label) + ": physical variants/metrics are copied or stale");
      WorkerReply reply;
      reply.status = status;
      const auto roundtrip = decode_worker_reply(encode_worker_reply(reply), frame->color_configuration);
      require(roundtrip.status.mesh_family_resources == families && roundtrip.status.mesh_family_members == logical &&
                  roundtrip.status.mesh_variant_bindings == physical &&
                  roundtrip.status.mesh_metric_records == physical,
              "Worker status lost physical ownership counters");
      if (visible)
        verify_image(*frame);
      std::cout << "{\"phase\":\"" << label << "\",\"logical_bindings\":" << logical
                << ",\"adaptive\":" << (adaptive ? "true" : "false") << ",\"families\":" << families
                << ",\"physical_meshes\":" << physical
                << ",\"apply_seconds\":" << std::chrono::duration<double>(applied - start).count()
                << ",\"complete_seconds\":"
                << std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count()
                << ",\"owned_geometry_bytes\":" << status.owned_geometry_bytes
                << ",\"device_bytes\":" << status.device_allocation_bytes
                << ",\"process_memory\":" << process_memory_json() << "}" << std::endl;
      return status;
    };
    const auto physical = adaptive ? placements : 1;
    const auto first = run("Initial", scene, count, 1, physical);
    SceneUpdate pose;
    ObjectPoseData moved{scene.objects.back().key};
    moved.transform = scene.objects.back().transform;
    moved.transform[3] += step * .15f;
    pose.object_poses = {moved};
    const auto moving = run("Sparse pose", pose, count, 1, physical + std::size_t(adaptive));
    require(moving.geometry_conversions == first.geometry_conversions, "Sparse pose reconverted logical geometry");
    moved.transform = scene.objects.back().transform;
    pose.object_poses = {moved};
    run("Pose repair", pose, count, 1, physical);
    SceneUpdate remove;
    remove.deleted_geometry = {sources.front()->key};
    remove.deleted_objects = {scene.objects.front().key};
    sources.erase(sources.begin());
    scene.objects.erase(scene.objects.begin());
    run("Leader deletion", remove, count - 1, 1, physical);
    auto old = sources.front();
    auto replacement = std::make_shared<MeshData>(*old);
    replacement->key = "rebound/leader";
    ++replacement->revision;
    SceneUpdate handoff;
    handoff.deleted_geometry = {old->key};
    handoff.meshes = {replacement};
    handoff.objects = {scene.objects.front()};
    handoff.objects.front().geometry_key = replacement->key;
    sources.front() = replacement;
    scene.objects.front() = handoff.objects.front();
    run("Binding key handoff", handoff, count - 1, 1, physical);
    auto broken = std::make_shared<MeshData>(*replacement);
    broken->positions[0].z = .05f;
    ++broken->revision;
    SceneUpdate split;
    split.meshes = {broken};
    run("Shape split", split, count - 1, 2, physical + 1);
    auto repaired = std::make_shared<MeshData>(*replacement);
    repaired->revision = broken->revision + 1;
    split.meshes = {repaired};
    run("Shape repair", split, count - 1, 1, physical);
    SceneUpdate clear;
    for (const auto &source : sources)
      clear.deleted_geometry.push_back(source->key);
    for (const auto &object : scene.objects)
      clear.deleted_objects.push_back(object.key);
    run("All bindings removed", clear, 0, 0, 0, false);
    session.close();
    const auto closed = session.status();
    require(closed.owned_geometry_bytes == 0 && closed.device_allocation_bytes == 0 &&
                closed.mesh_family_members == 0 && closed.mesh_metric_records == 0,
            "Scale session close retained ownership");
  }
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"frames\":" << frames
            << ",\"logical_counts\":[1000,10000,100000],\"adaptive_bindings\":1024,\"adaptive_placements\":32,\"worker_"
               "protocol\":"
            << worker_protocol_version << "}" << std::endl;
}
} // namespace cycles_houdini::harness
