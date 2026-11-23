// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "motion_fixture.h"
#include "cycles_houdini/snapshot.h"

namespace cycles_houdini::harness {
inline void gaussian_splat_contracts(const std::string &device, const std::filesystem::path &destination) {
  std::filesystem::create_directories(destination);
  int checks = 0;
  SceneUpdate source;
  source.camera = CameraData{};
  source.camera->orthographic = true;
  source.camera->ortho_scale = 2;
  source.background = Float3{};
  source.transparent_background = true;
  source.motion_blur = false;
  auto points = std::make_shared<PointData>();
  points->key = "splat";
  points->revision = 1;
  points->positions = {{0, 0, 0}};
  points->radii = {.1f};
  points->splats.emplace();
  auto &g = *points->splats;
  g.scene_linear_color = true;
  g.scales = {{.3f, .1f, .05f}};
  g.rotations = {{0, 0, 0, 1}};
  g.radiance = {{.8f, .2f, .1f, .8f}};
  source.points = {points};
  source.objects = {{"splat/object", "splat"}};
  // Exercise both immutable scene replacement and independent worker copies.
  WorkerUpdate packet;
  packet.scene = source;
  const auto decoded = decode_worker_update(encode_worker_update(packet));
  motion_require(decoded.scene.points.front()->splats == points->splats, "Worker lost Gaussian payload");
  ++checks;
  const auto initial = snapshot_state({}, source);
  auto changed = std::make_shared<PointData>(*points);
  changed->splats->scales[0].x = .4f;
  SceneUpdate edit;
  edit.points = {changed};
  const auto replacement = snapshot_state(initial, edit);
  motion_require(snapshot_difference(initial.get(), *replacement).points.size() == 1,
                 "Splat-only edit omitted from snapshot difference");
  ++checks;
  SessionConfig config;
  config.device_id = device;
  config.width = 96;
  config.height = 96;
  config.samples = 256;
  config.threads = 2;
  config.interactive = false;
  RenderSession session(config);
  struct Measurement {
    double r{}, g{}, b{}, xx{}, yy{}, xy{};
  };
  const auto measure = [&](const FrameEvent &frame) {
    Measurement m;
    for (int y = 0; y < frame.height; ++y)
      for (int x = 0; x < frame.width; ++x) {
        const auto at = (std::size_t(y) * frame.width + x) * 4;
        const auto &p = frame.planes.front().pixels;
        for (int c = 0; c < 4; ++c)
          motion_require(std::isfinite(p[at + c]), "Nonfinite Gaussian image");
        const auto weight = double(p[at]);
        const auto dx = (x + .5 - frame.width * .5) / frame.width, dy = (y + .5 - frame.height * .5) / frame.height;
        m.r += weight;
        m.g += p[at + 1];
        m.b += p[at + 2];
        m.xx += weight * dx * dx;
        m.yy += weight * dy * dy;
        m.xy += weight * dx * dy;
      }
    return m;
  };
  auto frame = motion_wait(session, session.apply(source));
  const auto baseline = measure(*frame);
  motion_require(baseline.r > 1 && baseline.xx > baseline.yy * 6,
                 "Native splat scales did not produce anisotropic support");
  ++checks;
  motion_require(std::abs(baseline.r / baseline.g - 4) < .08 && std::abs(baseline.g / baseline.b - 2) < .04,
                 "Working-space Cd was not preserved through SH encoding");
  ++checks;
  session.write_exr((destination / "splat.exr").string());
  auto rotated = std::make_shared<PointData>(*points);
  rotated->revision = 2;
  rotated->splats->rotations = {{0, 0, .382683432f, .923879533f}};
  edit = {};
  edit.points = {rotated};
  const auto turned = measure(*motion_wait(session, session.apply(edit)));
  motion_require(std::abs(turned.xx / turned.yy - 1) < .25 && std::abs(turned.xy) > turned.xx * .5,
                 "Normalized xyzw orientation did not rotate splat support");
  ++checks;
  auto transparent = std::make_shared<PointData>(*points);
  transparent->revision = 3;
  transparent->splats->radiance[0][3] = 0;
  edit.points = {transparent};
  motion_require(measure(*motion_wait(session, session.apply(edit))).r < .001, "Zero-opacity GSplat remained visible");
  ++checks;
  // Attribute removal must reset the engine primitive type on the same key.
  auto sphere = std::make_shared<PointData>(*points);
  sphere->revision = 4;
  sphere->splats.reset();
  sphere->material = "sphere/emission";
  sphere->radii = {.25f};
  MaterialData emission;
  emission.key = sphere->material;
  emission.kind = MaterialKind::Emission;
  emission.color = {.8f, .2f, .1f};
  edit = {};
  edit.materials = {emission};
  edit.points = {sphere};
  const auto round = measure(*motion_wait(session, session.apply(edit)));
  motion_require(round.r > 1 && std::abs(round.xx / round.yy - 1) < .15,
                 "GSplat-to-sphere edit retained Gaussian state");
  ++checks;
  auto restored = std::make_shared<PointData>(*points);
  restored->revision = 5;
  edit = {};
  edit.points = {restored};
  motion_require(measure(*motion_wait(session, session.apply(edit))).xx > baseline.yy * 6,
                 "Sphere-to-GSplat edit failed");
  ++checks;
  // A second object shares the exact Gaussian resource. Attribute offsets and
  // object transforms are left for upstream's GSplat instancing path.
  const auto converted = session.status().geometry_conversions;
  edit = {};
  edit.objects = {{"splat/object", "splat"}, {"splat/instance", "splat"}};
  edit.objects.back().transform[3] = .55f;
  const auto instanced = measure(*motion_wait(session, session.apply(edit)));
  motion_require(instanced.r > baseline.r * 1.3 && session.status().geometry_conversions == converted,
                 "GSplat instance lost shared resource or radiance: baseline=" + std::to_string(baseline.r) +
                     ", instanced=" + std::to_string(instanced.r) + ", conversions=" + std::to_string(converted) +
                     "->" + std::to_string(session.status().geometry_conversions));
  ++checks;
  // Object-buffer growth and shrinkage must retain every packed attribute,
  // including unchanged quaternion and SH tables. Compare complete renders
  // against equivalent fresh scenes, rather than merely checking visibility.
  SceneUpdate object_scene = source;
  object_scene.points.clear();
  object_scene.objects = edit.objects;
  object_scene.objects.front().transform[3] = .1f;
  // A fresh scene requires the shared point resource; the retained update
  // omits it to prove the object-only route is sufficient.
  const auto compare_object_edit = [&](SceneUpdate update, const char *phase) {
    SceneUpdate expected = update;
    expected.points = {points};
    RenderSession fresh(config);
    const auto reference = measure(*motion_wait(fresh, fresh.apply(expected)));
    const auto retained = measure(*motion_wait(session, session.apply(update)));
    const auto relative = [](double a, double b) { return std::abs(a - b) / std::max(std::abs(b), .001); };
    motion_require(relative(retained.r, reference.r) < .03 && relative(retained.xx, reference.xx) < .05 &&
                       relative(retained.yy, reference.yy) < .05,
                   std::string("Retained Gaussian differs from fresh scene during ") + phase);
    motion_require(session.status().geometry_conversions == converted,
                   std::string("Object-only edit reconverted geometry during ") + phase);
    fresh.close();
    ++checks;
  };
  compare_object_edit(object_scene, "existing transform with two instances");
  object_scene.objects.pop_back();
  object_scene.objects.front().transform[3] = -.1f;
  object_scene.deleted_objects = {"splat/instance"};
  compare_object_edit(object_scene, "transform and instance removal");
  object_scene.deleted_objects.clear();
  object_scene.objects.front().transform[3] = .1f;
  compare_object_edit(object_scene, "existing-only transform");
  object_scene.objects.front().properties.pass_index = 3;
  compare_object_edit(object_scene, "existing-only property");
  object_scene.objects.push_back({"splat/instance", "splat"});
  object_scene.objects.back().transform[3] = .55f;
  object_scene.objects.front().transform[3] = -.1f;
  compare_object_edit(object_scene, "transform and instance insertion");
  auto moving = std::make_shared<PointData>(*points);
  moving->revision = 6;
  moving->motion.positions = {{{-.3f, 0, 0}}, {{.3f, 0, 0}}};
  moving->splats->motion_scales = {{{.2f, .1f, .05f}}, {{.4f, .1f, .05f}}};
  moving->splats->motion_rotations = {{{0, 0, 0, 1}}, {{0, 0, .382683432f, .923879533f}}};
  moving->splats->motion_radiance = {{{.8f, .2f, .1f, .2f}}, {{.8f, .2f, .1f, .9f}}};
  edit = {};
  edit.points = {moving};
  edit.objects = source.objects;
  edit.motion_blur = true;
  motion_require(measure(*motion_wait(session, session.apply(edit))).r > 1,
                 "Gaussian deformation/motion attributes failed");
  ++checks;
  const auto generation = session.status().generation;
  auto invalid = std::make_shared<PointData>(*points);
  invalid->revision = 7;
  invalid->splats->rotations[0] = {0, 0, 0, 0};
  edit = {};
  edit.points = {invalid};
  bool rejected = false;
  try {
    session.apply(edit);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  motion_require(rejected && session.status().generation == generation,
                 "Invalid Gaussian update mutated accepted scene");
  ++checks;
  session.close();
  motion_require(session.status().device_allocation_bytes == 0, "Gaussian session retained device allocations");
  ++checks;
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"device\":\"" << device
            << "\",\"worker_protocol\":" << worker_protocol_version << "}\n";
}
} // namespace cycles_houdini::harness
