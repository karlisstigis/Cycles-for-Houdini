// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/session.h"

namespace cycles_houdini {
SceneUpdate calibration_fixture() {
  SceneUpdate update;
  update.camera = CameraData{};
  update.background = Float3{0.05f, 0.05f, 0.05f};
  update.materials.push_back({"calibration/red", MaterialKind::Diffuse, {0.8f, 0.05f, 0.025f}, 0});
  update.materials.push_back({"calibration/grey", MaterialKind::Diffuse, {0.18f, 0.18f, 0.18f}, 0});
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "calibration/triangle";
  mesh->revision = 1;
  mesh->positions = {{-1, -0.75f, 0}, {1, -0.75f, 0}, {0, 0.9f, 0}};
  mesh->triangles = {{0, 1, 2}};
  mesh->materials = {"calibration/red"};
  mesh->corner_uv = {{0, 0}, {1, 0}, {0.5f, 1}};
  update.meshes.push_back(mesh);
  update.objects.push_back({"calibration/triangle", mesh->key});
  LightData light;
  light.key = "calibration/key";
  light.transform[3] = -2;
  light.transform[7] = 3;
  light.transform[11] = 4;
  light.strength = {500, 500, 500};
  update.lights.push_back(light);
  return update;
}
const char *state_name(SessionState state) {
  switch (state) {
  case SessionState::Idle:
    return "idle";
  case SessionState::Rendering:
    return "rendering";
  case SessionState::Paused:
    return "paused";
  case SessionState::Complete:
    return "complete";
  case SessionState::Cancelled:
    return "cancelled";
  case SessionState::Failed:
    return "failed";
  case SessionState::Closed:
    return "closed";
  }
  return "invalid";
}
} // namespace cycles_houdini
