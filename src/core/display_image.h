// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <chrono>
#include <memory>
#include <mutex>

namespace ccl {
class DisplayDriver;
class OutputDriver;
} // namespace ccl
namespace cycles_houdini {
// One publication lock protects generation, immutable frames and callback
// diagnostics. Session control is always acquired before this lock.
struct FrameStore {
  mutable std::mutex mutex;
  std::uint64_t generation{}, sequence{};
  std::shared_ptr<const FrameEvent> latest;
  std::shared_ptr<const FrameEvent> focus;
  bool focus_enabled{};
  bool complete{};
  bool interrupted{};
  double first_image_seconds{};
  double finished_seconds{};
  std::string error;
  std::chrono::steady_clock::time_point started{std::chrono::steady_clock::now()};
  std::vector<PassDescriptor> passes;
  std::shared_ptr<const std::map<std::string, std::string>> output_metadata;
  std::string denoiser{"none"}, denoise_device_id;
  std::string preview_pass{"combined"};
  std::chrono::steady_clock::time_point last_preview_read{};
  Crop crop;
  int full_width{}, full_height{};
  float pixel_aspect{1};
  SessionDiagnostics diagnostics;
  std::shared_ptr<const ColorConfiguration> color_configuration;
  std::uint64_t color_epoch{};
  void fail(const std::string &message);
  void publish_focus(std::shared_ptr<FrameEvent> frame);
  void publish(std::shared_ptr<FrameEvent> frame);
};
std::unique_ptr<ccl::DisplayDriver> make_buffer_display(std::shared_ptr<FrameStore>);
std::unique_ptr<ccl::OutputDriver> make_buffer_output(std::shared_ptr<FrameStore>);
} // namespace cycles_houdini
