// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/session.h"
#include <cstring>
#include <limits>
#include <unordered_set>
#include <string_view>
#include <cmath>

namespace cycles_houdini {
namespace {
std::size_t count(int width, int height, int channels) {
  if (width < 1 || height < 1 || channels < 1 || channels > 4)
    throw std::invalid_argument("Invalid image storage dimensions/channels");
  if (std::uint64_t(width) * height > 512ull * 1024 * 1024)
    throw std::invalid_argument("Image coverage exceeds allocation limit");
  const auto row = std::size_t(width) * channels;
  if (row > std::numeric_limits<std::size_t>::max() / sizeof(float) / std::size_t(height))
    throw std::invalid_argument("Image storage size overflows");
  return row * height;
}
} // namespace
std::pair<int, int> image_plane_size(const FrameEvent &frame, const ImagePlane &plane) {
  const bool implicit = plane.width == 0 && plane.height == 0;
  const int width = implicit ? frame.width : plane.width, height = implicit ? frame.height : plane.height;
  const auto expected = count(width, height, plane.channels);
  if (width > frame.width || height > frame.height || plane.pixels.size() != expected)
    throw std::invalid_argument("Image plane storage does not match its logical coverage: " + plane.pass);
  if (frame.complete && (width != frame.width || height != frame.height))
    throw std::invalid_argument("Completed image contains a compact preview plane: " + plane.pass);
  return {width, height};
}
void validate_frame_image(const FrameEvent &frame) {
  count(frame.full_width, frame.full_height, 4);
  if (frame.width < 1 || frame.height < 1 || frame.full_width < 1 || frame.full_height < 1 || frame.x < 0 ||
      frame.y < 0 || frame.width > frame.full_width || frame.height > frame.full_height ||
      frame.x > frame.full_width - frame.width || frame.y > frame.full_height - frame.height)
    throw std::invalid_argument("Invalid image coverage");
  std::unordered_set<std::string_view> names;
  for (const auto &plane : frame.planes) {
    if (plane.pass.empty() || !names.insert(plane.pass).second)
      throw std::invalid_argument("Missing or duplicate image pass identity");
    image_plane_size(frame, plane);
  }
}
void copy_image_plane_rows(const FrameEvent &frame, const ImagePlane &plane, int first_row, int rows, float *target) {
  const auto [width, height] = image_plane_size(frame, plane);
  if (first_row < 0 || rows < 0 || rows > frame.height || first_row > frame.height - rows || (rows && !target))
    throw std::invalid_argument("Invalid image row request");
  if (rows == 0)
    return;
  if (width == frame.width && height == frame.height) {
    std::memcpy(target, plane.pixels.data() + std::size_t(first_row) * width * plane.channels,
                std::size_t(rows) * width * plane.channels * sizeof(float));
    return;
  }
  std::vector<std::size_t> columns(frame.width);
  for (int x = 0; x < frame.width; ++x)
    columns[x] = std::size_t(x) * width / frame.width * plane.channels;
  std::size_t previous_y = std::numeric_limits<std::size_t>::max();
  const auto row_values = std::size_t(frame.width) * plane.channels;
  for (int row = 0; row < rows; ++row) {
    const auto source_y = std::size_t(first_row + row) * height / frame.height;
    if (source_y == previous_y) {
      std::memcpy(target + std::size_t(row) * row_values, target + std::size_t(row - 1) * row_values,
                  row_values * sizeof(float));
      continue;
    }
    previous_y = source_y;
    for (int x = 0; x < frame.width; ++x) {
      const auto source = source_y * width * plane.channels + columns[x];
      const auto destination = (std::size_t(row) * frame.width + x) * plane.channels;
      // Preserve float ID bit patterns; no interpolation or arithmetic.
      std::memcpy(target + destination, plane.pixels.data() + source, plane.channels * sizeof(float));
    }
  }
}
std::vector<float> expand_image_plane(const FrameEvent &frame, const ImagePlane &plane) {
  std::vector<float> result(count(frame.width, frame.height, plane.channels));
  copy_image_plane_rows(frame, plane, 0, frame.height, result.data());
  return result;
}
FocusSample sample_focus(const FrameEvent &frame, int x, int y) {
  if (x < frame.x || y < frame.y || x - frame.x >= frame.width || y - frame.y >= frame.height)
    throw std::invalid_argument("Click inside the rendered region");
  const ImagePlane *position = nullptr, *depth = nullptr;
  for (const auto &plane : frame.planes) {
    if (plane.pass == "position")
      position = &plane;
    if (plane.pass == "depth")
      depth = &plane;
  }
  if (!position || !depth)
    throw std::invalid_argument("Waiting for focus-picking passes");
  if (position->channels != 3 || depth->channels != 1)
    throw std::invalid_argument("Invalid focus-picking pass channels");
  const auto size = image_plane_size(frame, *position);
  if (size != image_plane_size(frame, *depth))
    throw std::invalid_argument("Waiting for matching focus-picking passes");
  const auto [width, height] = size;
  const auto index =
      std::size_t(y - frame.y) * height / frame.height * width + std::size_t(x - frame.x) * width / frame.width;
  const float z = depth->pixels[index];
  FocusSample result{{position->pixels[index * 3], position->pixels[index * 3 + 1], position->pixels[index * 3 + 2]},
                     z};
  if (!std::isfinite(z) || z <= 0 || z >= 1e10f || !std::isfinite(result.position.x) ||
      !std::isfinite(result.position.y) || !std::isfinite(result.position.z))
    throw std::invalid_argument("No rendered surface at this pixel");
  return result;
}
std::vector<std::uint8_t> expand_display_pixels(std::span<const std::uint8_t> pixels, int source_width,
                                                int source_height, int width, int height) {
  const auto input = count(source_width, source_height, 4), output = count(width, height, 4);
  if (pixels.size() != input || source_width > width || source_height > height)
    throw std::invalid_argument("Invalid compact display storage");
  std::vector<std::uint8_t> result(output);
  if (source_width == width && source_height == height) {
    std::memcpy(result.data(), pixels.data(), output);
    return result;
  }
  std::vector<std::size_t> columns(width);
  for (int x = 0; x < width; ++x)
    columns[x] = std::size_t(x) * source_width / width * 4;
  std::size_t previous_y = std::numeric_limits<std::size_t>::max();
  for (int y = 0; y < height; ++y) {
    const auto source_y = source_height - 1 - std::size_t(height - 1 - y) * source_height / height;
    if (source_y == previous_y) {
      std::memcpy(result.data() + std::size_t(y) * width * 4, result.data() + std::size_t(y - 1) * width * 4,
                  std::size_t(width) * 4);
      continue;
    }
    previous_y = source_y;
    for (int x = 0; x < width; ++x) {
      const auto source = source_y * source_width * 4 + columns[x];
      const auto destination = (std::size_t(y) * width + x) * 4;
      std::memcpy(result.data() + destination, pixels.data() + source, 4);
    }
  }
  return result;
}
} // namespace cycles_houdini
