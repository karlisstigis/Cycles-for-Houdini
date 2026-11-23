// SPDX-License-Identifier: Apache-2.0
#include "display_image.h"
#include "session/display_driver.h"
#include "session/output_driver.h"
#include <algorithm>
#include <stdexcept>
namespace cycles_houdini {
namespace {
using Clock = std::chrono::steady_clock;
double seconds(Clock::time_point since) { return std::chrono::duration<double>(Clock::now() - since).count(); }
} // namespace
void FrameStore::fail(const std::string &message) {
  std::lock_guard lock(mutex);
  error = message;
}
void FrameStore::publish_focus(std::shared_ptr<FrameEvent> frame) {
  std::lock_guard lock(mutex);
  if (!focus_enabled || interrupted || frame->generation != generation)
    return;
  validate_frame_image(*frame);
  frame->pixel_aspect = pixel_aspect;
  frame->color_configuration = color_configuration;
  frame->color_epoch = color_epoch;
  frame->published_seconds = seconds(started);
  frame->sequence = ++sequence;
  focus = std::move(frame);
}
void FrameStore::publish(std::shared_ptr<FrameEvent> frame) {
  std::lock_guard lock(mutex);
  // Cycles may deliver pass buffers during cancellation, including a
  // lower-resolution/incomplete GPU render. Keep the last usable image;
  // a final callback does not establish that the requested render finished.
  if (interrupted || frame->generation != generation || (complete && !frame->complete))
    return;
  validate_frame_image(*frame);
  frame->pixel_aspect = pixel_aspect;
  frame->color_configuration = color_configuration;
  frame->color_epoch = color_epoch;
  frame->requested_passes = passes;
  frame->denoiser = denoiser;
  frame->denoise_device_id = denoise_device_id;
  if (frame->complete)
    frame->output_metadata = output_metadata;
  frame->published_seconds = seconds(started);
  if (first_image_seconds == 0)
    first_image_seconds = frame->published_seconds;
  if (frame->complete)
    finished_seconds = frame->published_seconds;
  frame->sequence = ++sequence;
  complete = frame->complete;
  latest = std::move(frame);
}

namespace {
class BufferDisplay final : public ccl::DisplayDriver {
public:
  explicit BufferDisplay(std::shared_ptr<FrameStore> store) : store_(std::move(store)) {}
  void next_tile_begin() override {}
  bool update_begin(const Params &params, int width, int height) override {
    params_ = params;
    width_ = width;
    height_ = height;
    {
      std::lock_guard lock(store_->mutex);
      generation_ = store_->generation;
    }
    if (width <= 0 || height <= 0)
      return false;
    // DisplayDriver requires storage for params.size even when effective
    // progressive resolution is lower. The active subset is contiguous.
    try {
      pixels_.resize(static_cast<std::size_t>(params.size.x) * params.size.y);
    } catch (const std::exception &e) {
      store_->fail(e.what());
      return false;
    }
    return true;
  }
  ccl::half4 *map_texture_buffer() override { return pixels_.data(); }
  void unmap_texture_buffer() override {}
  void update_end() override {
    try {
      const auto began = Clock::now();
      // Progressive output already carries both float32 beauty variants in
      // this generation. Keep those planes rather than replacing them with
      // the display driver's single half-float plane.
      {
        std::lock_guard lock(store_->mutex);
        if (store_->interrupted || generation_ != store_->generation) {
          if (store_->diagnostics.enabled)
            ++store_->diagnostics.display_updates_skipped;
          return;
        }
        if (store_->denoiser != "none" && store_->latest && store_->latest->generation == generation_) {
          if (store_->diagnostics.enabled)
            ++store_->diagnostics.display_updates;
          return;
        }
      }
      auto frame = std::make_shared<FrameEvent>();
      frame->generation = generation_;
      frame->x = params_.full_offset.x;
      frame->y = params_.full_offset.y;
      frame->width = params_.size.x;
      frame->height = params_.size.y;
      frame->full_width = params_.full_size.x;
      frame->full_height = params_.full_size.y;
      frame->rendered_width = width_;
      frame->rendered_height = height_;
      ImagePlane plane{"combined", 4, {}};
      plane.width = width_;
      plane.height = height_;
      plane.pixels.resize(static_cast<std::size_t>(width_) * height_ * 4);
      for (std::size_t i = 0; i < std::size_t(width_) * height_; ++i) {
        plane.pixels[4 * i] = ccl::half_to_float(pixels_[i].x);
        plane.pixels[4 * i + 1] = ccl::half_to_float(pixels_[i].y);
        plane.pixels[4 * i + 2] = ccl::half_to_float(pixels_[i].z);
        plane.pixels[4 * i + 3] = ccl::half_to_float(pixels_[i].w);
      }
      frame->planes.push_back(std::move(plane));
      {
        std::lock_guard lock(store_->mutex);
        if (store_->diagnostics.enabled) {
          ++store_->diagnostics.display_updates;
          store_->diagnostics.display_conversion_seconds += seconds(began);
        }
      }
      store_->publish(std::move(frame));
    } catch (const std::exception &e) {
      store_->fail(e.what());
    }
  }
  void zero() override { std::fill(pixels_.begin(), pixels_.end(), ccl::half4{0, 0, 0, 0}); }
  void draw(const Params &) override {} // Host viewers consume immutable FrameEvents.
private:
  std::shared_ptr<FrameStore> store_;
  Params params_;
  int width_{}, height_{};
  std::uint64_t generation_{};
  std::vector<ccl::half4> pixels_;
};
class BufferOutput final : public ccl::OutputDriver {
public:
  explicit BufferOutput(std::shared_ptr<FrameStore> store) : store_(std::move(store)) {}
  void write_render_tile(const Tile &tile) override {
    try {
      read(tile, true);
    } catch (const std::exception &e) {
      std::lock_guard lock(store_->mutex);
      store_->error = e.what();
    }
  }
  bool update_render_tile(const Tile &tile) override {
    {
      std::lock_guard lock(store_->mutex);
      if (store_->interrupted)
        return false;
      const bool extra = store_->preview_pass != "combined" &&
                         std::any_of(store_->passes.begin(), store_->passes.end(), [&](const auto &p) {
                           return store_->preview_pass == "*" || p.id == store_->preview_pass;
                         });
      if (store_->denoiser == "none" && !extra && !store_->focus_enabled)
        return false;
      // The first selected-pass update is immediate; subsequent reads are
      // limited to 10 Hz. Cycles' scheduler can choose a slower cadence.
      if (store_->denoiser == "none" && store_->latest && store_->latest->generation == store_->generation &&
          seconds(store_->last_preview_read) < 0.1)
        return false;
    }
    try {
      return read(tile, false);
    } catch (const std::exception &e) {
      store_->fail(e.what());
      return false;
    }
  }
  bool need_display_update() const override {
    std::lock_guard lock(store_->mutex);
    if (!store_->latest || store_->latest->generation != store_->generation)
      return true;
    if (store_->denoiser != "none")
      return false;
    if (store_->preview_pass == "combined")
      return true;
    return std::none_of(store_->latest->planes.begin(), store_->latest->planes.end(),
                        [&](const auto &p) { return store_->preview_pass == "*" || p.pass == store_->preview_pass; });
  }

private:
  bool read(const Tile &tile, bool complete) {
    const auto began = Clock::now();
    std::shared_ptr<FrameEvent> frame;
    std::vector<PassDescriptor> descriptors;
    std::string preview;
    bool denoising = false, focus_enabled = false;
    std::shared_ptr<const FrameEvent> cached_focus;
    {
      std::lock_guard lock(store_->mutex);
      if (store_->interrupted) {
        if (store_->diagnostics.enabled)
          ++store_->diagnostics.output_reads_skipped;
        return false;
      }
      frame = std::make_shared<FrameEvent>();
      frame->generation = store_->generation;
      frame->x = store_->crop.x + tile.offset.x;
      frame->y = store_->crop.y + tile.offset.y;
      frame->full_width = store_->full_width;
      frame->full_height = store_->full_height;
      descriptors = store_->passes;
      preview = store_->preview_pass;
      denoising = store_->denoiser != "none";
      focus_enabled = store_->focus_enabled;
      cached_focus = store_->focus;
    }
    frame->width = tile.size.x;
    frame->height = tile.size.y;
    frame->rendered_width = tile.size.x;
    frame->rendered_height = tile.size.y;
    frame->complete = complete;
    frame->samples = tile.get_num_samples();
    const int buffer_samples = tile.get_num_buffer_samples();
    // Picking needs the two unfiltered first-hit passes once per effective
    // resolution, not a download of every enabled AOV on every sample.
    // Keep it separate so ordinary beauty updates cannot overwrite it and
    // a slower host poll cannot miss its only delivery.
    const bool has_focus_passes =
        std::any_of(descriptors.begin(), descriptors.end(), [](const auto &p) { return p.id == "position"; }) &&
        std::any_of(descriptors.begin(), descriptors.end(), [](const auto &p) { return p.id == "depth"; });
    bool focus_needed = false;
    if (focus_enabled && has_focus_passes) {
      const auto size = tile.get_pass_render_size("position");
      const auto depth_size = tile.get_pass_render_size("depth");
      focus_needed = !cached_focus || cached_focus->generation != frame->generation || cached_focus->x != frame->x ||
                     cached_focus->y != frame->y || cached_focus->width != frame->width ||
                     cached_focus->height != frame->height || cached_focus->rendered_width != size.x ||
                     cached_focus->rendered_height != size.y;
      // Different sizes may appear transiently during progressive
      // scheduling. Wait for a matching pair rather than mix pixels.
      focus_needed = focus_needed && size.x == depth_size.x && size.y == depth_size.y;
    }
    const bool extra = preview != "combined" && std::any_of(descriptors.begin(), descriptors.end(), [&](const auto &p) {
                         return preview == "*" || p.id == preview;
                       });
    if (!complete && !denoising && !extra && !focus_needed)
      return false;
    auto read_plane = [&](const PassDescriptor &pass, bool full) {
      ImagePlane plane{pass.id, static_cast<int>(pass.channels.size()), {}};
      const auto pass_began = Clock::now();
      const auto name = pass.id == "noisy_combined" && !denoising ? std::string("combined") : pass.id;
      const auto rendered = tile.get_pass_render_size(name);
      if (rendered.x < 1 || rendered.y < 1 || rendered.x > tile.size.x || rendered.y > tile.size.y)
        throw std::runtime_error("Cycles delivered invalid pass dimensions: " + pass.id);
      plane.width = full ? tile.size.x : rendered.x;
      plane.height = full ? tile.size.y : rendered.y;
      plane.pixels.resize(static_cast<std::size_t>(plane.width) * plane.height * plane.channels);
      const bool read = full ? tile.get_pass_pixels(name, plane.channels, plane.pixels.data())
                             : tile.get_pass_pixels_compact(name, plane.channels, plane.pixels.data());
      if (!read)
        throw std::runtime_error("Cycles did not deliver pass " + pass.id);
      if (pass.id == "sample_count")
        for (auto &value : plane.pixels)
          value *= buffer_samples;
      {
        std::lock_guard lock(store_->mutex);
        if (store_->diagnostics.enabled) {
          auto &stats = store_->diagnostics.output_plane_reads[pass.id];
          ++stats.calls;
          stats.requested_bytes += plane.pixels.size() * sizeof(float);
          stats.host_seconds += seconds(pass_began);
        }
      }
      return plane;
    };
    if (focus_needed) {
      auto focus = std::make_shared<FrameEvent>();
      focus->generation = frame->generation;
      focus->x = frame->x;
      focus->y = frame->y;
      focus->width = frame->width;
      focus->height = frame->height;
      focus->full_width = frame->full_width;
      focus->full_height = frame->full_height;
      focus->samples = frame->samples;
      for (const auto &pass : descriptors)
        if (pass.id == "position" || pass.id == "depth") {
          {
            std::lock_guard lock(store_->mutex);
            if (store_->interrupted || frame->generation != store_->generation)
              return false;
          }
          focus->planes.push_back(read_plane(pass, false));
        }
      focus->rendered_width = focus->planes.front().width;
      focus->rendered_height = focus->planes.front().height;
      store_->publish_focus(std::move(focus));
    }
    for (const auto &pass : descriptors) {
      if (!complete && preview != "*" && pass.id != preview && pass.id != "combined" && pass.id != "noisy_combined")
        continue;
      // Cancellation can arrive between passes. Avoid allocating and
      // reading another full GPU plane that publish() would discard.
      {
        std::lock_guard lock(store_->mutex);
        if (store_->interrupted || frame->generation != store_->generation) {
          if (store_->diagnostics.enabled)
            ++store_->diagnostics.output_reads_skipped;
          return false;
        }
      }
      auto plane = read_plane(pass, complete);
      if (pass.id == "combined") {
        const auto rendered = tile.get_pass_render_size("combined");
        frame->rendered_width = rendered.x;
        frame->rendered_height = rendered.y;
      }
      frame->planes.push_back(std::move(plane));
    }
    {
      std::lock_guard lock(store_->mutex);
      store_->last_preview_read = Clock::now();
      if (store_->diagnostics.enabled) {
        ++store_->diagnostics.output_deliveries;
        store_->diagnostics.output_delivery_seconds += seconds(began);
      }
    }
    store_->publish(std::move(frame));
    return true;
  }
  std::shared_ptr<FrameStore> store_;
};

} // namespace
std::unique_ptr<ccl::DisplayDriver> make_buffer_display(std::shared_ptr<FrameStore> store) {
  return std::make_unique<BufferDisplay>(std::move(store));
}
std::unique_ptr<ccl::OutputDriver> make_buffer_output(std::shared_ptr<FrameStore> store) {
  return std::make_unique<BufferOutput>(std::move(store));
}
} // namespace cycles_houdini
