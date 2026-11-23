// SPDX-License-Identifier: Apache-2.0
#include "camera_motion.h"
#include "cycles_houdini/transform.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace cycles_houdini {
namespace {
bool valid_fov(float value) { return std::isfinite(value) && value > 0 && value < 3.14159f; }
bool positive(float value) { return std::isfinite(value) && value > 0; }
bool clipping(const CameraData &value) {
  return positive(value.near_clip) && std::isfinite(value.far_clip) && value.far_clip > value.near_clip;
}
double matrix_determinant(const Matrix &m) { return transform_determinant(m); }
bool matrix_valid(const Matrix &m) { return invertible_transform(m); }
} // namespace
void resolve_camera_source_optics(CameraData &c, const std::string &source) {
  if (c.intrinsic_motion.empty())
    return;
  double aperture = c.aperture_size, ratio = c.aperture_ratio, focus = std::max(c.focus_distance, 1e-5f),
         near = c.near_clip;
  for (const auto &s : c.dof_motion) {
    aperture = std::max(aperture, double(s.aperture_size));
    ratio = std::min(ratio, double(s.aperture_ratio));
    focus = std::min(focus, double(std::max(s.focus_distance, 1e-5f)));
  }
  for (const auto &s : c.intrinsic_motion)
    near = std::max(near, double(s.near_clip));
  const double radius = aperture * std::max(1.0, 1 / ratio) * (c.image_iris_method ? std::sqrt(2.0) : 1.0),
               slope = radius / focus, limit = std::numeric_limits<float>::max();
  const auto extent = [&](double clip) { return radius + std::max(clip, slope * clip); };
  // Only a bad shutter clip/lens interaction is recoverable here. Invalid
  // nominal or independently malformed lens data remains a strict contract.
  if (std::isfinite(extent(c.near_clip)) && extent(c.near_clip) <= limit &&
      (!std::isfinite(extent(near)) || extent(near) > limit)) {
    for (auto &s : c.intrinsic_motion) {
      s.near_clip = c.near_clip;
      s.far_clip = c.far_clip;
    }
    c.source_diagnostics.push_back(
        {"camera.shutter_clipping", source, "camera:" + source,
         "Shutter clipping and depth-of-field origin bounds exceed numeric range",
         "Holding requested-frame clipping; retaining valid depth of field, projection and independent motion",
         "warning", "near far"});
  }
}
CameraData resolve_camera_source(const CameraSourceSample &nominal, std::span<const CameraSourceSample> samples,
                                 const std::string &source, float shutter_frames, bool motion_vectors, int width,
                                 int height, float pixel_aspect) {
  const auto &requested = nominal.value;
  const auto essential = [&](bool valid, const char *field) {
    if (!valid)
      throw std::invalid_argument("Invalid requested-frame camera " + std::string(field) + ": " + source);
  };
  essential((nominal.projection == 0 || nominal.projection == 1 || nominal.projection == 2) &&
                (nominal.projection == 2) == requested.panorama.has_value(),
            "projection");
  essential(matrix_valid(requested.transform), "transform");
  essential(clipping(requested), "clipping range");
  essential(nominal.projection == 2   ? !panorama_error(canonical_panorama(*requested.panorama))
            : nominal.projection == 1 ? positive(requested.ortho_scale)
                                      : valid_fov(requested.short_axis_fov),
            "projection parameters");
  essential(!camera_intrinsic_error(
                {requested.short_axis_fov, requested.near_clip, requested.far_clip, requested.ortho_scale,
                 requested.panorama ? std::optional(canonical_panorama(*requested.panorama)) : std::nullopt},
                nominal.projection == 1, width, height, pixel_aspect),
            "derived projection numeric range");
  if (!samples.empty() && (samples.size() < 3 || samples.size() > 129 || samples.size() % 2 != 1))
    throw std::invalid_argument("Camera source requires odd 3..129 shutter samples");
  CameraData result = requested;
  result.source_diagnostics.clear();
  result.motion.clear();
  result.intrinsic_motion.clear();
  result.shutter_frames = shutter_frames > 0 ? shutter_frames : requested.shutter_frames;
  const auto diagnostic = [&](const char *code, const char *message, const char *action, const char *field) {
    result.source_diagnostics.push_back({code, source, "camera:" + source, message, action, "warning", field});
  };
  result.orthographic = nominal.projection == 1;
  const auto canonical = [&](CameraData &camera) {
    if (nominal.projection == 2) {
      camera.panorama = canonical_panorama(*camera.panorama);
      camera.short_axis_fov = CameraData{}.short_axis_fov;
      camera.ortho_scale = CameraData{}.ortho_scale;
    } else {
      camera.panorama.reset();
      if (camera.orthographic)
        camera.short_axis_fov = CameraData{}.short_axis_fov;
      else
        camera.ortho_scale = CameraData{}.ortho_scale;
    }
  };
  canonical(result);
  if (samples.empty())
    return result;
  std::vector<CameraData> resolved;
  resolved.reserve(samples.size());
  for (const auto &sample : samples)
    resolved.push_back(sample.value);
  const auto recover = [&](const char *code, const char *message, const char *field, auto valid, auto restore) {
    if (std::any_of(resolved.begin(), resolved.end(), [&](const auto &value) { return !valid(value); })) {
      for (auto &value : resolved)
        restore(value);
      diagnostic(code, message,
                 "Holding only the requested-frame affected projection controls; retaining valid continuous optics and "
                 "independent motion",
                 field);
    }
  };
  const bool projection_ok = std::all_of(samples.begin(), samples.end(), [&](const auto &s) {
    return s.projection == nominal.projection && (s.projection == 2) == s.value.panorama.has_value() &&
           (nominal.projection != 2 || s.value.panorama->type == requested.panorama->type);
  });
  if (!projection_ok) {
    for (auto &value : resolved) {
      value.short_axis_fov = requested.short_axis_fov;
      value.ortho_scale = requested.ortho_scale;
      value.panorama = requested.panorama;
    }
    diagnostic(
        "camera.shutter_projection", "Discrete camera projection type changes or is unsupported during the shutter",
        "Holding requested-frame projection controls; retaining valid clipping, depth of field and transform motion",
        "projection panorama_type");
  } else if (nominal.projection == 2) {
    const auto &p = *requested.panorama;
    if (p.type == "equirectangular") {
      recover(
          "camera.shutter_panorama", "Invalid shutter latitude range", "latitude_min latitude_max",
          [](const auto &c) {
            const auto &p = *c.panorama;
            return std::isfinite(p.latitude_min) && std::isfinite(p.latitude_max) && p.latitude_min < p.latitude_max &&
                   p.latitude_min >= -1.5707964f && p.latitude_max <= 1.5707964f;
          },
          [&](auto &c) {
            c.panorama->latitude_min = p.latitude_min;
            c.panorama->latitude_max = p.latitude_max;
          });
      recover(
          "camera.shutter_panorama", "Invalid shutter longitude range", "longitude_min longitude_max",
          [](const auto &c) {
            const auto &p = *c.panorama;
            return std::isfinite(p.longitude_min) && std::isfinite(p.longitude_max) &&
                   p.longitude_min < p.longitude_max && std::isfinite(p.longitude_max - p.longitude_min);
          },
          [&](auto &c) {
            c.panorama->longitude_min = p.longitude_min;
            c.panorama->longitude_max = p.longitude_max;
          });
    } else if (p.type == "central_cylindrical") {
      recover(
          "camera.shutter_panorama", "Invalid shutter cylindrical longitude range",
          "cylindrical_u_min cylindrical_u_max",
          [](const auto &c) {
            const auto &p = *c.panorama;
            return std::isfinite(p.cylindrical_u_min) && std::isfinite(p.cylindrical_u_max) &&
                   p.cylindrical_u_min < p.cylindrical_u_max &&
                   std::isfinite(p.cylindrical_u_max - p.cylindrical_u_min);
          },
          [&](auto &c) {
            c.panorama->cylindrical_u_min = p.cylindrical_u_min;
            c.panorama->cylindrical_u_max = p.cylindrical_u_max;
          });
      recover(
          "camera.shutter_panorama", "Invalid shutter cylindrical height/radius range",
          "cylindrical_v_min cylindrical_v_max cylindrical_radius",
          [](const auto &c) {
            const auto &p = *c.panorama;
            return std::isfinite(p.cylindrical_v_min) && std::isfinite(p.cylindrical_v_max) &&
                   p.cylindrical_v_min < p.cylindrical_v_max &&
                   std::isfinite(p.cylindrical_v_max - p.cylindrical_v_min);
          },
          [&](auto &c) {
            c.panorama->cylindrical_v_min = p.cylindrical_v_min;
            c.panorama->cylindrical_v_max = p.cylindrical_v_max;
          });
    } else if (p.type == "fisheye_equidistant" || p.type == "fisheye_equisolid" ||
               p.type == "fisheye_lens_polynomial") {
      recover(
          "camera.shutter_panorama", "Invalid shutter fisheye field of view", "fisheye_fov",
          [](const auto &c) { return positive(c.panorama->fisheye_fov) && c.panorama->fisheye_fov <= 6.2831854f; },
          [&](auto &c) { c.panorama->fisheye_fov = p.fisheye_fov; });
      if (p.type == "fisheye_equisolid")
        recover(
            "camera.shutter_panorama", "Invalid shutter fisheye lens", "fisheye_lens",
            [](const auto &c) { return positive(c.panorama->fisheye_lens); },
            [&](auto &c) { c.panorama->fisheye_lens = p.fisheye_lens; });
      if (p.type == "fisheye_lens_polynomial")
        recover(
            "camera.shutter_panorama", "Invalid shutter polynomial coefficients", "polynomial",
            [](const auto &c) {
              const auto &v = c.panorama->polynomial;
              return std::all_of(v.begin(), v.end(), [](float x) { return std::isfinite(x); }) &&
                     std::any_of(v.begin() + 1, v.end(), [](float x) { return x != 0; });
            },
            [&](auto &c) { c.panorama->polynomial = p.polynomial; });
      if (p.type != "fisheye_equidistant") {
        if (std::any_of(resolved.begin(), resolved.end(),
                        [&](const auto &c) { return c.panorama->sensor_fit != p.sensor_fit; })) {
          for (auto &c : resolved)
            c.panorama->sensor_fit = p.sensor_fit;
          diagnostic("camera.shutter_sensor_fit", "Discrete panoramic sensor fit changes during the shutter",
                     "Holding requested-frame sensor fit; retaining valid active sensor-size and lens motion",
                     "sensor_fit");
        }
        recover(
            "camera.shutter_panorama", "Invalid shutter active sensor size", "sensor_width sensor_height",
            [&](const auto &c) {
              return positive(p.sensor_fit == "vertical" ? c.panorama->sensor_height : c.panorama->sensor_width);
            },
            [&](auto &c) {
              if (p.sensor_fit == "vertical")
                c.panorama->sensor_height = p.sensor_height;
              else
                c.panorama->sensor_width = p.sensor_width;
            });
      }
    }
  } else if (result.orthographic) {
    recover(
        "camera.shutter_orthographic_scale", "Invalid shutter orthographic width", "orthowidth",
        [](const auto &c) { return positive(c.ortho_scale); }, [&](auto &c) { c.ortho_scale = requested.ortho_scale; });
  } else
    recover(
        "camera.shutter_projection", "Invalid shutter focal length or aperture", "focal aperture",
        [](const auto &c) { return valid_fov(c.short_axis_fov); },
        [&](auto &c) { c.short_axis_fov = requested.short_axis_fov; });
  if (motion_vectors) {
    for (auto &c : resolved) {
      c.near_clip = requested.near_clip;
      c.far_clip = requested.far_clip;
    }
  } else
    recover(
        "camera.shutter_clipping", "Invalid shutter clipping range", "near far",
        [](const auto &c) { return clipping(c); },
        [&](auto &c) {
          c.near_clip = requested.near_clip;
          c.far_clip = requested.far_clip;
        });
  const auto intrinsic = [](const CameraData &c) {
    return CameraIntrinsicSample{c.short_axis_fov, c.near_clip, c.far_clip, c.ortho_scale, c.panorama};
  };
  const auto requested_intrinsic = intrinsic(result);
  // Group checks above preserve independent valid controls. Catch derived
  // sensor/polynomial/clip inverses as known source failures before the strict
  // engine contract, using the actual raster and pixel aspect.
  recover(
      "camera.shutter_clipping", "Shutter clipping projection exceeds numeric range", "near far",
      [&](const auto &c) {
        auto test = requested_intrinsic;
        test.near_clip = c.near_clip;
        test.far_clip = c.far_clip;
        return !camera_intrinsic_error(test, result.orthographic, width, height, pixel_aspect);
      },
      [&](auto &c) {
        c.near_clip = requested.near_clip;
        c.far_clip = requested.far_clip;
      });
  if (nominal.projection == 2) {
    const auto &p = *requested.panorama;
    const auto derived = [&](auto replace) {
      return [&, replace](const auto &c) {
        auto test = requested_intrinsic;
        replace(*test.panorama, *c.panorama);
        test.panorama = canonical_panorama(*test.panorama);
        return !camera_intrinsic_error(test, false, width, height, pixel_aspect);
      };
    };
    if (p.type == "equirectangular") {
      recover("camera.shutter_panorama", "Shutter latitude inverse exceeds numeric range", "latitude_min latitude_max",
              derived([](auto &a, const auto &b) {
                a.latitude_min = b.latitude_min;
                a.latitude_max = b.latitude_max;
              }),
              [&](auto &c) {
                c.panorama->latitude_min = p.latitude_min;
                c.panorama->latitude_max = p.latitude_max;
              });
      recover("camera.shutter_panorama", "Shutter longitude inverse exceeds numeric range",
              "longitude_min longitude_max", derived([](auto &a, const auto &b) {
                a.longitude_min = b.longitude_min;
                a.longitude_max = b.longitude_max;
              }),
              [&](auto &c) {
                c.panorama->longitude_min = p.longitude_min;
                c.panorama->longitude_max = p.longitude_max;
              });
    } else if (p.type == "central_cylindrical") {
      recover("camera.shutter_panorama", "Shutter cylindrical longitude inverse exceeds numeric range",
              "cylindrical_u_min cylindrical_u_max", derived([](auto &a, const auto &b) {
                a.cylindrical_u_min = b.cylindrical_u_min;
                a.cylindrical_u_max = b.cylindrical_u_max;
              }),
              [&](auto &c) {
                c.panorama->cylindrical_u_min = p.cylindrical_u_min;
                c.panorama->cylindrical_u_max = p.cylindrical_u_max;
              });
      recover("camera.shutter_panorama", "Shutter cylindrical height inverse exceeds numeric range",
              "cylindrical_v_min cylindrical_v_max cylindrical_radius", derived([](auto &a, const auto &b) {
                a.cylindrical_v_min = b.cylindrical_v_min;
                a.cylindrical_v_max = b.cylindrical_v_max;
              }),
              [&](auto &c) {
                c.panorama->cylindrical_v_min = p.cylindrical_v_min;
                c.panorama->cylindrical_v_max = p.cylindrical_v_max;
              });
    } else if (p.type == "fisheye_equisolid" || p.type == "fisheye_lens_polynomial") {
      recover("camera.shutter_panorama", "Shutter sensor projection/inverse exceeds numeric range",
              "sensor_width sensor_height", derived([](auto &a, const auto &b) {
                a.sensor_width = b.sensor_width;
                a.sensor_height = b.sensor_height;
              }),
              [&](auto &c) {
                c.panorama->sensor_width = p.sensor_width;
                c.panorama->sensor_height = p.sensor_height;
              });
      if (p.type == "fisheye_equisolid")
        recover("camera.shutter_panorama", "Shutter lens inverse exceeds numeric range", "fisheye_lens",
                derived([](auto &a, const auto &b) { a.fisheye_lens = b.fisheye_lens; }),
                [&](auto &c) { c.panorama->fisheye_lens = p.fisheye_lens; });
      if (p.type == "fisheye_lens_polynomial") {
        recover("camera.shutter_panorama", "Shutter polynomial/inverse exceeds numeric range", "polynomial",
                derived([](auto &a, const auto &b) { a.polynomial = b.polynomial; }),
                [&](auto &c) { c.panorama->polynomial = p.polynomial; });
        // Bound coupled sensor and coefficient extrema across every
        // interpolation interval, retaining independent FOV/size motion.
        auto extreme = requested_intrinsic;
        for (const auto &c : resolved) {
          const auto &v = *c.panorama;
          extreme.panorama->sensor_width = std::max(extreme.panorama->sensor_width, v.sensor_width);
          extreme.panorama->sensor_height = std::max(extreme.panorama->sensor_height, v.sensor_height);
          for (int i = 0; i < 5; ++i)
            extreme.panorama->polynomial[i] =
                std::max(std::abs(extreme.panorama->polynomial[i]), std::abs(v.polynomial[i]));
        }
        if (camera_intrinsic_error(extreme, false, width, height, pixel_aspect)) {
          for (auto &c : resolved)
            c.panorama->polynomial = p.polynomial;
          diagnostic("camera.shutter_panorama", "Combined shutter sensor/polynomial extrema exceed numeric range",
                     "Holding requested-frame coefficients; retaining valid sensor, FOV and independent motion",
                     "polynomial");
        }
      }
    }
  } else
    recover(
        result.orthographic ? "camera.shutter_orthographic_scale" : "camera.shutter_projection",
        "Shutter projection/inverse exceeds numeric range", result.orthographic ? "orthowidth" : "focal aperture",
        [&](const auto &c) {
          auto test = requested_intrinsic;
          test.short_axis_fov = c.short_axis_fov;
          test.ortho_scale = c.ortho_scale;
          return !camera_intrinsic_error(test, result.orthographic, width, height, pixel_aspect);
        },
        [&](auto &c) {
          c.short_axis_fov = requested.short_axis_fov;
          c.ortho_scale = requested.ortho_scale;
        });
  for (auto &c : resolved) {
    c.orthographic = result.orthographic;
    canonical(c);
    result.intrinsic_motion.push_back(intrinsic(c));
  }
  if (std::all_of(result.intrinsic_motion.begin(), result.intrinsic_motion.end(),
                  [&](const auto &c) { return c == requested_intrinsic; }))
    result.intrinsic_motion.clear();
  const bool negative = matrix_determinant(requested.transform) < 0;
  if (std::all_of(samples.begin(), samples.end(), [&](const auto &s) {
        return matrix_valid(s.value.transform) && (matrix_determinant(s.value.transform) < 0) == negative;
      })) {
    for (const auto &s : samples)
      result.motion.push_back(s.value.transform);
    // Camera ray generation interpolates this complete owned array. Its
    // midpoint may differ from the independently retained requested pose.
    if (std::all_of(result.motion.begin(), result.motion.end(), [&](const auto &m) { return m == result.transform; }))
      result.motion.clear();
  } else {
    result.transform = requested.transform;
    diagnostic("camera.shutter_transform",
               "A camera transform is non-finite, singular or changes handedness during the shutter",
               "Using requested-frame camera transform; retaining supported lens and object motion", "transform");
  }
  // Irrelevant source controls must not invalidate the effective projection.
  return result;
}
} // namespace cycles_houdini
