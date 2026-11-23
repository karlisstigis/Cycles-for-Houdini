// SPDX-License-Identifier: Apache-2.0
#include "dicing_policy.h"
#include <algorithm>
#include <tuple>
namespace cycles_houdini {
namespace {
// The screen dicer uses the requested transform with conservative bounds over
// active sampled projection fields, excluding DOF and source diagnostics.
auto camera_projection_samples(const CameraData &c) {
  auto samples = c.intrinsic_motion;
  const CameraIntrinsicSample nominal{c.short_axis_fov, .01f, 10000, c.ortho_scale, c.panorama};
  for (auto &sample : samples) {
    sample.near_clip = .01f;
    sample.far_clip = 10000;
  }
  if (std::all_of(samples.begin(), samples.end(), [&](const auto &sample) { return sample == nominal; }))
    samples.clear();
  return samples;
}
auto camera_dicing_state(const CameraData &c) {
  return std::tuple(c.transform, c.short_axis_fov, c.near_clip, c.far_clip, c.orthographic, c.ortho_scale,
                    c.pixel_aspect, c.panorama, camera_projection_samples(c));
}

} // namespace
DicingResolution resolve_dicing(const DicingSettings &accepted, const std::optional<DicingSettings> &candidate,
                                const std::optional<CameraData> &desired_camera,
                                const std::optional<CameraData> &applied_camera, float applied_offscreen_scale,
                                bool interactive) {
  DicingResolution result;
  result.desired = candidate.value_or(accepted);
  result.force = interactive && result.desired.refresh_revision != accepted.refresh_revision;
  result.hold = interactive && !result.desired.update && !result.force;
  result.recalculate = result.force || (interactive && !accepted.update && result.desired.update);
  result.camera = result.desired.camera ? result.desired.camera : desired_camera;
  result.projection_changed =
      result.camera &&
      (!applied_camera || camera_dicing_state(*result.camera) != camera_dicing_state(*applied_camera) ||
       result.desired.offscreen_scale != applied_offscreen_scale);
  return result;
}
} // namespace cycles_houdini
