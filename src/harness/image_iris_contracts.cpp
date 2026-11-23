// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/image_iris.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <stdexcept>
#ifdef WITH_IMAGE_IRIS
#include <OpenImageIO/imageio.h>
#endif

namespace cycles_houdini {
namespace {
void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
void pgm(const std::filesystem::path &path, int width, int height, const std::vector<unsigned char> &image) {
  std::ofstream file(path, std::ios::binary);
  file << "P5\n" << width << ' ' << height << "\n255\n";
  file.write(reinterpret_cast<const char *>(image.data()), std::streamsize(image.size()));
  require(bool(file), "Cannot write image iris contract fixture");
}
std::uint32_t random_bits(std::uint64_t &state) {
  state = state * 6364136223846793005ull + 1442695040888963407ull;
  const auto value = std::uint32_t(((state >> 18) ^ state) >> 27);
  const unsigned rotation = unsigned(state >> 59);
  return (value >> rotation) | (value << ((-rotation) & 31));
}
float uniform(std::uint64_t &state) { return float((double(random_bits(state)) + 0.5) / 4294967296.0); }
} // namespace

void image_iris_contracts(const std::string &output_root) {
  const auto root = std::filesystem::u8path(output_root);
  std::filesystem::create_directories(root);
  const auto filename = (root / "asymmetric-iris.pgm").string();
  constexpr int width = 65, height = 33, cells_x = width - 1, cells_y = height - 1;
  std::vector<unsigned char> pixels(width * height);
  for (int y = 0; y < height; ++y)
    for (int x = 0; x < width; ++x) {
      const double dx = (x - 20.) / 16., dy = (y - 14.) / 10.;
      const bool ring = dx * dx + dy * dy < 1 && dx * dx + dy * dy > 0.18;
      const bool island = x > 46 && x < 61 && y > 4 && y < 20;
      pixels[y * width + x] = (ring || island) ? static_cast<unsigned char>(30 + 210. * x / (width - 1)) : 0;
    }
  pgm(filename, width, height, pixels);
  require(!prepare_image_iris(""), "Empty iris filename must select analytic camera");
  if (!image_iris_available()) {
    bool rejected = false;
    try {
      prepare_image_iris(filename);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected, "Feature-disabled build silently accepted an image iris");
    std::cout << "{\"result\":\"passed\",\"enabled\":false,\"unsupported_image_rejected\":true}\n";
    return;
  }
  const auto resource = prepare_image_iris(filename);
  require(resource == prepare_image_iris(filename, resource), "Unchanged iris was not reused");
#ifdef WITH_IMAGE_IRIS
  // Straight RGBA must multiply alpha once, including readers that associate
  // RGB unless explicitly asked to retain unassociated alpha.
  const auto rgba_filename = (root / "straight-alpha-iris.png").string();
  auto rgba_output = OIIO::ImageOutput::create(rgba_filename);
  OIIO::ImageSpec rgba_spec(2, 2, 4, OIIO::TypeDesc::UINT8);
  rgba_spec.alpha_channel = 3;
  rgba_spec.attribute("oiio:UnassociatedAlpha", 1);
  const std::array<unsigned char, 16> rgba{255, 255, 255, 64,  255, 255, 255, 128,
                                           255, 255, 255, 192, 255, 255, 255, 255};
  require(rgba_output && rgba_output->open(rgba_filename, rgba_spec) &&
              rgba_output->write_image(OIIO::TypeDesc::UINT8, rgba.data()) && rgba_output->close(),
          "Cannot write straight-alpha iris fixture");
  const auto alpha_resource = prepare_image_iris(rgba_filename);
  for (int i = 0; i < 4; ++i)
    require(std::abs(alpha_resource->table[8 + i] - rgba[i * 4 + 3] / 255.f) < 0.008,
            "Image iris multiplied straight alpha more than once");
  for (float u : {0.f, 0.37f, 1.f})
    for (float v : {0.f, 0.61f, 1.f}) {
      const auto weight = image_iris_weight(*alpha_resource, u, v);
      for (float channel : weight)
        require(std::abs(channel - 1.f) < 0.000001, "Neutral RGBA iris changed camera white balance");
      const auto gray_weight = image_iris_weight(*resource, u, v);
      for (float channel : gray_weight)
        require(channel == 1.f, "Grayscale iris changed unit ray weight");
    }
  auto write_float_image = [&](const std::string &name, int w, int h, int channels, const std::vector<float> &data) {
    auto output = OIIO::ImageOutput::create(name);
    OIIO::ImageSpec spec(w, h, channels, OIIO::TypeDesc::FLOAT);
    if (channels == 4)
      spec.alpha_channel = 3;
    spec.attribute("oiio:UnassociatedAlpha", 1);
    require(output && output->open(name, spec) && output->write_image(OIIO::TypeDesc::FLOAT, data.data()) &&
                output->close(),
            "Cannot write colored image iris fixture");
  };
  // A colored physical filter has one exposure gain, not an independent gain
  // per channel: red transmission stays red, including zero-density endpoints.
  const auto red_filename = (root / "pure-red-iris.tif").string();
  const std::vector<float> red{0, 0, 0, 0.2f, 0, 0, 0.7f, 0, 0, 1, 0, 0};
  write_float_image(red_filename, 2, 2, 3, red);
  const auto red_resource = prepare_image_iris(red_filename);
  for (float u : {0.f, 0.00001f, 0.37f, 1.f})
    for (float v : {0.f, 0.61f, 1.f}) {
      const auto weight = image_iris_weight(*red_resource, u, v);
      require(std::isfinite(weight[0]) && std::abs(weight[0] - 1.f / 0.2126f) < 0.00001 && weight[1] == 0 &&
                  weight[2] == 0,
              "Colored iris endpoint lost filter tint or introduced an absent channel");
    }
  constexpr int color_width = 17, color_height = 13;
  std::vector<float> colored(color_width * color_height * 4);
  for (int y = 0; y < color_height; ++y)
    for (int x = 0; x < color_width; ++x) {
      const float u = float(x) / (color_width - 1), v = float(y) / (color_height - 1);
      const int p = 4 * (y * color_width + x);
      colored[p] = u * u;
      colored[p + 1] = 0.05f + 0.8f * v;
      colored[p + 2] = (1.f - u) * (1.f - v) * 0.7f;
      colored[p + 3] = 0.15f + 0.85f * u * v;
    }
  const auto color_filename = (root / "colored-alpha-iris.tif").string();
  write_float_image(color_filename, color_width, color_height, 4, colored);
  const auto color_resource = prepare_image_iris(color_filename);
  std::array<double, 3> integrated_color{};
  double integrated_luminance = 0;
  for (int y = 0; y < color_height - 1; ++y)
    for (int x = 0; x < color_width - 1; ++x) {
      for (int dy = 0; dy < 2; ++dy)
        for (int dx = 0; dx < 2; ++dx) {
          const int p = 4 * ((y + dy) * color_width + x + dx);
          for (int c = 0; c < 3; ++c)
            integrated_color[c] += double(colored[p + c]) * colored[p + 3] * 0.25;
        }
    }
  integrated_luminance = 0.2126 * integrated_color[0] + 0.7152 * integrated_color[1] + 0.0722 * integrated_color[2];
  std::array<double, 3> target_color{};
  for (int c = 0; c < 3; ++c)
    target_color[c] = integrated_color[c] / integrated_luminance;
  // Independent closed-form RGB integration checks the complete estimator,
  // including a spatially varying alpha and both sampling distributions.
  for (int method = 1; method <= 2; ++method) {
    std::array<double, 3> mean{};
    std::uint64_t state = 0x52b31eab;
    constexpr int color_samples = 262144;
    for (int i = 0; i < color_samples; ++i) {
      const float u = uniform(state), v = uniform(state);
      const auto uv = sample_image_iris(*color_resource, method, u, v);
      const auto weight = image_iris_weight(*color_resource, uv[0], uv[1]);
      const double luminance = 0.2126 * weight[0] + 0.7152 * weight[1] + 0.0722 * weight[2];
      require(std::abs(luminance - 1.) < 0.000002, "Colored iris changed the common luminance normalization");
      for (int c = 0; c < 3; ++c) {
        require(std::isfinite(weight[c]) && weight[c] >= 0,
                "Colored iris generated non-finite or negative camera weight");
        mean[c] += weight[c];
      }
    }
    for (int c = 0; c < 3; ++c)
      require(std::abs(mean[c] / color_samples - target_color[c]) < 0.008,
              "Colored iris RGB mean disagrees with independent transmission integral");
  }
  // Direct vertex checks ensure alpha is included once in RGB and scalar
  // transmission, rather than being applied again after dividing by the PDF.
  for (int y = 0; y < color_height; ++y)
    for (int x = 0; x < color_width; ++x) {
      const int p = 4 * (y * color_width + x);
      const double luminance = 0.2126 * colored[p] + 0.7152 * colored[p + 1] + 0.0722 * colored[p + 2];
      const auto weight =
          image_iris_weight(*color_resource, float(x) / (color_width - 1), float(y) / (color_height - 1));
      for (int c = 0; c < 3; ++c)
        require(std::abs(weight[c] - colored[p + c] / luminance) < 0.00001,
                "Image iris applied alpha twice to colored camera weight");
    }
#endif
  std::array<double, 32> expected{};
  double total = 0, expected_x = 0, expected_y = 0;
  // Independent exact integration of each source bilinear cell, not the packed
  // sampler's CDF/hierarchy, including its first moments and 8x4 bin masses.
  for (int y = 0; y < cells_y; ++y)
    for (int x = 0; x < cells_x; ++x) {
      const double a = pixels[y * width + x], b = pixels[y * width + x + 1];
      const double c = pixels[(y + 1) * width + x], d = pixels[(y + 1) * width + x + 1];
      const double mass = (a + b + c + d) * 0.25;
      const double local_x = (a + c + 2 * b + 2 * d) / 12.;
      const double local_y = (a + b + 2 * c + 2 * d) / 12.;
      total += mass;
      expected[(y / 8) * 8 + x / 8] += mass;
      expected_x += (x * mass + local_x) / cells_x;
      expected_y += (y * mass + local_y) / cells_y;
    }
  expected_x /= total;
  expected_y /= total;
  for (auto &value : expected)
    value /= total;
  constexpr int samples = 524288;
  std::array<double, 2> elapsed{}, chisquare{}, mean_x{}, mean_y{};
  for (int method = 1; method <= 2; ++method) {
    std::array<int, 32> bins{};
    std::uint64_t state = 0x4d595df4d0f33173ull;
    std::ofstream points(root / (method == 1 ? "cdf-samples.csv" : "hierarchy-samples.csv"));
    points << "x,y\n";
    const auto begin = std::chrono::steady_clock::now();
    for (int sample = 0; sample < samples; ++sample) {
      const float u = uniform(state), v = uniform(state);
      const auto p = sample_image_iris(*resource, method, u, v);
      require(std::isfinite(p[0]) && std::isfinite(p[1]) && p[0] >= 0 && p[0] <= 1 && p[1] >= 0 && p[1] <= 1,
              "Iris sampler generated an invalid coordinate");
      ++bins[std::min(3, int(p[1] * 4)) * 8 + std::min(7, int(p[0] * 8))];
      mean_x[method - 1] += p[0];
      mean_y[method - 1] += p[1];
      if (sample < 8192)
        points << p[0] << ',' << p[1] << '\n';
    }
    elapsed[method - 1] = std::chrono::duration<double>(std::chrono::steady_clock::now() - begin).count();
    mean_x[method - 1] /= samples;
    mean_y[method - 1] /= samples;
    for (int bin = 0; bin < 32; ++bin) {
      const double count = expected[bin] * samples;
      if (count > 0)
        chisquare[method - 1] += std::pow(bins[bin] - count, 2) / count;
      else
        require(bins[bin] == 0, "Iris sampler entered a zero-density region");
    }
    require(chisquare[method - 1] < 95, "Iris bilinear cell frequencies disagree with independent target");
    require(std::abs(mean_x[method - 1] - expected_x) < 0.003 && std::abs(mean_y[method - 1] - expected_y) < 0.003,
            "Iris bilinear first moments disagree with independent target");
    for (float u : {0.f, 1.f})
      for (float v : {0.f, 1.f}) {
        const auto p = sample_image_iris(*resource, method, u, v);
        require(std::isfinite(p[0]) && std::isfinite(p[1]) && p[0] >= 0 && p[0] <= 1 && p[1] >= 0 && p[1] <= 1,
                "Iris endpoint diagnostic produced NaN or escaped support");
      }
  }
  const auto old_revision = resource->revision;
  pgm(filename, width, height, std::vector<unsigned char>(width * height, 0));
  bool rejected = false;
  try {
    prepare_image_iris(filename, resource);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected && resource->revision == old_revision,
          "Invalid iris replacement modified accepted immutable resource");
  pixels.assign(width * height, 128);
  pgm(filename, width, height, pixels);
  const auto repaired = prepare_image_iris(filename, resource);
  require(repaired != resource && repaired->revision != old_revision, "Iris repair did not replace the resource");
  std::filesystem::remove(std::filesystem::u8path(filename));
  rejected = false;
  try {
    prepare_image_iris(filename, repaired);
  } catch (const std::invalid_argument &) {
    rejected = true;
  }
  require(rejected, "Missing image iris was accepted");
  // A normalized scalar aperture returns unit ray throughput for every sample.
  // Its Monte Carlo estimate of constant radiance is exactly the same constant.
  const double constant_radiance = 0.731;
  for (int method = 1; method <= 2; ++method) {
    double radiance = 0;
    std::uint64_t state = 71;
    for (int i = 0; i < 4096; ++i) {
      sample_image_iris(*repaired, method, uniform(state), uniform(state));
      radiance += constant_radiance;
    }
    require(std::abs(radiance / 4096 - constant_radiance) < 1e-12, "Iris mapping changed constant-field brightness");
  }
  std::cout
      << "{\"result\":\"passed\",\"enabled\":true,\"samples_per_method\":" << samples
      << ",\"cdf_chi_square\":" << chisquare[0] << ",\"hierarchy_chi_square\":" << chisquare[1]
      << ",\"cdf_seconds\":" << elapsed[0] << ",\"hierarchy_seconds\":" << elapsed[1]
      << ",\"preparation_seconds\":" << resource->preparation_seconds
      << ",\"table_bytes\":" << resource->table.size() * sizeof(float)
      << ",\"constant_field_brightness\":true,\"replacement_and_repair\":true,\"colored_transmission_integral\":true,"
         "\"colored_alpha_once\":true,\"neutral_color_unit_weight\":true,\"colored_endpoints_finite\":true}\n";
}
} // namespace cycles_houdini
