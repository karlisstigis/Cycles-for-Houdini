// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include <iostream>
namespace cycles_houdini::harness {
inline const ImagePlane &displacement_plane(const FrameEvent &frame, const char *name) {
  for (const auto &plane : frame.planes)
    if (plane.pass == name)
      return plane;
  throw std::runtime_error(std::string("Missing displacement test pass: ") + name);
}
inline SceneUpdate displacement_reference_fixture(bool seam = false) {
  SceneUpdate result;
  MaterialData material;
  material.key = "reference";
  material.kind = MaterialKind::Emission;
  material.color = {.2f, .4f, .8f};
  result.materials = {material};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "reference";
  mesh->revision = seam ? 2 : 1;
  mesh->materials = {"reference"};
  if (!seam) {
    mesh->positions = {{-.5f, -.5f, .3f}, {.5f, -.5f, .3f}, {.5f, .5f, .3f}, {-.5f, .5f, .3f}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  } else {
    // Independently positioned faces: no native topology, material palette
    // or displacement branch is replayed in this correctness reference.
    mesh->positions = {{-1, -.5f, .1f}, {0, -.5f, .1f}, {0, .5f, .1f}, {-1, .5f, .1f},
                       {0, -.5f, .5f},  {1, -.5f, .5f}, {1, .5f, .5f}, {0, .5f, .5f}};
    mesh->triangles = {{0, 1, 2}, {0, 2, 3}, {4, 5, 6}, {4, 6, 7}};
  }
  result.meshes = {mesh};
  ObjectData object;
  object.key = "reference";
  object.geometry_key = mesh->key;
  result.objects = {object};
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 2.25f;
  result.camera = camera;
  result.background = Float3{0, 0, 0};
  return result;
}
inline void displacement_test(const std::string &device, const std::string &output) {
  const std::filesystem::path directory(output);
  std::filesystem::create_directories(directory);
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 90;
  config.samples = 16;
  config.interactive = false;
  config.passes = {"combined", "depth", "normal", "uv", "position"};
  RenderSession reference(config);
  for (bool seam : {false, true}) {
    motion_wait(reference, reference.apply(displacement_reference_fixture(seam)));
    reference.write_exr((directory / (seam ? "reference-seams.exr" : "reference-plane.exr")).string());
  }
  reference.close();
  motion_require(reference.status().owned_geometry_bytes == 0, "Displacement reference retained source buffers");
  // A true-displaced resource may exist before an object is bound. Its
  // first usable image must not evaluate the shader with OBJECT_NONE.
  RenderSession session(config);
  auto source = displacement_reference_fixture();
  auto owned = std::make_shared<MeshData>(*source.meshes.front());
  for (auto &p : owned->positions)
    p.z = 0;
  source.meshes = {owned};
  source.objects.clear();
  auto &material = source.materials.front();
  material.kind = MaterialKind::Graph;
  ShaderGraphData graph;
  graph.nodes = {{"emission", "cycles/emission/1", {{"color", std::array<float, 3>{.2f, .4f, .8f}}, {"strength", 1.f}}},
                 {"offset",
                  "cycles/vector_displacement/1",
                  {{"space", std::string("object")}, {"vector", std::array<float, 3>{0, 0, .3f}}}}};
  graph.surface = {"emission", "emission"};
  graph.displacement = ShaderEndpoint{"offset", "displacement"};
  graph.displacement_method = DisplacementMethod::True;
  material.graph = graph;
  motion_wait(session, session.apply(source));
  int checks = 1;
  for (auto &p : owned->positions)
    p.z = 99; // Prove source ownership is independent of the caller.
  SceneUpdate bind;
  ObjectData object;
  object.key = "receiver";
  object.geometry_key = "reference";
  bind.objects = {object};
  motion_wait(session, session.apply(bind));
  auto frame = session.frame();
  const auto &positions = displacement_plane(*frame, "position").pixels;
  const int centre = (45 * 160 + 80) * 3;
  motion_require(std::abs(positions[centre + 2] - .3f) < .001f, "Source ownership or unbound displacement is wrong");
  ++checks;
  const auto baseline = session.status().geometry_conversions;
  for (float z : {.1f, .4f, .2f}) {
    material.graph->nodes.back().parameters["vector"] = std::array<float, 3>{0, 0, z};
    SceneUpdate edit;
    edit.materials = {material};
    motion_wait(session, session.apply(edit));
    motion_require(std::abs(displacement_plane(*session.frame(), "position").pixels[centre + 2] - z) < .001f,
                   "True displacement accumulated offsets");
    ++checks;
    motion_require(session.status().geometry_conversions == baseline, "Material edit reconverted source mesh");
  }
  const auto reject = [&](SceneUpdate invalid) {
    const auto generation = session.status().generation;
    bool rejected = false;
    try {
      session.apply(invalid);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    motion_require(rejected && session.status().generation == generation,
                   "Invalid nominal recovery data escaped precommit validation");
    ++checks;
  };
  for (int failure = 0; failure < 3; ++failure) {
    auto mesh = std::make_shared<MeshData>(*source.meshes.front());
    mesh->revision = 10 + failure;
    mesh->nominal_pose = MeshNominalPose{mesh->positions, mesh->corner_normals, mesh->corner_uv, mesh->corner_colors};
    if (failure == 0)
      mesh->nominal_pose->positions.pop_back();
    else if (failure == 1)
      mesh->nominal_pose->positions.front().x = std::numeric_limits<float>::quiet_NaN();
    else
      mesh->nominal_pose->corner_uv.push_back({0, 0});
    SceneUpdate invalid;
    invalid.meshes = {mesh};
    reject(std::move(invalid));
  }
  {
    SceneUpdate invalid;
    object.nominal_transform = identity_matrix;
    (*object.nominal_transform)[3] = std::numeric_limits<float>::quiet_NaN();
    invalid.objects = {object};
    reject(std::move(invalid));
  }
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
                 "Displacement close retained resources");
  ++checks;
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
