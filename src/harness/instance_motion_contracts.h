// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "camera_sampling_contracts.h"
#include "cycles_houdini/snapshot.h"
#include "../core/instance_motion.h"
#include <bit>
#include <functional>
#include <set>

namespace cycles_houdini::harness {
inline ShaderGraphData instance_field_graph(const std::string &name, int components, bool noise = false,
                                            std::optional<float> constant = {}) {
  ShaderGraphData graph;
  graph.nodes = {{"field", "cycles/attribute/1", {{"attribute", name}}},
                 {"surface", "cycles/emission/1", {{"strength", 1.f}}}};
  ShaderEndpoint value{"field", components == 1 ? "fac" : name == "Cd" ? "color" : "vector"};
  if (noise) {
    graph.nodes.push_back({"noise", "cycles/white_noise_texture/1", {{"dimensions", std::string("1D")}}});
    if (constant) {
      graph.nodes.push_back({"constant", "cycles/value/1", {{"value", *constant}}});
      value = {"constant", "value"};
    }
    graph.links.push_back({value, {"noise", "w"}});
    value = {"noise", "color"};
  } else {
    graph.nodes.push_back({"square",
                           components == 1 ? "cycles/math/1" : "cycles/vector_math/1",
                           {{"math_type", std::string("multiply")}}});
    graph.links.push_back({value, {"square", components == 1 ? "value1" : "vector1"}});
    graph.links.push_back({value, {"square", components == 1 ? "value2" : "vector2"}});
    value = {"square", components == 1 ? "value" : "vector"};
  }
  graph.links.push_back({value, {"surface", "color"}});
  graph.surface = {"surface", "emission"};
  return graph;
}
inline UniformAttribute instance_field(const std::string &name, int components, int steps, int object = 0) {
  UniformAttribute field{name, components, {.93f, .82f, .71f}};
  if (components < 3)
    field.value.z = 0;
  if (components < 2)
    field.value.y = 0;
  for (int step = 0; step < steps; ++step) {
    const float t = float(step) / float(steps - 1);
    Float3 value{.125f + .5f * t, .625f - .25f * t, .25f + .125f * t};
    if (object)
      value = {.75f - .375f * t, .125f + .5f * t, .625f - .25f * t};
    if (components < 3)
      value.z = 0;
    if (components < 2)
      value.y = 0;
    field.motion_values.push_back(value);
  }
  return field;
}
inline std::vector<Float3> instance_positions(int kind, float z) {
  if (kind == 1)
    return {{0, 0, z}};
  if (kind == 2)
    return {{0, -.65f, z}, {0, .65f, z}};
  return {{-.6f, -.65f, z}, {.6f, -.65f, z}, {.6f, .65f, z}, {-.6f, .65f, z}};
}
// The reference stores independent ordinary corner/point/key fields. It never
// uses object uniform storage, its descriptor or the common-grid resampler.
inline SceneUpdate instance_field_scene(int kind, const std::vector<UniformAttribute> &fields, int geometry_steps,
                                        const std::string &prefix, bool reference = false, bool blur = true,
                                        bool noise = false, std::optional<float> constant = {}) {
  SceneUpdate scene;
  scene.background = Float3{};
  scene.motion_blur = blur;
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 3.5f;
  camera.shutter_frames = .75f;
  // Host sampling owns the offset; nominal values below deliberately differ
  // from the shutter midpoint, just as they do for an offset host shutter.
  scene.camera = camera;
  MaterialData material;
  material.key = "instance/material";
  material.kind = MaterialKind::Graph;
  material.pass_index = 9;
  material.cryptomatte_name = "/mat/temporal_instance";
  material.graph = instance_field_graph(fields.front().name, fields.front().components, noise, constant);
  scene.materials = {material};
  for (std::size_t object = 0; object < fields.size(); ++object) {
    const auto &field = fields[object];
    const auto key = prefix + (reference ? "/" + std::to_string(object) : "/shared");
    const int steps = reference && !field.motion_values.empty() ? int(field.motion_values.size()) : geometry_steps;
    const auto centre =
        blur && !field.motion_values.empty() ? field.motion_values[field.motion_values.size() / 2] : field.value;
    ObjectData owner{"instance/object" + std::to_string(object), key};
    owner.transform[3] = object ? .8f : -.8f;
    owner.properties.cryptomatte_object = "/obj/packed/" + std::to_string(object);
    if (!reference)
      owner.attributes = {field};
    scene.objects.push_back(owner);
    if (object && !reference)
      continue;
    const auto full_value = [&](int step) {
      return blur && !field.motion_values.empty() ? field.motion_values[step] : field.value;
    };
    const auto bind = [&](auto &data, std::size_t count) {
      if (!reference) {
        data.instance_attributes = {{field.name, field.components}};
        return;
      }
      if (field.name == "Cd") {
        if constexpr (requires { data.corner_colors; })
          data.corner_colors.assign(count, centre);
        else
          data.colors.assign(count, centre);
      } else
        data.attributes = {{field.name, field.components, std::vector<Float3>(count, centre)}};
      for (int step = 0; step < steps; ++step)
        if (step != steps / 2) {
          const auto values = std::vector<Float3>(count, full_value(step));
          if (field.name == "Cd") {
            if constexpr (requires { data.motion_corner_colors; })
              data.motion_corner_colors.push_back(values);
            else
              data.motion_colors.push_back(values);
          } else
            data.attributes[0].motion_values.push_back(values);
        }
    };
    const auto motion = [&](auto &data) {
      for (int step = 0; step < steps; ++step)
        if (step != steps / 2)
          data.motion.positions.push_back(instance_positions(
              kind, geometry_steps < 3 ? 0.f : .0625f * (2.f * float(step) / float(steps - 1) - 1.f)));
    };
    if (kind == 0) {
      auto data = std::make_shared<MeshData>();
      data->key = key;
      data->revision = 1;
      data->materials = {material.key};
      data->positions = instance_positions(kind, 0);
      data->triangles = {{0, 1, 2}, {0, 2, 3}};
      motion(*data);
      bind(*data, 6);
      scene.meshes.push_back(data);
    } else if (kind == 1) {
      auto data = std::make_shared<PointData>();
      data->key = key;
      data->revision = 1;
      data->material = material.key;
      data->positions = instance_positions(kind, 0);
      data->radii = {.55f};
      motion(*data);
      bind(*data, 1);
      scene.points.push_back(data);
    } else {
      auto data = std::make_shared<CurveData>();
      data->key = key;
      data->revision = 1;
      data->material = material.key;
      data->positions = instance_positions(kind, 0);
      data->radii = {.2f, .2f};
      data->first_key = {0};
      data->shape = CurveShape::Thick;
      motion(*data);
      bind(*data, 2);
      scene.curves.push_back(data);
    }
  }
  return scene;
}
inline std::set<std::string> instance_scene_geometry(const SceneUpdate &scene) {
  std::set<std::string> keys;
  for (const auto &item : scene.meshes)
    keys.insert(item->key);
  for (const auto &item : scene.points)
    keys.insert(item->key);
  for (const auto &item : scene.curves)
    keys.insert(item->key);
  return keys;
}
inline SceneUpdate instance_height_scene(const std::vector<UniformAttribute> &fields, int geometry_steps,
                                         const std::string &prefix, bool reference = false, bool hold = false,
                                         int graph_steps = 0) {
  auto scene = instance_field_scene(0, fields, geometry_steps, prefix, reference);
  if (reference) {
    for (std::size_t index = 0; index < scene.meshes.size(); ++index) {
      auto mesh = std::make_shared<MeshData>(*scene.meshes[index]);
      const auto &field = fields[index];
      const auto value = [&](std::size_t step) {
        return hold || field.motion_values.empty() ? field.value.x : field.motion_values[step].x;
      };
      const auto count = mesh->motion.positions.size() + 1;
      for (auto &position : mesh->positions)
        position.z += value(count / 2);
      for (std::size_t step = 0, sample = 0; step < count; ++step)
        if (step != count / 2) {
          for (auto &position : mesh->motion.positions[sample])
            position.z += value(step);
          ++sample;
        }
      scene.meshes[index] = std::move(mesh);
    }
  } else {
    auto &material = scene.materials[0];
    auto &graph = *material.graph;
    graph.nodes.push_back({"height", "cycles/combine_xyz/1", {}});
    graph.nodes.push_back({"offset",
                           "cycles/vector_displacement/1",
                           {{"space", std::string("object")}, {"midlevel", 0.f}, {"scale", 1.f}}});
    graph.links.push_back({{"field", "fac"}, {"height", "z"}});
    graph.links.push_back({{"height", "vector"}, {"offset", "vector"}});
    graph.displacement = ShaderEndpoint{"offset", "displacement"};
    graph.displacement_method = DisplacementMethod::True;
    if (graph_steps) {
      graph.displacement_time_dependent = true;
      material.displacement_samples.assign(graph_steps, displacement_snapshot(graph));
    }
  }
  return scene;
}
inline void instance_motion_contracts(const std::vector<std::string> &devices) {
  int checks = camera_uniform_shutter_contracts(), images = 0, rejected = 0;
  float maximum_error = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  const auto bits = [&](float a, float b) {
    require(float_bits_equal(a, b), "Exact uniform sample/endpoint bits changed");
  };
  require(instance_motion_grid(3, 5) == 5 && instance_motion_grid(5, 7) == 13 && !instance_motion_grid(127, 129),
          "Incorrect rational displacement sample lattice");
  bits(instance_motion_lerp(-0.f, 1.f, 0, 4), -0.f);
  bits(instance_motion_lerp(1.f, -0.f, 4, 4), -0.f);
  bits(instance_motion_lerp(-0.f, -0.f, 1, 3), -0.f);
  const auto authored = std::bit_cast<float>(std::uint32_t(0x3ea0d97c));
  bits(instance_motion_lerp(authored, authored, 1, 3), authored);
  MeshData resample;
  resample.positions = {{-0.f, .25f, 0}};
  resample.motion.positions = {{{-.5f, 0, 0}}, {{.5f, 0, 0}}};
  const auto lattice = instance_resample_mesh(resample, 5);
  bits(lattice.positions[0].x, -0.f);
  bits(lattice.motion.positions.front()[0].x, -.5f);
  bits(lattice.motion.positions.back()[0].x, .5f);
  require(lattice.motion_corner_normals.empty(), "Resampling invented authored normals");
  MeshData stationary;
  stationary.positions = {{0, 0, 0}};
  stationary.corner_normals = {{1, authored, -0.f}};
  const auto held_normals = instance_resample_mesh(stationary, 5);
  require(held_normals.motion_corner_normals.size() == 4, "Static authored normals lack displacement shutter samples");
  for (const auto &sample : held_normals.motion_corner_normals) {
    bits(sample[0].x, 1.f);
    bits(sample[0].y, authored);
    bits(sample[0].z, -0.f);
  }
  stationary.motion_corner_normals = {{{0, 0, 1}}, {{0, 1, 0}}};
  const auto animated_normals = instance_resample_mesh(stationary, 5);
  require(animated_normals.motion_corner_normals.front() == stationary.motion_corner_normals.front() &&
              animated_normals.motion_corner_normals.back() == stationary.motion_corner_normals.back(),
          "Resampling replaced authored normal motion");
  auto field = instance_field("temporal", 3, 5);
  field.motion_values[0].y = -0.f;
  WorkerUpdate packet;
  packet.scene.objects = {{"wire/object", "wire/geometry"}};
  packet.scene.objects[0].attributes = {field};
  const auto wire = encode_worker_update(packet);
  const auto restored = decode_worker_update(wire);
  require(restored.scene.objects[0].attributes == packet.scene.objects[0].attributes,
          "Independent uniform samples changed in current worker schema");
  auto sign = field;
  sign.motion_values[0].y = 0.f;
  require(!(field == sign), "Uniform equality ignored negative zero");
  auto mutable_field = field;
  auto *exposed = mutable_field.motion_values.data();
  auto owned = mutable_field;
  exposed[0].x = .999f;
  require(!float_bits_equal(owned.motion_values[0].x, mutable_field.motion_values[0].x),
          "Uniform sample snapshot aliases exported mutable storage");
  const auto state = snapshot_state({}, packet.scene);
  packet.scene.objects[0].attributes[0].motion_values[0].x = .888f;
  require(state->objects->at("wire/object").attributes[0] == field,
          "Owned scene snapshot changed with caller sample edits");
  for (int issue = 0; issue < 3; ++issue) {
    auto bad = wire;
    if (issue == 0)
      bad.pop_back();
    else if (issue == 1)
      bad[4] ^= std::byte{1};
    bool refused = false;
    try {
      decode_worker_update(bad, nullptr, issue == 2 ? 1 : 8ull * 1024 * 1024 * 1024);
    } catch (const std::invalid_argument &) {
      refused = true;
    }
    require(refused, "Malformed or allocation-excessive uniform wire payload accepted");
    ++rejected;
  }
  SessionConfig config;
  config.device_ids = devices;
  config.width = 80;
  config.height = 48;
  config.samples = 64;
  config.threads = 2;
  config.interactive = false;
  config.persistent_updates = true;
  config.use_texture_cache = false;
  config.passes = {"combined", "position", "normal", "material_id"};
  RenderSession actual(config), reference(config);
  std::set<std::string> prior_actual, prior_reference;
  std::uint64_t reference_revision{};
  const auto submit = [&](RenderSession &session, SceneUpdate scene, std::set<std::string> &prior) {
    // Reference fields/geometry change independently between controls.
    // The owned core contract deliberately trusts producer revisions.
    if (&session == &reference) {
      ++reference_revision;
      for (auto &source : scene.meshes) {
        auto data = std::make_shared<MeshData>(*source);
        data->revision = reference_revision;
        source = std::move(data);
      }
      for (auto &source : scene.points) {
        auto data = std::make_shared<PointData>(*source);
        data->revision = reference_revision;
        source = std::move(data);
      }
      for (auto &source : scene.curves) {
        auto data = std::make_shared<CurveData>(*source);
        data->revision = reference_revision;
        source = std::move(data);
      }
    }
    const auto next = instance_scene_geometry(scene);
    for (const auto &key : prior)
      if (!next.contains(key))
        scene.deleted_geometry.push_back(key);
    prior = next;
    return motion_wait(session, session.apply(scene));
  };
  const auto compare = [&](SceneUpdate scene, SceneUpdate expected, const std::string &label, float tolerance = .00004f,
                           bool warnings = false) {
    const auto frame = submit(actual, std::move(scene), prior_actual),
               control = submit(reference, std::move(expected), prior_reference);
    require(frame->planes.size() == control->planes.size(), label + ": missing output plane");
    float worst = 0;
    for (const auto &plane : frame->planes) {
      const auto match = std::find_if(control->planes.begin(), control->planes.end(),
                                      [&](const auto &p) { return p.pass == plane.pass; });
      require(match != control->planes.end() && match->pixels.size() == plane.pixels.size(),
              label + ": reference plane layout changed");
      float error = 0;
      for (std::size_t i = 0; i < plane.pixels.size(); ++i) {
        const auto a = plane.pixels[i], b = match->pixels[i];
        if (float_bits_equal(a, b))
          continue;
        require(std::isfinite(a) && std::isfinite(b), label + ": non-finite image");
        error = std::max(error, std::abs(a - b));
      }
      require(error < tolerance,
              label + ": " + plane.pass + " differs from independent geometry fields by " + std::to_string(error));
      worst = std::max(worst, error);
    }
    const auto status = actual.status();
    require(status.geometry_resources == 1 && status.object_resources == 2,
            label + ": shared prototype/object ownership changed");
    const auto indices =
        std::find_if(frame->planes.begin(), frame->planes.end(), [](const auto &p) { return p.pass == "material_id"; });
    require(indices != frame->planes.end() && std::count_if(indices->pixels.begin(), indices->pixels.end(),
                                                            [](float value) { return value == 9; }) > 30,
            label + ": usable geometry or material identity disappeared");
    require(warnings ? !status.source_diagnostics.empty() : status.source_diagnostics.empty(),
            label + ": unexpected source diagnostic state");
    maximum_error = std::max(maximum_error, worst);
    ++images;
    std::cout << "{\"check\":\"" << label << "\",\"maximum_error\":" << worst << ",\"tolerance\":" << tolerance
              << ",\"geometry_resources\":" << status.geometry_resources
              << ",\"mesh_variants\":" << status.mesh_variant_resources
              << ",\"warnings\":" << status.source_diagnostics.size() << "}" << std::endl;
    return frame;
  };
  for (int kind = 0; kind < 3; ++kind)
    for (int channel = 0; channel < 4; ++channel) {
      const auto name = channel == 3 ? "Cd" : "temporal";
      const int components = channel == 3 ? 3 : channel + 1, steps = channel % 2 ? 5 : 3;
      std::vector<UniformAttribute> fields = {instance_field(name, components, steps),
                                              instance_field(name, components, steps, 1)};
      const auto label = std::string(kind == 0   ? "Mesh"
                                     : kind == 1 ? "Point"
                                                 : "Hair") +
                         " uniform " + name + " components" + std::to_string(components) + " steps" +
                         std::to_string(steps);
      const auto prefix = "surface/" + std::to_string(kind) + "/" + std::to_string(channel);
      auto scene = instance_field_scene(kind, fields, steps == 3 ? 5 : 3, prefix);
      compare(scene, instance_field_scene(kind, fields, steps, prefix + "/reference", true), label);
      const auto conversions = actual.status().geometry_conversions;
      if (kind == 0)
        require(actual.status().mesh_variant_resources == 1, "Surface-only temporal fields split the shared mesh");
      compare(instance_field_scene(kind, fields, steps == 3 ? 5 : 3, prefix, false, false),
              instance_field_scene(kind, fields, steps, prefix + "/reference", true, false),
              label + " blur disabled uses requested frame");
      require(actual.status().geometry_conversions == conversions,
              "Uniform blur toggle converted unchanged source geometry");
      fields[0].motion_values.clear();
      fields[0].motion_complete = false;
      compare(instance_field_scene(kind, fields, steps == 3 ? 5 : 3, prefix),
              instance_field_scene(kind, fields, steps, prefix + "/reference", true),
              label + " incomplete local nominal hold");
      require(actual.status().geometry_conversions == conversions, "Local uniform hold converted shared geometry");
    }
  // Exercise the upper count bit through the uploaded GPU descriptor,
  // not only through validation or a wire round-trip.
  std::vector<UniformAttribute> edge = {instance_field("temporal", 1, 129), instance_field("temporal", 1, 129, 1)};
  compare(instance_field_scene(0, edge, 3, "count129"), instance_field_scene(0, edge, 129, "count129/reference", true),
          "129-sample descriptor image");
  auto constant_exposure = instance_field_scene(0, edge, 3, "constant-exposure");
  constant_exposure.camera->sampling.shutter_curve = {.4f, .4f, .4f};
  compare(constant_exposure, instance_field_scene(0, edge, 3, "constant-exposure/reference", true),
          "Explicit positive constant exposure matches empty default");
  std::uint64_t transition_conversions{};
  for (int steps : {3, 5, 129, 0}) {
    auto a = instance_field("temporal", 1, steps ? steps : 3), b = instance_field("temporal", 1, steps ? steps : 3, 1);
    if (!steps) {
      a.motion_values.clear();
      b.motion_values.clear();
    }
    const std::vector<UniformAttribute> transition = {a, b};
    compare(instance_field_scene(0, transition, 3, "descriptor-transition"),
            instance_field_scene(0, transition, 3, "descriptor-transition/reference", true),
            "Retained descriptor sample count " + std::to_string(steps));
    if (steps == 3)
      transition_conversions = actual.status().geometry_conversions;
    else
      require(actual.status().geometry_conversions == transition_conversions,
              "Object sample count change converted prototype geometry");
  }
  for (bool swap : {false, true}) {
    const std::vector<UniformAttribute> rebalance = {instance_field("temporal", 1, swap ? 5 : 3),
                                                     instance_field("temporal", 1, swap ? 3 : 5, 1)};
    compare(instance_field_scene(0, rebalance, 3, "descriptor-rebalance"),
            instance_field_scene(0, rebalance, 3, "descriptor-rebalance/reference", true),
            swap ? "Same-sized uniform storage exchange updates offsets" : "Uniform storage exchange control");
    if (!swap)
      transition_conversions = actual.status().geometry_conversions;
    else
      require(actual.status().geometry_conversions == transition_conversions,
              "Uniform storage exchange converted prototype geometry");
  }
  for (float value : {-0.f, authored}) {
    auto first = instance_field("temporal", 1, 5);
    first.motion_values.assign(5, Float3{value, 0, 0});
    auto second = first;
    std::vector<UniformAttribute> fields = {first, second};
    compare(instance_field_scene(0, fields, 3, "hashed", false, true, true),
            instance_field_scene(0, fields, 3, "hashed/reference", true, true, true, value),
            std::signbit(value) ? "White Noise signed-zero constants" : "White Noise exact-bit constants", .000001f);
  }
  std::vector<UniformAttribute> heights = {instance_field("height", 1, 5), instance_field("height", 1, 5, 1)};
  compare(instance_height_scene(heights, 3, "height-grid"),
          instance_height_scene(heights, 3, "height-grid/reference", true),
          "True height resamples independent 3/5 grids");
  require(actual.status().mesh_variant_resources == 2,
          "Differing temporal true heights did not retain necessary private geometry");
  const auto rebuilds = actual.status().displacement_rebuilds;
  auto unused = instance_height_scene(heights, 3, "height-grid");
  for (auto &owner : unused.objects)
    owner.attributes.push_back(instance_field("unused", 3, 129));
  compare(unused, instance_height_scene(heights, 3, "height-grid/reference", true),
          "Unused temporal field preserves true-height reuse");
  require(actual.status().displacement_rebuilds == rebuilds, "Unused temporal samples invalidated true displacement");
  auto constrained = instance_height_scene(heights, 3, "height-fixed", false, false, 3);
  compare(constrained, instance_height_scene(heights, 3, "height-fixed/reference", true, true),
          "Frozen graph grid holds only true-height field", .00004f, true);
  const auto check_holds = [&]() {
    const auto diagnostics = actual.status().source_diagnostics;
    for (int object = 0; object < 2; ++object)
      require(std::any_of(diagnostics.begin(), diagnostics.end(),
                          [&](const auto &item) {
                            return item.code == "motion.instance_attribute_displacement" &&
                                   item.attribute == "height" &&
                                   item.source == "/obj/packed/" + std::to_string(object) &&
                                   item.action.find("temporal surface") != std::string::npos;
                          }),
              "Displacement hold lost precise object/field recovery diagnostics");
  };
  check_holds();
  SceneUpdate camera_only;
  camera_only.camera = constrained.camera;
  motion_wait(actual, actual.apply(camera_only));
  check_holds();
  heights = {instance_field("height", 1, 3), instance_field("height", 1, 3, 1)};
  compare(instance_height_scene(heights, 3, "height-fixed", false, false, 3),
          instance_height_scene(heights, 3, "height-fixed/reference", true),
          "Aligned graph-grid repair clears field hold");
  heights = {instance_field("height", 1, 5), instance_field("height", 1, 5, 1)};
  compare(instance_height_scene(heights, 1, "height-hold-bit", false, false, 3),
          instance_height_scene(heights, 1, "height-hold-bit/reference", true, true),
          "Unchanged five-sample payload enables bake-only hold", .00004f, true);
  check_holds();
  compare(instance_height_scene(heights, 1, "height-hold-bit", false, false, 5),
          instance_height_scene(heights, 1, "height-hold-bit/reference", true),
          "Unchanged five-sample payload clears bake-only hold");
  heights = {instance_field("height", 1, 129), instance_field("height", 1, 129, 1)};
  compare(instance_height_scene(heights, 127, "height-limit"),
          instance_height_scene(heights, 127, "height-limit/reference", true, true),
          "129-step budget holds only height and preserves geometry motion", .00004f, true);
  check_holds();
  // Strict candidate validation remains transactional even though native source
  // capture recovers missing/invalid off-frame values before this boundary.
  auto fields = std::vector<UniformAttribute>{instance_field("temporal", 3, 3), instance_field("temporal", 3, 3, 1)};
  auto valid = instance_field_scene(0, fields, 5, "strict");
  submit(actual, valid, prior_actual);
  const auto generation = actual.status().generation, owned_bytes = actual.status().owned_geometry_bytes;
  for (int issue = 0; issue < 13; ++issue) {
    SceneUpdate candidate;
    candidate.objects = {valid.objects[0]};
    auto &attribute = candidate.objects[0].attributes[0];
    if (issue < 4)
      attribute.motion_values.assign(std::array<std::size_t, 4>{1, 2, 4, 131}[issue], Float3{});
    if (issue == 4)
      attribute.motion_values[1].x = std::numeric_limits<float>::quiet_NaN();
    if (issue == 5)
      attribute.motion_values[1].z = std::numeric_limits<float>::infinity();
    if (issue == 6)
      attribute.motion_complete = false;
    if (issue == 7)
      attribute.name.clear();
    if (issue == 8)
      attribute.components = 4;
    if (issue == 9)
      candidate.objects[0].attributes.push_back(attribute);
    if (issue == 10)
      attribute.name = std::string("bad\0name", 8);
    if (issue == 11)
      attribute.name = std::string(257, 'x');
    if (issue == 12)
      attribute.value.x = INFINITY;
    bool refused = false;
    try {
      actual.apply(candidate);
    } catch (const SceneUpdateRejected &) {
      refused = true;
    }
    require(refused && actual.status().generation == generation && actual.status().owned_geometry_bytes == owned_bytes,
            "Invalid uniform samples changed accepted state/ownership");
    ++rejected;
  }
  SceneUpdate static_fields;
  static_fields.objects = valid.objects;
  for (auto &owner : static_fields.objects)
    owner.attributes[0].motion_values.clear();
  motion_wait(actual, actual.apply(static_fields));
  require(actual.status().owned_geometry_bytes + 2 * 3 * sizeof(Float3) == owned_bytes,
          "Owned sample buffers were not included in byte accounting");
  actual.close();
  reference.close();
  require(actual.status().owned_geometry_bytes == 0 && actual.status().device_allocation_bytes == 0 &&
              reference.status().owned_geometry_bytes == 0 && reference.status().device_allocation_bytes == 0,
          "Closed uniform sessions retain allocations");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"image_comparisons\":" << images
            << ",\"rejected_inputs\":" << rejected << ",\"maximum_error\":" << maximum_error
            << ",\"closed_owned_bytes\":0,\"closed_device_bytes\":0}" << std::endl;
}
} // namespace cycles_houdini::harness
