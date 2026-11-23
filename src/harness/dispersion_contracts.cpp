// SPDX-License-Identifier: Apache-2.0
#include "dispersion_contracts.h"
#include "kernel/closure/dispersion.h"
#include <array>
#include <cmath>
#include <iostream>
#include <stdexcept>

namespace cycles_houdini::harness {
void dispersion_contracts() {
  int checks = 0;
  const auto require = [&](bool ok, const char *message) {
    ++checks;
    if (!ok)
      throw std::runtime_error(message);
  };
  // Independent spectroscopic identities: the Abbe number is (n_d-1)/(n_F-n_C).
  for (float nd : {1.05f, 1.333f, 1.5f, 1.8f, 2.4f})
    for (float abbe : {5.f, 20.f, 64.f}) {
      const auto n = [&](float wavelength) { return ccl::bsdf_cauchy_ior(nd, 1.f / abbe, wavelength); };
      require(std::abs(n(.5876f) - nd) < 3e-7f, "Cauchy medium lost its specified d-line IOR");
      require(std::abs((n(.4861f) - n(.6563f)) - (nd - 1.f) / abbe) < 4e-7f, "Cauchy medium violates its Abbe number");
      require(n(.4861f) > n(.6563f), "Normal dispersion reversed its spectral order");
    }
  bool ratio_is_distinct = false;
  for (float lambda : {.40f, .4861f, .5876f, .6563f, .75f}) {
    const float outer = ccl::bsdf_cauchy_ior(1.333f, 1.f / 64.f, lambda);
    const float inner = ccl::bsdf_cauchy_ior(1.60f, 1.f / 20.f, lambda);
    const float ratio = inner / outer;
    require(std::abs(ratio * (outer / inner) - 1.f) < 2e-7f, "Two-medium relative IOR is not reciprocal");
    // Snell's law over successive parallel boundaries telescopes through the absolute media.
    const float sin_air = .45f;
    const float sin_outer = sin_air / outer;
    const float sin_inner = sin_outer / ratio;
    require(std::abs(sin_inner - sin_air / inner) < 2e-7f, "Two-medium Snell transport uses inconsistent wavelengths");
    require(inner / inner == 1.f, "Identical dispersive media introduce an optical interface");
    const float encoded = ccl::nested_ior_encode(outer, true);
    require(encoded < 0.f && std::abs(encoded) == outer,
            "Nested entry did not retain absolute spectral IOR and marker");
    require(ccl::nested_ior_encode(outer, false) == outer, "Non-dispersive nested entry changed");
    const float wrong = ccl::bsdf_cauchy_ior(1.60f / 1.333f, 1.f / 20.f, lambda);
    ratio_is_distinct |= std::abs(ratio - wrong) > 1e-4f;
  }
  require(ratio_is_distinct, "Two-media fixture does not distinguish dispersing a ratio from taking a ratio");
  std::cout << "{\"result\":\"passed\",\"check\":\"Absolute spectral IOR and two-medium Snell law\",\"checks\":"
            << checks << "}\n";
}
} // namespace cycles_houdini::harness
