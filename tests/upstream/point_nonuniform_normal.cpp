/* SPDX-License-Identifier: Apache-2.0
 * Standalone regression using the actual Cycles point and object kernel headers.
 * No Houdini adapter, copied normal implementation, scene exporter or renderer DLL.
 */
#define CCL_NAMESPACE_BEGIN namespace ccl {
#define CCL_NAMESPACE_END }
#include "util/math.h"
#include "util/transform.h"
#include "util/color.h"
#include "kernel/globals.h"
/* These functions read only the shared kernel arrays. Use their actual base
 * without constructing a render thread and its unrelated profiler/OSL state. */
#define KernelGlobals const KernelGlobalsCPU *
#include "kernel/geom/point_intersect.h"
#undef KernelGlobals
#include <algorithm>
#include <cmath>
#include <cstdio>
/* Supply the actual allocator accounting used by KernelGlobalsCPU's profiling
 * vector; this keeps the executable independent of an installed Cycles library. */
#include "util/guarded_allocator.cpp"

using namespace ccl;

struct Case {
  const char *name;
  double sx, sy, sz, angle;
  bool applied, object_motion;
};

int main() {
  const Case cases[] = {
      {"identity", 1, 1, 1, 0, false, false},
      {"uniform", 2.5, 2.5, 2.5, 0.37, false, false},
      {"nonuniform", 3, 1, 0.5, 0, false, false},
      {"rotated_nonuniform", 3, 1, 0.5, 0.73, false, false},
      {"mirrored_nonuniform", -3, 1, 0.5, 0.73, false, false},
      {"object_motion_nonuniform", 3, 1, 0.5, 0.73, false, true},
      {"transform_applied", 1, 1, 1, 0, true, false},
  };
  const float3 directions[] = {
      make_float3(1, 0, 0),
      make_float3(0, 1, 0),
      make_float3(0, 0, 1),
      make_float3(-1, 0, 0),
      make_float3(0, -1, 0),
      make_float3(0, 0, -1),
      normalize(make_float3(1, 1, 1)),
      normalize(make_float3(-1, 1, 1)),
      normalize(make_float3(1, -1, 1)),
      normalize(make_float3(1, 1, -1)),
      normalize(make_float3(1, 2, 3)),
      normalize(make_float3(-3, 2, 1)),
  };
  int checks = 0, failures = 0, control_failures = 0;
  double max_error = 0, max_lambert_error = 0;
  std::printf("{\"cases\":[");
  bool first = true;
  for (const Case &c : cases) {
    const double cs = std::cos(c.angle), sn = std::sin(c.angle);
    const double tx = 0.7, ty = -1.1, tz = 0.4;
    auto world = [&](const float3 p, bool position) {
      return make_float3(float(cs * c.sx * p.x - sn * c.sy * p.y + (position ? tx : 0)),
                         float(sn * c.sx * p.x + cs * c.sy * p.y + (position ? ty : 0)),
                         float(c.sz * p.z + (position ? tz : 0)));
    };
    KernelObject object = {};
    object.position_offset = 0;
    object.num_geom_steps = 3;
    object.numverts = 1;
    object.tfm.x = make_float4(float(cs * c.sx), float(-sn * c.sy), 0, float(tx));
    object.tfm.y = make_float4(float(sn * c.sx), float(cs * c.sy), 0, float(ty));
    object.tfm.z = make_float4(0, 0, float(c.sz), float(tz));
    object.itfm.x = make_float4(float(cs / c.sx), float(sn / c.sx), 0, float(-(cs * tx + sn * ty) / c.sx));
    object.itfm.y = make_float4(float(-sn / c.sy), float(cs / c.sy), 0, float((sn * tx - cs * ty) / c.sy));
    object.itfm.z = make_float4(0, 0, float(1 / c.sz), float(-tz / c.sz));
    const float3 center = make_float3(0.3f, -0.2f, 0.4f);
    const float3 shift = make_float3(0.13f, -0.07f, 0.09f);
    const float radius = 0.7f;
    float4 points[] = {make_float4(center.x, center.y, center.z, radius),
                       make_float4(center.x - shift.x, center.y - shift.y, center.z - shift.z, radius),
                       make_float4(center.x + shift.x, center.y + shift.y, center.z + shift.z, radius)};
    uint shader = 17;
    KernelGlobalsCPU globals = {};
    globals.objects = {&object, 1};
    globals.points = {points, 3};
    globals.points_shader = {&shader, 1};
    int case_failures = 0;
    double case_error = 0;
    for (bool moving : {false, true}) {
      for (float time : {0.25f, 0.5f, 0.75f}) {
        const float3 sampled_center = center + (moving ? (2 * time - 1) * shift : zero_float3());
        for (const float3 u : directions) {
          Ray ray = {};
          const float3 local_P = sampled_center + 3 * u;
          const float3 local_D = -u;
          ray.P = c.applied ? local_P : world(local_P, true);
          ray.D = c.applied ? local_D : world(local_D, false);
          Intersection isect = {};
          const int type = PRIMITIVE_POINT | (moving ? PRIMITIVE_MOTION : 0);
          const bool hit = point_intersect(&globals, &isect, local_P, local_D, 0, 100, 0, 0, time, type);
          ShaderData sd = {};
          sd.object = sd.prim = 0;
          sd.object_flag = (c.applied ? SD_OBJECT_TRANSFORM_APPLIED : 0) | (c.object_motion ? SD_OBJECT_MOTION : 0);
          sd.type = type;
          sd.time = time;
          sd.ob_tfm_motion = object.tfm;
          sd.ob_itfm_motion = object.itfm;
          point_shader_setup(&globals, &sd, &isect, &ray);
          /* Gradient of the independently parameterized implicit ellipsoid:
           * f=(R^-1(P-T)-center)^2/axes^2 - 1. Rotation is orthonormal.
           * Evaluate in double precision without Cycles normal helpers. */
          double gx = u.x, gy = u.y, gz = u.z;
          if (!c.applied) {
            gx = cs * u.x / c.sx - sn * u.y / c.sy;
            gy = sn * u.x / c.sx + cs * u.y / c.sy;
            gz = u.z / c.sz;
          }
          const double n = std::sqrt(gx * gx + gy * gy + gz * gz);
          gx /= n;
          gy /= n;
          gz /= n;
          const double err =
              std::sqrt(std::pow(sd.Ng.x - gx, 2) + std::pow(sd.Ng.y - gy, 2) + std::pow(sd.Ng.z - gz, 2));
          const double lambert = std::max(0.0, (sd.Ng.x + 2 * sd.Ng.y + 3 * sd.Ng.z) / std::sqrt(14.0));
          const double expected = std::max(0.0, (gx + 2 * gy + 3 * gz) / std::sqrt(14.0));
          max_lambert_error = std::max(max_lambert_error, std::abs(lambert - expected));
          const bool failed = !hit || std::abs(isect.t - (3 - radius)) > 2e-6 || sd.shader != shader || err > 3e-6 ||
                              len(sd.N - sd.Ng) > 1e-6;
          ++checks;
          case_failures += failed;
          if (c.sx == c.sy && c.sy == c.sz)
            control_failures += failed;
          case_error = std::max(case_error, err);
        }
      }
    }
    if (!first)
      std::printf(",");
    first = false;
    std::printf("{\"name\":\"%s\",\"checks\":72,\"failures\":%d,\"max_normal_error\":%.10g}", c.name, case_failures,
                case_error);
    failures += case_failures;
    max_error = std::max(max_error, case_error);
  }
  std::printf("],\"checks\":%d,\"failures\":%d,\"control_failures\":%d,"
              "\"max_normal_error\":%.10g,\"max_lambert_response_error\":%.10g,"
              "\"result\":\"%s\"}\n",
              checks, failures, control_failures, max_error, max_lambert_error, failures ? "failed" : "passed");
  return failures ? 1 : 0;
}
