// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include <memory>
#include <map>
#include <mutex>
#include <stdexcept>
#include <utility>
class IMG_TileDevice;
namespace cycles_houdini {
class DisplayConnectionError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};
class DisplayRemoteQuit : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};
class TileDisplay {
public:
  TileDisplay(const std::string &destination, const std::string &socket_port, int width, int height,
              std::vector<PassDescriptor> passes, std::map<std::string, std::string> options = {});
  ~TileDisplay();
  void present(std::shared_ptr<const FrameEvent> image, std::uint64_t presentation_sequence = 0);
  // Claim a destination only after the session has installed this display.
  void activate();
  void close();
  // Bounded test rendezvous with actual native device writes.
  std::uint64_t wait_for_idle();
  bool ready();
  // Schedule a final MPlay status on an open connection before retiring it.
  bool set_failure_status(std::string message);
  // Called by Python atexit, while the extension and HDK are still loaded.
  static void shutdown_all();

private:
  void close_impl();
  std::mutex operation_mutex_;
  struct Impl;
  struct Lane;
  struct Registry;
  static Registry &registry();
  static std::pair<std::shared_ptr<Lane>, std::uint64_t>
  claim_lane(const std::string &destination, const std::string &socket_port, int width, int height, float aspect,
             const std::vector<PassDescriptor> &passes, const std::map<std::string, std::string> &options);
  std::shared_ptr<Lane> lane_;
  std::uint64_t owner_{};
  std::string destination_, socket_port_;
  int width_, height_;
  float pixel_aspect_{1};
  std::vector<PassDescriptor> passes_;
  std::map<std::string, std::string> options_;
  bool closed_{};
  // A layout rebuild replaces the device, not the display's serial namespace.
  std::uint64_t sequence_{};
  bool has_sequence_{};
  std::string presented_color_;
};
} // namespace cycles_houdini
