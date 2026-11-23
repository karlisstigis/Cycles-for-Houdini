// SPDX-License-Identifier: Apache-2.0
#include "camera_motion.h"
#include <algorithm>
#include <cmath>
#include <numbers>
#include <limits>
#include <stdexcept>

namespace cycles_houdini {
namespace {
constexpr float pi = std::numbers::pi_v<float>;
bool positive(float v) { return std::isfinite(v) && v > 0; }
bool interval(float lo, float hi, float bound) {
  return std::isfinite(lo) && std::isfinite(hi) && lo < hi && lo >= -bound && hi <= bound;
}
bool range(float lo, float hi) { return std::isfinite(lo) && std::isfinite(hi) && lo < hi && std::isfinite(hi - lo); }
} // namespace
PanoramaSettings canonical_panorama(const PanoramaSettings &p) {
  PanoramaSettings c;
  c.type = p.type;
  if (p.type == "equirectangular") {
    c.latitude_min = p.latitude_min;
    c.latitude_max = p.latitude_max;
    c.longitude_min = p.longitude_min;
    c.longitude_max = p.longitude_max;
  } else if (p.type == "fisheye_equidistant" || p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
    c.fisheye_fov = p.fisheye_fov;
    if (p.type == "fisheye_equisolid")
      c.fisheye_lens = p.fisheye_lens;
    if (p.type == "fisheye_lens_polynomial")
      c.polynomial = p.polynomial;
    if (p.type != "fisheye_equidistant") {
      c.sensor_fit = p.sensor_fit;
      if (p.sensor_fit == "vertical")
        c.sensor_height = p.sensor_height;
      else
        c.sensor_width = p.sensor_width;
    }
  } else if (p.type == "central_cylindrical") {
    c.cylindrical_u_min = p.cylindrical_u_min;
    c.cylindrical_u_max = p.cylindrical_u_max;
    c.cylindrical_v_min = p.cylindrical_v_min;
    c.cylindrical_v_max = p.cylindrical_v_max;
  }
  return c;
}
const char *panorama_error(const PanoramaSettings &p) {
  if (p.type == "equirectangular") {
    if (!interval(p.latitude_min, p.latitude_max, pi / 2) || !range(p.longitude_min, p.longitude_max))
      return "Invalid panoramic latitude/longitude range";
  } else if (p.type == "central_cylindrical") {
    if (!range(p.cylindrical_u_min, p.cylindrical_u_max) || !range(p.cylindrical_v_min, p.cylindrical_v_max))
      return "Invalid central cylindrical range or radius";
  } else if (p.type == "fisheye_equidistant" || p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
    if (!positive(p.fisheye_fov) || p.fisheye_fov > 2 * pi)
      return "Fisheye field of view must be between zero and 360 degrees";
    if (p.type == "fisheye_equisolid" && !positive(p.fisheye_lens))
      return "Fisheye lens must be finite and positive";
    if (p.type == "fisheye_lens_polynomial" && (!std::all_of(p.polynomial.begin(), p.polynomial.end(), [](float v) {
          return std::isfinite(v);
        }) || std::none_of(p.polynomial.begin() + 1, p.polynomial.end(), [](float v) { return v != 0; })))
      return "Fisheye polynomial requires finite coefficients and a varying radial projection";
    if (p.type != "fisheye_equidistant") {
      if (p.sensor_fit != "auto" && p.sensor_fit != "horizontal" && p.sensor_fit != "vertical")
        return "Unknown panoramic sensor fit";
      if (!positive(p.sensor_fit == "vertical" ? p.sensor_height : p.sensor_width))
        return "Panoramic sensor size must be finite and positive";
    }
  } else if (p.type != "mirrorball" && p.type != "equiangular_cubemap_face")
    return "Unknown panoramic projection";
  return nullptr;
}
Matrix camera_engine_transform(const CameraData &camera, const Matrix &source) {
  if (!camera.panorama)
    return source;
  Matrix result = source;
  for (int row = 0; row < 3; ++row) {
    if (camera.panorama->type == "mirrorball") {
      result[row * 4] = source[row * 4];
      result[row * 4 + 1] = -source[row * 4 + 2];
      result[row * 4 + 2] = source[row * 4 + 1];
    } else {
      result[row * 4] = source[row * 4 + 2];
      result[row * 4 + 1] = -source[row * 4];
      result[row * 4 + 2] = source[row * 4 + 1];
    }
  }
  return result;
}
std::pair<float, float> panorama_sensor(const PanoramaSettings &p, int width, int height, float pixel_aspect) {
  const double aspect = double(width) * pixel_aspect / height;
  const bool horizontal = p.sensor_fit == "horizontal" || (p.sensor_fit == "auto" && aspect > 1);
  const double size = p.sensor_fit == "vertical" ? p.sensor_height : p.sensor_width;
  const float x = float(horizontal ? size : size * aspect), y = float(horizontal ? size / aspect : size);
  if (!positive(x) || !positive(y))
    throw std::invalid_argument("Panorama sensor fitting exceeds the engine numeric range");
  return {x, y};
}
const char *camera_intrinsic_error(const CameraIntrinsicSample &c, bool orthographic, int width, int height,
                                   float pixel_aspect) {
  const double limit = std::numeric_limits<float>::max(), aspect = double(width) * pixel_aspect / height;
  if (!positive(c.near_clip) || !std::isfinite(c.far_clip) || c.far_clip <= c.near_clip)
    return "Invalid clipping range";
  const double length = double(c.far_clip) - c.near_clip;
  if (!c.panorama &&
      (orthographic ? 1 / length > limit
                    : double(c.near_clip) * c.far_clip > limit || double(c.near_clip) * c.far_clip / length > limit))
    return "Clipping projection exceeds numeric range";
  if (c.panorama) {
    const auto &p = *c.panorama;
    if (const auto *error = panorama_error(p))
      return error;
    const auto interval_safe = [&](float a, float b) {
      return 1 / (double(b) - a) <= limit && std::max(std::abs(double(a)), std::abs(double(b))) < limit / 4;
    };
    if (p.type == "equirectangular" &&
        (!interval_safe(p.latitude_min, p.latitude_max) || !interval_safe(p.longitude_min, p.longitude_max)))
      return "Panoramic interval inverse exceeds numeric range";
    if (p.type == "central_cylindrical" && (!interval_safe(p.cylindrical_u_min, p.cylindrical_u_max) ||
                                            !interval_safe(p.cylindrical_v_min, p.cylindrical_v_max)))
      return "Cylindrical interval inverse exceeds numeric range";
    if (p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
      std::pair<float, float> sensor;
      try {
        sensor = panorama_sensor(p, width, height, pixel_aspect);
      } catch (const std::invalid_argument &) {
        return "Sensor fitting exceeds numeric range";
      }
      const double radius = std::hypot(double(sensor.first), double(sensor.second)) * .5;
      if (1 / double(sensor.first) > limit || 1 / double(sensor.second) > limit || radius > std::sqrt(limit) / 512)
        return "Sensor projection exceeds numeric range";
      if (p.type == "fisheye_equisolid" &&
          (double(p.fisheye_lens) * 2 > limit || 1 / (2 * double(p.fisheye_lens)) > limit))
        return "Fisheye lens inverse exceeds numeric range";
      if (p.type == "fisheye_lens_polynomial") {
        double bound = 0, power = 1;
        for (const float coefficient : p.polynomial) {
          bound += std::abs(double(coefficient)) * power;
          power *= std::max(radius * 256, 1.0);
        }
        if (!std::isfinite(bound) || bound > limit / 16)
          return "Polynomial projection/inverse exceeds numeric range";
      }
    }
  } else if (orthographic) {
    const double vertical_span = double(c.ortho_scale) / pixel_aspect,
                 horizontal_span = double(c.ortho_scale) * width / height;
    if (!positive(c.ortho_scale) || 1 / vertical_span > limit || vertical_span > limit / 4 ||
        horizontal_span > limit / 4)
      return "Orthographic projection exceeds numeric range";
  } else {
    if (!positive(c.short_axis_fov) || c.short_axis_fov >= 3.14159f)
      return "Invalid perspective field of view";
    const double tangent =
        std::tan(double(c.short_axis_fov) * .5) * std::max(1.0, double(width) / height) / std::max(1.0, aspect);
    const float angle = float(2 * std::atan(tangent));
    if (!(angle > 0 && angle < std::numbers::pi_v<float>) || 1 / tangent > limit ||
        tangent * std::max(1.0, aspect) > limit / 4)
      return "Perspective projection exceeds numeric range";
  }
  return nullptr;
}
} // namespace cycles_houdini
