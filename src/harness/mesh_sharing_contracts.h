// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/mesh_sharing.h"
#include "motion_fixture.h"
#include <cstdlib>
#include <iostream>

namespace cycles_houdini::harness {
inline void mesh_sharing_control(bool disabled) {
#ifdef _WIN32
  _putenv_s("CYCLES_HOUDINI_DISABLE_MESH_SHARING", disabled ? "1" : "");
#else
  if (disabled)
    setenv("CYCLES_HOUDINI_DISABLE_MESH_SHARING", "1", 1);
  else
    unsetenv("CYCLES_HOUDINI_DISABLE_MESH_SHARING");
#endif
}
inline void mesh_sharing_contracts(const std::string &device) {
  int checks = 0, images = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  {
    MeshSharingFieldOwners mesh, uniform, unrelated;
    mesh.fields = {"rand"};
    uniform.uniform_only = {"rand"};
    unrelated.fields = {"other"};
    require(!mesh.compatible(uniform) && !uniform.compatible(mesh),
            "Uniform and prototype owners of one name shared storage");
    require(unrelated.compatible(uniform), "Unrelated uniform field prevented sharing");
    unrelated.include(mesh);
    require(!unrelated.compatible(uniform), "Aggregate ownership lost an earlier member");
    mesh = {};
    uniform = {};
    mesh.corner_color = true;
    uniform.uniform_color_only = true;
    require(!mesh.compatible(uniform) && !uniform.compatible(mesh), "Prototype color replaced an instance color");
    mesh = {};
    mesh.fields = {"Cd"};
    uniform.fields = {"Cd"};
    require(mesh.compatible(uniform), "Generic authored Cd was mistaken for corner colors");
    mesh.corner_color = true;
    require(!mesh.compatible(uniform), "Corner color ownership was hidden by a generic Cd field");
  }
  MeshData a;
  a.positions = {{-.5f, -.5f, 0}, {.5f, -.5f, 0}, {.5f, .5f, 0}, {-.5f, .5f, 0}};
  a.triangles = {{0, 1, 2}, {0, 2, 3}};
  a.triangle_material = {0, 1};
  a.materials = {"red", "red"};
  auto b = a;
  b.key = "other";
  b.materials = {"blue", "green"};
  require(same_mesh_shape(a, b) && mesh_shape_hash(a) == mesh_shape_hash(b), "Surface identity split an exact shape");
  b.triangle_material = {0, 0};
  require(!same_mesh_shape(a, b), "Different face slot maps shared geometry");
  a.triangle_material.clear();
  b.triangle_material = {0, 0};
  require(same_mesh_shape(a, b) && mesh_shape_hash(a) == mesh_shape_hash(b),
          "Implicit zero slots differed from explicit zero slots");
  b = a;
  b.positions[0].z = -0.f;
  require(!same_mesh_shape(a, b), "Signed shape bits were lost");
  b = a;
  b.corner_uv.assign(6, {.2f, .3f});
  require(compatible_mesh_shading_fields(a, b) && adds_mesh_shading_fields(a, b),
          "Independent UV field could not be retained");
  auto merged = a;
  merge_mesh_shading_fields(merged, b);
  require(merged.corner_uv == b.corner_uv && a.corner_uv.empty(), "Field union mutated a logical source");
  auto animated = b;
  animated.motion_corner_uv = {b.corner_uv};
  require(!compatible_mesh_shading_fields(animated, b), "Animated UVs replaced a static field");
  auto invalid = merged;
  invalid.corner_uv[2].x = .8f;
  require(!compatible_mesh_shading_fields(merged, invalid), "Conflicting UV values shared storage");
  bool refused = false;
  try {
    merge_mesh_shading_fields(merged, invalid);
  } catch (const std::logic_error &) {
    refused = true;
  }
  require(refused && mesh_payload_equal(merged.corner_uv, b.corner_uv),
          "Failed field union partially mutated accepted values");
  b = a;
  b.attributes = {{"field", 1, std::vector<Float3>(6, {.2f, 0, 0})}};
  merged = a;
  merge_mesh_shading_fields(merged, b);
  require(merged.attributes == b.attributes && a.attributes.empty(), "Named field union lost ownership");
  b.attributes[0].values[0].x = -0.f;
  merged.attributes[0].values[0].x = 0.f;
  require(!compatible_mesh_shading_fields(merged, b), "Signed scalar data was merged numerically");
  for (int mutation = 0; mutation < 7; ++mutation) {
    b = a;
    if (mutation == 0)
      b.positions[0].x += .1f;
    if (mutation == 1)
      b.triangles[0].b = 3;
    if (mutation == 2)
      b.smooth = true;
    if (mutation == 3)
      b.motion.positions = {b.positions};
    if (mutation == 4)
      b.motion_corner_normals = {std::vector<Float3>(6, {0, 0, 1})};
    if (mutation == 5)
      b.motion_blur = false;
    if (mutation == 6)
      b.nominal_pose = MeshNominalPose{b.positions};
    require(!same_mesh_shape(a, b), "A shape, normal or shutter edit shared incompatible storage");
  }
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 96;
  config.samples = 8;
  config.threads = 2;
  config.interactive = false;
  config.persistent_updates = true;
  config.use_texture_cache = false;
  config.passes = {"combined", "depth", "normal", "position", "material_id"};
  config.cryptomatte = {true, true, true, 2};
  SceneUpdate scene;
  scene.background = Float3{0, 0, 0};
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 2;
  scene.camera = camera;
  for (const auto &[key, color] : std::vector<std::pair<std::string, Float3>>{
           {"red", {.8f, .1f, .2f}}, {"green", {.1f, .7f, .3f}}, {"blue", {.2f, .3f, .8f}}}) {
    MaterialData material;
    material.key = key;
    material.kind = MaterialKind::Emission;
    material.color = color;
    material.pass_index = int(scene.materials.size() + 1);
    scene.materials.push_back(material);
  }
  a.triangle_material = {0, 1};
  std::vector<std::shared_ptr<MeshData>> sources;
  for (int index = 0; index < 3; ++index) {
    auto mesh = std::make_shared<MeshData>(a);
    mesh->key = "mesh/" + std::to_string(index);
    mesh->revision = 1;
    if (index == 1)
      mesh->materials = {"green", "blue"};
    if (index == 2)
      mesh->materials = {"blue", "green"};
    sources.push_back(mesh);
    ObjectData object;
    object.key = "object/" + std::to_string(index);
    object.geometry_key = mesh->key;
    object.transform[3] = float(index - 1) * 1.1f;
    scene.objects.push_back(object);
  }
  const auto refresh = [&]() {
    scene.meshes.clear();
    for (const auto &source : sources)
      scene.meshes.push_back(source);
  };
  refresh();
  RenderSession session(config);
  const auto compare = [&](const char *label, std::size_t physical, bool diagnostic = false, bool pass_only = false) {
    refresh();
    SceneUpdate pass_edit;
    pass_edit.passes = scene.passes;
    const auto frame = motion_wait(session, session.apply(pass_only ? pass_edit : scene));
    const auto status = session.status();
    require(status.mesh_variant_resources == physical,
            std::string(label) + ": wrong physical mesh count " + std::to_string(status.mesh_variant_resources));
    require(status.source_diagnostics.empty() != diagnostic,
            std::string(label) + ": wrong binding recovery diagnostics");
    mesh_sharing_control(true);
    config.persistent_updates = false;
    std::shared_ptr<const FrameEvent> expected;
    try {
      RenderSession reference(config);
      expected = motion_wait(reference, reference.apply(scene));
      require(reference.status().mesh_variant_resources == sources.size(),
              std::string(label) + ": independent reference shared a mesh");
      reference.close();
      require(reference.status().device_allocation_bytes == 0 && reference.status().owned_geometry_bytes == 0,
              "Independent reference retained allocations");
    } catch (...) {
      mesh_sharing_control(false);
      throw;
    }
    mesh_sharing_control(false);
    require(frame->planes.size() == expected->planes.size(), std::string(label) + ": reference pass count changed");
    for (const auto &plane : frame->planes) {
      const auto found = std::find_if(expected->planes.begin(), expected->planes.end(),
                                      [&](const auto &value) { return plane.pass == value.pass; });
      require(found != expected->planes.end() && plane.pixels.size() == found->pixels.size(),
              "Reference plane is missing");
      float worst = 0;
      for (std::size_t i = 0; i < plane.pixels.size(); ++i) {
        const float x = plane.pixels[i], y = found->pixels[i];
        if (std::bit_cast<std::uint32_t>(x) == std::bit_cast<std::uint32_t>(y))
          continue;
        if (!std::isfinite(x) || !std::isfinite(y)) {
          worst = INFINITY;
          break;
        }
        worst = std::max(worst, std::abs(x - y));
      }
      require(worst < .0001f, std::string(label) + ": shared " + plane.pass +
                                  " differs from independent meshes: " + std::to_string(worst));
    }
    ++images;
    std::cout << "{\"check\":\"" << label << "\",\"physical_meshes\":" << physical << "}" << std::endl;
  };
  compare("Duplicate shader IDs in separate face slots", 1);
  {
    const auto frame = session.frame();
    const auto &pixels = frame->planes.front().pixels;
    require(std::count_if(pixels.begin(), pixels.end(), [](float value) { return value > .05f && value < .9f; }) > 100,
            "The material-slot reference fixture has no visible colored surface");
  }
  scene.materials[1].color = {.2f, .6f, .4f};
  compare("Live surface value edit", 1);
  sources[1]->positions[0].z = .2f;
  ++sources[1]->revision;
  compare("Shape splits from shared storage", 2);
  // Keep the live mesh buffers and transforms fixed. A geometry rebind
  // must update the instance acceleration structure, including on undo.
  std::swap(scene.objects[0].geometry_key, scene.objects[1].geometry_key);
  compare("Existing objects exchange two live geometry bindings", 2);
  std::swap(scene.objects[0].geometry_key, scene.objects[1].geometry_key);
  compare("Restore the original geometry bindings", 2);
  sources[1]->positions = a.positions;
  ++sources[1]->revision;
  compare("Shape rejoins shared storage", 1);
  sources[1]->corner_uv.assign(6, {.2f, .3f});
  ++sources[1]->revision;
  compare("Independent UV union", 1);
  scene.passes = config.passes;
  scene.passes->push_back("uv");
  compare("UV pass preserves each binding's missing UVs", 2, false, true);
  scene.passes = config.passes;
  compare("Disabling UV output permits field-union sharing", 1, false, true);
  sources[0]->corner_uv.assign(6, {.5f, .3f});
  ++sources[0]->revision;
  compare("Conflicting UVs split storage", 2);
  sources[0]->corner_uv = sources[1]->corner_uv;
  ++sources[0]->revision;
  compare("UV repair rejoins storage", 1);
  sources[2]->corner_uv = sources[1]->corner_uv;
  ++sources[2]->revision;
  compare("All bindings retain one identical UV map", 1);
  scene.passes = config.passes;
  scene.passes->push_back("uv");
  compare("Pass-only UV restore on a shared mesh", 1, false, true);
  scene.passes = config.passes;
  compare("Pass-only UV disable retains shared storage", 1, false, true);
  sources[2]->corner_uv.clear();
  ++sources[2]->revision;
  compare("Restore optional UV field union", 1);
  scene.materials[0].kind = MaterialKind::Graph;
  ShaderGraphData attribute_graph;
  attribute_graph.nodes = {{"surface", "cycles/emission/1", {{"color", std::array<float, 3>{.8f, .1f, .2f}}}},
                           {"field", "cycles/attribute/1", {{"attribute", std::string("rand")}}}};
  attribute_graph.links = {{{"field", "fac"}, {"surface", "strength"}}};
  attribute_graph.surface = {"surface", "emission"};
  scene.materials[0].graph = attribute_graph;
  sources[0]->attributes = {{"rand", 1, std::vector<Float3>(6, {.5f, 0, 0})}};
  ++sources[0]->revision;
  sources[1]->materials = {"red", "red"};
  ++sources[1]->revision;
  compare("Missing field retains local fallback", 1, true);
  sources[1]->attributes = {{"rand", 1, std::vector<Float3>(6, {.25f, 0, 0})}};
  ++sources[1]->revision;
  compare("Repaired distinct field splits storage", 2);
  sources[1]->attributes = sources[0]->attributes;
  ++sources[1]->revision;
  compare("Equal repaired fields share storage", 1);
  sources[1]->attributes.clear();
  ++sources[1]->revision;
  compare("Broken field falls back without hiding geometry", 1, true);
  sources[1]->attributes = sources[0]->attributes;
  ++sources[1]->revision;
  compare("Field repair while rendering", 1);
  // Change only one material's geometry: its surface identity remains independent.
  scene.materials[0].color_attribute.clear();
  scene.materials[0].kind = MaterialKind::Graph;
  ShaderGraphData graph;
  graph.nodes = {{"surface", "cycles/emission/1", {{"color", std::array<float, 3>{.8f, .1f, .2f}}}},
                 {"height",
                  "cycles/vector_displacement/1",
                  {{"vector", std::array<float, 3>{0, 0, .2f}}, {"space", std::string("object")}}}};
  graph.surface = {"surface", "emission"};
  graph.displacement = ShaderEndpoint{"height", "displacement"};
  graph.displacement_method = DisplacementMethod::True;
  scene.materials[0].graph = graph;
  compare("True displacement creates private geometry", 3);
  scene.materials[0].kind = MaterialKind::Emission;
  scene.materials[0].graph.reset();
  compare("Displacement removal rejoins storage", 1);
  auto old = sources[0]->key;
  sources[0] = std::make_shared<MeshData>(*sources[0]);
  sources[0]->key = "mesh/rebound";
  ++sources[0]->revision;
  scene.objects[0].geometry_key = sources[0]->key;
  scene.deleted_geometry = {old};
  compare("Shared source ownership handoff", 1);
  scene.deleted_geometry.clear();
  for (auto &source : sources) {
    source->triangles.clear();
    source->triangle_material.clear();
    source->corner_uv.clear();
    source->attributes.clear();
    auto &sub = source->subdivision.emplace();
    sub.settings.type = SubdivisionType::Linear;
    sub.settings.adaptive = false;
    sub.settings.max_level = 2;
    sub.face_sizes = {3, 3};
    sub.face_corners = {0, 1, 2, 0, 2, 3};
    sub.face_material = {0, 1};
    ++source->revision;
  }
  compare("Fixed render subdivision shares one mesh", 1);
  scene.objects.erase(scene.objects.begin());
  scene.deleted_objects = {"object/0"};
  scene.deleted_geometry = {sources.front()->key};
  sources.erase(sources.begin());
  compare("Primary owner deletion preserves siblings", 1);
  scene.deleted_objects.clear();
  scene.deleted_geometry.clear();
  for (int repeat = 0; repeat < 4; ++repeat) {
    sources[0]->positions[0].z = .1f;
    ++sources[0]->revision;
    compare("Repeated split", 2);
    sources[0]->positions = a.positions;
    ++sources[0]->revision;
    compare("Repeated repair", 1);
  }
  session.close();
  require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
          "Shared mesh close retained owned geometry or device buffers");
  // Surface-only palette expansion changes the face-slot stream while all
  // effective height functions remain identical. Exercise the real family
  // reconciliation path against fresh images, for triangles and diced faces.
  for (const bool subdivision : {false, true}) {
    SceneUpdate owned;
    owned.background = Float3{};
    owned.camera = camera;
    MaterialData first;
    first.key = "retained/first";
    first.kind = MaterialKind::Graph;
    first.pass_index = 1;
    ShaderGraphData height_graph;
    height_graph.nodes = {{"surface", "cycles/emission/1", {{"color", std::array<float, 3>{.6f, .2f, .1f}}}},
                          {"height",
                           "cycles/vector_displacement/1",
                           {{"vector", std::array<float, 3>{0, 0, .15f}}, {"space", std::string("object")}}}};
    height_graph.surface = {"surface", "emission"};
    height_graph.displacement = ShaderEndpoint{"height", "displacement"};
    height_graph.displacement_method = DisplacementMethod::True;
    first.graph = height_graph;
    owned.materials = {first};
    auto body = std::make_shared<MeshData>();
    body->key = "retained/body";
    body->revision = 1;
    body->positions = {{-.5f, -.5f, 0}, {.5f, -.5f, 0}, {.5f, .5f, 0}, {-.5f, .5f, 0}};
    body->materials = {first.key};
    body->triangles = {{0, 1, 2}, {0, 2, 3}};
    if (subdivision) {
      body->triangles.clear();
      auto &sub = body->subdivision.emplace();
      sub.settings.type = SubdivisionType::Linear;
      sub.settings.adaptive = false;
      sub.settings.space = SubdivisionSpace::Object;
      sub.settings.max_level = 2;
      sub.face_sizes = {3, 3};
      sub.face_corners = {0, 1, 2, 0, 2, 3};
    }
    owned.meshes = {body};
    ObjectData owner;
    owner.key = "retained/object";
    owner.geometry_key = body->key;
    owned.objects = {owner};
    auto retained_config = config;
    retained_config.persistent_updates = true;
    RenderSession retained(retained_config);
    const auto compare_private = [&](const char *label, int rebuild) {
      const auto before = retained.status();
      const auto actual = motion_wait(retained, retained.apply(owned));
      const auto after = retained.status();
      if (rebuild >= 0) {
        require((after.displacement_rebuilds > before.displacement_rebuilds) == bool(rebuild),
                std::string(label) + ": private displacement rebuild decision changed");
        if (!rebuild)
          require(after.subdivision_tessellations == before.subdivision_tessellations,
                  std::string(label) + ": a surface-only palette edit repeated dicing");
      }
      require(after.mesh_variant_resources == 1 && after.source_diagnostics.empty(),
              std::string(label) + ": private geometry or diagnostics changed");
      RenderSession independent(config);
      const auto expected = motion_wait(independent, independent.apply(owned));
      require(actual->planes.size() == expected->planes.size(), std::string(label) + ": fresh reference lost a pass");
      for (const auto &plane : actual->planes) {
        const auto found = std::find_if(expected->planes.begin(), expected->planes.end(),
                                        [&](const auto &p) { return p.pass == plane.pass; });
        require(found != expected->planes.end() && found->pixels.size() == plane.pixels.size(),
                std::string(label) + ": fresh reference pass layout differs");
        float worst = 0;
        for (std::size_t n = 0; n < plane.pixels.size(); ++n) {
          const auto x = plane.pixels[n], y = found->pixels[n];
          if (std::bit_cast<std::uint32_t>(x) == std::bit_cast<std::uint32_t>(y))
            continue;
          if (!std::isfinite(x) || !std::isfinite(y)) {
            worst = INFINITY;
            break;
          }
          worst = std::max(worst, std::abs(x - y));
        }
        require(worst < .0001f, std::string(label) + ": retained " + plane.pass +
                                    " differs from a fresh displaced body: " + std::to_string(worst));
      }
      independent.close();
      require(independent.status().device_allocation_bytes == 0 && independent.status().owned_geometry_bytes == 0,
              "Private displacement reference retained allocations");
      ++images;
      std::cout << "{\"check\":\"" << label << "\",\"subdivision\":" << subdivision
                << ",\"displacement_rebuilds\":" << after.displacement_rebuilds << "}" << std::endl;
    };
    const auto slots = [&](std::vector<int> values) {
      if (subdivision)
        body->subdivision->face_material = std::move(values);
      else
        body->triangle_material = std::move(values);
      ++body->revision;
    };
    compare_private("Private displaced body before palette expansion", -1);
    auto second = first;
    second.key = "retained/second";
    second.pass_index = 2;
    second.graph->nodes[0].parameters["color"] = std::array<float, 3>{.1f, .3f, .8f};
    owned.materials.push_back(second);
    body->materials.push_back(second.key);
    slots({0, 1});
    compare_private("Equal-height surface palette expansion", 0);
    slots({1, 0});
    compare_private("Equal-height surface palette swap", 0);
    body->materials = {first.key};
    slots({});
    compare_private("Equal-height surface palette contraction", 0);
    owned.materials[0].graph->nodes[1].parameters["vector"] = std::array<float, 3>{0, 0, .25f};
    compare_private("Real height edit invalidates a private body", 1);
    body->corner_normals.assign(6, {0, .3f, .95f});
    ++body->revision;
    compare_private("Authored normal edit invalidates a private body", 1);
    retained.close();
    require(retained.status().owned_geometry_bytes == 0 && retained.status().device_allocation_bytes == 0,
            "Retained private displacement close leaked allocations");
  }
  // Preserve exact retired bodies even when a palette permutation moves
  // owner groups through bindings that survive. Each donor transfers once;
  // changed fields, true displacement and adaptive dicing remain real work.
  for (int mutation = 0; mutation < 5; ++mutation) {
    SceneUpdate initial;
    initial.background = Float3{};
    initial.camera = camera;
    initial.rendering = RenderSettings{};
    initial.rendering->samples = 8;
    initial.rendering->light_tree = false;
    initial.rendering->adaptive_sampling = false;
    std::vector<std::shared_ptr<MeshData>> bodies;
    for (int index = 0; index < 4; ++index) {
      MaterialData m;
      m.key = "permutation/material/" + std::to_string(index);
      m.kind = MaterialKind::Emission;
      m.color = {.1f * float(index + 1), .2f, .5f};
      initial.materials.push_back(m);
      auto mesh = std::make_shared<MeshData>();
      mesh->key = "permutation/mesh/" + std::to_string(index);
      mesh->revision = 1;
      mesh->positions = {{-.2f, -.35f, 0}, {.2f, -.35f, 0}, {.2f, .35f, 0}, {-.2f, .35f, 0}};
      mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
      mesh->materials = {m.key};
      bodies.push_back(mesh);
      initial.meshes.push_back(mesh);
      ObjectData o;
      o.key = "permutation/owner/" + std::to_string(index);
      o.geometry_key = mesh->key;
      o.transform[3] = float(index) * .5f - .75f;
      initial.objects.push_back(o);
    }
    const auto displaced = [](MaterialData &material) {
      material.kind = MaterialKind::Graph;
      ShaderGraphData g;
      g.nodes = {{"surface", "cycles/emission/1", {{"color", std::array<float, 3>{.6f, .2f, .4f}}}},
                 {"height",
                  "cycles/vector_displacement/1",
                  {{"vector", std::array<float, 3>{0, 0, .1f}}, {"space", std::string("object")}}}};
      g.surface = {"surface", "emission"};
      g.displacement = ShaderEndpoint{"height", "displacement"};
      g.displacement_method = DisplacementMethod::True;
      material.graph = g;
    };
    if (mutation == 4)
      displaced(initial.materials[1]);
    RenderSession live(config);
    motion_wait(live, live.apply(initial));
    const auto conversions = live.status().geometry_conversions;
    SceneUpdate edit;
    edit.deleted_geometry = {bodies[1]->key, bodies[3]->key};
    edit.objects = initial.objects;
    auto first = std::make_shared<MeshData>(*bodies[0]), second = std::make_shared<MeshData>(*bodies[2]);
    first->key = "permutation/new/first";
    second->key = "permutation/new/second";
    ++first->revision;
    ++second->revision;
    MaterialData material = initial.materials[0];
    material.key = "permutation/new/material";
    material.color = {.7f, .4f, .2f};
    first->materials = {material.key};
    edit.materials = {material};
    edit.meshes = {first, second};
    edit.objects[0].geometry_key = first->key;
    edit.objects[1].geometry_key = bodies[2]->key;
    edit.objects[2].geometry_key = second->key;
    edit.objects[3].geometry_key = bodies[0]->key;
    if (mutation == 1)
      first->attributes = {{"changed", 1, std::vector<Float3>(6, {.25f, 0, 0})}};
    if (mutation == 2)
      displaced(edit.materials.front());
    if (mutation == 3) {
      first->triangles.clear();
      auto &s = first->subdivision.emplace();
      s.settings.type = SubdivisionType::Linear;
      s.settings.adaptive = true;
      s.settings.max_level = 2;
      s.settings.dicing_rate = 4;
      s.face_sizes = {4};
      s.face_corners = {0, 1, 2, 3};
    }
    const auto actual = motion_wait(live, live.apply(edit));
    require(live.status().geometry_conversions == conversions + std::size_t(mutation != 0),
            "Palette permutation skipped required work or reconverted an exact surface body");
    require(live.status().source_diagnostics.empty(), "Palette permutation introduced a source problem");
    const auto owned = live.retained_mesh_sources();
    require(owned.size() == 4 && owned.at(first->key).get() != first.get() &&
                owned.at(second->key).get() != second.get(),
            "Cross-group handoff borrowed mutable sender storage");
    SceneUpdate expected = initial;
    expected.meshes = {bodies[0], bodies[2], first, second};
    expected.objects = edit.objects;
    expected.materials.push_back(edit.materials.front());
    mesh_sharing_control(true);
    std::shared_ptr<const FrameEvent> reference;
    try {
      RenderSession independent(config);
      reference = motion_wait(independent, independent.apply(expected));
      independent.close();
      require(independent.status().owned_geometry_bytes == 0 && independent.status().device_allocation_bytes == 0,
              "Permutation reference retained allocations");
    } catch (...) {
      mesh_sharing_control(false);
      throw;
    }
    mesh_sharing_control(false);
    for (const auto &plane : actual->planes) {
      const auto found = std::find_if(reference->planes.begin(), reference->planes.end(),
                                      [&](const auto &p) { return p.pass == plane.pass; });
      require(found != reference->planes.end() && found->pixels.size() == plane.pixels.size(),
              "Permutation reference pass shape changed");
      float worst = 0;
      for (std::size_t n = 0; n < plane.pixels.size(); ++n) {
        const auto x = plane.pixels[n], y = found->pixels[n];
        if (std::bit_cast<std::uint32_t>(x) == std::bit_cast<std::uint32_t>(y))
          continue;
        if (!std::isfinite(x) || !std::isfinite(y)) {
          worst = INFINITY;
          break;
        }
        worst = std::max(worst, std::abs(x - y));
      }
      require(worst < .0001f, "Palette permutation image differs from independent meshes");
    }
    ++images;
    const float original = owned.at(first->key)->positions[0].x;
    first->positions[0].x += .03f;
    require(owned.at(first->key)->positions[0].x == original, "Caller mutation changed a cross-group source owner");
    live.close();
    require(live.status().owned_geometry_bytes == 0 && live.status().device_allocation_bytes == 0,
            "Permutation close retained allocations");
  }
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"reference_images\":" << images << "}" << std::endl;
}
} // namespace cycles_houdini::harness
