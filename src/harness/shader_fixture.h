// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
namespace cycles_houdini::harness {
inline SceneUpdate shader_reference_fixture() {
  SceneUpdate result;
  // Independently constructed uniform engine material: no versioned graph
  // translation, parameters or links from the native test are replayed here.
  MaterialData material;
  material.key = "reference";
  material.kind = MaterialKind::Emission;
  material.color = {.2f, .4f, .8f};
  material.emission_strength = 2;
  result.materials = {material};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "receiver";
  mesh->revision = 1;
  mesh->positions = {{-4, -4, 0}, {4, -4, 0}, {4, 4, 0}, {-4, 4, 0}};
  mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  mesh->materials = {"reference"};
  result.meshes = {mesh};
  ObjectData object;
  object.key = "receiver";
  object.geometry_key = mesh->key;
  result.objects = {object};
  CameraData camera;
  camera.orthographic = true;
  camera.ortho_scale = 4;
  result.camera = camera;
  result.background = Float3{0, 0, 0};
  return result;
}
} // namespace cycles_houdini::harness
