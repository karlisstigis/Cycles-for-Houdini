// SPDX-License-Identifier: Apache-2.0
#include "../core/display_image.h"
#include "session/output_driver.h"
#include <algorithm>
#include <cmath>
#include <iostream>
#include <stdexcept>
using namespace cycles_houdini;
namespace {
void require(bool value) {
  if (!value)
    throw std::runtime_error("Frame publication contract failed");
}
std::shared_ptr<FrameEvent> frame(std::uint64_t generation, bool complete = false) {
  auto result = std::make_shared<FrameEvent>();
  result->generation = generation;
  result->width = result->height = result->full_width = result->full_height = 1;
  result->rendered_width = result->rendered_height = 1;
  result->complete = complete;
  result->planes.push_back({"combined", 4, {1, 2, 3, 1}, 1, 1});
  return result;
}
// Independent normalized pass fixture: recover raw per-pixel counts even
// when temporal progress has advanced beyond the current accumulation buffer.
class SampleTile : public ccl::OutputDriver::Tile {
public:
  SampleTile(int total, int current, std::vector<float> raw)
      : Tile(ccl::make_int2(0, 0), ccl::make_int2(4, 1), ccl::make_int2(4, 1), "", ""), total_(total),
        current_(current), raw_(std::move(raw)) {}
  int get_num_samples() const override { return total_; }
  int get_num_buffer_samples() const override { return current_; }
  bool get_pass_pixels(ccl::string_view name, int channels, float *pixels) const override {
    if (name != "sample_count" || channels != 1)
      return false;
    std::transform(raw_.begin(), raw_.end(), pixels, [&](float n) { return n / current_; });
    return true;
  }
  bool set_pass_pixels(ccl::string_view, int, const float *) const override { return false; }

private:
  int total_, current_;
  std::vector<float> raw_;
};

class LegacySampleTile : public ccl::OutputDriver::Tile {
public:
  LegacySampleTile() : Tile(ccl::make_int2(0, 0), ccl::make_int2(4, 1), ccl::make_int2(4, 1), "", "") {}
  int get_num_samples() const override { return 8; }
  bool get_pass_pixels(ccl::string_view name, int channels, float *pixels) const override {
    if (name != "sample_count" || channels != 1)
      return false;
    const float normalized[] = {1, 0.5f, 0.25f, 0};
    std::copy(std::begin(normalized), std::end(normalized), pixels);
    return true;
  }
  bool set_pass_pixels(ccl::string_view, int, const float *) const override { return false; }
};

void sample_count_contracts() {
  auto store = std::make_shared<FrameStore>();
  store->generation = 1;
  store->full_width = 4;
  store->full_height = 1;
  store->preview_pass = "sample_count";
  store->denoiser = "dlss";
  store->passes.push_back({"sample_count", {"X"}, Storage::Float32, true, false, false});
  auto output = make_buffer_output(store);
  auto check = [&](const ccl::OutputDriver::Tile &tile, const std::vector<float> &expected, bool complete) {
    if (complete)
      output->write_render_tile(tile);
    else
      require(output->update_render_tile(tile));
    require(store->error.empty() && store->latest && store->latest->samples == tile.get_num_samples());
    require(store->latest->generation == store->generation && store->latest->complete == complete);
    const auto &actual = store->latest->planes.at(0).pixels;
    require(actual.size() == expected.size());
    for (size_t i = 0; i < actual.size(); ++i)
      require(std::abs(actual[i] - expected[i]) < 1e-6f);
  };
  check(SampleTile(1, 1, {1, 1, 1, 1}), {1, 1, 1, 1}, false);
  check(SampleTile(3, 2, {2, 1, 0, 2}), {2, 1, 0, 2}, false);
  check(SampleTile(7, 4, {4, 2, 1, 0}), {4, 2, 1, 0}, false);
  check(SampleTile(7, 4, {4, 2, 1, 0}), {4, 2, 1, 0}, true);
  // New generations clear progress and completion, retaining ordinary counts.
  ++store->generation;
  store->complete = false;
  check(SampleTile(1, 1, {1, 1, 1, 1}), {1, 1, 1, 1}, false);
  check(LegacySampleTile(), {8, 4, 2, 0}, true);
}
} // namespace
int main() {
  try {
    sample_count_contracts();
    FrameStore store;
    store.generation = 7;
    store.pixel_aspect = 1.5f;
    store.color_epoch = 13;
    store.publish(frame(6));
    require(!store.latest && store.sequence == 0);
    store.publish(frame(7));
    require(store.latest && store.latest->sequence == 1 && store.latest->pixel_aspect == 1.5f);
    require(store.latest->color_epoch == 13 && !store.complete);
    store.publish(frame(7, true));
    const auto complete = store.latest;
    require(store.complete && complete->sequence == 2);
    store.publish(frame(7));
    require(store.latest == complete && store.sequence == 2);
    store.focus_enabled = true;
    store.publish_focus(frame(7));
    require(store.focus && store.focus->sequence == 3 && store.latest == complete);
    const auto focus = store.focus;
    store.interrupted = true;
    store.publish(frame(7, true));
    store.publish_focus(frame(7));
    require(store.sequence == 3 && store.latest == complete && store.focus == focus);
    store.fail("callback failure");
    require(store.error == "callback failure");
    std::cout << "Frame publication contracts passed\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
