// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <array>
#include <memory>
#include <string>
#include <vector>

namespace cycles_houdini {
// Immutable raw-intensity aperture resource. Both mappings sample the same
// normalized bilinear density, so changing the method never rebuilds the image.
struct ImageIris {
  std::string filename, revision;
  std::vector<float> table;
  int width = 0, height = 0;
  double preparation_seconds = 0;
};
using ImageIrisPtr = std::shared_ptr<const ImageIris>;
bool image_iris_available() noexcept;
// Missing files have a stable revision too, allowing an IPR repair to be seen.
std::string image_iris_revision(const std::string &filename);
ImageIrisPtr prepare_image_iris(const std::string &filename, const ImageIrisPtr &previous = {});
// Diagnostic entry point using exactly the device sampler. Returns lens UV [0,1].
std::array<float, 2> sample_image_iris(const ImageIris &iris, int method, float u, float v);
// Exposure-compensated RGB transmission evaluated at sampled lens UV. The
// scalar importance PDF is unchanged; gray transmission always returns unity.
std::array<float, 3> image_iris_weight(const ImageIris &iris, float u, float v);
// Native diagnostic harness, linked only into ch_harness.
void image_iris_contracts(const std::string &output_root);
} // namespace cycles_houdini
