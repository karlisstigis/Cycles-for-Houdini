// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/image_iris.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <stdexcept>
#ifdef WITH_IMAGE_IRIS
#include <OpenImageIO/imageio.h>
#include "util/math.h"
#include "kernel/camera/image_iris.h"
#endif

namespace cycles_houdini {
bool image_iris_available() noexcept {
#ifdef WITH_IMAGE_IRIS
  return true;
#else
  return false;
#endif
}

std::string image_iris_revision(const std::string &filename) {
  if (filename.empty())
    return {};
  std::error_code error;
  const auto path = std::filesystem::u8path(filename);
  const auto size = std::filesystem::file_size(path, error);
  if (error)
    return filename + ":missing";
  const auto time = std::filesystem::last_write_time(path, error);
  if (error)
    return filename + ":unreadable";
  return filename + ":" + std::to_string(size) + ":" + std::to_string(time.time_since_epoch().count());
}

ImageIrisPtr prepare_image_iris(const std::string &filename, const ImageIrisPtr &previous) {
  if (filename.empty())
    return {};
#ifndef WITH_IMAGE_IRIS
  throw std::invalid_argument("Image iris is not enabled in this build");
#else
  const auto started = std::chrono::steady_clock::now();
  const auto revision = image_iris_revision(filename);
  if (previous && previous->filename == filename && previous->revision == revision)
    return previous;
  OIIO::ImageSpec config;
  // PNG and other unassociated-alpha readers normally return associated RGB.
  // Request the original straight values so transmission alpha is applied once.
  config.attribute("oiio:UnassociatedAlpha", 1);
  auto input = OIIO::ImageInput::open(filename, &config);
  if (!input)
    throw std::invalid_argument("Cannot open image iris: " + filename + ": " + OIIO::geterror());
  const auto spec = input->spec();
  if (spec.width < 2 || spec.height < 2 || spec.width > 2048 || spec.height > 2048 || spec.depth != 1 ||
      spec.nchannels < 1 || spec.nchannels > 4 || spec.deep)
    throw std::invalid_argument("Image iris requires a flat 2D image, 2..2048 pixels per axis, with 1..4 channels: " +
                                filename);
  std::vector<float> pixels(std::size_t(spec.width) * spec.height * spec.nchannels);
  if (!input->read_image(0, 0, 0, spec.nchannels, OIIO::TypeDesc::FLOAT, pixels.data()))
    throw std::invalid_argument("Cannot decode image iris: " + filename + ": " + input->geterror());
  input->close();
  if (image_iris_revision(filename) != revision)
    throw std::invalid_argument("Image iris changed while being read; retry after saving: " + filename);
  auto result = std::make_shared<ImageIris>();
  result->filename = filename;
  result->revision = revision;
  result->width = spec.width;
  result->height = spec.height;
  const int width = spec.width, height = spec.height, cells_x = width - 1, cells_y = height - 1;
  std::vector<double> intensity(std::size_t(width) * height);
  // Retain RGB even while color is disabled. A live color toggle changes only
  // camera throughput, never the immutable distribution or its device upload.
  std::vector<std::array<double, 3>> transmission;
  if (spec.nchannels >= 3)
    transmission.resize(intensity.size());
  double maximum = 0;
  for (std::size_t i = 0; i < intensity.size(); ++i) {
    const float *p = pixels.data() + i * spec.nchannels;
    for (int c = 0; c < spec.nchannels; ++c)
      if (!std::isfinite(p[c]) || p[c] < 0)
        throw std::invalid_argument("Image iris contains negative or non-finite intensity: " + filename);
    // Raw linear luminance; no texture/display colorspace conversion. Alpha
    // multiplies transmission when present. Gray+alpha uses its gray channel.
    const double alpha = spec.alpha_channel >= 0 && spec.alpha_channel < spec.nchannels ? p[spec.alpha_channel] : 1.;
    double value = spec.nchannels >= 3 ? 0.2126 * double(p[0]) + 0.7152 * double(p[1]) + 0.0722 * double(p[2]) : p[0];
    value *= alpha;
    if (!transmission.empty())
      for (int c = 0; c < 3; ++c)
        transmission[i][c] = double(p[c]) * alpha;
    intensity[i] = value;
    maximum = std::max(maximum, value);
  }
  if (!(maximum > 0))
    throw std::invalid_argument("Image iris has zero transmission: " + filename);
  auto &table = result->table;
  table.resize(8, 0);
  table[0] = float(width);
  table[1] = float(height);
  table[4] = float(table.size());
  for (const double value : intensity)
    table.push_back(float(value / maximum));
  std::vector<double> mass(std::size_t(cells_x) * cells_y), rows(cells_y, 0);
  for (int y = 0; y < cells_y; ++y)
    for (int x = 0; x < cells_x; ++x) {
      const std::size_t p = 8 + std::size_t(y) * width + x;
      const double value = (double(table[p]) + table[p + 1] + table[p + width] + table[p + width + 1]) * 0.25;
      mass[std::size_t(y) * cells_x + x] = value;
      rows[y] += value;
    }
  table[5] = float(table.size());
  for (int y = 0; y < cells_y; ++y) {
    table.push_back(0);
    double sum = 0;
    for (int x = 0; x < cells_x; ++x) {
      sum += mass[std::size_t(y) * cells_x + x];
      table.push_back(rows[y] > 0 ? float(sum / rows[y]) : float(x + 1) / cells_x);
    }
    table.back() = 1;
  }
  table[6] = float(table.size());
  table.push_back(0);
  double total = 0;
  for (const double row : rows)
    total += row;
  double sum = 0;
  for (const double row : rows) {
    sum += row;
    table.push_back(float(sum / total));
  }
  table.back() = 1;
  int side = 1;
  while (side < cells_x || side < cells_y)
    side *= 2;
  table[2] = float(side);
  table[7] = float(table.size());
  std::vector<std::vector<float>> levels;
  levels.emplace_back(std::size_t(side) * side, 0);
  for (int y = 0; y < cells_y; ++y)
    for (int x = 0; x < cells_x; ++x)
      levels.back()[std::size_t(y) * side + x] = float(mass[std::size_t(y) * cells_x + x]);
  for (int width_level = side; width_level > 1; width_level /= 2) {
    const auto &fine = levels.back();
    const int coarse_width = width_level / 2;
    std::vector<float> coarse(std::size_t(coarse_width) * coarse_width);
    for (int y = 0; y < coarse_width; ++y)
      for (int x = 0; x < coarse_width; ++x) {
        const std::size_t p = std::size_t(y * 2) * width_level + x * 2;
        coarse[std::size_t(y) * coarse_width + x] =
            float(double(fine[p]) + fine[p + 1] + fine[p + width_level] + fine[p + width_level + 1]);
      }
    levels.push_back(std::move(coarse));
  }
  for (auto level = levels.rbegin(); level != levels.rend(); ++level)
    table.insert(table.end(), level->begin(), level->end());
  if (!transmission.empty()) {
    // Only this start offset is stored in float. It remains exactly
    // representable for the supported 2048-square resource limit; subsequent
    // RGB addressing uses integer arithmetic even beyond 2^24 entries.
    if (table.size() > 16777216)
      throw std::invalid_argument("Image iris RGB table offset exceeds exact addressing: " + filename);
    table[3] = float(table.size());
    table.reserve(table.size() + transmission.size() * 3);
    for (const auto &pixel : transmission)
      for (double value : pixel)
        table.push_back(float(value / maximum));
  }
  result->preparation_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  return result;
#endif
}

std::array<float, 2> sample_image_iris(const ImageIris &iris, int method, float u, float v) {
#ifndef WITH_IMAGE_IRIS
  throw std::invalid_argument("Image iris is not enabled in this build");
#else
  if ((method != 1 && method != 2) || iris.table.size() < 8 || !std::isfinite(u) || !std::isfinite(v))
    throw std::invalid_argument("Invalid image iris diagnostic sample");
  struct Table {
    const float *data;
    float operator()(int index) const { return data[index]; }
  };
  const auto uv = ccl::image_iris_sample(Table{iris.table.data()}, method, ccl::make_float2(u, v));
  return {uv.x, uv.y};
#endif
}

std::array<float, 3> image_iris_weight(const ImageIris &iris, float u, float v) {
#ifndef WITH_IMAGE_IRIS
  throw std::invalid_argument("Image iris is not enabled in this build");
#else
  if (iris.table.size() < 8 || !std::isfinite(u) || !std::isfinite(v))
    throw std::invalid_argument("Invalid image iris diagnostic weight");
  struct Table {
    const float *data;
    float operator()(int index) const { return data[index]; }
  };
  const auto rgb = ccl::image_iris_weight(Table{iris.table.data()}, ccl::make_float2(u, v));
  return {rgb.x, rgb.y, rgb.z};
#endif
}
} // namespace cycles_houdini
