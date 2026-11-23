// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"

namespace cycles_houdini::harness {
// Authored from the fixture specifications, without a host adapter, exported
// plugin scene or Houdini geometry. Keep these references independent.
inline SceneUpdate point_curve_fixture(bool hair, float aspect) {
  SceneUpdate scene;
  scene.background = Float3{};
  scene.camera = CameraData{};
  scene.camera->orthographic = true;
  scene.camera->ortho_scale = 4.0f / aspect;
  MaterialData material;
  material.key = "reference/emission";
  material.kind = MaterialKind::Emission;
  material.color = {1, 1, 1};
  material.color_attribute = "Cd";
  scene.materials.push_back(material);
  std::string key;
  if (hair) {
    auto curves = std::make_shared<CurveData>();
    curves->key = "reference/linear_hair";
    curves->material = material.key;
    curves->revision = 1;
    curves->positions = {{-0.8f, -0.6f, 0}, {-0.8f, 0.6f, 0}, {0.8f, -0.6f, 0}, {0.8f, 0.6f, 0}};
    curves->radii = {0.1f, 0.1f, 0.15f, 0.15f};
    curves->first_key = {0, 2};
    curves->colors = {{0.8f, 0.1f, 0.02f}, {0.8f, 0.1f, 0.02f}, {0.02f, 0.1f, 0.8f}, {0.02f, 0.1f, 0.8f}};
    curves->uv = {{0.2f, 0.3f}, {0.6f, 0.7f}};
    key = curves->key;
    scene.curves.push_back(curves);
  } else {
    auto points = std::make_shared<PointData>();
    points->key = "reference/particles";
    points->material = material.key;
    points->revision = 1;
    points->positions = {{-0.8f, 0, 0}, {0.8f, 0, 0}};
    points->radii = {0.2f, 0.3f};
    points->colors = {{0.8f, 0.1f, 0.02f}, {0.02f, 0.1f, 0.8f}};
    points->uv = {{0.2f, 0.3f}, {0.6f, 0.7f}};
    key = points->key;
    scene.points.push_back(points);
  }
  scene.objects.push_back({key, key});
  return scene;
}
} // namespace cycles_houdini::harness
