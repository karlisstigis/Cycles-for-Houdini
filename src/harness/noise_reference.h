// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "kernel/svm/fractal_noise.h"
#include <fstream>
#include <filesystem>
#include <iomanip>
#include <stdexcept>

namespace cycles_houdini::harness {
// Direct CPU kernel evaluation, without the host's VOP graph conversion,
// coordinate defaults, material bindings or scene extraction.
template <class T> float noise_reference_value(T p, int kind, bool normalize) {
  constexpr float detail = 2.75f, roughness = .6f, lacunarity = 2.0f, offset = .8f, gain = 1.1f;
  switch (kind) {
  case 0:
    return ccl::noise_multi_fractal(p, detail, roughness, lacunarity);
  case 1:
    return ccl::noise_fbm(p, detail, roughness, lacunarity, normalize);
  case 2:
    return ccl::noise_hybrid_multi_fractal(p, detail, roughness, lacunarity, offset, gain);
  case 3:
    return ccl::noise_ridged_multi_fractal(p, detail, roughness, lacunarity, offset, gain);
  default:
    return ccl::noise_hetero_terrain(p, detail, roughness, lacunarity, offset);
  }
}
inline void write_noise_reference(const std::filesystem::path &destination) {
  std::ofstream out(destination);
  if (!out)
    throw std::runtime_error("Cannot write direct noise reference");
  const char *kinds[] = {"multifractal", "fBM", "hybrid_multifractal", "ridged_multifractal", "hetero_terrain"};
  const auto p = ccl::make_float3(.3125f, -.625f, .125f) * 2.5f;
  const float w = .375f * 2.5f;
  out << std::setprecision(10) << "{\"result\":\"passed\",\"cases\":[";
  bool first = true;
  for (int dimensions = 1; dimensions <= 4; ++dimensions)
    for (int kind = 0; kind < 5; ++kind)
      for (bool normalize : {false, true}) {
        float value{};
        if (dimensions == 1)
          value = noise_reference_value(w, kind, normalize);
        else if (dimensions == 2)
          value = noise_reference_value(ccl::make_float2(p.x, p.y), kind, normalize);
        else if (dimensions == 3)
          value = noise_reference_value(p, kind, normalize);
        else
          value = noise_reference_value(ccl::make_float4(p.x, p.y, p.z, w), kind, normalize);
        if (!first)
          out << ',';
        first = false;
        out << "{\"dimensions\":\"" << dimensions << "D\",\"type\":\"" << kinds[kind]
            << "\",\"normalize\":" << (normalize ? "true" : "false") << ",\"fac\":" << value << '}';
      }
  out << "]}";
  out.flush();
  if (!out)
    throw std::runtime_error("Failed to write direct noise reference");
}
} // namespace cycles_houdini::harness
