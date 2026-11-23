// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <span>

namespace cycles_houdini {
// Untrusted host-source values, before strict CameraData engine validation.
// Keeping the raw projection distinguishes unknown types from orthographic.
struct CameraSourceSample {
  CameraData value;
  int projection{};
};
// Host sources copy only active parameters into a coherent owned projection.
PanoramaSettings canonical_panorama(const PanoramaSettings &value);
const char *panorama_error(const PanoramaSettings &value);
Matrix camera_engine_transform(const CameraData &camera, const Matrix &matrix);
std::pair<float, float> panorama_sensor(const PanoramaSettings &value, int width, int height, float pixel_aspect);
const char *camera_intrinsic_error(const CameraIntrinsicSample &value, bool orthographic, int width, int height,
                                   float pixel_aspect);
// Known host-source interaction after independently recovered DOF/projection
// capture. Direct engine-owned payloads retain strict validation.
void resolve_camera_source_optics(CameraData &camera, const std::string &source);
CameraData resolve_camera_source(const CameraSourceSample &nominal, std::span<const CameraSourceSample> samples,
                                 const std::string &source, float shutter_frames, bool motion_vectors = false,
                                 int width = 1, int height = 1, float pixel_aspect = 1);
} // namespace cycles_houdini
