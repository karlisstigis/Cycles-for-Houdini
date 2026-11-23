// SPDX-License-Identifier: Apache-2.0
#include "../core/dicing_policy.h"
#include <iostream>
#include <stdexcept>
using namespace cycles_houdini;
namespace {
void require(bool value) {
  if (!value)
    throw std::runtime_error("Dicing policy contract failed");
}
} // namespace
int main() {
  try {
    DicingSettings accepted;
    accepted.offscreen_scale = 1;
    CameraData camera;
    const auto initial = resolve_dicing(accepted, {}, camera, {}, 1, true);
    require(initial.projection_changed && !initial.force && !initial.hold && !initial.recalculate);
    const auto stable = resolve_dicing(accepted, {}, camera, camera, 1, true);
    require(!stable.projection_changed);
    auto candidate = accepted;
    candidate.update = false;
    auto held = resolve_dicing(accepted, candidate, camera, camera, 1, true);
    require(held.hold && !held.force && !held.recalculate);
    candidate.refresh_revision = 1;
    auto refreshed = resolve_dicing(accepted, candidate, camera, camera, 1, true);
    require(refreshed.force && !refreshed.hold && refreshed.recalculate);
    auto final = resolve_dicing(accepted, candidate, camera, camera, 1, false);
    require(!final.force && !final.hold && !final.recalculate);
    accepted.update = false;
    candidate.update = true;
    candidate.refresh_revision = 0;
    require(resolve_dicing(accepted, candidate, camera, camera, 1, true).recalculate);
    candidate.offscreen_scale = 2;
    require(resolve_dicing(accepted, candidate, camera, camera, 1, true).projection_changed);
    candidate.offscreen_scale = 1;
    auto changed = camera;
    changed.focus_distance += 1;
    changed.aperture_size += 0.1f;
    changed.source_diagnostics.push_back({});
    require(!resolve_dicing(accepted, candidate, changed, camera, 1, true).projection_changed);
    changed.short_axis_fov += 0.1f;
    require(resolve_dicing(accepted, candidate, changed, camera, 1, true).projection_changed);
    candidate.camera = camera;
    require(!resolve_dicing(accepted, candidate, changed, camera, 1, true).projection_changed);
    candidate.camera.reset();
    changed = camera;
    changed.intrinsic_motion.push_back({camera.short_axis_fov, 0.1f, 100.f, camera.ortho_scale, camera.panorama});
    require(!resolve_dicing(accepted, candidate, changed, camera, 1, true).projection_changed);
    changed.intrinsic_motion[0].short_axis_fov += 0.1f;
    require(resolve_dicing(accepted, candidate, changed, camera, 1, true).projection_changed);
    require(!accepted.update && accepted.refresh_revision == 0 && accepted.offscreen_scale == 1);
    std::cout << "Dicing policy contracts passed\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
