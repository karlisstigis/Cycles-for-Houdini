// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "displacement_fixture.h"
#include <map>
#include <set>

namespace cycles_houdini::harness {
inline std::shared_ptr<MeshData> subdivision_cube() {
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "reference";
  mesh->revision = 1;
  mesh->materials = {"reference"};
  mesh->positions = {{-.7f, -.7f, -.7f}, {.7f, -.7f, -.7f}, {.7f, .7f, -.7f}, {-.7f, .7f, -.7f},
                     {-.7f, -.7f, .7f},  {.7f, -.7f, .7f},  {.7f, .7f, .7f},  {-.7f, .7f, .7f}};
  mesh->subdivision.emplace();
  auto &s = *mesh->subdivision;
  s.settings.type = SubdivisionType::CatmullClark;
  s.settings.space = SubdivisionSpace::Object;
  s.settings.dicing_rate = .025f;
  s.settings.max_level = 7;
  s.face_sizes = {4, 4, 4, 4, 4, 4};
  s.face_corners = {0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7};
  return mesh;
}
// Independent uniform Catmull-Clark equations, used only to construct an
// already-positioned triangle reference. No Cycles subdivision is involved.
inline std::shared_ptr<MeshData> subdivision_cube_reference() {
  auto cube = subdivision_cube();
  auto positions = cube->positions;
  std::vector<std::vector<int>> faces;
  for (int f = 0; f < 6; ++f)
    faces.emplace_back(cube->subdivision->face_corners.begin() + f * 4,
                       cube->subdivision->face_corners.begin() + f * 4 + 4);
  const auto add = [](Float3 a, Float3 b) { return Float3{a.x + b.x, a.y + b.y, a.z + b.z}; };
  const auto mul = [](Float3 a, float s) { return Float3{a.x * s, a.y * s, a.z * s}; };
  for (int level = 0; level < 5; ++level) {
    std::vector<Float3> centres;
    std::map<std::pair<int, int>, std::vector<int>> edge_faces;
    std::vector<std::vector<int>> incident(positions.size());
    for (std::size_t f = 0; f < faces.size(); ++f) {
      Float3 centre{};
      for (std::size_t c = 0; c < faces[f].size(); ++c) {
        const int a = faces[f][c], b = faces[f][(c + 1) % faces[f].size()];
        centre = add(centre, positions[a]);
        incident[a].push_back(int(f));
        const auto pair = std::minmax(a, b);
        edge_faces[{pair.first, pair.second}].push_back(int(f));
      }
      centres.push_back(mul(centre, 1.f / faces[f].size()));
    }
    std::vector<Float3> result(positions.size());
    std::vector<std::vector<Float3>> midpoints(positions.size());
    for (const auto &[edge, neighbors] : edge_faces) {
      motion_require(neighbors.size() == 2, "Independent cube reference must be closed");
      const auto midpoint = mul(add(positions[edge.first], positions[edge.second]), .5f);
      midpoints[edge.first].push_back(midpoint);
      midpoints[edge.second].push_back(midpoint);
    }
    for (std::size_t v = 0; v < positions.size(); ++v) {
      const float n = float(incident[v].size());
      Float3 f{}, r{};
      for (int face : incident[v])
        f = add(f, centres[face]);
      for (const auto &midpoint : midpoints[v])
        r = add(r, midpoint);
      result[v] = mul(add(add(mul(f, 1 / n), mul(r, 2 / n)), mul(positions[v], n - 3)), 1 / n);
    }
    std::map<std::pair<int, int>, int> edge_index;
    for (const auto &[edge, neighbors] : edge_faces) {
      edge_index[edge] = int(result.size());
      result.push_back(mul(
          add(add(positions[edge.first], positions[edge.second]), add(centres[neighbors[0]], centres[neighbors[1]])),
          .25f));
    }
    std::vector<int> face_index;
    for (const auto &centre : centres) {
      face_index.push_back(int(result.size()));
      result.push_back(centre);
    }
    std::vector<std::vector<int>> refined;
    for (std::size_t f = 0; f < faces.size(); ++f)
      for (std::size_t c = 0; c < faces[f].size(); ++c) {
        const auto &face = faces[f];
        const int v = face[c], next = face[(c + 1) % face.size()], previous = face[(c + face.size() - 1) % face.size()];
        const auto a = std::minmax(v, next), b = std::minmax(v, previous);
        refined.push_back({v, edge_index.at({a.first, a.second}), face_index[f], edge_index.at({b.first, b.second})});
      }
    positions = std::move(result);
    faces = std::move(refined);
  }
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "reference";
  mesh->revision = 2;
  mesh->materials = {"reference"};
  mesh->positions = std::move(positions);
  mesh->smooth = true;
  for (const auto &face : faces) {
    mesh->triangles.push_back({face[0], face[1], face[2]});
    mesh->triangles.push_back({face[0], face[2], face[3]});
  }
  return mesh;
}
inline SceneUpdate subdivision_scene(const std::shared_ptr<MeshData> &mesh) {
  auto scene = displacement_reference_fixture();
  scene.meshes = {mesh};
  return scene;
}
inline void camera_tessellation_supersession(const std::string &device) {
  SessionConfig config;
  config.device_id = device;
  config.width = config.height = 320;
  config.samples = 2;
  config.threads = 2;
  config.persistent_updates = true;
  config.collect_diagnostics = true;
  RenderSession session(config);

  auto scene = displacement_reference_fixture();
  scene.materials.front().kind = MaterialKind::Diffuse;
  scene.background = Float3{.25f, .25f, .25f};
  scene.camera->ortho_scale = 16;
  scene.dicing.emplace().offscreen_scale = 1;
  auto mesh = std::make_shared<MeshData>(*scene.meshes.front());
  mesh->positions.clear();
  mesh->triangles.clear();
  auto &subdivision = mesh->subdivision.emplace();
  subdivision.settings.type = SubdivisionType::CatmullClark;
  subdivision.settings.space = SubdivisionSpace::Pixel;
  subdivision.settings.dicing_rate = .16f;
  subdivision.settings.max_level = 4;
  subdivision.settings.triangle_limit = 12000000;
  constexpr int side = 65;
  for (int y = 0; y < side; ++y)
    for (int x = 0; x < side; ++x)
      mesh->positions.push_back({x / 8.f - 4, y / 8.f - 4, .1f * ((x + y) % 3)});
  for (int y = 0; y < side - 1; ++y)
    for (int x = 0; x < side - 1; ++x) {
      const auto corner = y * side + x;
      subdivision.face_sizes.push_back(4);
      const std::array corners = {corner, corner + 1, corner + side + 1, corner + side};
      subdivision.face_corners.insert(subdivision.face_corners.end(), corners.begin(), corners.end());
    }
  scene.meshes = {mesh};
  motion_wait(session, session.apply(scene));
  const auto camera_triangles = session.status().subdivision_triangles;

  auto object_space = subdivision_cube();
  object_space->key = "object-space";
  object_space->subdivision->settings.max_level = 3;
  SceneUpdate mixed;
  mixed.meshes = {object_space};
  mixed.objects = {{object_space->key, object_space->key}};
  mixed.objects.front().transform[3] = 20;
  motion_wait(session, session.apply(mixed));
  const auto initial = session.status();
  motion_require(initial.subdivision_triangles > camera_triangles, "Missing object-space subdivision fixture");
  const auto object_triangles = initial.subdivision_triangles - camera_triangles;
  const auto cancels = session.diagnostics().session_cancel_calls;

  SceneUpdate stale;
  stale.camera = scene.camera;
  stale.camera->ortho_scale = 4.5f;
  const auto refining = session.apply(stale);
  session.pause(true);
  const auto active = session.status();
  motion_require(active.generation == refining && active.subdivision_triangles == object_triangles,
                 "Expected a camera generation with an unpublished tessellation");

  SceneUpdate drag;
  drag.camera = scene.camera;
  drag.camera->transform[3] = .5f;
  drag.defer_camera_subdivision = true;
  const auto preview = session.apply(drag);
  motion_require(session.status().state == SessionState::Paused &&
                     session.diagnostics().session_cancel_calls == cancels + 1,
                 "Superseding a camera tessellation did not cancel it while preserving Pause");
  session.pause(false);
  motion_wait(session, preview);
  const auto coarse = session.status();
  motion_require(coarse.subdivision_tessellations == initial.subdivision_tessellations &&
                     coarse.subdivision_triangles == 2 * subdivision.face_sizes.size() + object_triangles,
                 "Deferred navigation used unfinished triangles instead of valid source polygons");

  // A later motion can return to the projection first submitted by the
  // interrupted generation. The same-camera release still owes a redice.
  auto back = stale;
  back.defer_camera_subdivision = true;
  motion_wait(session, session.apply(back));
  motion_wait(session, session.apply(SceneUpdate{}, true));
  const auto refined = session.status();
  motion_require(refined.subdivision_tessellations == initial.subdivision_tessellations + 1 &&
                     refined.subdivision_triangles > coarse.subdivision_triangles &&
                     refined.geometry_conversions == initial.geometry_conversions,
                 "Releasing an unchanged returned camera did not refine retained source geometry");
}

inline void subdivision_test(const std::string &device, const std::string &output) {
  const std::filesystem::path directory(output);
  std::filesystem::create_directories(directory);
  SessionConfig config;
  config.device_id = device;
  config.width = 160;
  config.height = 90;
  config.samples = 16;
  config.interactive = false;
  config.passes = {"combined", "depth", "normal", "uv", "position"};
  RenderSession session(config);
  int checks = 0;
  auto reference = subdivision_cube_reference();
  motion_wait(session, session.apply(subdivision_scene(reference)));
  session.write_exr((directory / "reference-cube.exr").string());
  ++checks;
  auto cube = subdivision_cube();
  motion_wait(session, session.apply(subdivision_scene(cube)));
  auto status = session.status();
  motion_require(status.subdivision_control_faces == 6 && status.subdivision_triangles > 12 &&
                     status.subdivision_tessellations == 1,
                 "Coarse cube was not tessellated");
  const auto z = displacement_plane(*session.frame(), "position").pixels[(45 * 160 + 80) * 3 + 2];
  motion_require(z > .45f && z < .6f, "Catmull-Clark cube did not shrink from its independent .7 control extent");
  ++checks;
  for (auto &p : cube->positions)
    p.z = 99; // Owned control mesh must survive caller mutation.
  const auto before = status;
  SceneUpdate camera;
  camera.camera = subdivision_scene(reference).camera;
  camera.camera->transform[3] = .05f;
  motion_wait(session, session.apply(camera));
  status = session.status();
  motion_require(status.geometry_conversions == before.geometry_conversions &&
                     status.subdivision_tessellations == before.subdivision_tessellations,
                 "Object-space camera edit rebuilt the control mesh");
  ++checks;
  auto creased = subdivision_cube();
  creased->revision = 3;
  std::set<std::pair<int, int>> edges;
  for (int f = 0; f < 6; ++f)
    for (int c = 0; c < 4; ++c) {
      auto e = std::minmax(creased->subdivision->face_corners[f * 4 + c],
                           creased->subdivision->face_corners[f * 4 + (c + 1) % 4]);
      edges.emplace(e.first, e.second);
    }
  for (const auto &e : edges)
    creased->subdivision->edge_creases.push_back({e.first, e.second, 10});
  motion_wait(session, session.apply(subdivision_scene(creased)));
  motion_require(std::abs(displacement_plane(*session.frame(), "position").pixels[(45 * 160 + 80) * 3 + 2] - .7f) <
                     .001f,
                 "OpenSubdiv crease sharpness did not preserve the planar cube face");
  ++checks;
  auto invalid = std::make_shared<MeshData>(*creased);
  invalid->revision = 4;
  invalid->subdivision->face_corners[0] = 99;
  const auto generation = session.status().generation;
  bool rejected = false;
  try {
    SceneUpdate update;
    update.meshes = {invalid};
    session.apply(update);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  motion_require(rejected && session.status().generation == generation,
                 "Invalid coarse topology mutated the committed session");
  ++checks;
  // Budget failure must be reported by the update thread, retain the previous
  // completed image, and recover from the immutable control mesh.
  auto limited = subdivision_cube();
  limited->revision = 5;
  limited->subdivision->settings.triangle_limit = 1;
  session.apply(subdivision_scene(limited));
  const auto started = std::chrono::steady_clock::now();
  while (session.status().state != SessionState::Failed) {
    motion_require(std::chrono::steady_clock::now() - started < std::chrono::seconds(15),
                   "Subdivision budget failure did not terminate");
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
  motion_require(session.status().error.find("Subdivision triangle budget") != std::string::npos,
                 "Wrong subdivision budget error");
  ++checks;
  auto recovered = subdivision_cube();
  recovered->revision = 6;
  motion_wait(session, session.apply(subdivision_scene(recovered)));
  ++checks;
  session.close();
  motion_require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
                 "Subdivision shutdown retained geometry");
  ++checks;
  camera_tessellation_supersession(device);
  ++checks;
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << "}\n";
}
} // namespace cycles_houdini::harness
