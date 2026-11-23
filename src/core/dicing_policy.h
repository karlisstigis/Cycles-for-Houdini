// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
namespace cycles_houdini {
struct DicingResolution {
  DicingSettings desired;
  std::optional<CameraData> camera;
  bool force{}, hold{}, recalculate{}, projection_changed{};
};
// Pure candidate resolution. Never writes the accepted policy or camera.
DicingResolution resolve_dicing(const DicingSettings &accepted, const std::optional<DicingSettings> &candidate,
                                const std::optional<CameraData> &desired_camera,
                                const std::optional<CameraData> &applied_camera, float applied_offscreen_scale,
                                bool interactive);
} // namespace cycles_houdini
